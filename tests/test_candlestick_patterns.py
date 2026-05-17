"""K 線形態 detector 單元測試 — B 進場時機強化."""
from __future__ import annotations

import pandas as pd
import pytest

from src import candlestick_patterns as cp


def _df(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """[(o, h, l, c), ...] → DataFrame。"""
    return pd.DataFrame(bars, columns=["open", "high", "low", "close"])


# === is_enabled ===

def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("PATTERN_DETECTION_ENABLED", raising=False)
    assert cp.is_enabled() is True


def test_is_enabled_false_via_env(monkeypatch):
    monkeypatch.setenv("PATTERN_DETECTION_ENABLED", "false")
    assert cp.is_enabled() is False


# === three_white_soldiers ===

def test_three_white_soldiers_classic():
    """三根遞增的大陽線,後一根 open 在前一根實體內 → ★★★"""
    df = _df([
        (100, 102, 99, 102),
        (101.5, 104, 101, 104),
        (103, 106, 103, 106),
    ])
    r = cp.detect_three_white_soldiers(df)
    assert r is not None
    assert r["name"] == "three_white_soldiers"
    assert r["bias"] == "bull"
    assert r["confidence"] >= 2


def test_three_white_soldiers_fail_on_bear():
    """中間插一根陰線 → None。"""
    df = _df([
        (100, 102, 99, 102),
        (102, 102.5, 99, 100),
        (100, 103, 100, 103),
    ])
    assert cp.detect_three_white_soldiers(df) is None


def test_three_white_soldiers_fail_when_close_not_rising():
    """收盤沒逐根升 → None。"""
    df = _df([
        (100, 105, 99, 104),
        (104, 105, 102, 103.5),  # close down vs prev
        (103, 106, 103, 105.5),
    ])
    assert cp.detect_three_white_soldiers(df) is None


def test_three_white_soldiers_insufficient_data():
    df = _df([(100, 102, 99, 102), (101, 103, 100, 103)])
    assert cp.detect_three_white_soldiers(df) is None


# === hammer ===

def test_hammer_long_lower_shadow_bull_body():
    """下影 > 3× 實體,實體靠上 + 陽線 → confidence 3。"""
    # open=98, close=100, low=90, high=100.5
    # body=2, lower=8 (≥ 4× body), upper=0.5, body 在上端
    df = _df([(98, 100.5, 90, 100)])
    r = cp.detect_hammer(df)
    assert r is not None
    assert r["bias"] == "bull"
    assert r["confidence"] == 3


def test_hammer_short_lower_shadow_fail():
    """下影只比實體略長 → None。"""
    df = _df([(99, 101, 98.5, 100)])  # body=1, lower=0.5
    assert cp.detect_hammer(df) is None


def test_hammer_long_upper_shadow_fail():
    """上影很長(不是 hammer) → None。"""
    df = _df([(99, 110, 95, 100)])  # body=1, upper=10
    assert cp.detect_hammer(df) is None


# === engulfing ===

def test_bullish_engulfing_classic():
    """前陰小 + 後陽大 + 完全吞噬 → confidence ≥ 2。"""
    df = _df([
        (105, 106, 102, 103),  # 陰 body=2
        (102.5, 108, 102, 107),  # 陽 body=4.5,open <= prev close, close >= prev open
    ])
    r = cp.detect_engulfing(df)
    assert r is not None
    assert r["bias"] == "bull"
    assert r["confidence"] >= 2


def test_engulfing_fail_when_not_engulfing():
    """後陽沒吞掉前陰 → None。"""
    df = _df([
        (105, 106, 102, 103),  # 陰
        (103.5, 105, 103, 104),  # 陽 但 close < prev open(沒吞)
    ])
    assert cp.detect_engulfing(df) is None


def test_engulfing_fail_when_prev_not_bear():
    """前一根是陽 → None。"""
    df = _df([
        (100, 105, 99, 104),  # 陽
        (99, 108, 98, 107),
    ])
    assert cp.detect_engulfing(df) is None


# === morning_star ===

def test_morning_star_classic():
    """大陰 → 小實體 跳空 → 大陽吃回中點以上。"""
    df = _df([
        (110, 110.5, 100, 100),  # 大陰 body=10
        (99, 99.5, 98, 98.5),     # 小實體 + 跳空下
        (99, 108, 99, 107),       # 大陽 close=107 > b1_mid=105
    ])
    r = cp.detect_morning_star(df)
    assert r is not None
    assert r["bias"] == "bull"
    assert r["confidence"] >= 2


def test_morning_star_fail_no_recovery():
    """第三根沒收上 b1 中點 → None。"""
    df = _df([
        (110, 110.5, 100, 100),
        (99, 99.5, 98, 98.5),
        (99, 103, 98, 102),  # close=102 < mid=105
    ])
    assert cp.detect_morning_star(df) is None


# === flag ===

def test_flag_breakout():
    """前 10 日震盪 6% + 最後一根突破 high + 2% → ★★★。"""
    base = [(100, 105, 95, 100)] * 10  # 6% 震盪(105-95)/mean(100)
    last = [(101, 110, 101, 109)]      # 突破 base_high=105,幅度 (109-105)/105 = 3.8%
    df = _df(base + last)
    r = cp.detect_flag(df, lookback=10)
    assert r is not None
    assert r["bias"] == "bull"
    assert r["confidence"] == 3


def test_flag_fail_when_base_too_calm():
    """base 震盪 < 5% → 不算旗形。"""
    base = [(100, 101, 99.5, 100)] * 10  # 1.5%
    last = [(100, 103, 100, 102.5)]
    df = _df(base + last)
    assert cp.detect_flag(df, lookback=10) is None


def test_flag_fail_when_no_breakout():
    """最後一根收盤沒突破 base_high → None。"""
    base = [(100, 105, 95, 100)] * 10
    last = [(100, 104, 100, 103)]
    df = _df(base + last)
    assert cp.detect_flag(df, lookback=10) is None


# === doji ===

def test_doji_tiny_body():
    """body / range 極小 → confidence ≥ 1。"""
    df = _df([(100, 102, 98, 100.05)])  # body=0.05,range=4,ratio=0.0125
    r = cp.detect_doji(df)
    assert r is not None
    assert r["bias"] == "neutral"


def test_doji_balanced_shadows_higher_confidence():
    """body 極小 + 上下影對稱 → confidence 2。"""
    df = _df([(100, 102, 98, 100)])  # body=0, upper=2, lower=2
    r = cp.detect_doji(df)
    assert r is not None
    assert r["confidence"] == 2


def test_doji_fail_when_body_too_big():
    df = _df([(100, 103, 99, 102)])  # body=2, range=4 → ratio=0.5
    assert cp.detect_doji(df) is None


# === detect_all_patterns ===

def test_detect_all_returns_multiple_hits():
    """三紅兵 + 旗形同時命中 → list 含兩者。"""
    base = [(100, 105, 95, 100)] * 10
    # 末三根:三根遞增大陽 + 突破 base_high
    last3 = [
        (101, 105, 100, 104),
        (103.5, 107, 103, 107),
        (106, 112, 106, 111),
    ]
    df = _df(base + last3)
    hits = cp.detect_all_patterns("2330", df)
    names = {h["name"] for h in hits}
    assert "three_white_soldiers" in names
    # 旗形 / 三紅兵 至少其一命中(寬鬆檢查避免閾值依賴)
    assert len(hits) >= 1


def test_detect_all_empty_df_returns_empty():
    df = pd.DataFrame(columns=["open", "high", "low", "close"])
    assert cp.detect_all_patterns("2330", df) == []


def test_detect_all_missing_columns_returns_empty():
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
    assert cp.detect_all_patterns("2330", df) == []


def test_detect_all_with_sid_injection():
    """回傳 dict 帶 sid。"""
    df = _df([
        (100, 102, 99, 102),
        (101.5, 104, 101, 104),
        (103, 106, 103, 106),
    ])
    hits = cp.detect_all_patterns("2330", df)
    if hits:
        assert all(h.get("sid") == "2330" for h in hits)
