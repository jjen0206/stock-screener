"""src/take_profit_alerts.py — TP/SL 達標 + 分批了結警報."""
from __future__ import annotations

from datetime import date

import pytest

from src import database as db, take_profit_alerts as tpa


def _seed_price(sid: str, close: float, d: str = "2026-05-01"):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, d, close, close, close, close, 1000),
        )


# === is_enabled ===

def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("TAKE_PROFIT_ALERT_ENABLED", raising=False)
    assert tpa.is_enabled() is True


def test_is_enabled_false_via_env(monkeypatch):
    monkeypatch.setenv("TAKE_PROFIT_ALERT_ENABLED", "false")
    assert tpa.is_enabled() is False


# === partial_exit_suggestion ===

def test_partial_exit_under_threshold_returns_none():
    assert tpa.partial_exit_suggestion(3.0) is None
    assert tpa.partial_exit_suggestion(-5.0) is None


def test_partial_exit_tier1_5pct():
    r = tpa.partial_exit_suggestion(7.0)
    assert r is not None
    assert r["kind"] == "partial_exit_5"
    assert r["tier"] == 1
    assert "1/3" in r["suggested_action"]


def test_partial_exit_tier2_10pct():
    r = tpa.partial_exit_suggestion(12.0)
    assert r is not None
    assert r["kind"] == "partial_exit_10"
    assert r["tier"] == 2
    assert "再" in r["suggested_action"]


# === check_take_profit_hit ===

def test_check_stop_loss_hit(tmp_db):
    _seed_price("2330", close=550.0)
    db.add_position(
        "2330", "2026-04-10", entry_price=600.0, shares=1000,
        stop_loss=560.0, take_profit=700.0,
    )
    alerts = tpa.check_take_profit_hit()
    kinds = [a["kind"] for a in alerts]
    assert "stop_loss" in kinds
    sl_alert = next(a for a in alerts if a["kind"] == "stop_loss")
    assert sl_alert["severity"] == "danger"
    assert sl_alert["pnl_pct"] < 0


def test_check_take_profit_hit(tmp_db):
    _seed_price("2330", close=720.0)
    db.add_position(
        "2330", "2026-04-10", entry_price=600.0, shares=1000,
        stop_loss=560.0, take_profit=700.0,
    )
    alerts = tpa.check_take_profit_hit()
    kinds = [a["kind"] for a in alerts]
    assert "take_profit" in kinds
    tp_alert = next(a for a in alerts if a["kind"] == "take_profit")
    assert tp_alert["pnl_pct"] > 0
    # +20% → 也應該觸發 partial_exit_10
    assert "partial_exit_10" in kinds


def test_check_partial_exit_5pct_alone(tmp_db):
    """+7% 漲幅 → partial_exit_5 但沒達 TP。"""
    _seed_price("2330", close=642.0)  # +7%
    db.add_position(
        "2330", "2026-04-10", entry_price=600.0, shares=1000,
        stop_loss=560.0, take_profit=700.0,
    )
    alerts = tpa.check_take_profit_hit()
    kinds = [a["kind"] for a in alerts]
    assert "partial_exit_5" in kinds
    assert "take_profit" not in kinds


def test_check_no_alerts_when_neutral(tmp_db):
    """+1% 漲幅 → 沒有任何 alert。"""
    _seed_price("2330", close=606.0)
    db.add_position(
        "2330", "2026-04-10", entry_price=600.0, shares=1000,
        stop_loss=560.0, take_profit=700.0,
    )
    assert tpa.check_take_profit_hit() == []


def test_check_kill_switch_off(tmp_db, monkeypatch):
    monkeypatch.setenv("TAKE_PROFIT_ALERT_ENABLED", "false")
    _seed_price("2330", close=550.0)
    db.add_position(
        "2330", "2026-04-10", entry_price=600.0, shares=1000,
        stop_loss=560.0,
    )
    assert tpa.check_take_profit_hit() == []


def test_check_no_positions_returns_empty(tmp_db):
    assert tpa.check_take_profit_hit() == []


def test_check_trailing_stop_distinct_from_stop_loss(tmp_db):
    """trailing_stop > stop_loss 且 cp 跌破 trailing → 觸發 trailing_stop alert(獨立)。"""
    _seed_price("2330", close=640.0)
    pid = db.add_position(
        "2330", "2026-04-10", entry_price=600.0, shares=1000,
        stop_loss=560.0,
    )
    # 手動寫 trailing_stop 比較高
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE user_positions SET trailing_stop=648.0 WHERE id=?",
            (pid,),
        )
    alerts = tpa.check_take_profit_hit()
    kinds = [a["kind"] for a in alerts]
    # 640 < 648 → trailing 觸發 / 640 > 560 → SL 沒觸發
    assert "trailing_stop" in kinds
    assert "stop_loss" not in kinds
