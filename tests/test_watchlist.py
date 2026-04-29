"""watchlist CRUD 測試。"""
from __future__ import annotations

import pytest

from src import config, database as db


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "wl.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def test_add_to_watchlist_creates_row(tmp_db):
    db.add_to_watchlist("2330", note="長線觀察")
    items = db.get_watchlist()
    assert len(items) == 1
    assert items[0]["stock_id"] == "2330"
    assert items[0]["note"] == "長線觀察"
    assert items[0]["added_at"]  # 自動帶 timestamp


def test_add_to_watchlist_idempotent_keeps_added_at(tmp_db):
    """重複 add 不該插重複,且 added_at 不變,只更新 note。"""
    db.add_to_watchlist("2330", note="第一次")
    first = db.get_watchlist()[0]
    db.add_to_watchlist("2330", note="第二次")
    second = db.get_watchlist()
    assert len(second) == 1
    assert second[0]["note"] == "第二次"
    assert second[0]["added_at"] == first["added_at"]  # 沒變


def test_is_in_watchlist(tmp_db):
    assert db.is_in_watchlist("2330") is False
    db.add_to_watchlist("2330")
    assert db.is_in_watchlist("2330") is True


def test_remove_from_watchlist(tmp_db):
    db.add_to_watchlist("2330")
    assert db.remove_from_watchlist("2330") is True
    assert db.is_in_watchlist("2330") is False
    # 第二次 remove 該回 False
    assert db.remove_from_watchlist("2330") is False


def test_get_watchlist_sorted_by_added_at_desc(tmp_db):
    """較晚加入的排前面。"""
    import time
    db.add_to_watchlist("2330")
    time.sleep(1.05)  # 確保 timestamp 不同(timespec=seconds)
    db.add_to_watchlist("2454")
    items = db.get_watchlist()
    assert [it["stock_id"] for it in items] == ["2454", "2330"]


def test_remove_unknown_stock_returns_false(tmp_db):
    """移除沒在清單的股票該回 False,不該炸。"""
    assert db.remove_from_watchlist("9999") is False


def test_add_without_note_uses_none(tmp_db):
    db.add_to_watchlist("2330")
    items = db.get_watchlist()
    assert items[0]["note"] is None


# === UI 整合測試:個股查詢 toggle 第二次新增不該失敗 ===
# 主公回報 bug:同 session 改 stock_id 後 toggle 失效;root cause = button key 固定。
# 修法:button key 包含當下 stock_id (key=f"star_toggle_{sid}");每次 render 重查 DB。

def test_query_page_toggle_first_add(monkeypatch, tmp_path):
    """情境 1:預設 2330,按 toggle → 加入 watchlist。"""
    from streamlit.testing.v1 import AppTest
    from src import config as cfg
    cfg.DATABASE_PATH = str(tmp_path / "ux1.db")
    db.init_db()

    at = AppTest.from_file("app.py").run(timeout=30)
    at.sidebar.radio[0].set_value("個股查詢").run(timeout=30)
    add_btn = next(
        (b for b in at.button if "2330" in b.label and "加入" in b.label),
        None,
    )
    assert add_btn is not None
    add_btn.click().run(timeout=30)
    assert db.is_in_watchlist("2330")


def test_query_page_toggle_second_add_after_changing_stock(monkeypatch, tmp_path):
    """情境 2(主公的 bug):加入 2330 後改成 2317,該能再加入 — 兩檔都在。"""
    from streamlit.testing.v1 import AppTest
    from src import config as cfg
    cfg.DATABASE_PATH = str(tmp_path / "ux2.db")
    db.init_db()

    at = AppTest.from_file("app.py").run(timeout=30)
    at.sidebar.radio[0].set_value("個股查詢").run(timeout=30)
    # 第一次加 2330
    btn = next(b for b in at.button if "2330" in b.label and "加入" in b.label)
    btn.click().run(timeout=30)
    # 改成 2317 第二次加
    at.text_input[0].set_value("2317").run(timeout=30)
    btn = next(b for b in at.button if "2317" in b.label and "加入" in b.label)
    btn.click().run(timeout=30)
    # 兩檔都該在(2330 不該被誤殺)
    assert db.is_in_watchlist("2317")
    assert db.is_in_watchlist("2330")


def test_backfill_watchlist_history_skips_full_cache(monkeypatch, tmp_path):
    """daily_prices 已 >= 15 筆的個股不該被 fetch。"""
    from src import config as cfg
    cfg.DATABASE_PATH = str(tmp_path / "bf.db")
    db.init_db()
    db.upsert_stocks([{"stock_id": "FULL", "name": "X", "market": "TW"}])
    # 灌 20 筆假 daily_prices
    db.upsert_daily_prices([
        {"stock_id": "FULL", "date": f"2024-01-{i:02d}", "open": 100,
         "high": 101, "low": 99, "close": 100, "volume": 1000,
         "trading_money": None, "trading_turnover": None, "spread": None}
        for i in range(1, 21)
    ])
    # 模擬 backfill helper(import 從 app.py)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "app", tmp_path.parent.parent.parent / "app.py"
    )
    # 直接用 backfill 邏輯:對 daily_prices < 15 的補
    fetch_calls = []
    monkeypatch.setattr(
        "src.data_fetcher.fetch_daily_price",
        lambda sid, s, e: fetch_calls.append(sid),
    )
    # 只 import 函式,不跑整個 app
    import sys
    sys.path.insert(0, str(tmp_path.parent.parent.parent))
    from app import _backfill_watchlist_history
    n = _backfill_watchlist_history(["FULL"], min_required=15)
    assert n == 0
    assert fetch_calls == []


def test_query_page_toggle_remove_existing(monkeypatch, tmp_path):
    """情境 3:對已關注的股票按 toggle → 取消關注。"""
    from streamlit.testing.v1 import AppTest
    from src import config as cfg
    cfg.DATABASE_PATH = str(tmp_path / "ux3.db")
    db.init_db()
    db.add_to_watchlist("2330")  # 預先加入

    at = AppTest.from_file("app.py").run(timeout=30)
    at.sidebar.radio[0].set_value("個股查詢").run(timeout=30)
    # 該顯示「已關注 2330」按鈕
    remove_btn = next(
        (b for b in at.button if "2330" in b.label and "已關注" in b.label),
        None,
    )
    assert remove_btn is not None, "已在 watchlist 該顯示移除 button"
    remove_btn.click().run(timeout=30)
    assert not db.is_in_watchlist("2330")
