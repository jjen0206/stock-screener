"""src/price_alerts.py + db helpers 單元測試。

涵蓋:
  - add_alert / list_alerts / mark_triggered / delete_alert(CRUD)
  - check_price_alerts:price_above / price_below / pct_change / ex_dividend
  - kill-switch PRICE_ALERT_ENABLED=false → 全部回 []
  - format_alert_message 對齊主公格式
"""
from __future__ import annotations

import sys
from datetime import date as _date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from src import database as db  # noqa: E402
from src import price_alerts as pa  # noqa: E402

# tmp_db fixture 從 tests/conftest.py 來


def _seed_price(sid: str, close: float, date: str = "2026-05-17") -> None:
    """寫一筆 daily_prices,讓 _latest_close 算得出。"""
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices (stock_id, date, open, high, low, close, "
            "volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, date, close, close, close, close, 1000),
        )
        conn.commit()


def _seed_stock(sid: str, name: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stocks (stock_id, name, market, updated_at) "
            "VALUES (?, ?, 'TW', '2026-05-17T00:00:00Z')",
            (sid, name),
        )
        conn.commit()


# === db helpers CRUD ===


def test_add_alert_returns_id(tmp_db):
    aid = db.add_alert("2330", "price_above", target_value=610.0, notes="watch")
    assert aid > 0
    rows = db.list_alerts()
    assert len(rows) == 1
    assert rows[0]["stock_id"] == "2330"
    assert rows[0]["alert_type"] == "price_above"
    assert rows[0]["target_value"] == 610.0
    assert rows[0]["is_active"] == 1
    assert rows[0]["triggered_at"] is None


def test_add_alert_rejects_invalid_type(tmp_db):
    with pytest.raises(ValueError):
        db.add_alert("2330", "not_a_type", target_value=1.0)


def test_add_alert_rejects_empty_sid(tmp_db):
    with pytest.raises(ValueError):
        db.add_alert("  ", "price_above", target_value=1.0)


def test_list_alerts_active_only_default(tmp_db):
    aid1 = db.add_alert("2330", "price_above", 610.0)
    aid2 = db.add_alert("2454", "price_below", 1200.0)
    db.mark_triggered(aid1)
    active = db.list_alerts()  # active_only=True default
    assert len(active) == 1
    assert active[0]["id"] == aid2
    all_rows = db.list_alerts(active_only=False)
    assert len(all_rows) == 2


def test_list_alerts_filter_by_sid(tmp_db):
    db.add_alert("2330", "price_above", 610.0)
    db.add_alert("2454", "price_below", 1200.0)
    rows = db.list_alerts(stock_id="2330")
    assert len(rows) == 1
    assert rows[0]["stock_id"] == "2330"


def test_mark_triggered_sets_inactive(tmp_db):
    aid = db.add_alert("2330", "price_above", 610.0)
    assert db.mark_triggered(aid) is True
    rows = db.list_alerts(active_only=False)
    assert rows[0]["triggered_at"] is not None
    assert rows[0]["is_active"] == 0
    # 二次呼叫 inactive 不該成功
    assert db.mark_triggered(aid) is False


def test_delete_alert(tmp_db):
    aid = db.add_alert("2330", "price_above", 610.0)
    assert db.delete_alert(aid) is True
    assert db.list_alerts(active_only=False) == []
    assert db.delete_alert(aid) is False


# === engine: check_price_alerts ===


def test_price_above_triggers(tmp_db):
    _seed_stock("2330", "台積電")
    _seed_price("2330", 612.0)
    db.add_alert("2330", "price_above", target_value=610.0)
    with db.get_conn() as conn:
        out = pa.check_price_alerts(conn)
    assert len(out) == 1
    assert out[0]["stock_id"] == "2330"
    assert out[0]["alert_type"] == "price_above"
    assert out[0]["current_price"] == 612.0
    assert out[0]["target_value"] == 610.0
    assert "2330" in out[0]["message"]
    assert "612" in out[0]["message"]


def test_price_above_not_triggered_when_below(tmp_db):
    _seed_price("2330", 600.0)
    db.add_alert("2330", "price_above", target_value=610.0)
    with db.get_conn() as conn:
        out = pa.check_price_alerts(conn)
    assert out == []


