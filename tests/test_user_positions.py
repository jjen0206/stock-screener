"""src/database.py user_positions 表 CRUD 測試。"""
from __future__ import annotations

import pytest

from src import database as db


# === schema ===

def test_user_positions_table_schema_exists(tmp_db):
    """init_db 後 user_positions 表 + 必要欄位。"""
    with db.get_conn() as conn:
        names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "user_positions" in names
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(user_positions)"
            ).fetchall()
        }
        for required in (
            "id", "stock_id", "entry_date", "entry_price", "shares",
            "side", "stop_loss", "take_profit", "notes",
            "is_open", "exit_date", "exit_price",
            "created_at", "updated_at",
        ):
            assert required in cols, f"missing col {required}"


# === add_position ===

def test_add_position_minimal(tmp_db):
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000)
    assert pid > 0
    opens = db.get_open_positions()
    assert len(opens) == 1
    assert opens[0]["stock_id"] == "2330"
    assert opens[0]["entry_price"] == 600.0
    assert opens[0]["shares"] == 1000
    assert opens[0]["is_open"] == 1


def test_add_position_with_stops(tmp_db):
    pid = db.add_position(
        "2330", "2026-05-17", 600.0, 1000,
        stop_loss=588.0, take_profit=632.0, notes="test",
    )
    p = db.get_position_pnl(pid)
    assert p["stop_loss"] == 588.0
    assert p["take_profit"] == 632.0


def test_add_position_short(tmp_db):
    pid = db.add_position(
        "2330", "2026-05-17", 600.0, 1000, side="short",
    )
    p = db.get_position_pnl(pid)
    assert p["side"] == "short"


def test_add_position_bad_inputs(tmp_db):
    with pytest.raises(ValueError):
        db.add_position("2330", "2026-05-17", 0, 1000)
    with pytest.raises(ValueError):
        db.add_position("2330", "2026-05-17", 600, 0)
    with pytest.raises(ValueError):
        db.add_position("2330", "2026-05-17", 600, 1000, side="bad")
    with pytest.raises(ValueError):
        db.add_position("", "2026-05-17", 600, 1000)


# === close_position ===

def test_close_position(tmp_db):
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000)
    ok = db.close_position(pid, exit_price=620.0, exit_date="2026-05-18")
    assert ok is True
    assert db.get_open_positions() == []
    all_ = db.get_all_positions(include_closed=True)
    assert len(all_) == 1
    closed = all_[0]
    assert closed["is_open"] == 0
    assert closed["exit_price"] == 620.0
    assert closed["exit_date"] == "2026-05-18"


def test_close_position_nonexistent(tmp_db):
    assert db.close_position(99999, exit_price=100.0) is False


def test_close_position_twice_returns_false(tmp_db):
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000)
    db.close_position(pid, 620.0)
    assert db.close_position(pid, 630.0) is False


def test_close_position_bad_inputs(tmp_db):
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000)
    with pytest.raises(ValueError):
        db.close_position(pid, exit_price=0)


# === update_position ===

def test_update_position_stop_loss(tmp_db):
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000, stop_loss=580.0)
    ok = db.update_position(pid, stop_loss=590.0)
    assert ok is True
    p = db.get_position_pnl(pid)
    assert p["stop_loss"] == 590.0


def test_update_position_take_profit(tmp_db):
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000)
    ok = db.update_position(pid, take_profit=650.0)
    assert ok is True
    p = db.get_position_pnl(pid)
    assert p["take_profit"] == 650.0


def test_update_position_no_changes_returns_false(tmp_db):
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000)
    assert db.update_position(pid) is False


# === delete_position ===

def test_delete_position(tmp_db):
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000)
    assert db.delete_position(pid) is True
    assert db.get_open_positions() == []


def test_delete_position_nonexistent(tmp_db):
    assert db.delete_position(99999) is False


# === get_open_positions / get_all_positions ===

def test_get_open_positions_filters_closed(tmp_db):
    p1 = db.add_position("2330", "2026-05-15", 600.0, 1000)
    p2 = db.add_position("2454", "2026-05-16", 1100.0, 500)
    db.close_position(p1, 620.0)
    opens = db.get_open_positions()
    assert len(opens) == 1
    assert opens[0]["stock_id"] == "2454"


def test_get_all_positions_include_closed(tmp_db):
    p1 = db.add_position("2330", "2026-05-15", 600.0, 1000)
    db.add_position("2454", "2026-05-16", 1100.0, 500)
    db.close_position(p1, 620.0)
    all_ = db.get_all_positions(include_closed=True)
    assert len(all_) == 2


# === get_position_pnl ===

def test_get_position_pnl_with_current_price(tmp_db):
    """灌 daily_prices 後 pnl 自動算。"""
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 610, 615, 605, 610, 1000)"
        )
    p = db.get_position_pnl(pid)
    assert p["current_price"] == 610.0
    assert p["pnl"] == 10000.0  # (610-600)*1000
    assert abs(p["pnl_pct"] - (10 / 600 * 100)) < 1e-6


def test_get_position_pnl_closed(tmp_db):
    """closed 部位用 exit_price。"""
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000)
    db.close_position(pid, 620.0)
    p = db.get_position_pnl(pid)
    assert p["is_open"] == 0
    assert p["current_price"] == 620.0
    assert p["pnl"] == 20000.0


def test_get_position_pnl_short(tmp_db):
    """short 部位:current < entry → 賺。"""
    pid = db.add_position("2330", "2026-05-17", 600.0, 1000, side="short")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 580, 585, 575, 580, 1000)"
        )
    p = db.get_position_pnl(pid)
    # short: (580-600) * 1000 * -1 = 20000
    assert p["pnl"] == 20000.0


def test_get_position_pnl_nonexistent(tmp_db):
    assert db.get_position_pnl(99999) is None
