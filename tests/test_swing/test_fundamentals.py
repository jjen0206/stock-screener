"""src/swing/features/fundamentals.py unit tests。

Fixtures 對齊 production schema:
- financials.quarterly: stock_id, period_type='quarterly', period (e.g. '2024Q3'),
  revenue, revenue_yoy, eps, eps_yoy, roe, announce_date
- financials.monthly_revenue: period (e.g. '2024-11'), revenue, revenue_yoy
- dividend: stock_id, year, cash_dividend, stock_dividend, ex_dividend_date
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.swing.features import fundamentals as fd


# === Fixtures(對齊 production schema)===

@pytest.fixture
def quarterly_df_strong():
    """近 4 季 ROE 都 > 12%,EPS 都 > 0。"""
    return pd.DataFrame(
        {
            "stock_id": "2330",
            "period_type": "quarterly",
            "period": ["2025Q1", "2025Q2", "2025Q3", "2025Q4"],
            "revenue": [100, 110, 120, 130],
            "revenue_yoy": [0.1, 0.12, 0.15, 0.18],
            "eps": [1.5, 1.6, 1.8, 2.0],
            "eps_yoy": [0.1, 0.15, 0.2, 0.25],
            "roe": [0.14, 0.15, 0.16, 0.17],
            "announce_date": ["2025-04-30", "2025-07-31", "2025-10-31", "2026-02-15"],
        }
    )


@pytest.fixture
def quarterly_df_weak():
    """ROE < 12%,EPS 有負。"""
    return pd.DataFrame(
        {
            "stock_id": "1234",
            "period_type": "quarterly",
            "period": ["2025Q1", "2025Q2", "2025Q3", "2025Q4"],
            "revenue": [100, 95, 90, 85],
            "revenue_yoy": [-0.05, -0.1, -0.15, -0.2],
            "eps": [0.5, -0.2, 0.1, -0.5],
            "eps_yoy": [-0.5, -1.5, -0.8, -1.2],
            "roe": [0.05, 0.03, 0.04, -0.02],
            "announce_date": ["2025-04-30", "2025-07-31", "2025-10-31", "2026-02-15"],
        }
    )


@pytest.fixture
def monthly_revenue_df_growing():
    """近 3 月 YoY 全正。"""
    return pd.DataFrame(
        {
            "stock_id": "2330",
            "period_type": "monthly_revenue",
            "period": ["2025-10", "2025-11", "2025-12"],
            "revenue": [100, 110, 120],
            "revenue_yoy": [0.10, 0.15, 0.20],
            "eps": [None, None, None],
            "eps_yoy": [None, None, None],
            "roe": [None, None, None],
            "announce_date": ["2025-11-10", "2025-12-10", "2026-01-10"],
        }
    )


@pytest.fixture
def monthly_revenue_df_mixed():
    """近 3 月 YoY:2 正 1 負(剛好夠 min_positive=2)。"""
    return pd.DataFrame(
        {
            "stock_id": "1234",
            "period_type": "monthly_revenue",
            "period": ["2025-10", "2025-11", "2025-12"],
            "revenue": [100, 90, 100],
            "revenue_yoy": [0.05, -0.10, 0.08],
            "eps": [None, None, None],
            "eps_yoy": [None, None, None],
            "roe": [None, None, None],
            "announce_date": ["2025-11-10", "2025-12-10", "2026-01-10"],
        }
    )


@pytest.fixture
def dividend_df_stable():
    """5 年都發 2-3 元股息,CV 低。"""
    return pd.DataFrame(
        {
            "stock_id": "2330",
            "year": [2021, 2022, 2023, 2024, 2025],
            "cash_dividend": [2.5, 2.7, 2.8, 3.0, 3.0],
            "stock_dividend": [0, 0, 0, 0, 0],
            "ex_dividend_date": [
                "2021-08-15",
                "2022-08-15",
                "2023-08-15",
                "2024-08-15",
                "2025-08-15",
            ],
        }
    )


@pytest.fixture
def dividend_df_skipped():
    """5 年中有 2 年沒發 → 不穩定。"""
    return pd.DataFrame(
        {
            "stock_id": "5678",
            "year": [2021, 2022, 2023, 2024, 2025],
            "cash_dividend": [2.0, 0.0, 2.0, 0.0, 2.0],
            "stock_dividend": [0, 0, 0, 0, 0],
            "ex_dividend_date": [None, None, None, None, None],
        }
    )


# === roe_ttm ===

def test_roe_ttm_strong(quarterly_df_strong):
    val = fd.roe_ttm(quarterly_df_strong, n_quarters=4)
    assert val == pytest.approx(0.155, abs=0.001)


def test_roe_ttm_weak(quarterly_df_weak):
    val = fd.roe_ttm(quarterly_df_weak, n_quarters=4)
    assert val < 0.10


def test_roe_ttm_insufficient_data(quarterly_df_strong):
    df = quarterly_df_strong.head(2)
    val = fd.roe_ttm(df, n_quarters=4)
    assert math.isnan(val)


def test_roe_ttm_empty_returns_nan():
    val = fd.roe_ttm(pd.DataFrame({"period": [], "roe": []}), n_quarters=4)
    assert math.isnan(val)


def test_roe_ttm_missing_column_raises(quarterly_df_strong):
    bad = quarterly_df_strong.drop(columns=["roe"])
    with pytest.raises(KeyError, match="roe"):
        fd.roe_ttm(bad)


# === roe_ttm_meets ===

def test_roe_ttm_meets_strong(quarterly_df_strong):
    assert fd.roe_ttm_meets(quarterly_df_strong, threshold=0.12) is True


def test_roe_ttm_meets_weak(quarterly_df_weak):
    assert fd.roe_ttm_meets(quarterly_df_weak, threshold=0.12) is False


def test_roe_ttm_meets_insufficient(quarterly_df_strong):
    df = quarterly_df_strong.head(2)
    assert fd.roe_ttm_meets(df) is None


# === eps_streak_positive ===

def test_eps_streak_strong(quarterly_df_strong):
    assert fd.eps_streak_positive(quarterly_df_strong, n_quarters=4) is True


def test_eps_streak_weak(quarterly_df_weak):
    assert fd.eps_streak_positive(quarterly_df_weak, n_quarters=4) is False


def test_eps_streak_insufficient(quarterly_df_strong):
    df = quarterly_df_strong.head(2)
    assert fd.eps_streak_positive(df, n_quarters=4) is None


# === monthly_revenue_yoy_positive ===

def test_revenue_yoy_growing(monthly_revenue_df_growing):
    assert (
        fd.monthly_revenue_yoy_positive(
            monthly_revenue_df_growing, lookback_months=3, min_positive=2
        )
        is True
    )


def test_revenue_yoy_mixed_meets_min(monthly_revenue_df_mixed):
    """2/3 正 → 達 min_positive=2。"""
    assert (
        fd.monthly_revenue_yoy_positive(
            monthly_revenue_df_mixed, lookback_months=3, min_positive=2
        )
        is True
    )


def test_revenue_yoy_mixed_fails_strict():
    """2/3 正,但 min_positive=3 → False。"""
    df = pd.DataFrame(
        {
            "period_type": "monthly_revenue",
            "period": ["2025-10", "2025-11", "2025-12"],
            "revenue_yoy": [0.05, -0.10, 0.08],
        }
    )
    assert (
        fd.monthly_revenue_yoy_positive(
            df, lookback_months=3, min_positive=3
        )
        is False
    )


def test_revenue_yoy_invalid_args():
    df = pd.DataFrame({"period": [], "revenue_yoy": []})
    with pytest.raises(ValueError):
        fd.monthly_revenue_yoy_positive(df, lookback_months=3, min_positive=5)


# === dividend_stability ===

def test_dividend_stable(dividend_df_stable):
    r = fd.dividend_stability(dividend_df_stable, lookback_years=5, cv_max=0.30)
    assert r["is_stable"] is True
    assert r["years_with_dividend"] == 5
    assert r["cv"] < 0.30


def test_dividend_skipped_years(dividend_df_skipped):
    r = fd.dividend_stability(dividend_df_skipped, lookback_years=5)
    assert r["is_stable"] is False
    assert r["reason"] == "skipped_years"


def test_dividend_insufficient():
    df = pd.DataFrame(
        {"year": [2024, 2025], "cash_dividend": [2.0, 2.0]}
    )
    r = fd.dividend_stability(df, lookback_years=5)
    assert r["is_stable"] is False
    assert r["reason"] == "insufficient_years"


def test_dividend_empty():
    df = pd.DataFrame({"year": [], "cash_dividend": []})
    r = fd.dividend_stability(df, lookback_years=5)
    assert r["is_stable"] is False
    assert r["reason"] == "no_data"


def test_dividend_high_cv_not_stable():
    """5 年都發但金額差很大 → cv > 0.30。"""
    df = pd.DataFrame(
        {
            "year": [2021, 2022, 2023, 2024, 2025],
            "cash_dividend": [1.0, 5.0, 1.5, 4.5, 1.0],
        }
    )
    r = fd.dividend_stability(df, lookback_years=5, cv_max=0.30)
    assert r["is_stable"] is False
    assert r["reason"] == "cv_too_high"


# === fundamental_filter_passes(整合 AND filter)===

def test_filter_all_pass(
    quarterly_df_strong, monthly_revenue_df_growing, dividend_df_stable
):
    r = fd.fundamental_filter_passes(
        quarterly_df_strong, monthly_revenue_df_growing, dividend_df_stable
    )
    assert r["passes"] is True
    assert all(v is True for v in r["checks"].values())


def test_filter_one_fail(
    quarterly_df_weak, monthly_revenue_df_growing, dividend_df_stable
):
    """ROE/EPS 弱 → fail。"""
    r = fd.fundamental_filter_passes(
        quarterly_df_weak, monthly_revenue_df_growing, dividend_df_stable
    )
    assert r["passes"] is False
    assert "roe_ttm_meets" in r["failed"] or "eps_streak_positive" in r["failed"]


def test_filter_unknown_when_data_missing(
    monthly_revenue_df_growing, dividend_df_stable
):
    """Quarterly 不夠 → unknown。"""
    df = pd.DataFrame(
        {"period_type": "quarterly", "period": ["2025Q4"], "eps": [1.0], "roe": [0.15]}
    )
    r = fd.fundamental_filter_passes(
        df, monthly_revenue_df_growing, dividend_df_stable
    )
    assert r["passes"] is None
    assert "roe_ttm_meets" in r["unknown"]
