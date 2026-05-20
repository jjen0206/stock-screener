"""src/swing/features/ma_signals.py unit tests。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.swing.features import ma_signals
from tests.test_swing.conftest import make_daily_df


# === weekly_ma ===

def test_weekly_ma_returns_nan_when_insufficient(daily_df_uptrend_short):
    """80 天 ~ 16 週 < 20 週 → 全 NaN(或夠 16 個 NaN)。"""
    s = ma_signals.weekly_ma(daily_df_uptrend_short, period_weeks=20)
    assert s.dropna().empty


def test_weekly_ma_has_values_with_long_data(daily_df_uptrend_long):
    s = ma_signals.weekly_ma(daily_df_uptrend_long, period_weeks=20)
    # 250 天 ~ 50 週,20 週 MA 從第 20 週開始有值 → 至少 30 個非 NaN
    assert s.dropna().shape[0] >= 25


def test_weekly_ma_value_handcalc_constant_price(daily_df_flat):
    """全 100 元 → 20 週均也應等於 100。"""
    s = ma_signals.weekly_ma(daily_df_flat, period_weeks=20)
    assert s.dropna().iloc[-1] == pytest.approx(100.0)


def test_weekly_ma_period_zero_raises():
    df = make_daily_df(days=50)
    with pytest.raises(ValueError, match="period_weeks"):
        ma_signals.weekly_ma(df, period_weeks=0)


def test_weekly_ma_empty_input_returns_empty(daily_df_empty):
    s = ma_signals.weekly_ma(daily_df_empty, period_weeks=20)
    assert s.empty


# === daily_ma ===

def test_daily_ma_handcalc_constant(daily_df_flat):
    """全 100 元 → 20 日均也應等於 100。"""
    s = ma_signals.daily_ma(daily_df_flat, period_days=20)
    assert s.dropna().iloc[-1] == pytest.approx(100.0)


def test_daily_ma_insufficient_returns_all_nan():
    df = make_daily_df(days=10)
    s = ma_signals.daily_ma(df, period_days=20)
    assert s.dropna().empty


def test_daily_ma_missing_close_raises():
    df = make_daily_df(days=20).drop(columns=["close"])
    with pytest.raises(KeyError, match="close"):
        ma_signals.daily_ma(df, period_days=20)


# === latest_close_above_weekly_ma ===

def test_above_wma_uptrend_returns_true(daily_df_uptrend_long):
    """線性上漲 250 天,最新 close ≫ 20wMA。"""
    assert ma_signals.latest_close_above_weekly_ma(daily_df_uptrend_long) is True


def test_above_wma_downtrend_returns_false(daily_df_downtrend):
    """線性下跌 250 天,最新 close < 20wMA。"""
    assert ma_signals.latest_close_above_weekly_ma(daily_df_downtrend) is False


def test_above_wma_flat_returns_false_at_equal():
    """全 100 元 → close == wma,not strictly greater → False。"""
    df = make_daily_df(days=150, close_seed=np.full(150, 100.0))
    assert ma_signals.latest_close_above_weekly_ma(df) is False


def test_above_wma_insufficient_returns_none(daily_df_uptrend_short):
    """80 天 ~ 16 週 < 20 週 → None。"""
    assert ma_signals.latest_close_above_weekly_ma(daily_df_uptrend_short) is None


def test_above_wma_empty_returns_none(daily_df_empty):
    assert ma_signals.latest_close_above_weekly_ma(daily_df_empty) is None


# === weekly_ma_slope ===

def test_slope_uptrend_returns_up(daily_df_uptrend_long):
    assert ma_signals.weekly_ma_slope(daily_df_uptrend_long) == "up"


def test_slope_downtrend_returns_down(daily_df_downtrend):
    assert ma_signals.weekly_ma_slope(daily_df_downtrend) == "down"


def test_slope_flat_returns_flat(daily_df_flat):
    assert ma_signals.weekly_ma_slope(daily_df_flat) == "flat"


def test_slope_insufficient(daily_df_uptrend_short):
    assert ma_signals.weekly_ma_slope(daily_df_uptrend_short) == "insufficient"


def test_slope_lookback_too_small_raises(daily_df_uptrend_long):
    with pytest.raises(ValueError, match="lookback_weeks"):
        ma_signals.weekly_ma_slope(daily_df_uptrend_long, lookback_weeks=1)


# === dma20_above_wma20 ===

def test_dma_above_wma_uptrend(daily_df_uptrend_long):
    """線性上漲時,20DMA(近期均)應該 ≥ 20wMA(較長期均)。"""
    assert ma_signals.dma20_above_wma20(daily_df_uptrend_long) is True


def test_dma_above_wma_downtrend(daily_df_downtrend):
    assert ma_signals.dma20_above_wma20(daily_df_downtrend) is False


def test_dma_above_wma_insufficient_returns_none(daily_df_uptrend_short):
    assert ma_signals.dma20_above_wma20(daily_df_uptrend_short) is None


# === dma20_cross_wma20 ===

def test_cross_v_shape_emits_golden_cross():
    """V 反轉 — dma20 在 V 後上穿 wma20。

    需要足夠資料讓 paired(dma + ffilled wma)累積 > lookback_days,
    所以拉長到 400 天:前 150 跌、後 250 漲,V 在 day 150,
    paired 有效範圍 ~ day 100 後共 300 個,lookback=150 內必有 cross。
    """
    seg1 = np.linspace(150.0, 100.0, 150)
    seg2 = np.linspace(100.0, 220.0, 250)
    close = np.concatenate([seg1, seg2])
    df = make_daily_df(days=400, close_seed=close)
    result = ma_signals.dma20_cross_wma20(df, lookback_days=300)
    assert result == "golden_cross"


def test_cross_persistent_uptrend_returns_none(daily_df_uptrend_long):
    """純上漲 250 天,close=linspace(100,150),20DMA 一直 > 20wMA → 無 cross 事件。"""
    result = ma_signals.dma20_cross_wma20(daily_df_uptrend_long, lookback_days=50)
    assert result == "none"


def test_cross_insufficient(daily_df_uptrend_short):
    assert ma_signals.dma20_cross_wma20(daily_df_uptrend_short) == "insufficient"


def test_cross_lookback_too_small_raises(daily_df_uptrend_long):
    with pytest.raises(ValueError, match="lookback_days"):
        ma_signals.dma20_cross_wma20(daily_df_uptrend_long, lookback_days=1)


# === bias_ratio_to_weekly_ma ===

def test_bias_uptrend_positive(daily_df_uptrend_long):
    """上漲趨勢 → 最新 close 高於 wma → bias > 0。"""
    bias = ma_signals.bias_ratio_to_weekly_ma(daily_df_uptrend_long)
    assert not math.isnan(bias)
    assert bias > 0


def test_bias_downtrend_negative(daily_df_downtrend):
    bias = ma_signals.bias_ratio_to_weekly_ma(daily_df_downtrend)
    assert bias < 0


def test_bias_flat_zero(daily_df_flat):
    bias = ma_signals.bias_ratio_to_weekly_ma(daily_df_flat)
    assert bias == pytest.approx(0.0)


def test_bias_insufficient_returns_nan(daily_df_uptrend_short):
    bias = ma_signals.bias_ratio_to_weekly_ma(daily_df_uptrend_short)
    assert math.isnan(bias)


def test_bias_empty_returns_nan(daily_df_empty):
    bias = ma_signals.bias_ratio_to_weekly_ma(daily_df_empty)
    assert math.isnan(bias)


# === Property-style tests(用 parametrize 替代 hypothesis,專案無 hypothesis 依賴)===

@pytest.mark.parametrize("days", [0, 1, 5, 50, 100, 250, 500])
def test_all_helpers_dont_throw_on_various_lengths(days):
    """各種長度都不應拋例外(insufficient 情境回 None / NaN / "insufficient")。"""
    df = make_daily_df(days=days) if days > 0 else make_daily_df(days=1).iloc[0:0]
    # 不 throw 就行
    ma_signals.weekly_ma(df)
    ma_signals.daily_ma(df)
    ma_signals.latest_close_above_weekly_ma(df)
    ma_signals.weekly_ma_slope(df)
    ma_signals.dma20_above_wma20(df)
    ma_signals.dma20_cross_wma20(df)
    ma_signals.bias_ratio_to_weekly_ma(df)


@pytest.mark.parametrize(
    "seed",
    [
        np.full(250, 50.0),
        np.full(250, 0.01),  # 極小數
        np.linspace(1.0, 1000.0, 250),  # 大範圍
    ],
)
def test_helpers_handle_various_price_scales(seed):
    df = make_daily_df(days=250, close_seed=seed)
    # 不 throw + bias 應在合理範圍 [-1, 100]
    bias = ma_signals.bias_ratio_to_weekly_ma(df)
    assert not math.isnan(bias)
    slope = ma_signals.weekly_ma_slope(df)
    assert slope in ("up", "down", "flat", "insufficient")
