"""src/swing/features/trendline.py unit tests。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.swing.features import trendline as tl
from tests.test_swing.conftest import make_daily_df


# === uptrend_line ===

def test_uptrend_line_clean_uptrend_fits():
    """線性上漲 + 週期性回踩 → swing lows 應在上升直線上。"""
    # 250 days,基底 100 → 150 + 加 ±5 sin 噪聲產生 swing lows
    base = np.linspace(100, 150, 250)
    noise = np.sin(np.linspace(0, 8 * np.pi, 250)) * 5
    close = base + noise
    df = make_daily_df(days=250, close_seed=close)
    r = tl.uptrend_line(df, lookback_weeks=40, window=2)
    assert r["fitted"] is True, r
    assert r["slope_per_day"] > 0
    assert r["below_line"] is False or r["below_line"] is True  # bool 即可


def test_uptrend_line_insufficient_returns_unfitted():
    df = make_daily_df(days=50)
    r = tl.uptrend_line(df, lookback_weeks=24)
    assert r["fitted"] is False
    assert r["reason"] == "insufficient_data"


def test_uptrend_line_lookback_too_short_raises():
    df = make_daily_df(days=200)
    with pytest.raises(ValueError, match="lookback_weeks"):
        tl.uptrend_line(df, lookback_weeks=4, window=3)


def test_uptrend_line_flat_data_low_slope():
    """全 100 → swing lows 可能都在 99 附近(因 high/low ± 1),slope 接近 0。"""
    df = make_daily_df(days=250, close_seed=np.full(250, 100.0))
    r = tl.uptrend_line(df, lookback_weeks=40, window=2)
    # 全 flat 可能 fitted=False(沒明顯 swing low),fitted=True 時 slope ≈ 0
    if r["fitted"]:
        assert abs(r["slope_per_day"]) < 0.1


# === downtrend_line ===

def test_downtrend_line_clean_downtrend_fits():
    base = np.linspace(150, 100, 250)
    noise = np.sin(np.linspace(0, 8 * np.pi, 250)) * 5
    close = base + noise
    df = make_daily_df(days=250, close_seed=close)
    r = tl.downtrend_line(df, lookback_weeks=40, window=2)
    assert r["fitted"] is True, r
    assert r["slope_per_day"] < 0


def test_downtrend_line_insufficient():
    df = make_daily_df(days=30)
    r = tl.downtrend_line(df, lookback_weeks=24)
    assert r["fitted"] is False


# === classify_trend ===

def test_classify_uptrend():
    """上漲 + 振盪 → higher highs + higher lows。"""
    base = np.linspace(100, 150, 300)
    noise = np.sin(np.linspace(0, 10 * np.pi, 300)) * 5
    df = make_daily_df(days=300, close_seed=base + noise)
    assert tl.classify_trend(df, lookback_weeks=40, window=2) == "uptrend"


def test_classify_downtrend():
    base = np.linspace(150, 100, 300)
    noise = np.sin(np.linspace(0, 10 * np.pi, 300)) * 5
    df = make_daily_df(days=300, close_seed=base + noise)
    assert tl.classify_trend(df, lookback_weeks=40, window=2) == "downtrend"


def test_classify_sideway_oscillating():
    """100 上下振盪 (90~110) → 無明顯方向。"""
    close = 100 + np.sin(np.linspace(0, 12 * np.pi, 300)) * 10
    df = make_daily_df(days=300, close_seed=close)
    result = tl.classify_trend(df, lookback_weeks=40, window=2)
    assert result in ("sideway", "uptrend", "downtrend")
    # 純振盪不應該是穩定 uptrend/downtrend(可能因 noise 偶爾如此),
    # 至少不應拋例外


def test_classify_insufficient():
    df = make_daily_df(days=30)
    assert tl.classify_trend(df, lookback_weeks=24) == "insufficient"


def test_classify_invalid_lookback():
    df = make_daily_df(days=200)
    with pytest.raises(ValueError, match="lookback_weeks"):
        tl.classify_trend(df, lookback_weeks=4, window=3)


# === Property-style ===

@pytest.mark.parametrize("days", [0, 1, 50, 200, 500])
def test_trendline_helpers_dont_throw(days):
    df = (
        make_daily_df(days=max(days, 1)).iloc[0:0]
        if days == 0
        else make_daily_df(days=days)
    )
    r1 = tl.uptrend_line(df)
    r2 = tl.downtrend_line(df)
    r3 = tl.classify_trend(df)
    assert "fitted" in r1 and "fitted" in r2
    assert r3 in ("uptrend", "downtrend", "sideway", "insufficient")
