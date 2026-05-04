"""scripts/precompute_strategies.py 測試 — mock run_all_strategies + 驗 daily_picks 寫入。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 讓 import scripts.precompute_strategies 找得到
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "precompute.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


def _seed_minimal_universe(monkeypatch):
    """灌幾檔股 + 假設 stocks_with_min_history 回 2 檔,讓 3 個 universe 都有 sids。"""
    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積電", "market": "TW"},
        {"stock_id": "2317", "name": "鴻海", "market": "TW"},
    ])
    monkeypatch.setattr(
        db, "stocks_with_min_history", lambda min_history=20: ["2330", "2317"],
    )
    # preload_snapshots 不用真跑(tmp DB 沒 CSV),no-op
    monkeypatch.setattr(db, "preload_snapshots", lambda: {})


def test_precompute_runs_all_universes_and_writes_daily_picks(
    tmp_db, monkeypatch,
):
    """precompute_for_date 跑 3 個 universe,每個都 dump 進 daily_picks。"""
    from scripts import precompute_strategies as pcs

    _seed_minimal_universe(monkeypatch)

    fake_agg = {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD"],
            "details": {
                "volume_kd": {
                    "stock_id": "2330", "name": "台積電", "close": 600.0,
                },
            },
        },
    }

    call_log: list[tuple] = []

    def _spy(date, enabled=None, params=None, stock_ids=None):
        call_log.append((date, tuple(stock_ids or [])))
        return fake_agg

    monkeypatch.setattr(pcs, "run_all_strategies", _spy)

    results = pcs.precompute_for_date("2026-05-04")

    # 3 個 universe 各被 dump 1 筆(只一個 strategy 命中)
    assert sorted(results.keys()) == ["pure_stock", "top_50", "with_etf"]
    assert results["pure_stock"] == 1
    assert results["with_etf"] == 1
    assert results["top_50"] == 1

    # daily_picks 表內 3 universe × 1 row = 3 rows
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM daily_picks WHERE trade_date=?",
            ("2026-05-04",),
        ).fetchone()["c"]
    assert n == 3

    # run_all_strategies 該被叫 3 次(各 universe 一次)
    assert len(call_log) == 3
    # 都同一個 date
    assert all(d == "2026-05-04" for d, _ in call_log)


def test_dump_daily_picks_csv_writes_file_in_snapshot_dir(
    tmp_db, tmp_path, monkeypatch,
):
    """dump_daily_picks_csv 把 daily_picks 全表寫成 CSV 進指定目錄。"""
    from scripts import precompute_strategies as pcs

    # 灌一筆 daily_picks
    db.dump_daily_picks(
        "2026-05-04", "pure_stock",
        {
            "2330": {
                "name": "台積電",
                "signals": ["量價KD"],
                "details": {"volume_kd": {
                    "stock_id": "2330", "name": "台積電", "close": 600.0,
                }},
            },
        },
    )

    snapshot_dir = tmp_path / "snap"
    n = pcs.dump_daily_picks_csv(snapshot_dir=snapshot_dir)
    assert n == 1
    csv_path = snapshot_dir / "daily_picks.csv"
    assert csv_path.exists()

    # CSV 內容驗證
    import pandas as pd
    df = pd.read_csv(csv_path, dtype={"sid": str, "trade_date": str})
    assert len(df) == 1
    assert df.iloc[0]["sid"] == "2330"
    assert df.iloc[0]["strategy"] == "volume_kd"
    assert df.iloc[0]["universe"] == "pure_stock"
    assert df.iloc[0]["params_hash"] == "default_v1"


def test_dump_daily_picks_csv_skips_when_table_empty(tmp_db, tmp_path):
    """daily_picks 表空 → 不寫 CSV,回 0(避免 commit 空檔)。"""
    from scripts import precompute_strategies as pcs

    snapshot_dir = tmp_path / "snap"
    n = pcs.dump_daily_picks_csv(snapshot_dir=snapshot_dir)
    assert n == 0
    assert not (snapshot_dir / "daily_picks.csv").exists()


def test_list_recent_trading_dates_returns_distinct_descending(tmp_db):
    """_list_recent_trading_dates 從 daily_prices 撈最近 N 個交易日,
    去重 + 降序,排除 TAIEX。"""
    from scripts import precompute_strategies as pcs

    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積電", "market": "TW"},
        {"stock_id": "2317", "name": "鴻海", "market": "TW"},
    ])
    # 灌 5 個日期(2330 + 2317 都有,確認去重)
    rows = []
    for sid in ("2330", "2317"):
        for d in ("2026-04-28", "2026-04-29", "2026-04-30", "2026-05-01", "2026-05-02"):
            rows.append({
                "stock_id": sid, "date": d,
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": 1000,
                "trading_money": None, "trading_turnover": None, "spread": None,
            })
    # TAIEX 一筆 — 不該出現在結果
    rows.append({
        "stock_id": "TAIEX", "date": "2026-05-03",
        "open": 39000.0, "high": 39500.0, "low": 38900.0, "close": 39200.0,
        "volume": 100000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    })
    db.upsert_daily_prices(rows)

    # backfill 3 → 拿最近 3 個交易日(降序)
    dates = pcs._list_recent_trading_dates(3)
    assert dates == ["2026-05-02", "2026-05-01", "2026-04-30"]

    # 0 → 空
    assert pcs._list_recent_trading_dates(0) == []

    # 100 → 上限即所有 5 天
    dates_all = pcs._list_recent_trading_dates(100)
    assert len(dates_all) == 5
    assert dates_all == [
        "2026-05-02", "2026-05-01", "2026-04-30", "2026-04-29", "2026-04-28",
    ]


def test_preload_snapshots_loads_daily_picks_csv(tmp_db, tmp_path):
    """preload_snapshots 讀 daily_picks.csv 灌進 SQLite,App 端 load 拿到資料。"""
    import pandas as pd

    # 寫一份 CSV(模擬 nightly workflow 推進來)
    csv_path = tmp_path / "daily_picks.csv"
    pd.DataFrame([
        {
            "trade_date": "2026-05-04", "universe": "pure_stock",
            "strategy": "volume_kd", "sid": "2330",
            "score": None, "rank": None,
            "params_hash": "default_v1",
            "payload": '{"stock_id": "2330", "name": "台積電", "close": 600.0}',
            "computed_at": "2026-05-04T00:00:00+00:00",
        },
        {
            "trade_date": "2026-05-04", "universe": "pure_stock",
            "strategy": "ma_alignment", "sid": "2330",
            "score": None, "rank": None,
            "params_hash": "default_v1",
            "payload": '{"stock_id": "2330", "name": "台積電", "close": 600.0, "ma5": 595.0}',
            "computed_at": "2026-05-04T00:00:00+00:00",
        },
    ]).to_csv(csv_path, index=False)

    # preload 模擬找 daily_picks.csv 路徑(snapshot_dir 直接指向 tmp_path)
    counts = db.preload_snapshots(snapshot_dir=tmp_path)
    assert counts.get("daily_picks") == 2

    # load_daily_picks 該拿到資料
    loaded = db.load_daily_picks("2026-05-04", "pure_stock", "default_v1")
    assert loaded is not None
    assert "2330" in loaded
    assert sorted(loaded["2330"]["details"].keys()) == [
        "ma_alignment", "volume_kd",
    ]


def test_precompute_clears_old_data_for_same_date(tmp_db, monkeypatch):
    """rerun 同一天 → 先清舊 + 重寫,沒遺留。"""
    from scripts import precompute_strategies as pcs

    _seed_minimal_universe(monkeypatch)

    # 第一次 run:有 2 檔
    agg_first = {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD"],
            "details": {"volume_kd": {"stock_id": "2330", "close": 600.0}},
        },
        "2317": {
            "name": "鴻海",
            "signals": ["量價KD"],
            "details": {"volume_kd": {"stock_id": "2317", "close": 200.0}},
        },
    }
    monkeypatch.setattr(
        pcs, "run_all_strategies",
        lambda *a, **kw: agg_first,
    )
    pcs.precompute_for_date("2026-05-04")

    with db.get_conn() as conn:
        first_count = conn.execute(
            "SELECT COUNT(*) c FROM daily_picks WHERE trade_date=?",
            ("2026-05-04",),
        ).fetchone()["c"]
    assert first_count == 6  # 3 universe × 2 sid

    # 第二次 run:只 1 檔(模擬資料變動,2317 從 universe 消失)
    agg_second = {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD"],
            "details": {"volume_kd": {"stock_id": "2330", "close": 999.0}},
        },
    }
    monkeypatch.setattr(
        pcs, "run_all_strategies",
        lambda *a, **kw: agg_second,
    )
    pcs.precompute_for_date("2026-05-04")

    with db.get_conn() as conn:
        second_count = conn.execute(
            "SELECT COUNT(*) c FROM daily_picks WHERE trade_date=?",
            ("2026-05-04",),
        ).fetchone()["c"]
    # 3 universe × 1 sid = 3,且 2330 close 應為 999(被覆蓋)
    assert second_count == 3
    loaded = db.load_daily_picks("2026-05-04", "pure_stock")
    assert loaded is not None
    assert "2317" not in loaded  # 舊資料清掉了
    assert loaded["2330"]["details"]["volume_kd"]["close"] == 999.0
