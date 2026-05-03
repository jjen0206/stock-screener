"""scripts/daily_fetch.py 單元測試(全市場 bulk 版)。

scripts/ 不是 package,用 importlib 載入。
mock fetch_all_daily_prices_bulk + get_full_universe + fetch_institutional。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "daily_fetch.py"
_spec = importlib.util.spec_from_file_location("daily_fetch", _SCRIPT)
daily_fetch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daily_fetch)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "fetch.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    # daily_fetch.run() 開頭新加了 db.preload_snapshots() 把 main repo 的
    # snapshot CSV 灌進 SQLite — 對 unit test 是污染(會塞 131K 行)。mock 成
    # no-op,test 用 mock_universe + monkeypatch 控制 SQLite 內容。
    monkeypatch.setattr(db, "preload_snapshots", lambda *a, **kw: {})
    db.init_db()
    return db_file


@pytest.fixture
def mock_universe(monkeypatch):
    """縮小 universe 到 3 檔加速測試。"""
    fake_universe = ["2330", "2454", "3680"]
    monkeypatch.setattr(daily_fetch, "get_full_universe", lambda: fake_universe)
    # TW_TOP_50 也縮減
    monkeypatch.setattr(daily_fetch, "TW_TOP_50", [
        ("2330", "台積電"), ("2454", "聯發科"),
    ])
    monkeypatch.setattr(daily_fetch, "load_watchlist", lambda: [])
    return fake_universe


def test_run_calls_bulk_and_writes_daily_prices(tmp_db, mock_universe, monkeypatch):
    """全市場 bulk 該被叫一次,結果寫進 daily_prices 表。"""
    fake_df = pd.DataFrame([
        {"stock_id": "2330", "date": "2026-04-28", "open": 2245, "high": 2280,
         "low": 2215, "close": 2215, "volume": 57336004,
         "trading_money": None, "trading_turnover": None, "spread": -50.0},
        {"stock_id": "2454", "date": "2026-04-28", "open": 1200, "high": 1220,
         "low": 1190, "close": 1210, "volume": 5000000,
         "trading_money": None, "trading_turnover": None, "spread": 10.0},
    ])
    monkeypatch.setattr(daily_fetch, "fetch_all_daily_prices_bulk",
                        lambda: fake_df)
    monkeypatch.setattr(daily_fetch, "fetch_institutional",
                        lambda sid, s, e: None)

    summary = daily_fetch.run(institutional_days=7)
    assert summary["bulk_rows"] == 2
    assert summary["universe_size"] == 3

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id FROM daily_prices ORDER BY stock_id"
        ).fetchall()
    assert sorted(r["stock_id"] for r in rows) == ["2330", "2454"]


def test_run_calls_institutional_for_top50_only(tmp_db, mock_universe, monkeypatch):
    """institutional 只該對 TW_TOP_50 + watchlist 抓,不對全 universe。"""
    monkeypatch.setattr(daily_fetch, "fetch_all_daily_prices_bulk",
                        lambda: pd.DataFrame())
    inst_calls = []
    monkeypatch.setattr(
        daily_fetch, "fetch_institutional",
        lambda sid, s, e: inst_calls.append(sid),
    )

    summary = daily_fetch.run(institutional_days=7)
    # Mock 的 TW_TOP_50 = 2 檔(2330, 2454),watchlist 0 檔 → 該 2 次
    assert summary["institutional_ok"] == 2
    assert sorted(inst_calls) == ["2330", "2454"]
    # 不該對 3680 抓(在 universe 但不在 TW_TOP_50)
    assert "3680" not in inst_calls


def test_run_continues_on_institutional_failure(tmp_db, mock_universe, monkeypatch):
    """單檔 institutional 失敗不中斷,記錄到 fail count。"""
    monkeypatch.setattr(daily_fetch, "fetch_all_daily_prices_bulk",
                        lambda: pd.DataFrame())
    def fake_inst(sid, s, e):
        if sid == "2330":
            raise RuntimeError("FinMind 429")
    monkeypatch.setattr(daily_fetch, "fetch_institutional", fake_inst)

    summary = daily_fetch.run(institutional_days=7)
    assert summary["institutional_ok"] == 1  # 只 2454 成功
    assert summary["institutional_fail"] == 1  # 2330 失敗


def test_run_continues_when_bulk_returns_empty(tmp_db, mock_universe, monkeypatch):
    """bulk 完全失敗 → bulk_rows=0 但 institutional 仍跑。"""
    monkeypatch.setattr(daily_fetch, "fetch_all_daily_prices_bulk",
                        lambda: pd.DataFrame())
    monkeypatch.setattr(daily_fetch, "fetch_institutional",
                        lambda sid, s, e: None)

    summary = daily_fetch.run(institutional_days=7)
    assert summary["bulk_rows"] == 0
    assert summary["institutional_ok"] == 2


def test_main_returns_zero_even_on_partial_failure(
    tmp_db, mock_universe, monkeypatch,
):
    """exit 0 即使 institutional 部分失敗(只要 bulk 健康)。"""
    # 給足 ≥ _MIN_BULK_ROWS_HEALTHY (2000) 行 bulk 資料,通過健康警戒
    fake_rows = [
        {"stock_id": f"{9000 + i:04d}", "date": "2026-04-28",
         "open": 100, "high": 110, "low": 95, "close": 105,
         "volume": 1000, "trading_money": None,
         "trading_turnover": None, "spread": 0.0}
        for i in range(2100)
    ]
    fake_df = pd.DataFrame(fake_rows)
    monkeypatch.setattr(daily_fetch, "fetch_all_daily_prices_bulk",
                        lambda: fake_df)
    monkeypatch.setattr(
        daily_fetch, "fetch_institutional",
        lambda sid, s, e: (_ for _ in ()).throw(RuntimeError("all fail")),
    )
    monkeypatch.setattr(daily_fetch, "fetch_daily_price",
                        lambda sid, s, e: None)
    monkeypatch.setattr("sys.argv", ["daily_fetch.py"])
    code = daily_fetch.main()
    assert code == 0


def test_main_returns_one_when_bulk_below_health_threshold(
    tmp_db, mock_universe, monkeypatch,
):
    """bulk 抓不到夠多資料(< 2000)→ exit 1 讓 GH Actions 標紅。"""
    monkeypatch.setattr(daily_fetch, "fetch_all_daily_prices_bulk",
                        lambda: pd.DataFrame())
    monkeypatch.setattr(daily_fetch, "fetch_institutional",
                        lambda sid, s, e: None)
    monkeypatch.setattr(daily_fetch, "fetch_daily_price",
                        lambda sid, s, e: None)
    monkeypatch.setattr("sys.argv", ["daily_fetch.py"])
    code = daily_fetch.main()
    assert code == 1


def test_run_fetches_90day_history_for_watchlist(tmp_db, mock_universe, monkeypatch):
    """對 watchlist 個股該抓 90 天 daily_price 歷史(補 ATR 用)。"""
    # mock_universe fixture 的 load_watchlist 是 [],改成有 2 檔
    monkeypatch.setattr(daily_fetch, "load_watchlist", lambda: [
        ("3680", "家登"), ("8069", "元太"),
    ])
    monkeypatch.setattr(daily_fetch, "fetch_all_daily_prices_bulk",
                        lambda: pd.DataFrame())
    monkeypatch.setattr(daily_fetch, "fetch_institutional",
                        lambda sid, s, e: None)
    history_calls = []
    monkeypatch.setattr(
        daily_fetch, "fetch_daily_price",
        lambda sid, s, e: history_calls.append(sid),
    )

    summary = daily_fetch.run(institutional_days=7)
    # 該對 watchlist 兩檔都抓
    assert "3680" in history_calls and "8069" in history_calls
    assert summary["watchlist_history_ok"] == 2
