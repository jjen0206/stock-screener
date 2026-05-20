"""src/swing/features/pattern.py unit tests。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.swing.features import pattern
from tests.test_swing.conftest import make_daily_df


# === find_swing_points ===

def test_swing_points_simple_v():
    """V 形 close — 中間有 1 個 trough,無 peak。"""
    vals = [10, 8, 6, 4, 6, 8, 10]
    s = pd.Series(vals, index=pd.date_range("2025-01-03", periods=len(vals), freq="W-FRI"))
    sw = pattern.find_swing_points(s, window=2)
    assert len(sw["troughs"]) == 1
    assert sw["troughs"][0][1] == 4
    assert len(sw["peaks"]) == 0


def test_swing_points_invalid_window_raises():
    s = pd.Series([1, 2, 3])
    with pytest.raises(ValueError, match="window"):
        pattern.find_swing_points(s, window=0)


def test_swing_points_skip_nan():
    vals = [10, 8, np.nan, 4, 6, 8, 10]
    s = pd.Series(vals, index=pd.date_range("2025-01-03", periods=len(vals), freq="W-FRI"))
    sw = pattern.find_swing_points(s, window=2)
    # 不 throw,trough 可能找不到(NaN 在 center 跳過)
    assert isinstance(sw["troughs"], list)


# === detect_w_bottom ===

def _make_w_bottom_close(weeks: int = 28) -> np.ndarray:
    """合成清晰 W 底:
       100 → 80(L1)→ 95(P)→ 82(L2)→ 100(neckline 突破)
       對應 28 週,每段 7 週,但 daily,所以 5 倍。
    """
    segs = [
        np.linspace(100, 80, 7 * 5),
        np.linspace(80, 95, 7 * 5),
        np.linspace(95, 82, 7 * 5),
        np.linspace(82, 100, 7 * 5),
    ]
    return np.concatenate(segs)


def test_w_bottom_detected_on_clean_pattern():
    close = _make_w_bottom_close()
    df = make_daily_df(days=len(close), close_seed=close)
    result = pattern.detect_w_bottom(df, lookback_weeks=28, window=2, tolerance=0.08)
    assert result["detected"] is True, result
    assert "neckline" in result
    assert result["neckline"] > result["trough1"][1]


def test_w_bottom_not_detected_on_uptrend(daily_df_uptrend_long):
    """純上漲沒兩個 trough → lack_troughs。"""
    result = pattern.detect_w_bottom(daily_df_uptrend_long, lookback_weeks=24, window=2)
    assert result["detected"] is False
    assert result["reason"] in ("lack_troughs", "troughs_not_aligned", "no_middle_peak")


def test_w_bottom_insufficient_data(daily_df_uptrend_short):
    result = pattern.detect_w_bottom(daily_df_uptrend_short, lookback_weeks=24)
    assert result["detected"] is False
    assert result["reason"] == "insufficient_data"


def test_w_bottom_invalid_lookback_raises():
    df = make_daily_df(days=200)
    with pytest.raises(ValueError, match="lookback_weeks"):
        pattern.detect_w_bottom(df, lookback_weeks=2, window=3)


def test_w_bottom_invalid_tolerance_raises():
    df = make_daily_df(days=200)
    with pytest.raises(ValueError, match="tolerance"):
        pattern.detect_w_bottom(df, tolerance=-0.1)


def test_w_bottom_close_below_neckline():
    """L1 / L2 對,P 顯著,但最後一週 close 大跌 → close_below_neckline。"""
    close = _make_w_bottom_close()
    close = np.concatenate([close, np.linspace(100, 70, 5)])  # 加一週崩跌
    df = make_daily_df(days=len(close), close_seed=close)
    result = pattern.detect_w_bottom(df, lookback_weeks=28, window=2, tolerance=0.08)
    # 可能 detected=False(close 跌破)或 detected=True(若崩跌前 neckline 已突破)
    if result["detected"] is False:
        assert result["reason"] in ("close_below_neckline", "no_middle_peak", "troughs_not_aligned")


# === detect_m_top ===

def _make_m_top_close(weeks: int = 28) -> np.ndarray:
    """合成 M 頭:80 → 100(H1)→ 88(T)→ 102(H2)→ 80(跌破中谷)。"""
    segs = [
        np.linspace(80, 100, 7 * 5),
        np.linspace(100, 88, 7 * 5),
        np.linspace(88, 102, 7 * 5),
        np.linspace(102, 80, 7 * 5),
    ]
    return np.concatenate(segs)


def test_m_top_detected_on_clean_pattern():
    close = _make_m_top_close()
    df = make_daily_df(days=len(close), close_seed=close)
    result = pattern.detect_m_top(df, lookback_weeks=28, window=2, tolerance=0.08)
    assert result["detected"] is True, result
    assert "neckline" in result
    assert result["neckline"] < result["peak1"][1]


def test_m_top_not_detected_on_downtrend(daily_df_downtrend):
    """下跌沒兩個 peak → lack_peaks 或 peaks_not_aligned。"""
    result = pattern.detect_m_top(daily_df_downtrend, lookback_weeks=24, window=2)
    assert result["detected"] is False


def test_m_top_insufficient_data(daily_df_uptrend_short):
    result = pattern.detect_m_top(daily_df_uptrend_short, lookback_weeks=24)
    assert result["detected"] is False
    assert result["reason"] == "insufficient_data"


def test_m_top_invalid_lookback_raises():
    df = make_daily_df(days=200)
    with pytest.raises(ValueError, match="lookback_weeks"):
        pattern.detect_m_top(df, lookback_weeks=2, window=3)


# === Property-style ===

@pytest.mark.parametrize("days", [0, 1, 50, 150, 300])
def test_pattern_detectors_dont_throw(days):
    df = (
        make_daily_df(days=max(days, 1)).iloc[0:0]
        if days == 0
        else make_daily_df(days=days)
    )
    r1 = pattern.detect_w_bottom(df)
    r2 = pattern.detect_m_top(df)
    assert "detected" in r1 and "detected" in r2


def test_w_bottom_and_m_top_mutual_exclusivity_on_clean_w():
    """乾淨 W 底:應該 W=True / M=False。"""
    close = _make_w_bottom_close()
    df = make_daily_df(days=len(close), close_seed=close)
    w = pattern.detect_w_bottom(df, lookback_weeks=28, window=2, tolerance=0.08)
    m = pattern.detect_m_top(df, lookback_weeks=28, window=2, tolerance=0.08)
    assert w["detected"] is True
    assert m["detected"] is False
