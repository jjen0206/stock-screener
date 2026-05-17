"""Guard the notifier → drawdown alert wire path (daily-notify + morning_brief)。"""
from __future__ import annotations

import inspect

import src.notifier as notifier_mod
from src import database as db, notifier
from scripts import morning_brief


# === notifier.format_top_picks_message ===

def test_format_top_picks_calls_drawdown_alert():
    src = inspect.getsource(notifier_mod.format_top_picks_message)
    assert "_format_drawdown_alert(" in src, (
        "format_top_picks_message 必須 wire _format_drawdown_alert"
    )


def test_format_drawdown_alert_function_exists():
    assert hasattr(notifier_mod, "_format_drawdown_alert")
    assert callable(notifier_mod._format_drawdown_alert)


def test_drawdown_alert_empty_when_no_positions(tmp_db):
    """無 user_positions → 空字串(整段 skip)。"""
    result = notifier_mod._format_drawdown_alert(channel="telegram")
    assert result == ""


def test_drawdown_alert_empty_when_kill_switch_off(tmp_db, monkeypatch):
    monkeypatch.setenv("RISK_MGMT_ENABLED", "false")
    db.add_position("2330", "2026-05-15", 600.0, 1000)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 500, 505, 495, 500, 1000)"
        )
    assert notifier_mod._format_drawdown_alert(channel="telegram") == ""


def test_drawdown_alert_warns_at_10pct(tmp_db, monkeypatch):
    """虧 ~16% → 黃燈 warn。"""
    monkeypatch.setenv("RISK_MGMT_ENABLED", "true")
    db.add_position("2330", "2026-05-15", 600.0, 1000)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 500, 505, 495, 500, 1000)"
        )
    result = notifier_mod._format_drawdown_alert(channel="telegram")
    assert result != ""
    assert "drawdown" in result
    assert "暫停加碼" in result or "停手" in result


def test_drawdown_alert_danger_at_20pct(tmp_db, monkeypatch):
    """虧 ~25% → 紅燈 danger。"""
    monkeypatch.setenv("RISK_MGMT_ENABLED", "true")
    db.add_position("2330", "2026-05-15", 600.0, 1000)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 450, 455, 445, 450, 1000)"
        )
    result = notifier_mod._format_drawdown_alert(channel="telegram")
    assert "🚨" in result
    assert "停手" in result


def test_drawdown_alert_ok_when_profit(tmp_db, monkeypatch):
    """賺錢場景 → 空字串(severity ok 不警報)。"""
    monkeypatch.setenv("RISK_MGMT_ENABLED", "true")
    db.add_position("2330", "2026-05-15", 600.0, 1000)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 650, 655, 645, 650, 1000)"
        )
    assert notifier_mod._format_drawdown_alert(channel="telegram") == ""


# === morning_brief wire ===

def test_morning_brief_calls_drawdown_alert_lines_telegram():
    src = inspect.getsource(morning_brief._format_full_telegram)
    assert "_build_drawdown_alert_lines(" in src, (
        "morning_brief._format_full_telegram 必須 wire 持倉警報"
    )


def test_morning_brief_calls_drawdown_alert_lines_discord():
    src = inspect.getsource(morning_brief._format_full_discord)
    assert "_build_drawdown_alert_lines(" in src


def test_morning_brief_drawdown_alert_function_exists():
    assert hasattr(morning_brief, "_build_drawdown_alert_lines")
    assert callable(morning_brief._build_drawdown_alert_lines)


def test_morning_brief_drawdown_alert_empty_with_no_positions(tmp_db):
    lines = morning_brief._build_drawdown_alert_lines("telegram")
    assert lines == []


def test_morning_brief_drawdown_alert_kill_switch(tmp_db, monkeypatch):
    monkeypatch.setenv("RISK_MGMT_ENABLED", "false")
    db.add_position("2330", "2026-05-15", 600.0, 1000)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 500, 505, 495, 500, 1000)"
        )
    assert morning_brief._build_drawdown_alert_lines("telegram") == []


def test_morning_brief_drawdown_warns(tmp_db, monkeypatch):
    monkeypatch.setenv("RISK_MGMT_ENABLED", "true")
    db.add_position("2330", "2026-05-15", 600.0, 1000)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 500, 505, 495, 500, 1000)"
        )
    lines = morning_brief._build_drawdown_alert_lines("telegram")
    assert lines, "預期非空"
    joined = "\n".join(lines)
    assert "持倉警報" in joined
    assert "drawdown" in joined
