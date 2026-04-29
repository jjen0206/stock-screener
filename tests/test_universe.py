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


# === is_pure_stock 過濾 ETF / 債券 / 槓桿反向 ===

def test_is_pure_stock_normal_stock():
    """純股票 → True。"""
    assert universe.is_pure_stock("2330", "台積電") is True
    assert universe.is_pure_stock("3680", "家登") is True
    assert universe.is_pure_stock("8069", "元太") is True


def test_is_pure_stock_etf_by_id():
    """代號 00 開頭 = ETF/ETN → False。"""
    assert universe.is_pure_stock("0050", "元大台灣50") is False
    assert universe.is_pure_stock("00929", "復華台灣科技優息") is False
    assert universe.is_pure_stock("00631L", "元大台灣50正2") is False


def test_is_pure_stock_bond_etf_by_id_and_name():
    """債券 ETF — 代號 00 + 名稱含「美債/公債/債券」→ False。"""
    assert universe.is_pure_stock("00764B", "群益25年美債") is False
    assert universe.is_pure_stock("00679B", "元大美債20年") is False
    assert universe.is_pure_stock("00687B", "國泰20年美債") is False


def test_is_pure_stock_leveraged_inverse_by_name():
    """槓桿 / 反向商品 — 名稱含「正2 / 反1 / 槓桿 / 反向」 → False。"""
    assert universe.is_pure_stock("00631L", "元大台灣50正2") is False
    assert universe.is_pure_stock("00632R", "元大台灣50反1") is False
    # 假設某檔 4 碼也帶槓桿關鍵字也應過濾(防真實有此狀況)
    assert universe.is_pure_stock("9999", "ABC 槓桿基金") is False


def test_is_pure_stock_etn_by_name_keyword():
    """名稱含 ETN(英文不分大小寫) → False。"""
    assert universe.is_pure_stock("020027", "永豐ETN") is False
    assert universe.is_pure_stock("9999", "Test ETF Fund") is False


def test_is_pure_stock_handles_empty_name():
    """name=None / 空字串 → 保守留著(避免誤殺剛上市股)。"""
    assert universe.is_pure_stock("2330", None) is True
    assert universe.is_pure_stock("2330", "") is True


def test_is_pure_stock_handles_empty_id():
    """空 stock_id → False(不可能是有效個股)。"""
    assert universe.is_pure_stock("", "台積電") is False


def test_is_pure_stock_corp_bond_keyword():
    """金融債 / 投等債 / 高收債 / 可轉債 都該被過濾。"""
    assert universe.is_pure_stock("9999", "00xxx 投等債") is False
    assert universe.is_pure_stock("9999", "美國高收債 ETF") is False
    assert universe.is_pure_stock("9999", "金融債券") is False
