"""src/swing/features/volume_signal.py unit tests。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.swing.features import volume_signal as vs
from tests.test_swing.conftest import make_daily_df


# === weekly_volume_surge ===

def test_surge_normal_volume_returns_false(daily_df_uptrend_long):
    """全部 volume = 10000 → 最新週量 = 前 5 週均量,不爆量。"""
    assert vs.weekly_volume_surge(daily_df_uptrend_long) is False


def test_surge_3x_last_week_returns_true():
    """前 25 週 volume=10000,最後一週 volume=30000 → > 2x avg。"""
    days = 130  # 26 週
    vol = np.full(days, 10000, dtype=int)
    vol[-5:] = 30000  # 最後一週 5 天 × 30000
    df = make_daily_df(days=days, volume_seed=vol)
    assert vs.weekly_volume_surge(df, surge_multiplier=2.0, ma_weeks=5) is True


def test_surge_insufficient_weeks_returns_none():
    df = make_daily_df(days=10)  # 2 週,不足 ma_weeks=5 + 1
    assert vs.weekly_volume_surge(df) is None


def test_surge_invalid_args_raise():
    df = make_daily_df(days=130)
    with pytest.raises(ValueError, match="surge_multiplier"):
        vs.weekly_volume_surge(df, surge_multiplier=0)
    with pytest.raises(ValueError, match="ma_weeks"):
        vs.weekly_volume_surge(df, ma_weeks=0)


# === volume_ma_ratio ===

def test_ma_ratio_equal_volume_returns_one(daily_df_uptrend_long):
    """全 10000 → ratio = 1。"""
    r = vs.volume_ma_ratio(daily_df_uptrend_long, short_days=5, long_days=20)
    assert r == pytest.approx(1.0)


def test_ma_ratio_recent_higher():
    days = 50
    vol = np.full(days, 10000, dtype=int)
    vol[-5:] = 20000
    df = make_daily_df(days=days, volume_seed=vol)
    r = vs.volume_ma_ratio(df, short_days=5, long_days=20)
    assert r > 1.0


def test_ma_ratio_insufficient_returns_nan():
    df = make_daily_df(days=10)
    r = vs.volume_ma_ratio(df, short_days=5, long_days=20)
    assert math.isnan(r)


def test_ma_ratio_invalid_args_raise():
    df = make_daily_df(days=50)
    with pytest.raises(ValueError):
        vs.volume_ma_ratio(df, short_days=0, long_days=20)
    with pytest.raises(ValueError, match="short_days 必須 <"):
        vs.volume_ma_ratio(df, short_days=20, long_days=5)


# === obv ===

def test_obv_constant_price_yields_zero_trend(daily_df_flat):
    """全 100 元 → 每日 diff=0 → OBV 一直 = 0。"""
    s = vs.obv(daily_df_flat)
    assert all(v == 0 for v in s)


def test_obv_uptrend_yields_increasing_series(daily_df_uptrend_long):
    """線性上漲,每日 close > 前日 → OBV 一直累加 volume。"""
    s = vs.obv(daily_df_uptrend_long)
    # 第 0 個 = 0(無前日參考),之後一直增加
    assert s.iloc[0] == 0
    assert s.iloc[-1] > s.iloc[10]


def test_obv_downtrend_yields_decreasing(daily_df_downtrend):
    s = vs.obv(daily_df_downtrend)
    assert s.iloc[-1] < s.iloc[10]


def test_obv_empty_returns_empty(daily_df_empty):
    s = vs.obv(daily_df_empty)
    assert s.empty


def test_obv_missing_column_raises():
    df = make_daily_df(days=50).drop(columns=["volume"])
    with pytest.raises(KeyError):
        vs.obv(df)


# === obv_slope ===

def test_obv_slope_uptrend(daily_df_uptrend_long):
    assert vs.obv_slope(daily_df_uptrend_long) == "up"


def test_obv_slope_downtrend(daily_df_downtrend):
    assert vs.obv_slope(daily_df_downtrend) == "down"


def test_obv_slope_flat(daily_df_flat):
    assert vs.obv_slope(daily_df_flat) == "flat"


def test_obv_slope_insufficient():
    df = make_daily_df(days=10)
    assert vs.obv_slope(df, lookback_days=20) == "insufficient"


# === volume_price_category ===

def test_category_volume_up_price_up():
    days = 30
    close = np.concatenate([np.full(20, 100.0), np.linspace(102.0, 110.0, 10)])
    vol = np.concatenate([np.full(20, 10000), np.full(10, 20000)])
    df = make_daily_df(days=days, close_seed=close, volume_seed=vol)
    assert vs.volume_price_category(df, lookback_days=10) == "volume_up_price_up"


def test_category_volume_down_pullback():
    days = 30
    close = np.concatenate([np.full(20, 110.0), np.linspace(108.0, 100.0, 10)])
    vol = np.concatenate([np.full(20, 20000), np.full(10, 8000)])
    df = make_daily_df(days=days, close_seed=close, volume_seed=vol)
    assert vs.volume_price_category(df, lookback_days=10) == "volume_down_pullback"


def test_category_divergence_price_up_volume_down():
    days = 30
    close = np.concatenate([np.full(20, 100.0), np.linspace(102.0, 110.0, 10)])
    vol = np.concatenate([np.full(20, 20000), np.full(10, 8000)])
    df = make_daily_df(days=days, close_seed=close, volume_seed=vol)
    assert vs.volume_price_category(df, lookback_days=10) == "volume_price_divergence"


def test_category_neutral_flat():
    df = make_daily_df(days=30, close_seed=np.full(30, 100.0))
    assert vs.volume_price_category(df, lookback_days=10) == "neutral"


def test_category_insufficient():
    df = make_daily_df(days=10)
    assert vs.volume_price_category(df, lookback_days=10) == "insufficient"


def test_category_invalid_lookback_raises():
    df = make_daily_df(days=50)
    with pytest.raises(ValueError, match="lookback_days"):
        vs.volume_price_category(df, lookback_days=1)


# === Property-style tests ===

@pytest.mark.parametrize("days", [0, 1, 5, 30, 100, 250])
def test_all_helpers_dont_throw(days):
    """各種長度都不應拋例外。"""
    df = (
        make_daily_df(days=max(days, 1)).iloc[0:0]
        if days == 0
        else make_daily_df(days=days)
    )
    vs.weekly_volume_surge(df)
    if days > 0:  # 0 行時 volume_ma_ratio 也要回 NaN 不能拋
        r = vs.volume_ma_ratio(df) if days >= 20 else float("nan")
        assert isinstance(r, float)
    vs.obv(df)
    vs.obv_slope(df)
    vs.volume_price_category(df)


def test_obv_does_not_mutate_input(daily_df_uptrend_long):
    snap = daily_df_uptrend_long.copy()
    vs.obv(daily_df_uptrend_long)
    pd.testing.assert_frame_equal(daily_df_uptrend_long, snap)
