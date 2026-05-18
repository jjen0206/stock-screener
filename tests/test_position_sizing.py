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


# === P2-8:EV-based 半 Kelly 倉位(compute_suggested_position + render_position_str) ===

def test_ev_position_negative_ev_zero():
    """EV < 0 → 不該進場,倉位 0%。"""
    assert ps.compute_suggested_position(-0.01) == 0.0
    assert ps.compute_suggested_position(-0.05) == 0.0


def test_ev_position_above_3pct_caps_at_5pct():
    """EV > 3% → 滿倉 5%(spec 上限,訊號最強)。"""
    assert ps.compute_suggested_position(0.03) == pytest.approx(0.05)
    assert ps.compute_suggested_position(0.05) == pytest.approx(0.05)
    assert ps.compute_suggested_position(0.10) == pytest.approx(0.05)


def test_ev_position_mid_band_linear():
    """EV +1% ~ +3% → 線性 2% ~ 5%。中點 EV=2% → 倉位 3.5%。"""
    assert ps.compute_suggested_position(0.01) == pytest.approx(0.02)
    assert ps.compute_suggested_position(0.02) == pytest.approx(0.035)
    assert ps.compute_suggested_position(0.025) == pytest.approx(0.0425)


def test_ev_position_low_band_linear():
    """EV 0 ~ +1% → 線性 1% ~ 2%(小試水溫)。"""
    assert ps.compute_suggested_position(0.0) == pytest.approx(0.01)
    assert ps.compute_suggested_position(0.005) == pytest.approx(0.015)
    # 接 mid band 下緣:EV=1% 兩段都該得 2%(連續)
    assert ps.compute_suggested_position(0.01) == pytest.approx(0.02)


def test_ev_position_boundary_continuous():
    """分段邊界值連續:EV=1% 兩段都 = 2%;EV=3% 兩段都 = 5%。"""
    # 1% 邊界
    eps = 1e-9
    a = ps.compute_suggested_position(0.01 - eps)
    b = ps.compute_suggested_position(0.01 + eps)
    assert abs(a - b) < 1e-6
    # 3% 邊界
    a = ps.compute_suggested_position(0.03 - eps)
    b = ps.compute_suggested_position(0.03 + eps)
    assert abs(a - b) < 1e-6


def test_ev_position_none_nan_safe():
    """None / NaN / 怪型別 → 0.0,不該炸。"""
    assert ps.compute_suggested_position(None) == 0.0
    assert ps.compute_suggested_position(float("nan")) == 0.0
    assert ps.compute_suggested_position("not-a-number") == 0.0  # type: ignore[arg-type]


def test_render_position_str_basic():
    """渲染常見值 — 1 位小數 + 「建議倉位 X.X%」 prefix。"""
    assert ps.render_position_str(0.035) == "建議倉位 3.5%"
    assert ps.render_position_str(0.05) == "建議倉位 5.0%"
    assert ps.render_position_str(0.012) == "建議倉位 1.2%"


def test_render_position_str_zero_and_none():
    """0 → 「建議倉位 0%」(不進場);None → 「建議倉位 —」。"""
    assert ps.render_position_str(0.0) == "建議倉位 0%"
    assert ps.render_position_str(None) == "建議倉位 —"
    assert ps.render_position_str(float("nan")) == "建議倉位 —"
    assert ps.render_position_str("bad") == "建議倉位 —"  # type: ignore[arg-type]


def test_ev_position_end_to_end_via_score_to_ev(tmp_path, monkeypatch):
    """整合:ml_prob → score_to_ev → compute_suggested_position 走純線性 fallback。

    用空白 snapshot_dir 強制走 linear fallback(避免 repo 內 score_to_ev.csv 干擾)。
    線性公式:EV = score*0.05 - (1-score)*0.03。
    """
    from src.score_to_ev import score_to_ev, invalidate_cache
    invalidate_cache()
    # 沒 CSV → 直接走 linear fallback:
    #   score=0.9 → EV = 0.9*0.05 - 0.1*0.03 = 0.045 - 0.003 = 0.042 (> 3% → 滿倉)
    ev_high = score_to_ev(0.9, snapshot_dir=str(tmp_path))
    assert ev_high is not None
    assert ev_high == pytest.approx(0.042, abs=1e-6)
    assert ps.compute_suggested_position(ev_high) == pytest.approx(0.05)
    # score=0.5 → EV = 0.5*0.05 - 0.5*0.03 = 0.01 (mid band 下緣) → 2%
    ev_mid = score_to_ev(0.5, snapshot_dir=str(tmp_path))
    assert ev_mid == pytest.approx(0.01, abs=1e-6)
    assert ps.compute_suggested_position(ev_mid) == pytest.approx(0.02)
    # score=0.3 → EV = 0.3*0.05 - 0.7*0.03 = 0.015 - 0.021 = -0.006 (< 0 → 0%)
    ev_neg = score_to_ev(0.3, snapshot_dir=str(tmp_path))
    assert ev_neg is not None and ev_neg < 0
    assert ps.compute_suggested_position(ev_neg) == 0.0
    invalidate_cache()
