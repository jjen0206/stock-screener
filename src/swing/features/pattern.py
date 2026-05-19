"""W 底 / M 頭型態識別(簡化版)— 林恩如進場條件之 4。

簡化原則(`docs/swing_implementation_plan.md` § 3 A4):**不過度工程**。

採週線級別 swing point 偵測:
- swing point = 在前後 `window` 週內為 local max(peak)/ min(trough)
- W 底 = 近 lookback_weeks 內最後兩個 troughs L1, L2,且:
  - |L2 - L1| / L1 <= tolerance(兩底接近)
  - L1, L2 中間有 peak P,P > L1 × (1 + tolerance)(中峰明顯)
  - 最新 close >= P × (1 - 0.05)(逼近 / 突破 neckline)
- M 頭 = 對偶定義(兩頭接近 + 中谷明顯 + close 跌破中谷)

回傳 dict 含 detected + 細節(L1/L2/peak/neckline/dates),caller 可用來做 UI 標註。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from src.swing.features.weekly_resample import resample_to_weekly


def find_swing_points(
    series: pd.Series, window: int = 3
) -> dict[str, list[tuple[pd.Timestamp, float]]]:
    """找 series 的 swing peaks 與 troughs(local extrema)。

    window:前後 window 個值都嚴格 < / > 才算 swing point。
    series:value-indexed by 時間(週五 timestamp)。

    回傳 {"peaks": [(date, value), ...], "troughs": [(date, value), ...]},
    順序對應 series.index(由舊到新)。
    """
    if window < 1:
        raise ValueError("window 必須 >= 1")
    vals = series.to_numpy(dtype=float)
    idx = series.index
    peaks: list[tuple[pd.Timestamp, float]] = []
    troughs: list[tuple[pd.Timestamp, float]] = []
    n = len(vals)
    for i in range(window, n - window):
        center = vals[i]
        if np.isnan(center):
            continue
        left = vals[i - window : i]
        right = vals[i + 1 : i + 1 + window]
        if len(left) < window or len(right) < window:
            continue
        if np.all(left < center) and np.all(right < center):
            peaks.append((idx[i], float(center)))
        elif np.all(left > center) and np.all(right > center):
            troughs.append((idx[i], float(center)))
    return {"peaks": peaks, "troughs": troughs}


def detect_w_bottom(
    daily_df: pd.DataFrame,
    lookback_weeks: int = 24,
    window: int = 3,
    tolerance: float = 0.05,
    neckline_tolerance: float = 0.05,
) -> dict:
    """簡化 W 底偵測。

    回傳 dict:
        {"detected": bool, "trough1": (date, value), "trough2": (date, value),
         "peak": (date, value), "neckline": float, "reason": str}

    detected=False 時 reason 標明為何:
        "insufficient_data" / "lack_troughs" / "troughs_not_aligned" /
        "no_middle_peak" / "close_below_neckline"
    """
    if lookback_weeks < window * 2 + 1:
        raise ValueError("lookback_weeks 太短,需要 > window * 2")
    if tolerance < 0 or neckline_tolerance < 0:
        raise ValueError("tolerance 必須 >= 0")

    weekly = resample_to_weekly(daily_df)
    if len(weekly) < lookback_weeks:
        return {"detected": False, "reason": "insufficient_data"}
    window_df = weekly.iloc[-lookback_weeks:]
    swing = find_swing_points(window_df["close"], window=window)
    troughs = swing["troughs"]
    peaks = swing["peaks"]
    if len(troughs) < 2:
        return {"detected": False, "reason": "lack_troughs", "troughs": troughs}

    t1 = troughs[-2]
    t2 = troughs[-1]
    # 兩底接近度
    if abs(t2[1] - t1[1]) / max(t1[1], 1e-9) > tolerance:
        return {
            "detected": False,
            "reason": "troughs_not_aligned",
            "trough1": t1,
            "trough2": t2,
        }
    # 中間需有 peak,且 peak 顯著高於 troughs
    middle_peaks = [
        p for p in peaks if t1[0] < p[0] < t2[0] and p[1] > t1[1] * (1 + tolerance)
    ]
    if not middle_peaks:
        return {
            "detected": False,
            "reason": "no_middle_peak",
            "trough1": t1,
            "trough2": t2,
        }
    peak = max(middle_peaks, key=lambda p: p[1])
    neckline = peak[1]
    latest_close = float(weekly["close"].iloc[-1])
    if latest_close < neckline * (1 - neckline_tolerance):
        return {
            "detected": False,
            "reason": "close_below_neckline",
            "trough1": t1,
            "trough2": t2,
            "peak": peak,
            "neckline": neckline,
            "latest_close": latest_close,
        }
    return {
        "detected": True,
        "trough1": t1,
        "trough2": t2,
        "peak": peak,
        "neckline": neckline,
        "latest_close": latest_close,
        "reason": "ok",
    }


def detect_m_top(
    daily_df: pd.DataFrame,
    lookback_weeks: int = 24,
    window: int = 3,
    tolerance: float = 0.05,
    neckline_tolerance: float = 0.05,
) -> dict:
    """簡化 M 頭偵測(W 底對偶)— 林恩如停損訊號之一。

    detected=True 表示符合 M 頭且最新 close 已跌破中谷(neckline),
    應觸發停損訊號。reason 同 detect_w_bottom 但鏡像。
    """
    if lookback_weeks < window * 2 + 1:
        raise ValueError("lookback_weeks 太短")
    if tolerance < 0 or neckline_tolerance < 0:
        raise ValueError("tolerance 必須 >= 0")

    weekly = resample_to_weekly(daily_df)
    if len(weekly) < lookback_weeks:
        return {"detected": False, "reason": "insufficient_data"}
    window_df = weekly.iloc[-lookback_weeks:]
    swing = find_swing_points(window_df["close"], window=window)
    peaks = swing["peaks"]
    troughs = swing["troughs"]
    if len(peaks) < 2:
        return {"detected": False, "reason": "lack_peaks", "peaks": peaks}

    p1 = peaks[-2]
    p2 = peaks[-1]
    if abs(p2[1] - p1[1]) / max(p1[1], 1e-9) > tolerance:
        return {
            "detected": False,
            "reason": "peaks_not_aligned",
            "peak1": p1,
            "peak2": p2,
        }
    middle_troughs = [
        t for t in troughs if p1[0] < t[0] < p2[0] and t[1] < p1[1] * (1 - tolerance)
    ]
    if not middle_troughs:
        return {
            "detected": False,
            "reason": "no_middle_trough",
            "peak1": p1,
            "peak2": p2,
        }
    trough = min(middle_troughs, key=lambda t: t[1])
    neckline = trough[1]
    latest_close = float(weekly["close"].iloc[-1])
    if latest_close > neckline * (1 + neckline_tolerance):
        return {
            "detected": False,
            "reason": "close_above_neckline",
            "peak1": p1,
            "peak2": p2,
            "trough": trough,
            "neckline": neckline,
            "latest_close": latest_close,
        }
    return {
        "detected": True,
        "peak1": p1,
        "peak2": p2,
        "trough": trough,
        "neckline": neckline,
        "latest_close": latest_close,
        "reason": "ok",
    }
