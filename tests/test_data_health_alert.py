"""scripts/data_health_alert.py 單元測試(production schema fixture)。

涵蓋:
  - 有 stale 推單則整合訊息
  - 無 stale silent(不推)
  - 多重 stale 一則訊息列出全部
  - 完全沒資料 → 也算 stale
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import data_health_alert  # noqa: E402
from src import database as db  # noqa: E402

# tmp_db fixture 共用 tests/conftest.py


def _patch_pushers(monkeypatch, tg_calls: list, dc_calls: list):
    def _fake_tg(text, **kwargs):
        tg_calls.append(text)
        return True

    def _fake_dc(content, **kwargs):
        dc_calls.append(content)
        return True

    monkeypatch.setattr(data_health_alert, "send_telegram_message", _fake_tg)
    monkeypatch.setattr(data_health_alert, "send_discord_message", _fake_dc)


def _seed_all_fresh(today: date) -> None:
    """5 表全餵 today 一筆 → 全 fresh(0 stale)。"""
    yesterday = (today - timedelta(days=1)).isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices (stock_id, date, close) VALUES (?,?,?)",
            ("2330", yesterday, 1000.0),
        )
        conn.execute(
            "INSERT INTO institutional (stock_id, date) VALUES (?,?)",
            ("2330", yesterday),
        )
        # shareholder week_end:最近的週五
        wk = today
        while wk.weekday() != 4:
            wk -= timedelta(days=1)
        conn.execute(
            "INSERT INTO shareholder_concentration "
            "(sid, week_end, holders_1000up_count, total_holders, fetched_at) "
            "VALUES (?,?,?,?,?)",
            ("2330", wk.isoformat(), 100, 1000, today.isoformat()),
        )
        conn.execute(
            "INSERT INTO pick_outcomes (pick_date, sid, strategy, evaluated_at) "
            "VALUES (?,?,?,?)",
            (yesterday, "2330", "ma_alignment", today.isoformat()),
        )
        conn.execute(
            "INSERT INTO daily_picks "
            "(trade_date, universe, strategy, sid, params_hash, computed_at) "
            "VALUES (?,?,?,?,?,?)",
            (yesterday, "pure_stock", "ma_alignment", "2330",
             "default_v1", today.isoformat()),
        )
        conn.commit()


def test_no_stale_silent(tmp_db, monkeypatch):
    """全表 fresh → run() 回 stale_count=0,不推 channel。"""
    today = date(2026, 5, 14)  # Thursday — yesterday 2026-05-13 是 Wed
    _seed_all_fresh(today)
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)

    r = data_health_alert.run(today=today)
    assert r["stale_count"] == 0
    assert r["pushed"] is False
    assert tg_calls == []
    assert dc_calls == []


def test_single_stale_pushes_alert(tmp_db, monkeypatch):
    """單一表 stale → push 整合訊息(只列那一張)。"""
    today = date(2026, 5, 14)  # Thursday
    _seed_all_fresh(today)
    # 改 daily_prices 變成 5/8 (Fri),距 5/14 (Thu) trading days = 4 → stale
    with db.get_conn() as conn:
        conn.execute("DELETE FROM daily_prices WHERE stock_id='2330'")
        conn.execute(
            "INSERT INTO daily_prices (stock_id, date, close) VALUES (?,?,?)",
            ("2330", "2026-05-08", 1000.0),
        )
        conn.commit()
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)

    r = data_health_alert.run(today=today)
    assert r["stale_count"] == 1
    assert r["pushed"] is True
    assert len(tg_calls) == 1
    assert "⚠️" in tg_calls[0]
    assert "daily_prices" in tg_calls[0]
    assert "落後" in tg_calls[0]
    assert "2026-05-08" in tg_calls[0]
    # 其他 4 張表 fresh 不應出現在訊息
    assert "institutional" not in tg_calls[0]
    assert "shareholder" not in tg_calls[0]
    assert "pick_outcomes" not in tg_calls[0]
    assert "daily_picks" not in tg_calls[0]


def test_multiple_stale_single_message(tmp_db, monkeypatch):
    """多張 stale → 仍只推一則,把所有 stale 列在訊息內。"""
    today = date(2026, 5, 14)
    # 故意 seed 全部 stale:daily_prices/inst 用上週,shareholder 用 2 週前
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices (stock_id, date, close) VALUES (?,?,?)",
            ("2330", "2026-04-30", 1000.0),
        )
        conn.execute(
            "INSERT INTO institutional (stock_id, date) VALUES (?,?)",
            ("2330", "2026-04-30"),
        )
        conn.execute(
            "INSERT INTO shareholder_concentration "
            "(sid, week_end, holders_1000up_count, total_holders, fetched_at) "
            "VALUES (?,?,?,?,?)",
            ("2330", "2026-04-25", 100, 1000, today.isoformat()),
        )
        conn.execute(
            "INSERT INTO pick_outcomes (pick_date, sid, strategy, evaluated_at) "
            "VALUES (?,?,?,?)",
            ("2026-05-01", "2330", "ma_alignment", today.isoformat()),
        )
        conn.execute(
            "INSERT INTO daily_picks "
            "(trade_date, universe, strategy, sid, params_hash, computed_at) "
            "VALUES (?,?,?,?,?,?)",
            ("2026-05-01", "pure_stock", "ma_alignment", "2330",
             "default_v1", today.isoformat()),
        )
        conn.commit()
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)

    r = data_health_alert.run(today=today)
    assert r["stale_count"] == 5
    assert len(tg_calls) == 1
    assert len(dc_calls) == 1
    msg = tg_calls[0]
    for label in [
        "daily_prices", "institutional", "shareholder_concentration",
        "pick_outcomes", "daily_picks",
    ]:
        assert label in msg, f"{label} 應在整合訊息內,實際: {msg}"


def test_empty_table_counted_as_stale(tmp_db, monkeypatch):
    """完全沒資料的表 → 視為 stale,訊息顯「無資料」。"""
    today = date(2026, 5, 14)
    # 不 seed 任何 row
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)

    r = data_health_alert.run(today=today)
    assert r["stale_count"] == 5  # 5 張表全部空
    assert "無資料" in tg_calls[0]


def test_dry_run_no_push(tmp_db, monkeypatch):
    """--dry-run 有 stale 仍不推。"""
    today = date(2026, 5, 14)
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)

    r = data_health_alert.run(today=today, dry_run=True)
    assert r["stale_count"] == 5
    assert tg_calls == []
    assert dc_calls == []
    assert r["pushed"] is False


def test_trading_days_helper_skips_weekend():
    """週一今天,週五 latest → 0 trading days(週六/日不算)。"""
    later = date(2026, 5, 18)   # Monday
    earlier = date(2026, 5, 15)  # Friday
    assert data_health_alert._trading_days_between(later, earlier) == 0
    # 週一今天,週四 latest → 1 trading day(只算週五)
    assert data_health_alert._trading_days_between(
        date(2026, 5, 18), date(2026, 5, 14),
    ) == 1
