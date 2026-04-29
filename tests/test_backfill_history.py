"""scripts/backfill_history.py 單元測試。

scripts/ 不是 package,用 importlib 載入。mock fetch_daily_price /
fetch_institutional / get_full_universe,用 tmp DB + tmp snapshot dir。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_history.py"
_spec = importlib.util.spec_from_file_location("backfill_history", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


@pytest.fixture
def tmp_setup(monkeypatch, tmp_path):
    """tmp DB + tmp snapshot dir。"""
    db_file = tmp_path / "backfill.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    snapshot_dir = tmp_path / "twse_snapshot"
    monkeypatch.setattr(backfill, "SNAPSHOT_DIR", snapshot_dir)
    db.init_db()
    return tmp_path


def test_backfill_skips_stocks_with_enough_history(tmp_setup, monkeypatch):
    """daily_prices 已有 >= min-existing 天的個股該跳過。"""
    # 預灌一檔 90 天歷史,另一檔 0 天
    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積", "market": "TW"},
        {"stock_id": "2454", "name": "聯發", "market": "TW"},
    ])
    db.upsert_daily_prices([
        {"stock_id": "2330", "date": f"2026-01-{i:02d}",
         "open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000,
         "trading_money": None, "trading_turnover": None, "spread": 0.0}
        for i in range(1, 31)  # 30 天
    ])
    monkeypatch.setattr(backfill, "get_full_universe", lambda: ["2330", "2454"])
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])

    fetch_calls = []
    monkeypatch.setattr(
        backfill, "fetch_daily_price",
        lambda sid, s, e: fetch_calls.append(sid),
    )
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_history.py", "--days", "10", "--min-existing", "20"],
    )

    code = backfill.main()
    assert code == 0
    # 2330 已有 30 天 >= 20 該跳過,只該抓 2454
    assert fetch_calls == ["2454"]


def test_backfill_dumps_csv_to_snapshot(tmp_setup, monkeypatch):
    """完成後該 dump daily_prices.csv / institutional.csv / stocks.csv。"""
    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積", "market": "TW"},
    ])
    monkeypatch.setattr(backfill, "get_full_universe", lambda: ["2330"])
    monkeypatch.setattr(backfill, "TW_TOP_50", [("2330", "台積")])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])

    def fake_fetch_price(sid, s, e):
        # 模擬 fetch 後資料寫入 DB
        db.upsert_daily_prices([{
            "stock_id": sid, "date": "2026-04-28",
            "open": 1000, "high": 1010, "low": 990, "close": 1000, "volume": 5000,
            "trading_money": None, "trading_turnover": None, "spread": 0.0,
        }])

    monkeypatch.setattr(backfill, "fetch_daily_price", fake_fetch_price)
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_history.py", "--days", "10", "--no-institutional"],
    )

    code = backfill.main()
    assert code == 0
    snapshot = tmp_setup / "twse_snapshot"
    assert (snapshot / "daily_prices.csv").exists()
    assert (snapshot / "institutional.csv").exists()
    assert (snapshot / "stocks.csv").exists()
    assert (snapshot / "last_backfill.txt").exists()

    # CSV 內容該包含剛灌入的資料
    df = pd.read_csv(snapshot / "daily_prices.csv", dtype={"stock_id": str})
    assert "2330" in df["stock_id"].tolist()


def test_backfill_returns_one_when_mostly_failing(tmp_setup, monkeypatch):
    """成功率 < 50% 該回 exit 1(token 過期 / API 故障警示)。"""
    db.upsert_stocks([
        {"stock_id": s, "name": "X", "market": "TW"}
        for s in ["A", "B", "C", "D"]
    ])
    monkeypatch.setattr(
        backfill, "get_full_universe", lambda: ["A", "B", "C", "D"],
    )
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])

    def always_fail(sid, s, e):
        raise RuntimeError(f"FinMind 429 for {sid}")

    monkeypatch.setattr(backfill, "fetch_daily_price", always_fail)
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv", ["backfill_history.py", "--days", "10"],
    )

    code = backfill.main()
    assert code == 1
