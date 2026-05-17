"""Guard the notifier / morning_brief / intraday_alerts → price_alerts wire path。

跟 test_notifier_drawdown_alert.py 對齊的 pattern:
- 確保 notifier.format_top_picks_message 有 wire _format_price_alerts_section
- 確保 morning_brief 兩個 channel formatter 都有 wire _build_price_alert_lines
- 確保 scripts.intraday_alerts.run 有走 price_alerts check + 推播
"""
from __future__ import annotations

import inspect

import src.notifier as notifier_mod
from scripts import intraday_alerts, morning_brief
from src import database as db


# === notifier.format_top_picks_message wire ===

def test_format_top_picks_calls_price_alerts_section():
    src = inspect.getsource(notifier_mod.format_top_picks_message)
    assert "_format_price_alerts_section(" in src, (
        "format_top_picks_message 必須 wire _format_price_alerts_section"
    )


def test_format_price_alerts_section_function_exists():
    assert hasattr(notifier_mod, "_format_price_alerts_section")
    assert callable(notifier_mod._format_price_alerts_section)


def test_price_alerts_section_empty_when_kill_switch_off(tmp_db, monkeypatch):
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "false")
    db.add_alert("2330", "price_above", 100.0)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 200, 205, 195, 200, 1000)"
        )
        conn.commit()
    assert notifier_mod._format_price_alerts_section(channel="telegram") == ""


def test_price_alerts_section_empty_when_no_triggered(tmp_db, monkeypatch):
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "true")
    # 設一個不會觸發的 alert(target 比 current 高)
    db.add_alert("2330", "price_above", 1000.0)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 500, 505, 495, 500, 1000)"
        )
        conn.commit()
    assert notifier_mod._format_price_alerts_section(channel="telegram") == ""


def test_price_alerts_section_shows_triggered(tmp_db, monkeypatch):
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "true")
    db.add_alert("2330", "price_above", 600.0)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO stocks (stock_id, name, market, updated_at) "
            "VALUES ('2330', '台積電', 'TW', '2026-05-17T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 610, 615, 605, 612, 1000)"
        )
        conn.commit()
    out = notifier_mod._format_price_alerts_section(channel="telegram")
    assert "警報快訊" in out
    assert "2330" in out
    assert "price_above" in out


# === morning_brief wire ===

def test_morning_brief_calls_price_alert_lines_telegram():
    src = inspect.getsource(morning_brief._format_full_telegram)
    assert "_build_price_alert_lines(" in src


def test_morning_brief_calls_price_alert_lines_discord():
    src = inspect.getsource(morning_brief._format_full_discord)
    assert "_build_price_alert_lines(" in src


def test_morning_brief_build_price_alert_lines_exists():
    assert hasattr(morning_brief, "_build_price_alert_lines")
    assert callable(morning_brief._build_price_alert_lines)


def test_morning_brief_price_alert_lines_kill_switch(tmp_db, monkeypatch):
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "false")
    db.add_alert("2330", "price_above", 100.0)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 200, 205, 195, 200, 1000)"
        )
        conn.commit()
    assert morning_brief._build_price_alert_lines("telegram") == []


# === intraday_alerts wire ===

def test_intraday_alerts_run_imports_price_alerts():
    src = inspect.getsource(intraday_alerts)
    assert "from src import price_alerts as pa" in src
    assert "check_price_alerts" in src
    assert "check_intraday_drop" in src


def test_intraday_alerts_run_has_price_alert_label():
    """新增 ALERT_PRICE_ALERT / ALERT_INTRADAY_DROP constants 給 dedup 用。"""
    assert intraday_alerts.ALERT_PRICE_ALERT == "price_alert"
    assert intraday_alerts.ALERT_INTRADAY_DROP == "intraday_drop"


def test_intraday_alerts_pushes_price_alert(tmp_db, monkeypatch):
    """設一個會觸發的 price_above + active trade(避開 no-trades early-return),
    跑 run() 應觸發 price_alert push。"""
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "true")
    # active paper_trade(任何條件都不觸,只是為了讓 run() 不 early-return)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO stocks (stock_id, name, market, updated_at) "
            "VALUES ('2330', '台積電', 'TW', '2026-05-17T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO paper_trades "
            "(sid, name, entry_date, entry_price, target_price, stop_price, "
            " current_stop, hold_days, status, created_at) "
            "VALUES ('9999', 'X', '2026-05-14', 100, 110, 90, 90, 5, 'active', "
            " '2026-05-14T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('2330', '2026-05-17', 610, 615, 605, 612, 1000)"
        )
        conn.execute(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES ('9999', '2026-05-17', 100, 100, 100, 100, 1000)"
        )
        conn.commit()
    db.add_alert("2330", "price_above", 600.0)

    tg_calls: list = []
    dc_calls: list = []
    monkeypatch.setattr(
        intraday_alerts, "send_telegram_message",
        lambda text, **k: tg_calls.append(text) or True,
    )
    monkeypatch.setattr(
        intraday_alerts, "send_discord_message",
        lambda content, **k: dc_calls.append(content) or True,
    )
    monkeypatch.setattr(
        intraday_alerts, "get_intraday_quote",
        lambda sids: {"9999": {
            "current": 100.0, "prev_close": 100.0,
            "change_pct": 0.0, "volume": 1000,
        }},
    )

    stats = intraday_alerts.run(use_intraday=True)
    assert stats["n_price_alerts"] == 1
    # 推一筆 price_alert
    pa_pushed = [t for t in tg_calls if "警報觸發" in t and "2330" in t]
    assert len(pa_pushed) == 1
    # mark_triggered 寫進去了
    rows = db.list_alerts(active_only=False)
    assert rows[0]["triggered_at"] is not None
    assert rows[0]["is_active"] == 0