def test_price_below_triggers(tmp_db):
    _seed_stock("2330", "台積電")
    _seed_price("2330", 595.0)
    db.add_alert("2330", "price_below", target_value=600.0)
    with db.get_conn() as conn:
        out = pa.check_price_alerts(conn)
    assert len(out) == 1
    assert out[0]["alert_type"] == "price_below"
    assert out[0]["current_price"] == 595.0


def test_pct_change_triggers_with_baseline_in_notes(tmp_db):
    _seed_stock("2330", "台積電")
    _seed_price("2330", 660.0)  # +10% from 600
    db.add_alert("2330", "pct_change", target_value=5.0, notes="base=600")
    with db.get_conn() as conn:
        out = pa.check_price_alerts(conn)
    assert len(out) == 1
    assert out[0]["alert_type"] == "pct_change"


def test_pct_change_skipped_when_no_baseline(tmp_db):
    """沒 base=XXX → 不觸發(避免誤觸)。"""
    _seed_price("2330", 660.0)
    db.add_alert("2330", "pct_change", target_value=5.0, notes="no baseline here")
    with db.get_conn() as conn:
        out = pa.check_price_alerts(conn)
    assert out == []


def test_pct_change_below_threshold_no_trigger(tmp_db):
    _seed_price("2330", 610.0)  # +1.67% from 600
    db.add_alert("2330", "pct_change", target_value=5.0, notes="base=600")
    with db.get_conn() as conn:
        out = pa.check_price_alerts(conn)
    assert out == []


def test_ex_dividend_triggers_within_window(tmp_db):
    _seed_stock("2330", "台積電")
    ex = (_date.today() + timedelta(days=2)).isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO dividend (stock_id, year, cash_dividend, "
            "stock_dividend, ex_dividend_date) VALUES (?, ?, ?, ?, ?)",
            ("2330", 2026, 5.0, 0.0, ex),
        )
        conn.commit()
    db.add_alert("2330", "ex_dividend", target_value=3.0)
    with db.get_conn() as conn:
        out = pa.check_price_alerts(conn)
    assert len(out) == 1
    assert out[0]["alert_type"] == "ex_dividend"


def test_ex_dividend_outside_window_no_trigger(tmp_db):
    ex = (_date.today() + timedelta(days=30)).isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO dividend (stock_id, year, cash_dividend, "
            "stock_dividend, ex_dividend_date) VALUES (?, ?, ?, ?, ?)",
            ("2330", 2026, 5.0, 0.0, ex),
        )
        conn.commit()
    db.add_alert("2330", "ex_dividend", target_value=3.0)
    with db.get_conn() as conn:
        out = pa.check_price_alerts(conn)
    assert out == []


def test_inactive_alerts_ignored(tmp_db):
    _seed_price("2330", 612.0)
    aid = db.add_alert("2330", "price_above", 610.0)
    db.mark_triggered(aid)
    with db.get_conn() as conn:
        out = pa.check_price_alerts(conn)
    assert out == []


# === kill-switch ===


def test_kill_switch_disables_all(tmp_db, monkeypatch):
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "false")
    _seed_price("2330", 612.0)
    db.add_alert("2330", "price_above", 610.0)
    with db.get_conn() as conn:
        assert pa.check_price_alerts(conn) == []
        assert pa.check_intraday_drop(conn) == []
        assert pa.check_ex_dividend_alerts(conn) == []


# === message format ===


def test_format_alert_message_price_above():
    msg = pa.format_alert_message(
        "2330", "台積電", "price_above", 610.0, 612.0,
        triggered_at="2026-05-17 13:42",
    )
    assert "🚨 警報觸發" in msg
    assert "2330 台積電" in msg
    assert "$612" in msg
    assert "$610" in msg
    assert "2026-05-17 13:42" in msg
    assert "確認進場/出場" in msg


def test_format_alert_message_intraday_drop():
    msg = pa.format_alert_message(
        "2330", "台積電", "intraday_drop", -3.0, 580.0,
        triggered_at="2026-05-17 13:42",
        extra="當日跌幅 -3.50%・確認是否觸停損",
    )
    assert "當日跌幅 ≤ -3.00%" in msg
    assert "當日跌幅 -3.50%" in msg
