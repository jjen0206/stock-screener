"""量價訊號 — 林恩如進場條件之 5「週爆量」+ ecstatic-pike 2.3 量價分類。

5 個 helper(對應 `docs/swing_implementation_plan.md` § 3 A3):
- `weekly_volume_surge(daily_df, surge_multiplier, ma_weeks)`:最近一週量是否 > N 週平均 × m 倍
- `volume_ma_ratio(daily_df, short_days, long_days)`:近期 vs 長期均量比
- `obv(daily_df)`:On-Balance Volume 累計
- `obv_slope(daily_df, lookback_days)`:OBV 斜率三分類
- `volume_price_category(daily_df, lookback_days)`:量價分類四類

量價分類規則:
- price_chg = (close[-1] - close[-lookback]) / close[-lookback]
- vol_ratio = mean(volume[-lookback:]) / mean(volume[-2*lookback : -lookback])
- price up + vol up → "volume_up_price_up"(量增價漲)
- price down + vol down → "volume_down_pullback"(量縮回踩)
- price/volume 反向 → "volume_price_divergence"(量價背離)
- 其他 → "neutral"
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from src.swing.features.weekly_resample import resample_to_weekly


_PRICE_TREND_THRESHOLD = 0.02  # 5d 漲跌幅 ±2% 才算 up/down
_VOLUME_RATIO_THRESHOLD = 1.10  # 短期均量比長期均量高 10% 才算量增


def weekly_volume_surge(
    daily_df: pd.DataFrame,
    surge_multiplier: float = 2.0,
    ma_weeks: int = 5,
) -> Optional[bool]:
    """最新一週 volume 是否爆量(> 前 N 週均量 × multiplier)。

    林恩如「週爆量」=「至少 2 倍於最近 5 週均量」,本函式 default 對齊。

    回傳:
    - True / False:有足夠週資料時判定
    - None:週數 < ma_weeks + 1
    """
    if surge_multiplier <= 0:
        raise ValueError("surge_multiplier 必須 > 0")
    if ma_weeks < 1:
        raise ValueError("ma_weeks 必須 >= 1")
    weekly = resample_to_weekly(daily_df)
    if len(weekly) < ma_weeks + 1:
        return None
    latest_vol = float(weekly["volume"].iloc[-1])
    prev_avg = float(weekly["volume"].iloc[-(ma_weeks + 1) : -1].mean())
    if prev_avg <= 0 or np.isnan(prev_avg):
        return None
    return latest_vol >= prev_avg * surge_multiplier


def volume_ma_ratio(
    daily_df: pd.DataFrame,
    short_days: int = 5,
    long_days: int = 20,
) -> float:
    """近期均量 / 長期均量。

    回 NaN 表示資料不足(< long_days)或長期均量為 0。
    > 1 表示放量,< 1 表示縮量。
    """
    if short_days < 1 or long_days < 1:
        raise ValueError("days 必須 >= 1")
    if short_days >= long_days:
        raise ValueError("short_days 必須 < long_days")
    if "volume" not in daily_df.columns:
        raise KeyError("daily_df 缺 'volume' 欄位")
    if len(daily_df) < long_days:
        return float("nan")
    df = daily_df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
    vol = df["volume"].astype(float).to_numpy()
    short_avg = float(vol[-short_days:].mean())
    long_avg = float(vol[-long_days:].mean())
    if long_avg <= 0 or np.isnan(long_avg):
        return float("nan")
    return short_avg / long_avg


def obv(daily_df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume(累計成交量,日線級別)。

    OBV_t = OBV_{t-1} + sign(close_t - close_{t-1}) * volume_t
    第一個值定為 0(無前一日參考)。

    回傳 Series with date index(若 daily_df 有 date)。
    """
    if "close" not in daily_df.columns or "volume" not in daily_df.columns:
        raise KeyError("daily_df 需 close + volume 欄位")
    if len(daily_df) == 0:
        return pd.Series([], dtype=float, name="obv")
    df = daily_df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
    close = df["close"].astype(float).to_numpy()
    volume = df["volume"].astype(float).to_numpy()
    direction = np.sign(np.diff(close, prepend=close[0]))  # 第 0 個 = 0
    obv_arr = (direction * volume).cumsum()
    return pd.Series(obv_arr, index=df.index, name="obv")


