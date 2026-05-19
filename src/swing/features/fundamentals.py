"""基本面 toggle filter — 林恩如方法可選的「品質檢驗」(預設 OFF)。

對應 `docs/swing_implementation_plan.md` § 0「基本面 toggle 預設關」+ § 3 A7:
- ROE TTM(近 4 季 ROE 平均 ≥ 12%)
- 月營收 YoY(近 3 月 ≥ 2 月正成長)
- EPS streak(連 N 季正 EPS,proxy「獲利穩定」)
- 殖利率穩定度(近 N 年都有發股息 + std/mean 小)

⚠️ **spec 假設 vs schema 落差 surface**(守 `feedback_warn_dont_hide`):
spec § 3 A7 提到「毛利率 ≥ 25%」,但 `financials` table 沒有 `gross_margin` 欄位
(schema:revenue/revenue_yoy/eps/eps_yoy/roe/announce_date)。本 Phase A
不加 migration,改:
1. **不做毛利率 filter**(以 ROE + EPS streak 替代「獲利品質」訊號)
2. 在 Phase B 開 strategy 時,若主公決定 reinstall 毛利率 filter,
   應於 Phase 0c-B 後續 mini-PR 加 schema migration + FinMind income
   statement fetcher,**本檔保留 placeholder** 而非靜默忽略

helper 函式設計:
- 接 DataFrame 進來(I/O 在 caller 端做完)
- DataFrame 欄位對齊 `financials` / `dividend` schema
- 資料不足回 None / NaN,不拋例外
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# 預設門檻 — 對齊 spec § 3 A7 提示
DEFAULT_ROE_TTM_THRESHOLD = 0.12  # 近 4 季均 ≥ 12%
DEFAULT_EPS_STREAK_QUARTERS = 4
DEFAULT_REVENUE_YOY_LOOKBACK_MONTHS = 3
DEFAULT_REVENUE_YOY_MIN_POSITIVE = 2
DEFAULT_DIVIDEND_LOOKBACK_YEARS = 5
DEFAULT_DIVIDEND_STABILITY_CV_MAX = 0.30  # std / mean <= 0.30 算穩定


def _need_columns(df: pd.DataFrame, cols: tuple[str, ...], helper_name: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"{helper_name} 需欄位 {missing}(對齊 production schema)")


def roe_ttm(quarterly_df: pd.DataFrame, n_quarters: int = 4) -> float:
    """近 N 季 ROE 平均(Trailing Twelve Months proxy)。

    Args:
        quarterly_df: 對齊 `financials` schema 的 DataFrame,
                      `period_type` 應為 "quarterly",有 `period` + `roe` 欄位。
        n_quarters: 取近 N 季,預設 4(TTM)。

    回傳:
        float ROE 平均(0.12 = 12%);資料不足回 NaN。
    """
    if n_quarters < 1:
        raise ValueError("n_quarters 必須 >= 1")
    _need_columns(quarterly_df, ("period", "roe"), "roe_ttm")
    if len(quarterly_df) == 0:
        return float("nan")
    df = quarterly_df.sort_values("period").tail(n_quarters)
    if len(df) < n_quarters:
        return float("nan")
    vals = df["roe"].dropna()
    if len(vals) < n_quarters:
        return float("nan")
    return float(vals.mean())


def roe_ttm_meets(
    quarterly_df: pd.DataFrame,
    threshold: float = DEFAULT_ROE_TTM_THRESHOLD,
    n_quarters: int = 4,
) -> Optional[bool]:
    """近 4 季 ROE 平均是否 ≥ threshold。資料不足回 None。"""
    val = roe_ttm(quarterly_df, n_quarters=n_quarters)
    if np.isnan(val):
        return None
    return val >= threshold


def eps_streak_positive(
    quarterly_df: pd.DataFrame,
    n_quarters: int = DEFAULT_EPS_STREAK_QUARTERS,
) -> Optional[bool]:
    """近 N 季 EPS 是否「連續為正」。資料不足回 None。"""
    if n_quarters < 1:
        raise ValueError("n_quarters 必須 >= 1")
    _need_columns(quarterly_df, ("period", "eps"), "eps_streak_positive")
    if len(quarterly_df) == 0:
        return None
    df = quarterly_df.sort_values("period").tail(n_quarters)
    if len(df) < n_quarters:
        return None
    eps = df["eps"].dropna()
    if len(eps) < n_quarters:
        return None
    return bool((eps > 0).all())


def monthly_revenue_yoy_positive(
    monthly_df: pd.DataFrame,
    lookback_months: int = DEFAULT_REVENUE_YOY_LOOKBACK_MONTHS,
    min_positive: int = DEFAULT_REVENUE_YOY_MIN_POSITIVE,
) -> Optional[bool]:
    """近 N 月 YoY 正成長月數是否 ≥ min_positive。

    Args:
        monthly_df: 對齊 `financials` schema,`period_type='monthly_revenue'`,
                    有 `period` + `revenue_yoy` 欄位。
    """
    if lookback_months < 1 or min_positive < 1:
        raise ValueError("lookback_months / min_positive 必須 >= 1")
    if min_positive > lookback_months:
        raise ValueError("min_positive 不能 > lookback_months")
    _need_columns(monthly_df, ("period", "revenue_yoy"), "monthly_revenue_yoy_positive")
    if len(monthly_df) == 0:
        return None
    df = monthly_df.sort_values("period").tail(lookback_months)
    if len(df) < lookback_months:
        return None
    yoy = df["revenue_yoy"].dropna()
    if len(yoy) < lookback_months:
        return None
    positive_count = int((yoy > 0).sum())
    return positive_count >= min_positive


def dividend_stability(
    dividend_df: pd.DataFrame,
    lookback_years: int = DEFAULT_DIVIDEND_LOOKBACK_YEARS,
    cv_max: float = DEFAULT_DIVIDEND_STABILITY_CV_MAX,
) -> dict:
    """殖利率穩定度分析(回 dict,不是 single bool 因要 surface 細節)。

    Args:
        dividend_df: 對齊 `dividend` schema,有 `year` + `cash_dividend` 欄位。
        cv_max: coefficient of variation 上限(std/mean),預設 0.30。

    回傳:
        {"is_stable": bool, "years_with_dividend": int, "mean_cash": float,
         "std_cash": float, "cv": float, "reason": str}
    """
    if lookback_years < 2:
        raise ValueError("lookback_years 必須 >= 2")
    _need_columns(dividend_df, ("year", "cash_dividend"), "dividend_stability")
    if len(dividend_df) == 0:
        return {"is_stable": False, "reason": "no_data"}
    df = dividend_df.sort_values("year").tail(lookback_years)
    if len(df) < lookback_years:
        return {
            "is_stable": False,
            "reason": "insufficient_years",
            "years_available": len(df),
        }
    cash = df["cash_dividend"].fillna(0).astype(float).to_numpy()
    years_with_div = int((cash > 0).sum())
    if years_with_div < lookback_years:
        return {
            "is_stable": False,
            "reason": "skipped_years",
            "years_with_dividend": years_with_div,
            "lookback_years": lookback_years,
        }
    mean = float(cash.mean())
    std = float(cash.std(ddof=0))
    cv = std / mean if mean > 0 else float("inf")
    return {
        "is_stable": cv <= cv_max,
        "years_with_dividend": years_with_div,
        "mean_cash": mean,
        "std_cash": std,
        "cv": cv,
        "reason": "ok" if cv <= cv_max else "cv_too_high",
    }


def fundamental_filter_passes(
    quarterly_df: pd.DataFrame,
    monthly_revenue_df: pd.DataFrame,
    dividend_df: pd.DataFrame,
    params: Optional[dict] = None,
) -> dict:
    """彙總基本面 AND filter(toggle 開啟時 strategy.py 在進場條件加這個)。

    所有子條件必須 True 才回 passes=True。任一回 None(不知道)→ passes=None。
    任一回 False → passes=False + reason 列哪條 fail。

    params:
        可覆寫 threshold 的 dict,key 對齊本檔 DEFAULT_* 常數。
    """
    p = dict(
        roe_threshold=DEFAULT_ROE_TTM_THRESHOLD,
        eps_streak_quarters=DEFAULT_EPS_STREAK_QUARTERS,
        revenue_yoy_lookback=DEFAULT_REVENUE_YOY_LOOKBACK_MONTHS,
        revenue_yoy_min_positive=DEFAULT_REVENUE_YOY_MIN_POSITIVE,
        dividend_lookback_years=DEFAULT_DIVIDEND_LOOKBACK_YEARS,
        dividend_cv_max=DEFAULT_DIVIDEND_STABILITY_CV_MAX,
    )
    if params:
        p.update(params)

    roe_ok = roe_ttm_meets(
        quarterly_df,
        threshold=p["roe_threshold"],
        n_quarters=p["eps_streak_quarters"],
    )
    eps_ok = eps_streak_positive(
        quarterly_df, n_quarters=p["eps_streak_quarters"]
    )
    rev_ok = monthly_revenue_yoy_positive(
        monthly_revenue_df,
        lookback_months=p["revenue_yoy_lookback"],
        min_positive=p["revenue_yoy_min_positive"],
    )
    div = dividend_stability(
        dividend_df,
        lookback_years=p["dividend_lookback_years"],
        cv_max=p["dividend_cv_max"],
    )

    checks = {
        "roe_ttm_meets": roe_ok,
        "eps_streak_positive": eps_ok,
        "monthly_revenue_yoy_positive": rev_ok,
        "dividend_stable": div["is_stable"]
        if div.get("reason") != "insufficient_years" and div.get("reason") != "no_data"
        else None,
    }
    failed = [k for k, v in checks.items() if v is False]
    unknown = [k for k, v in checks.items() if v is None]
    if failed:
        return {"passes": False, "failed": failed, "checks": checks, "dividend_detail": div}
    if unknown:
        return {"passes": None, "unknown": unknown, "checks": checks, "dividend_detail": div}
    return {"passes": True, "checks": checks, "dividend_detail": div}
