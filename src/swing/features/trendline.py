"""趨勢線 + 趨勢三分類 — 林恩如進場條件 3 + 停損訊號之一(跌破上升趨勢線)。

3 個 helper(對應 `docs/swing_implementation_plan.md` § 3 A5):

- `uptrend_line(daily_df, lookback_weeks, window)`:用近 N 週 weekly low swing points
  跑 linear regression,回 slope/intercept/fit_quality/below_line
- `downtrend_line(daily_df, ...)`:對偶,用 swing highs
- `classify_trend(daily_df, lookback_weeks, window)`:高低點結構三分類
  - "uptrend":高點+低點皆遞增(higher highs + higher lows)
  - "downtrend":高點+低點皆遞減(lower highs + lower lows)
  - "sideway":其他(高低點交錯或不足)
  - "insufficient":資料不足

備註:不用 ADX(避免引入額外指標相依);高低點結構簡單可解釋,
比 ADX 對 swing 短訊號穩定。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from src.swing.features.pattern import find_swing_points
from src.swing.features.weekly_resample import resample_to_weekly


def _fit_line(
    points: list[tuple[pd.Timestamp, float]],
) -> Optional[dict]:
    """對 (timestamp, value) 序列跑 linear regression,回 slope/intercept/R²。

    Returns None if < 2 points。
    """
    if len(points) < 2:
        return None
    # 用「點到第一個點的天數」當 x,值穩定不受絕對時間影響
    base = points[0][0]
    xs = np.array([(p[0] - base).days for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    if xs.std() == 0:  # 所有點同一天(極端)
        return None
    slope, intercept = np.polyfit(xs, ys, deg=1)
    # R²
    y_pred = slope * xs + intercept
    ss_res = float(((ys - y_pred) ** 2).sum())
    ss_tot = float(((ys - ys.mean()) ** 2).sum())
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {
        "slope_per_day": float(slope),
        "intercept": float(intercept),
        "r_squared": r_squared,
        "base_date": base,
        "n_points": len(points),
    }


def uptrend_line(
    daily_df: pd.DataFrame,
    lookback_weeks: int = 24,
    window: int = 3,
) -> dict:
    """上升趨勢線(用 swing lows 擬合)。

    回傳:
        {"fitted": bool, "slope_per_day": float, "intercept": float,
         "r_squared": float, "last_fit_value": float, "below_line": bool,
         "reason": str}

    `below_line` = 最新一週 close 是否低於擬合線預測值 — 停損訊號。
    擬合失敗(swing lows < 2)→ fitted=False。
    """
    if lookback_weeks < window * 2 + 1:
        raise ValueError("lookback_weeks 太短")
    weekly = resample_to_weekly(daily_df)
    if len(weekly) < lookback_weeks:
        return {"fitted": False, "reason": "insufficient_data"}
    win = weekly.iloc[-lookback_weeks:]
    swing = find_swing_points(win["low"], window=window)
    troughs = swing["troughs"]
    fit = _fit_line(troughs)
    if fit is None:
        return {"fitted": False, "reason": "lack_swing_lows", "troughs": troughs}

    base = fit["base_date"]
    last_date = weekly.index[-1]
    days_from_base = (last_date - base).days
    last_fit_value = fit["slope_per_day"] * days_from_base + fit["intercept"]
    latest_close = float(weekly["close"].iloc[-1])
    return {
        "fitted": True,
        "slope_per_day": fit["slope_per_day"],
        "intercept": fit["intercept"],
        "r_squared": fit["r_squared"],
        "n_points": fit["n_points"],
        "last_fit_value": float(last_fit_value),
        "latest_close": latest_close,
        "below_line": bool(latest_close < last_fit_value),
        "reason": "ok",
    }


def downtrend_line(
    daily_df: pd.DataFrame,
    lookback_weeks: int = 24,
    window: int = 3,
) -> dict:
    """下降趨勢線(用 swing highs 擬合,對偶 uptrend_line)。

    回傳含 `above_line` = 最新 close 是否高於擬合線 — 突破訊號。
    """
    if lookback_weeks < window * 2 + 1:
        raise ValueError("lookback_weeks 太短")
    weekly = resample_to_weekly(daily_df)
    if len(weekly) < lookback_weeks:
        return {"fitted": False, "reason": "insufficient_data"}
    win = weekly.iloc[-lookback_weeks:]
    swing = find_swing_points(win["high"], window=window)
    peaks = swing["peaks"]
    fit = _fit_line(peaks)
    if fit is None:
        return {"fitted": False, "reason": "lack_swing_highs", "peaks": peaks}

    base = fit["base_date"]
    last_date = weekly.index[-1]
    days_from_base = (last_date - base).days
    last_fit_value = fit["slope_per_day"] * days_from_base + fit["intercept"]
    latest_close = float(weekly["close"].iloc[-1])
    return {
        "fitted": True,
        "slope_per_day": fit["slope_per_day"],
        "intercept": fit["intercept"],
        "r_squared": fit["r_squared"],
        "n_points": fit["n_points"],
        "last_fit_value": float(last_fit_value),
        "latest_close": latest_close,
        "above_line": bool(latest_close > last_fit_value),
        "reason": "ok",
    }


def classify_trend(
    daily_df: pd.DataFrame,
    lookback_weeks: int = 24,
    window: int = 3,
) -> str:
    """高低點結構三分類:uptrend / downtrend / sideway / insufficient。

    判定邏輯:
    1. 取近 lookback_weeks 的 weekly_close swing points
    2. 看最後 2 個 peaks + 最後 2 個 troughs 的趨勢:
       - higher_highs = peak2 > peak1
       - higher_lows = trough2 > trough1
       - higher_highs AND higher_lows → "uptrend"
       - !higher_highs AND !higher_lows → "downtrend"
       - 其他組合 → "sideway"
    3. swing point 不足 2 對 → "sideway"(視為無明顯趨勢)
       資料不足 lookback_weeks → "insufficient"
    """
    if lookback_weeks < window * 2 + 1:
        raise ValueError("lookback_weeks 太短")
    weekly = resample_to_weekly(daily_df)
    if len(weekly) < lookback_weeks:
        return "insufficient"
    win = weekly.iloc[-lookback_weeks:]
    swing = find_swing_points(win["close"], window=window)
    peaks = swing["peaks"]
    troughs = swing["troughs"]
    if len(peaks) < 2 or len(troughs) < 2:
        return "sideway"

    p1, p2 = peaks[-2][1], peaks[-1][1]
    t1, t2 = troughs[-2][1], troughs[-1][1]
    higher_highs = p2 > p1
    higher_lows = t2 > t1
    if higher_highs and higher_lows:
        return "uptrend"
    if (not higher_highs) and (not higher_lows):
        return "downtrend"
    return "sideway"
