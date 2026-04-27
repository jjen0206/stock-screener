"""src/indicators.py 單元測試。

策略:
- 用小型固定資料對拍每個函式的數值,公式可手算驗證
- 邊界:資料筆數 < period 應回 NaN,不拋例外
- 不打網路、不動 SQLite
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src import indicators as ind


# === Fixtures ===

@pytest.fixture
def linear_close_df() -> pd.DataFrame:
    """收盤價 1..10,high = close+1,low = close-1。"""
    close = list(range(1, 11))
    return pd.DataFrame(
        {
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
        }
    )


# === SMA ===

def test_sma_handcalc(linear_close_df):
    s = ind.sma(linear_close_df, period=5)
    assert s.iloc[:4].isna().all()
    assert s.iloc[4] == pytest.approx(3.0)   # mean(1..5)
    assert s.iloc[5] == pytest.approx(4.0)
    assert s.iloc[9] == pytest.approx(8.0)   # mean(6..10)


def test_sma_returns_series(linear_close_df):
    s = ind.sma(linear_close_df, period=3)
    assert isinstance(s, pd.Series)
    assert len(s) == len(linear_close_df)


def test_sma_period_too_large_returns_all_nan():
    df = pd.DataFrame({"close": [1, 2, 3]})
    s = ind.sma(df, period=10)
    assert s.isna().all()


def test_sma_invalid_period():
    df = pd.DataFrame({"close": [1, 2, 3]})
    with pytest.raises(ValueError):
        ind.sma(df, period=0)


def test_sma_missing_close_raises():
    with pytest.raises(KeyError):
        ind.sma(pd.DataFrame({"x": [1, 2]}), period=2)


# === EMA ===

def test_ema_handcalc():
    """α=0.5 (period=3), 對 close=[1,2,3,4,5] 手算前 5 期。"""
    df = pd.DataFrame({"close": [1, 2, 3, 4, 5]})
    e = ind.ema(df, period=3)
    # α = 2/(3+1) = 0.5;EMA_0 = 1
    expected = [1.0, 1.5, 2.25, 3.125, 4.0625]
    for i, exp in enumerate(expected):
        assert e.iloc[i] == pytest.approx(exp), f"EMA[{i}] 不符"


def test_ema_returns_series_length(linear_close_df):
    e = ind.ema(linear_close_df, period=5)
    assert isinstance(e, pd.Series)
    assert len(e) == len(linear_close_df)
    assert not pd.isna(e.iloc[0])  # adjust=False 從第 1 筆就有值


# === KD ===

def test_kd_handcalc_known_sequence():
    """構造前 9 日無波動 + 第 10 日跳漲,手算 K/D 對拍。"""
    closes = [10] * 9 + [20]
    df = pd.DataFrame({"high": closes, "low": closes, "close": closes})
    out = ind.kd(df, n=9)

    # 前 8 個 NaN
    assert out.iloc[:8].isna().all().all()

    # 第 9 日 (index=8): 9 日 high=10, low=10, close=10 → denom=0 → RSV=0
    # K = 2/3·50 + 1/3·0 = 100/3
    # D = 2/3·50 + 1/3·K = 100/3 + 100/9 = 400/9
    assert out["K"].iloc[8] == pytest.approx(100.0 / 3.0)
    assert out["D"].iloc[8] == pytest.approx(400.0 / 9.0)

    # 第 10 日 (index=9): 9 日 [low..high] 從 [index 1..9] = [10]*8 + [20]
    # high=20, low=10, close=20 → RSV = (20-10)/(20-10)*100 = 100
    # K = 2/3·(100/3) + 1/3·100 = 200/9 + 100/3 = 200/9 + 300/9 = 500/9
    # D = 2/3·(400/9) + 1/3·(500/9) = 800/27 + 500/27 = 1300/27
    assert out["K"].iloc[9] == pytest.approx(500.0 / 9.0)
    assert out["D"].iloc[9] == pytest.approx(1300.0 / 27.0)


def test_kd_returns_dataframe_columns(linear_close_df):
    out = ind.kd(linear_close_df, n=5)
    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == ["K", "D"]
    assert len(out) == len(linear_close_df)


def test_kd_insufficient_data():
    df = pd.DataFrame({
        "high": [1, 2, 3], "low": [1, 2, 3], "close": [1, 2, 3]
    })
    out = ind.kd(df, n=9)
    assert out.isna().all().all()


def test_kd_missing_columns():
    with pytest.raises(KeyError):
        ind.kd(pd.DataFrame({"close": [1, 2, 3]}), n=9)


# === MACD ===

def test_macd_dif_equals_ema_fast_minus_slow():
    """DIF 必須等於 EMA(fast) − EMA(slow)。"""
    rng = np.random.default_rng(seed=42)
    close = rng.uniform(50, 100, size=60)
    df = pd.DataFrame({"close": close})
    m = ind.macd(df, fast=12, slow=26, signal=9)

    expected_dif = ind.ema(df, 12) - ind.ema(df, 26)
    pd.testing.assert_series_equal(
        m["DIF"], expected_dif, check_names=False
    )


def test_macd_dea_is_ewm_of_dif():
    """DEA 必須等於 DIF 的 9-EMA(adjust=False)。"""
    rng = np.random.default_rng(seed=7)
    close = rng.uniform(50, 100, size=60)
    df = pd.DataFrame({"close": close})
    m = ind.macd(df)

    expected_dea = m["DIF"].ewm(span=9, adjust=False).mean()
    pd.testing.assert_series_equal(m["DEA"], expected_dea, check_names=False)


def test_macd_hist_is_2x_dif_minus_dea():
    rng = np.random.default_rng(seed=13)
    close = rng.uniform(50, 100, size=60)
    df = pd.DataFrame({"close": close})
    m = ind.macd(df)
    expected_hist = (m["DIF"] - m["DEA"]) * 2.0
    pd.testing.assert_series_equal(m["HIST"], expected_hist, check_names=False)


def test_macd_returns_dataframe_columns():
    df = pd.DataFrame({"close": list(range(1, 60))})
    m = ind.macd(df)
    assert list(m.columns) == ["DIF", "DEA", "HIST"]


def test_macd_invalid_params():
    df = pd.DataFrame({"close": [1, 2, 3]})
    with pytest.raises(ValueError):
        ind.macd(df, fast=26, slow=12)  # fast >= slow


# === RSI ===

def test_rsi_first_value_all_gains():
    """連續上漲 → 第 (period+1) 日的 RSI = 100。"""
    close = list(range(1, 8))  # 1,2,3,4,5,6,7 → 全是 +1
    df = pd.DataFrame({"close": close})
    r = ind.rsi(df, period=5)
    # 前 5 個 NaN, 第 6 個 (index=5) 是首期
    assert r.iloc[:5].isna().all()
    # avg_gain=1, avg_loss=0 → RSI=100
    assert r.iloc[5] == pytest.approx(100.0)


def test_rsi_known_pattern():
    """5 漲 + 1 跌,驗證首期 = 100,第二期可手算。"""
    close = [10, 11, 12, 13, 14, 15, 14]
    df = pd.DataFrame({"close": close})
    r = ind.rsi(df, period=5)
    # diff = [_, 1, 1, 1, 1, 1, -1]
    # gain = [0, 1, 1, 1, 1, 1, 0]
    # loss = [0, 0, 0, 0, 0, 0, 1]
    # 首期 (index=5): avg_g = mean(gain[1..5]) = 5/5 = 1, avg_l = 0 → RSI=100
    assert r.iloc[5] == pytest.approx(100.0)
    # 第二期 (index=6): avg_g = (1·4 + 0)/5 = 0.8, avg_l = (0·4 + 1)/5 = 0.2
    # RS = 4 → RSI = 100 - 100/5 = 80
    assert r.iloc[6] == pytest.approx(80.0)


def test_rsi_flat_returns_50():
    """全平盤,RSI 應為 50(慣例)。"""
    df = pd.DataFrame({"close": [10] * 10})
    r = ind.rsi(df, period=5)
    # 首期之後全 50
    assert r.iloc[5:].eq(50.0).all()


def test_rsi_insufficient_data():
    df = pd.DataFrame({"close": [1, 2, 3]})
    r = ind.rsi(df, period=14)
    assert r.isna().all()


def test_rsi_returns_series():
    df = pd.DataFrame({"close": [1, 2, 3, 4, 5, 6, 7, 8]})
    r = ind.rsi(df, period=3)
    assert isinstance(r, pd.Series)


# === Bollinger ===

def test_bollinger_handcalc():
    """close=[1..5], period=5, ddof=0 標準差 = sqrt(2)。"""
    df = pd.DataFrame({"close": [1, 2, 3, 4, 5]})
    bb = ind.bollinger(df, period=5, num_std=2.0)
    assert bb.iloc[:4].isna().all().all()
    assert bb["mid"].iloc[4] == pytest.approx(3.0)
    expected_std = math.sqrt(2.0)
    assert bb["upper"].iloc[4] == pytest.approx(3.0 + 2 * expected_std)
    assert bb["lower"].iloc[4] == pytest.approx(3.0 - 2 * expected_std)


def test_bollinger_mid_equals_sma():
    """mid 軌應該等於 SMA。"""
    rng = np.random.default_rng(seed=99)
    df = pd.DataFrame({"close": rng.uniform(50, 100, size=40)})
    bb = ind.bollinger(df, period=20, num_std=2.0)
    pd.testing.assert_series_equal(
        bb["mid"], ind.sma(df, 20), check_names=False
    )


def test_bollinger_band_relationship():
    """upper - mid 應該 = mid - lower(兩端對稱)。"""
    rng = np.random.default_rng(seed=99)
    df = pd.DataFrame({"close": rng.uniform(50, 100, size=40)})
    bb = ind.bollinger(df, period=20, num_std=2.0)
    diff_upper = (bb["upper"] - bb["mid"]).dropna()
    diff_lower = (bb["mid"] - bb["lower"]).dropna()
    pd.testing.assert_series_equal(diff_upper, diff_lower, check_names=False)


def test_bollinger_insufficient_data():
    df = pd.DataFrame({"close": [1, 2, 3]})
    bb = ind.bollinger(df, period=20)
    assert bb.isna().all().all()


def test_bollinger_returns_dataframe_columns():
    df = pd.DataFrame({"close": list(range(1, 40))})
    bb = ind.bollinger(df, period=20)
    assert list(bb.columns) == ["mid", "upper", "lower"]


# === 共通:不 in-place 修改輸入 ===

def test_indicators_do_not_mutate_input():
    df = pd.DataFrame({
        "high": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
        "low":  [0, 1, 2, 3, 4, 5, 6, 7,  8,  9],
        "close":[1, 2, 3, 4, 5, 6, 7, 8,  9, 10],
    })
    snapshot = df.copy(deep=True)
    ind.sma(df, 3)
    ind.ema(df, 3)
    ind.kd(df, 5)
    ind.macd(df)
    ind.rsi(df, 3)
    ind.bollinger(df, 5)
    pd.testing.assert_frame_equal(df, snapshot)
