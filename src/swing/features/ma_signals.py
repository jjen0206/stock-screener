"""20 週 / 20 日均線訊號 — 林恩如波段選股核心。

5 個訊號函式(對應 spec § 0「真實林恩如」進場條件之 1 + 2):

- `weekly_ma(daily_df, period_weeks)`:週線 SMA Series(index = 週五)
- `daily_ma(daily_df, period_days)`:日線 SMA Series(index = 日)
- `latest_close_above_weekly_ma(daily_df, period_weeks)`:最新收盤是否站上週均
- `weekly_ma_slope(daily_df, period_weeks, lookback_weeks)`:週均斜率三分類
- `dma20_above_wma20(daily_df)`:20DMA 是否站上 20wMA(20DMA 上穿 20wMA 等價於 True 且前一日 False)
- `dma20_cross_wma20(daily_df, lookback_days)`:近 N 日內是否發生 20DMA 上/下穿 20wMA
- `bias_ratio_to_weekly_ma(daily_df, period_weeks)`:乖離率 (close / wma - 1)

回傳慣例(對齊 `src/indicators.py`):
- Series:資料不足對應 index 回 NaN
- bool 函式:資料不足回 None(明確區分「不知道」與 False)
- 分類字串:資料不足回 "insufficient"
- float 函式:資料不足回 NaN
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from src.swing.features.weekly_resample import resample_to_weekly


def weekly_ma(daily_df: pd.DataFrame, period_weeks: int = 20) -> pd.Series:
    """週線 SMA。

    把 daily_df 先 resample 到週線,再對 weekly close 跑 rolling(period_weeks).mean()。
    Index = 週五 timestamp。資料不足 period_weeks 週 → 對應 index 回 NaN。

    範例:
        >>> wma = weekly_ma(daily_df, 20)  # 林恩如 20 週均
        >>> wma.iloc[-1]
    """
    if period_weeks <= 0:
        raise ValueError("period_weeks 必須 > 0")
    weekly = resample_to_weekly(daily_df)
    if len(weekly) == 0:
        return pd.Series([], dtype=float, name="weekly_ma")
    series = (
        weekly["close"]
        .astype(float)
        .rolling(window=period_weeks, min_periods=period_weeks)
        .mean()
    )
    series.name = "weekly_ma"
    return series


def daily_ma(daily_df: pd.DataFrame, period_days: int = 20) -> pd.Series:
    """日線 SMA(對齊既有 `src/indicators.sma` 行為,但 index 用 date)。

    若 daily_df 有 `date` 欄,輸出 series 用 date 當 index 方便對齊 weekly_ma。
    """
    if period_days <= 0:
        raise ValueError("period_days 必須 > 0")
    if "close" not in daily_df.columns:
        raise KeyError("daily_df 缺 'close' 欄位")
    if len(daily_df) == 0:
        return pd.Series([], dtype=float, name="daily_ma")
    df = daily_df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
    series = (
        df["close"]
        .astype(float)
        .rolling(window=period_days, min_periods=period_days)
        .mean()
    )
    series.name = "daily_ma"
    return series


def latest_close_above_weekly_ma(
    daily_df: pd.DataFrame, period_weeks: int = 20
) -> Optional[bool]:
    """最新收盤是否站上 N 週均。

    回 None 表示資料不足(週數 < period_weeks 或 daily_df 為空)。
    比較對象:最近一個有 weekly_ma 值的週 vs 該週的 close(也是該週最後一日收盤)。
    """
    wma = weekly_ma(daily_df, period_weeks=period_weeks)
    if wma.empty or wma.dropna().empty:
        return None
    last_valid_idx = wma.last_valid_index()
    if last_valid_idx is None:
        return None
    # weekly resample 後該週 close = 該週最後交易日收盤,跟 weekly_ma 同一 index
    weekly = resample_to_weekly(daily_df)
    weekly_close = float(weekly.loc[last_valid_idx, "close"])
    weekly_ma_val = float(wma.loc[last_valid_idx])
    if np.isnan(weekly_close) or np.isnan(weekly_ma_val):
        return None
    return weekly_close > weekly_ma_val


def weekly_ma_slope(
    daily_df: pd.DataFrame,
    period_weeks: int = 20,
    lookback_weeks: int = 4,
    flat_threshold: float = 0.001,
) -> str:
    """週均斜率三分類。

    取最近 `lookback_weeks` 個非 NaN 的 weekly_ma 值,跑 linear regression
    求斜率,再用 / mean 做 normalize:
    - normalized_slope >  flat_threshold → "up"
    - normalized_slope < -flat_threshold → "down"
    - 其餘 → "flat"

    資料不足 → "insufficient"。

    flat_threshold 預設 0.001 = 每週均值漲幅 0.1% 才算 up,經驗值。
    """
    if lookback_weeks < 2:
        raise ValueError("lookback_weeks 必須 >= 2")
    wma = weekly_ma(daily_df, period_weeks=period_weeks).dropna()
    if len(wma) < lookback_weeks:
        return "insufficient"
    tail = wma.iloc[-lookback_weeks:].to_numpy(dtype=float)
    x = np.arange(len(tail), dtype=float)
    slope, _intercept = np.polyfit(x, tail, deg=1)
    mean_val = float(tail.mean())
    if mean_val == 0 or np.isnan(mean_val):
        return "flat"
    normalized = slope / mean_val
    if normalized > flat_threshold:
        return "up"
    if normalized < -flat_threshold:
        return "down"
    return "flat"


def dma20_above_wma20(daily_df: pd.DataFrame) -> Optional[bool]:
    """最新 20DMA 是否站上 20wMA(進場條件之一:20DMA 上穿 20wMA 之 ongoing 狀態)。

    要求 daily_df 至少 100 個交易日(20 週 × 5)+ 20 個交易日(20DMA),取 ~120+ 較穩。
    資料不足回 None。
    """
    dma = daily_ma(daily_df, period_days=20)
    wma = weekly_ma(daily_df, period_weeks=20)
    if dma.dropna().empty or wma.dropna().empty:
        return None
    last_dma_idx = dma.last_valid_index()
    last_wma_idx = wma.last_valid_index()
    if last_dma_idx is None or last_wma_idx is None:
        return None
    # 把週均 forward-fill 到日線 index,取最新日的對齊值
    dma_aligned = dma.dropna()
    wma_daily = wma.reindex(dma_aligned.index, method="ffill")
    paired = pd.concat([dma_aligned, wma_daily], axis=1).dropna()
    if paired.empty:
        return None
    last_dma, last_wma = paired.iloc[-1, 0], paired.iloc[-1, 1]
    return bool(last_dma > last_wma)


def dma20_cross_wma20(
    daily_df: pd.DataFrame, lookback_days: int = 20
) -> str:
    """近 N 個交易日內 20DMA 是否上/下穿 20wMA。

    回傳:
    - "golden_cross":lookback 內任一日 20DMA 由下穿到上 20wMA
    - "death_cross":lookback 內任一日 20DMA 由上穿到下 20wMA
    - "none":無穿越
    - "insufficient":資料不足

    若同 lookback 期內 golden + death 都發生,以「最近發生那次」為主。
    """
    if lookback_days < 2:
        raise ValueError("lookback_days 必須 >= 2")
    dma = daily_ma(daily_df, period_days=20).dropna()
    wma = weekly_ma(daily_df, period_weeks=20)
    if len(dma) < lookback_days + 1 or wma.dropna().empty:
        return "insufficient"
    wma_daily = wma.reindex(dma.index, method="ffill")
    paired = pd.concat([dma, wma_daily], axis=1).dropna()
    paired.columns = ["dma", "wma"]
    if len(paired) < lookback_days + 1:
        return "insufficient"

    tail = paired.iloc[-(lookback_days + 1) :]
    above = tail["dma"] > tail["wma"]
    diff = above.astype(int).diff().fillna(0)
    golden_dates = diff[diff == 1].index
    death_dates = diff[diff == -1].index
    last_golden = golden_dates.max() if len(golden_dates) else None
    last_death = death_dates.max() if len(death_dates) else None
    if last_golden is None and last_death is None:
        return "none"
    if last_golden is None:
        return "death_cross"
    if last_death is None:
        return "golden_cross"
    return "golden_cross" if last_golden > last_death else "death_cross"


def bias_ratio_to_weekly_ma(
    daily_df: pd.DataFrame, period_weeks: int = 20
) -> float:
    """乖離率:(latest_weekly_close - weekly_ma) / weekly_ma。

    林恩如的「乖離過大不追高」風險訊號;搭配 entry 條件用。
    資料不足回 NaN。
    """
    wma = weekly_ma(daily_df, period_weeks=period_weeks)
    if wma.dropna().empty:
        return float("nan")
    last_idx = wma.last_valid_index()
    if last_idx is None:
        return float("nan")
    weekly = resample_to_weekly(daily_df)
    close = float(weekly.loc[last_idx, "close"])
    wma_val = float(wma.loc[last_idx])
    if wma_val == 0 or np.isnan(wma_val) or np.isnan(close):
        return float("nan")
    return (close - wma_val) / wma_val
