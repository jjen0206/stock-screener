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
