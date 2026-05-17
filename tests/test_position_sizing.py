"""src/position_sizing.py 單元測試:Kelly + suggest_position_size + win_stats。"""
from __future__ import annotations

import pytest

from src import database as db, position_sizing as ps

# tmp_db fixture 共用 tests/conftest.py


# === kelly_fraction ===

def test_kelly_zero_when_no_edge():
    """win_rate=0.5 / R:R=1 → Kelly = 0(等同擲硬幣 even-money)。"""
    assert ps.kelly_fraction(0.5, 1.0, kelly_multiplier=1.0) == 0.0


def test_kelly_negative_clamps_to_zero():
    """劣勢場景:win_rate=0.3 / R:R=1 → Kelly < 0 → clamp 到 0。"""
    assert ps.kelly_fraction(0.3, 1.0, kelly_multiplier=1.0) == 0.0


def test_kelly_full_when_advantage():
    """win_rate=0.7 / R:R=1 → Kelly = 0.7 - 0.3/1 = 0.4(full)。"""
    f = ps.kelly_fraction(0.7, 1.0, kelly_multiplier=1.0)
    assert abs(f - 0.4) < 1e-6


def test_kelly_multiplier_quarter():
    """× 0.25 multiplier → 0.4 × 0.25 = 0.1。"""
    f = ps.kelly_fraction(0.7, 1.0, kelly_multiplier=0.25)
    assert abs(f - 0.1) < 1e-6


def test_kelly_with_win_loss_ratio_2():
    """win_rate=0.5 / R:R=2 → Kelly = 0.5 - 0.5/2 = 0.25。"""
    f = ps.kelly_fraction(0.5, 2.0, kelly_multiplier=1.0)
    assert abs(f - 0.25) < 1e-6


def test_kelly_bad_inputs_raise():
    with pytest.raises(ValueError):
        ps.kelly_fraction(-0.1, 1.0)
    with pytest.raises(ValueError):
        ps.kelly_fraction(1.1, 1.0)
    with pytest.raises(ValueError):
        ps.kelly_fraction(0.5, 0.0)
    with pytest.raises(ValueError):
        ps.kelly_fraction(0.5, 1.0, kelly_multiplier=0.0)


# === is_enabled (env kill-switch) ===

def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("POSITION_SIZING_ENABLED", raising=False)
    assert ps.is_enabled() is True


def test_is_enabled_false_via_env(monkeypatch):
    monkeypatch.setenv("POSITION_SIZING_ENABLED", "false")
    assert ps.is_enabled() is False


# === get_recent_win_stats ===

def test_get_recent_win_stats_empty_fallback(tmp_db):
    """pick_outcomes 空表 → fallback 50% / 1.5。"""
    stats = ps.get_recent_win_stats(days=30)
    assert stats["is_fallback"] is True
    assert stats["n"] == 0
    assert stats["win_rate"] == 0.5
    assert stats["win_loss_ratio"] == 1.5


def test_get_recent_win_stats_from_rows(tmp_db):
    """灌幾筆 pick_outcomes → 計算 win_rate + R:R。"""
    today = "2026-05-10"
    rows = [
        # 3 wins(hit_target=1, return_d5=+0.05)
        {"pick_date": today, "sid": f"A{i}", "strategy": "x",
         "entry_close": 100.0, "return_d1": 0.01, "return_d3": 0.02,
         "return_d5": 0.05, "return_d10": 0.06,
         "hit_target": 1.0, "stopped_out": 0.0,
         "evaluated_at": "now"}
        for i in range(3)
    ] + [
        # 2 losses(hit_target=0, return_d5=-0.02)
        {"pick_date": today, "sid": f"B{i}", "strategy": "x",
         "entry_close": 100.0, "return_d1": -0.01, "return_d3": -0.015,
         "return_d5": -0.02, "return_d10": -0.025,
         "hit_target": 0.0, "stopped_out": 1.0,
         "evaluated_at": "now"}
        for i in range(2)
    ]
    db.dump_pick_outcomes(rows)
    stats = ps.get_recent_win_stats(days=30)
    assert stats["n"] == 5
    assert stats["is_fallback"] is False
    assert abs(stats["win_rate"] - 0.6) < 1e-6  # 3/5
    # R:R = mean(wins) / mean(losses) = 0.05 / 0.02 = 2.5
    assert abs(stats["win_loss_ratio"] - 2.5) < 1e-6


# === suggest_position_size ===

def _seed_close(sid: str, price: float, db_path=None) -> None:
    with db.get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "2026-05-10", price, price, price, price, 1000),
        )


def test_suggest_basic_calculation(tmp_db):
    """ml_prob=0.7, R:R=1.0 → Kelly 0.4 × 0.25 = 0.10。1MM × 10% = 100k。"""
    _seed_close("2330", 600.0)
    res = ps.suggest_position_size(
        "2330", ml_prob=0.7, total_capital=1_000_000,
        win_loss_ratio=1.0,
    )
    assert abs(res["position_pct"] - 0.10) < 1e-6
    assert abs(res["suggested_amount"] - 100_000) < 1e-6
    assert res["current_price"] == 600.0
    # 100k / 600 = 166 股 → 0 lot
    assert res["suggested_shares"] == 166
    assert res["suggested_lots"] == 0
    assert res["capped_by"] == "kelly"


def test_suggest_capped_by_max_single(tmp_db):
    """高 ML(99%) → Kelly 大,被 max_single_pct=0.20 截斷。"""
    _seed_close("2330", 600.0)
    res = ps.suggest_position_size(
        "2330", ml_prob=0.99, total_capital=1_000_000,
        win_loss_ratio=2.0,
        max_single_pct=0.20,
    )
    assert res["position_pct"] == 0.20
    assert res["capped_by"] == "max_single"


def test_suggest_no_edge_returns_zero(tmp_db):
    """ml_prob=0.3 → Kelly = 0 → position_pct = 0,shares = 0。"""
    _seed_close("2330", 600.0)
    res = ps.suggest_position_size(
        "2330", ml_prob=0.3, total_capital=1_000_000,
        win_loss_ratio=1.0,
    )
    assert res["position_pct"] == 0.0
    assert res["suggested_shares"] == 0


def test_suggest_weak_confidence_half(tmp_db):
    """confidence='weak' → 額外 × 0.5。"""
    _seed_close("2330", 600.0)
    base = ps.suggest_position_size(
        "2330", ml_prob=0.7, total_capital=1_000_000, win_loss_ratio=1.0,
    )
    weak = ps.suggest_position_size(
        "2330", ml_prob=0.7, total_capital=1_000_000, win_loss_ratio=1.0,
        confidence="weak",
    )
    assert abs(weak["position_pct"] - base["position_pct"] * 0.5) < 1e-6


def test_suggest_no_close_no_shares(tmp_db):
    """沒 daily_prices → suggested_shares = 0,但 position_pct 仍算。"""
    res = ps.suggest_position_size(
        "8888", ml_prob=0.7, total_capital=1_000_000, win_loss_ratio=1.0,
    )
    assert res["position_pct"] > 0
    assert res["current_price"] is None
    assert res["suggested_shares"] == 0


def test_suggest_bad_inputs_raise():
    with pytest.raises(ValueError):
        ps.suggest_position_size("2330", ml_prob=0.5, total_capital=0)
    with pytest.raises(ValueError):
        ps.suggest_position_size("2330", ml_prob=0.5, total_capital=1000,
                                 max_single_pct=0)
