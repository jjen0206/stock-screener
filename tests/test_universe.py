"""src/universe.py 測試 — 重點 get_full_universe。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src import config, database as db, universe


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "univ.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


_FAKE_FINMIND_INFO = {
    "2330": {
        "stock_id": "2330", "stock_name": "台積電",
        "industry_category": "半導體業", "type": "twse",
    },
    "3680": {
        "stock_id": "3680", "stock_name": "家登",
        "industry_category": "半導體業", "type": "tpex",
    },
    # type 是 etf 的不算(我們只要 twse / tpex)
    "0050.PRE": {
        "stock_id": "0050.PRE", "stock_name": "test",
        "industry_category": None, "type": "etf",
    },
    # 沒 name 的不算
    "BAD": {
        "stock_id": "BAD", "stock_name": "",
        "industry_category": None, "type": "twse",
    },
}


def test_get_full_universe_inits_from_finmind(tmp_db):
    """SQLite 不足 1000 筆 → 該打 FinMind 抓 + 寫入 stocks。"""
    with patch(
        "src.data_fetcher._fetch_all_stock_info",
        return_value=_FAKE_FINMIND_INFO,
    ):
        sids = universe.get_full_universe()
    # 應拿到 twse + tpex 兩檔(etf / 空 name 排除)
    assert sorted(sids) == ["2330", "3680"]
    # 寫進 stocks 表
    with db.get_conn() as conn:
        rows = conn.execute("SELECT stock_id, name, type FROM stocks").fetchall()
    assert len(rows) == 2
    by_id = {r["stock_id"]: r for r in rows}
    assert by_id["2330"]["name"] == "台積電"
    assert by_id["2330"]["type"] == "twse"
    assert by_id["3680"]["type"] == "tpex"


def test_get_full_universe_uses_cache_when_db_has_data(tmp_db):
    """SQLite 已有 >= 1000 筆 → 不該打 FinMind。"""
    # 灌 1000 筆假資料
    fake_rows = [
        {"stock_id": f"X{i:04d}", "name": f"name{i}", "market": "TW"}
        for i in range(1100)
    ]
    db.upsert_stocks(fake_rows)

    with patch("src.data_fetcher._fetch_all_stock_info") as m:
        sids = universe.get_full_universe()
    m.assert_not_called()  # 不該打 FinMind
    assert len(sids) == 1100


def test_get_full_universe_refresh_forces_fetch(tmp_db):
    """refresh=True 該強制打 FinMind 即使 SQLite 已有資料。"""
    db.upsert_stocks([
        {"stock_id": f"X{i:04d}", "name": f"old", "market": "TW"}
        for i in range(1100)
    ])
    with patch(
        "src.data_fetcher._fetch_all_stock_info",
        return_value=_FAKE_FINMIND_INFO,
    ) as m:
        sids = universe.get_full_universe(refresh=True)
    m.assert_called_once()
    assert "2330" in sids


def test_get_full_universe_finmind_failure_returns_empty(tmp_db):
    """FinMind 失敗 → 回空 list,不拋例外。"""
    with patch(
        "src.data_fetcher._fetch_all_stock_info",
        side_effect=RuntimeError("net boom"),
    ):
        sids = universe.get_full_universe()
    assert sids == []