def obv_slope(
    daily_df: pd.DataFrame,
    lookback_days: int = 20,
    flat_threshold: float = 0.001,
) -> str:
    """OBV 斜率三分類(對齊 weekly_ma_slope 風格)。

    取最近 lookback_days 個 OBV 值,linear regression 求斜率,
    用 / |mean(|obv|)| 做 normalize。

    資料不足 → "insufficient"。
    """
    if lookback_days < 2:
        raise ValueError("lookback_days 必須 >= 2")
    s = obv(daily_df)
    if len(s) < lookback_days:
        return "insufficient"
    tail = s.iloc[-lookback_days:].to_numpy(dtype=float)
    x = np.arange(len(tail), dtype=float)
    slope, _ = np.polyfit(x, tail, deg=1)
    norm_base = max(abs(float(tail.mean())), 1.0)  # 避免分母接近 0
    normalized = slope / norm_base
    if normalized > flat_threshold:
        return "up"
    if normalized < -flat_threshold:
        return "down"
    return "flat"


def volume_price_category(
    daily_df: pd.DataFrame,
    lookback_days: int = 5,
    price_threshold: float = _PRICE_TREND_THRESHOLD,
    volume_ratio_threshold: float = _VOLUME_RATIO_THRESHOLD,
) -> str:
    """量價分類四類(ecstatic-pike 2.3 量價分類 categorical)。

    比較近 lookback_days 與前 lookback_days 的價/量變化:
    - volume_up_price_up:量增 + 價漲 → 林恩如「健康放量上攻」訊號
    - volume_down_pullback:量縮 + 價跌 → 「縮量回踩」非主跌
    - volume_price_divergence:量縮但價漲(噴出乏力)或 量增但價跌(出貨)
    - neutral:其餘(無趨勢 / 微幅震盪)
    - insufficient:資料 < 2 × lookback_days
    """
    if lookback_days < 2:
        raise ValueError("lookback_days 必須 >= 2")
    if "close" not in daily_df.columns or "volume" not in daily_df.columns:
        raise KeyError("daily_df 需 close + volume 欄位")
    if len(daily_df) < 2 * lookback_days:
        return "insufficient"

    df = daily_df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
    close = df["close"].astype(float).to_numpy()
    volume = df["volume"].astype(float).to_numpy()

    recent_close = close[-lookback_days:]
    prev_close = close[-2 * lookback_days : -lookback_days]
    recent_vol = volume[-lookback_days:]
    prev_vol = volume[-2 * lookback_days : -lookback_days]

    # 用最近區段尾值 vs 前區段首值算總漲跌幅 — 比點對點穩
    base = float(prev_close[0])
    if base <= 0 or np.isnan(base):
        return "insufficient"
    price_chg = (float(recent_close[-1]) - base) / base

    prev_vol_avg = float(prev_vol.mean())
    if prev_vol_avg <= 0 or np.isnan(prev_vol_avg):
        return "insufficient"
    vol_ratio = float(recent_vol.mean()) / prev_vol_avg

    price_up = price_chg > price_threshold
    price_down = price_chg < -price_threshold
    vol_up = vol_ratio > volume_ratio_threshold
    vol_down = vol_ratio < (1.0 / volume_ratio_threshold)

    if price_up and vol_up:
        return "volume_up_price_up"
    if price_down and vol_down:
        return "volume_down_pullback"
    if (price_up and vol_down) or (price_down and vol_up):
        return "volume_price_divergence"
    return "neutral"
