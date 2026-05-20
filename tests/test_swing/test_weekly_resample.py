"""src/swing/features/weekly_resample.py unit tests。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.swing.features.weekly_resample import resample_to_weekly


def test_basic_5_day_week_aggregates_correctly(daily_df_uptrend_long):
    """Mon-Fri 5 個交易日 → 1 個 weekly row,OHLCV agg 正確。"""
    df = daily_df_uptrend_long.head(5).copy()  # 第一週 Mon-Fri
    wk = resample_to_weekly(df)
    assert len(wk) == 1
    row = wk.iloc[0]
    assert row["open"] == pytest.approx(df["open"].iloc[0])
    assert row["close"] == pytest.approx(df["close"].iloc[-1])
    assert row["high"] == pytest.approx(df["high"].max())
    assert row["low"] == pytest.approx(df["low"].min())
    assert row["volume"] == df["volume"].sum()


def test_index_is_friday(daily_df_uptrend_long):
    """週線 index 必須是週五(weekday == 4)。"""
    wk = resample_to_weekly(daily_df_uptrend_long.head(20))
    assert all(d.weekday() == 4 for d in wk.index)


def test_long_data_resamples_to_about_50_weeks(daily_df_uptrend_long):
    """250 個交易日 ~ 50 週(允許 ±2 容差)。"""
    wk = resample_to_weekly(daily_df_uptrend_long)
    assert 48 <= len(wk) <= 52


def test_empty_input_returns_empty_df(daily_df_empty):
    wk = resample_to_weekly(daily_df_empty)
    assert isinstance(wk, pd.DataFrame)
    assert len(wk) == 0
    # 確保 schema 至少含 OHLCV(callers 可放心 .iloc / .loc)
    for col in ("open", "high", "low", "close", "volume"):
        assert col in wk.columns


def test_missing_column_raises_keyerror(daily_df_uptrend_long):
    bad = daily_df_uptrend_long.drop(columns=["close"])
    with pytest.raises(KeyError, match="close"):
        resample_to_weekly(bad)


def test_all_nan_close_drops_to_empty(daily_df_all_nan_close):
    """全 NaN close → resample 完整週應該都被 drop。"""
    wk = resample_to_weekly(daily_df_all_nan_close)
    assert len(wk) == 0


def test_does_not_mutate_input(daily_df_uptrend_long):
    snap = daily_df_uptrend_long.copy()
    resample_to_weekly(daily_df_uptrend_long)
    pd.testing.assert_frame_equal(daily_df_uptrend_long, snap)


def test_unsorted_input_still_works(daily_df_uptrend_long):
    """輸入順序顛倒 → 內部 sort 後 OHLC 應與正序結果一致。"""
    shuffled = daily_df_uptrend_long.sample(frac=1, random_state=42).reset_index(
        drop=True
    )
    wk_sorted = resample_to_weekly(daily_df_uptrend_long)
    wk_shuffled = resample_to_weekly(shuffled)
    pd.testing.assert_frame_equal(wk_sorted, wk_shuffled)


def test_volume_sums_across_week(daily_df_uptrend_long):
    """5 天 × 10000 = 50000。"""
    wk = resample_to_weekly(daily_df_uptrend_long.head(5))
    assert wk["volume"].iloc[0] == 50000


@pytest.mark.parametrize("days", [1, 3, 7, 50, 250])
def test_various_lengths_dont_throw(days):
    """資料長度從 1 天到 250 天都不應 throw。"""
    from tests.test_swing.conftest import make_daily_df

    df = make_daily_df(days=days)
    wk = resample_to_weekly(df)
    assert isinstance(wk, pd.DataFrame)
    # 週數 <= 天數 / 5 + 1 (容差 1 週給跨週情況)
    assert len(wk) <= days // 5 + 2
