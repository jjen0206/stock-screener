"""K 線形態判讀模組(B 進場時機強化).

設計原則:
- 輸入: pd.DataFrame,需含 open/high/low/close 欄位(時間正序,index 任意)。
- 不依賴外部 ta lib,純粹幾何 + 簡單 ratio 判定。
- 每個 detector 只回最近一根(或形態組合最後一根)是否成立 → Optional[dict]。
- 資料不足(< 形態需要的 bar 數)→ None,不拋例外。

公開 API:
- `is_enabled() -> bool`                  讀 env `PATTERN_DETECTION_ENABLED`
- `detect_three_white_soldiers(df, lookback=3) -> Optional[dict]`
- `detect_hammer(df) -> Optional[dict]`
- `detect_engulfing(df) -> Optional[dict]`
- `detect_morning_star(df) -> Optional[dict]`
- `detect_flag(df, lookback=10) -> Optional[dict]`
- `detect_doji(df) -> Optional[dict]`
- `detect_all_patterns(sid, df) -> list[dict]`  每個 detector 結果聚合

每個 detector 回傳:
    {
        "name": "three_white_soldiers" | "hammer" | ...,
        "label": 中文名,
        "bias": "bull" | "bear" | "neutral",
        "confidence": 1 | 2 | 3 (★/★★/★★★),
    }

設計考量:
- confidence = 形態強弱;極端漂亮 ★★★,普通 ★★,勉強 ★。
- 形態組合(morning star = 3 日)以「最後一根」當訊號日。
- doji 的 bias=neutral(看上下文決定方向)。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# --- 基本參數(意圖性命名,避免 magic number)---
_BODY_NEAR_ZERO_PCT = 0.001   # < 0.1% range → 視為 zero body(避免除以 0)
_DOJI_BODY_PCT = 0.001        # 實體 / range ≤ 0.1% → doji
_HAMMER_TAIL_RATIO = 2.0      # 下影 ≥ 2× 實體
_HAMMER_BODY_TOP_PCT = 0.30   # 實體在 K 棒上 30% 範圍內
_BIG_BODY_PCT = 0.005         # 實體 / open ≥ 0.5% → 視為「明顯」漲跌


# === Module-level helpers ===

def is_enabled() -> bool:
    """讀 env `PATTERN_DETECTION_ENABLED`(預設 true)。"""
    raw = os.getenv("PATTERN_DETECTION_ENABLED", "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def _require_ohlc(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """檢查 df 有 open/high/low/close 且資料 > 0,return cast 後的 df 或 None。"""
    if df is None or len(df) == 0:
        return None
    needed = ("open", "high", "low", "close")
    if any(c not in df.columns for c in needed):
        return None
    sub = df[list(needed)].astype(float)
    if sub.isna().all().any():
        return None
    return sub


def _bar_stats(row: pd.Series) -> dict:
    """單根 K 線的幾何特徵(實體大小、影線、開盤收盤關係)。"""
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])  # noqa: E741
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return {
        "open": o, "high": h, "low": l, "close": c,
        "range": rng, "body": body, "upper": upper, "lower": lower,
        "is_bull": c > o, "is_bear": c < o,
    }


# === 1. 三紅兵 ===

def detect_three_white_soldiers(
    df: pd.DataFrame, lookback: int = 3,
) -> Optional[dict]:
    """連 3 根(可調)實體陽線 + 收盤逐日上升 + 實體不萎縮。

    判斷規則:
    - 最後 lookback 根全為陽線(close > open)。
    - 每根收盤都比前一根高。
    - 每根都不是「極小 doji」(實體 / open ≥ _BIG_BODY_PCT)。
    - 額外加分:後一根的 open 在前一根實體範圍內(經典 white soldiers 收斂進場)。

    confidence:
    - ★★★: 每根 body 都 > 1% 且 open 在前一根 body 內
    - ★★ : 平均 body > 0.5% 但未滿 ★★★
    - ★  : 只達基本條件
    """
    sub = _require_ohlc(df)
    if sub is None or len(sub) < lookback:
        return None
    seg = sub.iloc[-lookback:]
    stats = [_bar_stats(r) for _, r in seg.iterrows()]
    if not all(s["is_bull"] for s in stats):
        return None
    closes = [s["close"] for s in stats]
    if not all(closes[i] > closes[i - 1] for i in range(1, lookback)):
        return None
    opens = [s["open"] for s in stats]
    body_pcts = [s["body"] / max(o, 1e-9) for s, o in zip(stats, opens)]
    if any(bp < _BIG_BODY_PCT for bp in body_pcts):
        return None  # 任一根太小就不算
    # confidence
    avg_body_pct = sum(body_pcts) / len(body_pcts)
    open_inside_prev_body = all(
        min(stats[i - 1]["open"], stats[i - 1]["close"])
        <= stats[i]["open"]
        <= max(stats[i - 1]["open"], stats[i - 1]["close"])
        for i in range(1, lookback)
    )
    if avg_body_pct >= 0.01 and open_inside_prev_body:
        conf = 3
    elif avg_body_pct >= 0.005:
        conf = 2
    else:
        conf = 1
    return {
        "name": "three_white_soldiers",
        "label": "三紅兵",
        "bias": "bull",
        "confidence": int(conf),
    }


# === 2. 槌子線 ===

def detect_hammer(df: pd.DataFrame) -> Optional[dict]:
    """槌子線(Hammer):下影 ≥ 2× 實體,實體在 K 棒上 30% 區間,上影極短。

    判斷:
    - lower / max(body, 1e-9) >= _HAMMER_TAIL_RATIO
    - 實體中點離高點 <= 30% range(實體靠上)
    - 上影 <= body × 0.5(允許一點點)
    - 不要求顏色(陽 / 陰皆可),但陽線 confidence + 1。
    """
    sub = _require_ohlc(df)
    if sub is None or len(sub) < 1:
        return None
    s = _bar_stats(sub.iloc[-1])
    if s["range"] / max(s["close"], 1e-9) < _BODY_NEAR_ZERO_PCT:
        return None  # 真正 zero range 不算
    body = s["body"]
    if body <= 0:
        return None
    if s["lower"] / max(body, 1e-9) < _HAMMER_TAIL_RATIO:
        return None
    body_mid = (s["open"] + s["close"]) / 2.0
    body_pos_from_high = (s["high"] - body_mid) / s["range"]
    if body_pos_from_high > _HAMMER_BODY_TOP_PCT:
        return None
    if s["upper"] > body * 0.5:
        return None
    conf = 2  # 基本
    if s["is_bull"]:
        conf = 3
    if s["lower"] / max(body, 1e-9) >= 3.0:
        conf = min(conf + 1, 3)
    return {
        "name": "hammer",
        "label": "槌子線",
        "bias": "bull",
        "confidence": int(conf),
    }


# === 3. 看漲吞噬 ===

def detect_engulfing(df: pd.DataFrame) -> Optional[dict]:
    """看漲吞噬(Bullish Engulfing):前一根陰線 + 後一根陽線完全吞噬。

    判斷:
    - 前一根 close < open(陰)。
    - 後一根 close > open(陽)。
    - 後一根 open <= 前一根 close(下殺開盤)。
    - 後一根 close >= 前一根 open(收盤吞掉)。
    - 後一根實體 > 前一根實體 × 1.0(允許等於)。
    """
    sub = _require_ohlc(df)
    if sub is None or len(sub) < 2:
        return None
    p = _bar_stats(sub.iloc[-2])
    c = _bar_stats(sub.iloc[-1])
    if not (p["is_bear"] and c["is_bull"]):
        return None
    if not (c["open"] <= p["close"] and c["close"] >= p["open"]):
        return None
    if c["body"] < p["body"]:
        return None
    body_pct = c["body"] / max(c["open"], 1e-9)
    if body_pct >= 0.02 and c["body"] >= p["body"] * 1.5:
        conf = 3
    elif body_pct >= 0.01:
        conf = 2
    else:
        conf = 1
    return {
        "name": "engulfing",
        "label": "看漲吞噬",
        "bias": "bull",
        "confidence": int(conf),
    }


# === 4. 晨星(Morning Star)===

def detect_morning_star(df: pd.DataFrame) -> Optional[dict]:
    """晨星:三日反轉 — 大陰 + 小實體 (跳空) + 大陽(收盤回到第一根中點以上)。

    判斷:
    - bar1: 陰 + 實體 / open ≥ 1%
    - bar2: 小實體(body / range ≤ 0.4),不限陰陽。
    - bar3: 陽 + 實體 / open ≥ 0.5%,close >= bar1 實體中點。
    """
    sub = _require_ohlc(df)
    if sub is None or len(sub) < 3:
        return None
    b1 = _bar_stats(sub.iloc[-3])
    b2 = _bar_stats(sub.iloc[-2])
    b3 = _bar_stats(sub.iloc[-1])
    if not b1["is_bear"]:
        return None
    if b1["body"] / max(b1["open"], 1e-9) < 0.01:
        return None
    if b2["body"] / max(b2["range"], 1e-9) > 0.4:
        return None
    if not b3["is_bull"]:
        return None
    if b3["body"] / max(b3["open"], 1e-9) < 0.005:
        return None
    b1_mid = (b1["open"] + b1["close"]) / 2.0
    if b3["close"] < b1_mid:
        return None
    # confidence
    gap_down = b2["high"] < b1["close"]      # bar2 完全在 bar1 收盤之下 → 跳空
    gap_up = b3["open"] > b2["high"]          # bar3 跳空向上開
    if gap_down and gap_up:
        conf = 3
    elif b3["close"] > b1["open"]:           # 完全吃回 bar1
        conf = 3
    elif b3["close"] >= (b1["open"] + b1["close"]) / 2.0:
        conf = 2
    else:
        conf = 1
    return {
        "name": "morning_star",
        "label": "晨星",
        "bias": "bull",
        "confidence": int(conf),
    }


# === 5. 旗形(Flag)===

def detect_flag(df: pd.DataFrame, lookback: int = 10) -> Optional[dict]:
    """旗形:短期高震盪後突破。

    粗略定義(個人工具 MVP):
    - 前 lookback 日的最高 - 最低 / mean(close) > 5%(有夠大的震盪 base)。
    - 最後一根收盤突破 lookback 日(不含最後一根)的最高價。
    - 最後一根為陽線且實體 / open >= 1%。

    confidence:
    - ★★★: 突破幅度 > 2%
    - ★★ : 突破 0.5% ~ 2%
    - ★  : 剛突破(<0.5%)
    """
    sub = _require_ohlc(df)
    if sub is None or len(sub) < lookback + 1:
        return None
    base = sub.iloc[-(lookback + 1):-1]
    last = sub.iloc[-1]
    mean_close = float(base["close"].mean())
    if mean_close <= 0:
        return None
    rng_pct = (float(base["high"].max()) - float(base["low"].min())) / mean_close
    if rng_pct < 0.05:
        return None  # base 震盪不夠 → 不算旗形
    last_stats = _bar_stats(last)
    if not last_stats["is_bull"]:
        return None
    if last_stats["body"] / max(last_stats["open"], 1e-9) < 0.01:
        return None
    base_high = float(base["high"].max())
    if last_stats["close"] <= base_high:
        return None
    breakout_pct = (last_stats["close"] - base_high) / base_high
    if breakout_pct > 0.02:
        conf = 3
    elif breakout_pct > 0.005:
        conf = 2
    else:
        conf = 1
    return {
        "name": "flag",
        "label": "旗形突破",
        "bias": "bull",
        "confidence": int(conf),
    }


# === 6. 十字星(Doji)===

def detect_doji(df: pd.DataFrame) -> Optional[dict]:
    """十字星:實體極小(body / range ≤ 0.1%)。

    bias=neutral(看前後文)。confidence:
    - ★★: body / range ≤ 0.05% 且上下影差不多
    - ★ : 達基本條件
    """
    sub = _require_ohlc(df)
    if sub is None or len(sub) < 1:
        return None
    s = _bar_stats(sub.iloc[-1])
    if s["range"] <= 0:
        return None
    body_to_range = s["body"] / s["range"]
    if body_to_range > 0.05:  # 寬鬆閾值 5%(body 占 range ≤ 5% 算 doji)
        return None
    # 加碼條件(上下影對稱 & 極小實體)→ ★★
    if body_to_range <= 0.005:
        upper_lower_ratio = (
            min(s["upper"], s["lower"]) / max(s["upper"], s["lower"], 1e-9)
        )
        conf = 2 if upper_lower_ratio >= 0.5 else 1
    else:
        conf = 1
    return {
        "name": "doji",
        "label": "十字星",
        "bias": "neutral",
        "confidence": int(conf),
    }


# === 集合 detector ===

_ALL_DETECTORS = (
    ("three_white_soldiers", detect_three_white_soldiers),
    ("morning_star", detect_morning_star),
    ("engulfing", detect_engulfing),
    ("flag", detect_flag),
    ("hammer", detect_hammer),
    ("doji", detect_doji),
)


def detect_all_patterns(sid: str, df: pd.DataFrame) -> list[dict]:
    """跑全部 detector,回傳命中的形態 list(空 list 表示沒形態)。

    每個 dict 多帶一個 "sid" 欄,給下游 logging / UI 用。
    任何單一 detector 例外 silent skip,不擋整體。
    """
    sub = _require_ohlc(df)
    if sub is None:
        return []
    hits: list[dict] = []
    for name, fn in _ALL_DETECTORS:
        try:
            res = fn(sub)
        except Exception:  # noqa: BLE001
            logger.exception("[PATTERN] %s detect failed sid=%s", name, sid)
            continue
        if res is not None:
            hits.append({**res, "sid": str(sid).strip()})
    return hits


__all__ = [
    "is_enabled",
    "detect_three_white_soldiers",
    "detect_hammer",
    "detect_engulfing",
    "detect_morning_star",
    "detect_flag",
    "detect_doji",
    "detect_all_patterns",
]
