"""Swing module shared test fixtures。

fixtures 對齊 production schema(`daily_prices` 欄位:stock_id, date, open, high, low,
close, volume, trading_money, trading_turnover, spread)— 守
`feedback_test_fixture_real_schema`,別自編欄位名讓 helper + fixture 同錯 false-green。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest


_DAILY_PRICES_COLUMNS = [
    "stock_id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trading_money",
    "trading_turnover",
    "spread",
]


def _trading_days(start: str, days: int) -> list[str]:
    """產生 days 個工作日(Mon-Fri)— 簡化,不扣國定假日。

    台股實際年交易日約 245-250,本 fixture 為 unit test 用,Mon-Fri 5 天/週夠用。
    """
    out: list[str] = []
    cursor = datetime.strptime(start, "%Y-%m-%d")
    while len(out) < days:
        if cursor.weekday() < 5:  # Mon=0..Fri=4
            out.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return out


def make_daily_df(
    stock_id: str = "2330",
    start: str = "2025-01-06",  # Mon
    days: int = 250,
    close_seed: np.ndarray | None = None,
    volume_seed: np.ndarray | None = None,
) -> pd.DataFrame:
    """產生對齊 daily_prices schema 的 DataFrame。

    Args:
        close_seed: 若 None → np.linspace(100, 150, days)(線性上漲);
                    傳入即用該序列(長度需 = days)。
        volume_seed: 若 None → 全 10000(便於 baseline);傳入即用該序列。
    """
    dates = _trading_days(start, days)
    if close_seed is None:
        close_seed = np.linspace(100.0, 150.0, days)
    elif len(close_seed) != days:
        raise ValueError(f"close_seed 長度 {len(close_seed)} ≠ days {days}")
    if volume_seed is None:
        volume_seed = np.full(days, 10000, dtype=int)
    elif len(volume_seed) != days:
        raise ValueError(f"volume_seed 長度 {len(volume_seed)} ≠ days {days}")

    close = np.asarray(close_seed, dtype=float)
    high = close + 1.0
    low = close - 1.0
    open_ = close.copy()  # 簡化 — open ≈ close,test 不關 open
    return pd.DataFrame(
        {
            "stock_id": stock_id,
            "date": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume_seed,
            "trading_money": close * volume_seed,
            "trading_turnover": (volume_seed / 100).astype(int),
            "spread": np.zeros(days),
        },
        columns=_DAILY_PRICES_COLUMNS,
    )


@pytest.fixture
def daily_df_uptrend_long():
    """線性上漲,250 個交易日(~12 個月)。"""
    return make_daily_df(days=250)


@pytest.fixture
def daily_df_uptrend_short():
    """線性上漲,80 個交易日(不足 20 週,測 insufficient)。"""
    return make_daily_df(days=80)


@pytest.fixture
def daily_df_flat():
    """全相同價格,250 天 — 測 slope=flat / bias=0。"""
    return make_daily_df(close_seed=np.full(250, 100.0))


@pytest.fixture
def daily_df_downtrend():
    """線性下跌 150 → 100,250 天。"""
    return make_daily_df(close_seed=np.linspace(150.0, 100.0, 250))


@pytest.fixture
def daily_df_empty():
    """0 行但欄位齊。"""
    return pd.DataFrame(columns=_DAILY_PRICES_COLUMNS)


@pytest.fixture
def daily_df_all_nan_close():
    """全 NaN close — 測 helper 不拋例外。"""
    df = make_daily_df(days=250)
    df["close"] = np.nan
    return df
