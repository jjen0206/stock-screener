"""src/trailing_stop.py — 動態停損上移 / DB update / batch."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src import database as db, trailing_stop as ts


def _seed_bars(sid: str, n: int = 50, base: float = 600.0, range_pct: float = 0.01):
    """灌 n 筆 daily_prices 給 ATR 算用。"""
    start = date(2026, 4, 1)
    with db.get_conn() as conn:
        for i in range(n):
            d = (start + timedelta(days=i)).isoformat()
            close = base + i * 0.5
            high = close * (1 + range_pct)
            low = close * (1 - range_pct)
            conn.execute(
                "INSERT OR REPLACE INTO daily_prices "
                "(stock_id, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, d, close, high, low, close, 1000),
            )


# === is_enabled ===

def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("TRAILING_STOP_ENABLED", raising=False)
    assert ts.is_enabled() is True


def test_is_enabled_false_via_env(monkeypatch):
    monkeypatch.setenv("TRAILING_STOP_ENABLED", "false")
    assert ts.is_enabled() is False


# === compute_trailing_stop ===

def test_compute_raises_stop_when_price_above_threshold():
    """current_price 漲超 entry+ATR → stop 上移。"""
    r = ts.compute_trailing_stop(
        entry_price=600.0, current_price=660.0, atr=6.0, multiplier=2.0,
        high_water_mark=None, current_stop=588.0,  # 原停損 588
    )
    assert r["raised"] is True
    assert r["new_stop"] > 588.0
    # new_stop = max(588, 660 - 2*6 = 648) = 648
    assert r["new_stop"] == pytest.approx(648.0, abs=0.01)
    assert r["high_water_mark"] == 660.0


def test_compute_never_lowers_stop():
    """price 跌回但 hwm 已高 → stop 不能下移。"""
    # 假設先漲到 660 (hwm=660, stop=648),再跌到 640 → stop 維持 648 不變
    r1 = ts.compute_trailing_stop(
        entry_price=600.0, current_price=660.0, atr=6.0, multiplier=2.0,
        current_stop=588.0,
    )
    assert r1["new_stop"] == pytest.approx(648.0, abs=0.01)
    # 然後 price 跌到 640,hwm 保持 660
    r2 = ts.compute_trailing_stop(
        entry_price=600.0, current_price=640.0, atr=6.0, multiplier=2.0,
        high_water_mark=660.0, current_stop=648.0,
    )
    # candidate = 660 - 12 = 648,等於 current_stop → new_stop=648
    assert r2["new_stop"] == pytest.approx(648.0, abs=0.01)
    assert r2["raised"] is False  # 沒上移
    assert r2["high_water_mark"] == 660.0  # hwm 不下移


def test_compute_hwm_updates_with_new_high():
    """price 創新高 → hwm 跟著上去 → new stop 也上移。"""
    r = ts.compute_trailing_stop(
        entry_price=600.0, current_price=700.0, atr=6.0, multiplier=2.0,
        high_water_mark=660.0, current_stop=648.0,
    )
    assert r["high_water_mark"] == 700.0
    # candidate = 700 - 12 = 688 > 648 → raised
    assert r["new_stop"] == pytest.approx(688.0, abs=0.01)
    assert r["raised"] is True


def test_compute_safety_stop_below_current_price():
    """new_stop 不可 >= current_price(避免一推就觸發)。"""
    # entry=100, atr=50, multiplier=2 → candidate = hwm - 100;
    # 如果 hwm=110, candidate = 10,但 current_price=110 → new_stop=10 (<110)
    r = ts.compute_trailing_stop(
        entry_price=100.0, current_price=110.0, atr=50.0, multiplier=2.0,
    )
    assert r["new_stop"] < r["current_price"]


def test_compute_bad_inputs():
    with pytest.raises(ValueError):
        ts.compute_trailing_stop(0, 100, 5)
    with pytest.raises(ValueError):
        ts.compute_trailing_stop(100, 100, 0)
    with pytest.raises(ValueError):
        ts.compute_trailing_stop(100, 100, 5, multiplier=0)


def test_compute_short_side():
    """short:price 跌 → stop 下移。"""
    r = ts.compute_trailing_stop(
        entry_price=600.0, current_price=540.0, atr=6.0, multiplier=2.0,
        side="short", current_stop=612.0,  # 原停損(高於 entry)
    )
    # hwm = min(600, 540) = 540
    # candidate = 540 + 12 = 552 < 612 → 下移
    assert r["new_stop"] == pytest.approx(552.0, abs=0.01)
    assert r["raised"] is True


# === update_position_trailing_stop ===

def test_update_position_writes_back_to_db(tmp_db):
    _seed_bars("2330", n=50, base=600.0, range_pct=0.01)
    # 開倉 entry=600,stop=588
    pid = db.add_position(
        "2330", entry_date="2026-04-10", entry_price=600.0, shares=1000,
        stop_loss=588.0,
    )
    # 灌一根「漲到 660」的價格(最新一根)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE daily_prices SET close=660.0, high=660.5, low=659 "
            "WHERE stock_id='2330' AND date = "
            "(SELECT MAX(date) FROM daily_prices WHERE stock_id='2330')"
        )
    res = ts.update_position_trailing_stop(pid)
    assert res is not None
    assert res["raised"] is True
    # DB 應該寫回 trailing_stop
    row = db.get_open_positions()[0]
    assert row["trailing_stop"] is not None
    assert float(row["trailing_stop"]) > 588.0
    assert row["high_water_mark"] == 660.0


def test_update_position_kill_switch_off(tmp_db, monkeypatch):
    monkeypatch.setenv("TRAILING_STOP_ENABLED", "false")
    _seed_bars("2330", n=50)
    pid = db.add_position("2330", "2026-04-10", 600.0, 1000)
    assert ts.update_position_trailing_stop(pid) is None


def test_update_position_no_data_returns_none(tmp_db):
    """沒 daily_prices → None。"""
    pid = db.add_position("9999", "2026-04-10", 100.0, 1000)
    assert ts.update_position_trailing_stop(pid) is None


# === batch_update_trailing_stops ===

def test_batch_update_summary(tmp_db):
    _seed_bars("2330", n=50, base=600.0, range_pct=0.01)
    _seed_bars("2454", n=50, base=900.0, range_pct=0.01)
    db.add_position("2330", "2026-04-10", 600.0, 1000, stop_loss=588.0)
    db.add_position("2454", "2026-04-10", 900.0, 500, stop_loss=882.0)
    # 把 2330 拉高 → 該檔會 raised
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE daily_prices SET close=660.0, high=660.5, low=659 "
            "WHERE stock_id='2330' AND date = "
            "(SELECT MAX(date) FROM daily_prices WHERE stock_id='2330')"
        )
    summary = ts.batch_update_trailing_stops()
    assert summary["checked"] == 2
    assert summary["updated"] >= 1
    assert any(r["sid"] == "2330" for r in summary["raised_positions"])


def test_batch_update_kill_switch(tmp_db, monkeypatch):
    monkeypatch.setenv("TRAILING_STOP_ENABLED", "false")
    summary = ts.batch_update_trailing_stops()
    assert summary["checked"] == 0
    assert summary["raised_positions"] == []
