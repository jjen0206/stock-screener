"""持倉/watchlist 急跌 -5% 警報(2026-05-19 方案 B)單元測試。

涵蓋:
- 持倉觸發(user_positions is_open=1)+ watchlist 也觸發(union)
- threshold gating(-5% 才推,-3% 不該觸這條;-3% 走 check_intraday_drop)
- 訊息精簡格式驗證
- 持倉股 + stop_price 顯示停損
- PRICE_ALERT_ENABLED=false → 空 list
"""
from __future__ import annotations

import pytest


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    from src import config, database as db
    db_file = tmp_path / "severe_drop.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db


def _seed_daily(conn, sid: str, prices: list[float]):
    """寫近 N 天的 daily_prices,prices[0] 是最新一根(today)。"""
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    for i, p in enumerate(prices):
        d = (today - _td(days=i)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO daily_prices (stock_id, date, open, high, "
            "low, close, volume, trading_money, trading_turnover, spread) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, d, p, p, p, p, 1000, None, None, 0.0),
        )


def _seed_stock(conn, sid: str, name: str = "Test"):
    conn.execute(
        "INSERT OR IGNORE INTO stocks (stock_id, name, market) VALUES (?, ?, ?)",
        (sid, name, "TW"),
    )


def _seed_position(conn, sid: str, entry: float = 100.0, stop: float = 95.0):
    """寫一筆 open user_position。"""
    conn.execute(
        "INSERT INTO user_positions (stock_id, entry_date, entry_price, "
        "shares, stop_loss, take_profit, is_open, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (sid, "2026-05-15", entry, 1000, stop, None, "test",
         "2026-05-15T00:00:00"),
    )


def _seed_watchlist(conn, sid: str):
    conn.execute(
        "INSERT OR REPLACE INTO watchlist (stock_id, added_at, note) "
        "VALUES (?, ?, ?)",
        (sid, "2026-05-15T00:00:00", ""),
    )


def test_holding_severe_drop_triggers_at_minus_5pct(fresh_db, monkeypatch):
    """持倉今日跌 -5.5% → 觸發。"""
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "true")
    from src import price_alerts as pa
    db = fresh_db
    with db.get_conn() as conn:
        _seed_stock(conn, "2330", "台積電")
        _seed_position(conn, "2330", entry=100.0, stop=92.0)
        # prev 100 → today 94.5 = -5.5%
        _seed_daily(conn, "2330", [94.5, 100.0])
        conn.commit()

        alerts = pa.check_holding_severe_drop(conn)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["stock_id"] == "2330"
    assert a["alert_type"] == "holding_severe_drop"
    assert a["change_pct"] < -5.0
    assert "急跌" in a["message"]
    assert "建議檢視持倉" in a["message"]
    # 停損點 92 應該顯
    assert "92.00" in a["message"]


def test_holding_severe_drop_skips_minus_3pct(fresh_db, monkeypatch):
    """跌 -3.5%(< 5%)→ 不觸這條(由 check_intraday_drop 接手)。"""
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "true")
    from src import price_alerts as pa
    db = fresh_db
    with db.get_conn() as conn:
        _seed_stock(conn, "2330", "台積電")
        _seed_position(conn, "2330", entry=100.0, stop=95.0)
        # prev 100 → today 96.5 = -3.5%
        _seed_daily(conn, "2330", [96.5, 100.0])
        conn.commit()

        alerts = pa.check_holding_severe_drop(conn)
    assert alerts == []


def test_holding_severe_drop_triggers_for_watchlist(fresh_db, monkeypatch):
    """watchlist 股(無 user_position)跌 -6% → 觸發(union 來源)。"""
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "true")
    from src import price_alerts as pa
    db = fresh_db
    with db.get_conn() as conn:
        _seed_stock(conn, "2317", "鴻海")
        _seed_watchlist(conn, "2317")
        # prev 50 → today 47 = -6%
        _seed_daily(conn, "2317", [47.0, 50.0])
        conn.commit()

        alerts = pa.check_holding_severe_drop(conn)
    assert len(alerts) == 1
    assert alerts[0]["stock_id"] == "2317"
    # 無 user_position → 無停損段
    assert "停損" not in alerts[0]["message"]


def test_holding_severe_drop_kill_switch(fresh_db, monkeypatch):
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "false")
    from src import price_alerts as pa
    # 強制 re-eval is_enabled
    db = fresh_db
    with db.get_conn() as conn:
        _seed_stock(conn, "2330", "台積電")
        _seed_position(conn, "2330")
        _seed_daily(conn, "2330", [90.0, 100.0])  # -10%
        conn.commit()

        alerts = pa.check_holding_severe_drop(conn)
    assert alerts == []


def test_holding_severe_drop_union_dedups_position_and_watchlist(
    fresh_db, monkeypatch,
):
    """同一 sid 既在 user_position 又在 watchlist → 只觸發 1 次。"""
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "true")
    from src import price_alerts as pa
    db = fresh_db
    with db.get_conn() as conn:
        _seed_stock(conn, "2454", "聯發科")
        _seed_position(conn, "2454", entry=100.0, stop=95.0)
        _seed_watchlist(conn, "2454")
        _seed_daily(conn, "2454", [93.0, 100.0])  # -7%
        conn.commit()

        alerts = pa.check_holding_severe_drop(conn)
    assert len(alerts) == 1


def test_holding_severe_drop_no_sids_returns_empty(fresh_db, monkeypatch):
    """無 user_position 無 watchlist → 空。"""
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "true")
    from src import price_alerts as pa
    db = fresh_db
    with db.get_conn() as conn:
        alerts = pa.check_holding_severe_drop(conn)
    assert alerts == []
