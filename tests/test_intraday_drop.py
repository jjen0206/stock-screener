"""持倉急殺 check_intraday_drop 單元測試。

涵蓋:
  - open position 當日跌幅 ≤ threshold → 觸發
  - 跌幅未過 threshold → 不觸發
  - 沒有 open positions → []
  - 兩筆持倉同時跌 → 各自一筆 alert
  - kill-switch off → []
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src import price_alerts as pa  # noqa: E402


def _seed_two_day_price(sid: str, prev: float, latest: float) -> None:
    """寫兩天 daily_prices,讓 _current_change_pct 拿 prev close 算當日跌幅。"""
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO daily_prices (stock_id, date, open, high, low, close, "
            "volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (sid, "2026-05-16", prev, prev, prev, prev, 1000),
                (sid, "2026-05-17", latest, latest, latest, latest, 1000),
            ],
        )
        conn.commit()


def _add_open_position(sid: str, entry: float, shares: int = 1000) -> int:
    return db.add_position(
        sid, "2026-05-15", entry, shares,
    )


def test_intraday_drop_triggers_on_threshold(tmp_db):
    """latest=970 vs prev=1000 → -3.0%,門檻 -3.0 ≤ → 觸發。"""
    _add_open_position("2330", 1000.0)
    _seed_two_day_price("2330", prev=1000.0, latest=970.0)
    with db.get_conn() as conn:
        out = pa.check_intraday_drop(conn, threshold_pct=-3.0)
    assert len(out) == 1
    assert out[0]["stock_id"] == "2330"
    assert out[0]["alert_type"] == "intraday_drop"
    assert abs(out[0]["change_pct"] - (-3.0)) < 0.01
    assert "持倉急殺" in out[0]["message"]


def test_intraday_drop_below_threshold(tmp_db):
    """latest=985 vs prev=1000 → -1.5%,門檻 -3.0 → 不觸發。"""
    _add_open_position("2330", 1000.0)
    _seed_two_day_price("2330", prev=1000.0, latest=985.0)
    with db.get_conn() as conn:
        out = pa.check_intraday_drop(conn, threshold_pct=-3.0)
    assert out == []


def test_intraday_drop_no_open_positions(tmp_db):
    _seed_two_day_price("2330", prev=1000.0, latest=900.0)
    with db.get_conn() as conn:
        out = pa.check_intraday_drop(conn)
    assert out == []


def test_intraday_drop_skips_closed_positions(tmp_db):
    """已平倉不算。"""
    pid = _add_open_position("2330", 1000.0)
    db.close_position(pid, exit_price=900.0)
    _seed_two_day_price("2330", prev=1000.0, latest=900.0)
    with db.get_conn() as conn:
        out = pa.check_intraday_drop(conn)
    assert out == []


def test_intraday_drop_multiple_positions(tmp_db):
    """兩檔持倉同時急殺 → 兩筆 alert。"""
    _add_open_position("2330", 1000.0)
    _add_open_position("2454", 1200.0)
    _seed_two_day_price("2330", prev=1000.0, latest=950.0)
    _seed_two_day_price("2454", prev=1200.0, latest=1140.0)
    with db.get_conn() as conn:
        out = pa.check_intraday_drop(conn, threshold_pct=-3.0)
    sids = sorted(o["stock_id"] for o in out)
    assert sids == ["2330", "2454"]


def test_intraday_drop_kill_switch(tmp_db, monkeypatch):
    monkeypatch.setenv("PRICE_ALERT_ENABLED", "false")
    _add_open_position("2330", 1000.0)
    _seed_two_day_price("2330", prev=1000.0, latest=900.0)
    with db.get_conn() as conn:
        assert pa.check_intraday_drop(conn) == []


def test_intraday_drop_skips_when_no_prev_close(tmp_db):
    """只有一筆 close → 無法算當日跌幅(差太近),回 None,不觸發。"""
    _add_open_position("2330", 1000.0)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices (stock_id, date, open, high, low, close, "
            "volume) VALUES ('2330', '2026-05-17', 1000, 1000, 1000, 1000, 100)"
        )
        conn.commit()
    with db.get_conn() as conn:
        out = pa.check_intraday_drop(conn)
    assert out == []
