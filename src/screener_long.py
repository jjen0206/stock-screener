"""
長線選股策略。

預設策略「高 ROE + 低 PE + 連續配息 + 殖利率」(四條件 AND):
  條件 A: 近 N 年(預設 3)平均季 ROE > roe_threshold (預設 15)
  條件 B: 當日 PE < pe_max (預設 20) OR PE < 該股票所屬產業平均 PE
          (產業平均無資料時只用 pe_max fallback)
  條件 C: 近 M 年(預設 5)連續配息(每年 cash_dividend > 0)
  條件 D: 近 1 年現金殖利率 > dividend_yield_min (預設 4)

PE 即時計算:
  PE = 最新收盤 / TTM EPS(最近 4 季 EPS 加總);TTM EPS ≤ 0 → 跳過該股。

⚠️ 防呆 (重要):
  目前(2026-04)無 token 模式下:
  - financials 表的季 EPS / ROE 多半空(T1.5 卡點)
  - dividend 表已有 schema 但無自動抓取(待加 fetch_dividend)
  → 此函式偵測到 financials.quarterly 為空 OR dividend 為空時,
    會立刻回空 DataFrame + 在 stderr 印明顯 warning,**不拋例外**。

資料來源:
  從 SQLite 讀取,不直接打 API。
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from typing import Any

import pandas as pd

from src import database as db


logger = logging.getLogger(__name__)


DEFAULT_LONG_PARAMS: dict[str, Any] = {
    "roe_threshold": 15.0,
    "pe_max": 20.0,
    "dividend_yield_min": 4.0,
    "consecutive_dividend_years": 5,
    "roe_years": 3,
}

OUTPUT_COLUMNS = [
    "stock_id", "name", "industry", "close",
    "avg_roe", "pe", "industry_avg_pe",
    "consecutive_dividend_years", "dividend_yield",
]

_NO_DATA_MSG = (
    "[SCREEN_LONG] 缺財報/配息資料,回空結果。"
    "請升級 FinMind token 後重試。"
)


def screen_long(params: dict | None = None) -> pd.DataFrame:
    """長線選股。

    參數:
        params: 覆蓋 DEFAULT_LONG_PARAMS 的參數;None 用全部預設

    回傳:
        DataFrame[stock_id, name, industry, close, avg_roe, pe,
                  industry_avg_pe, consecutive_dividend_years, dividend_yield]
        缺財報/配息時回空 DataFrame(欄位仍存在)。
    """
    p = {**DEFAULT_LONG_PARAMS, **(params or {})}
    db.init_db()

    # 防呆檢查:必要資料是否就位
    fin_count, div_count = _data_availability()
    if fin_count == 0 or div_count == 0:
        msg = (
            f"{_NO_DATA_MSG} "
            f"(financials.quarterly={fin_count} 筆, dividend={div_count} 筆)"
        )
        print(msg, file=sys.stderr, flush=True)
        logger.warning(msg)
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    with db.get_conn() as conn:
        stocks = conn.execute(
            "SELECT stock_id, name, industry FROM stocks WHERE market='TW'"
        ).fetchall()
    if not stocks:
        logger.warning("[SCREEN_LONG] stocks 表為空")
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    # 第一輪:算每檔的 PE / ROE / 連續配息 / 殖利率(資料缺則 None)
    candidates: list[dict] = []
    for s in stocks:
        info = _evaluate_long(s["stock_id"], p)
        if info is None:
            continue
        candidates.append(
            {
                "stock_id": s["stock_id"],
                "name": s["name"],
                "industry": s["industry"],
                **info,
            }
        )
    if not candidates:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    cands_df = pd.DataFrame(candidates)

    # 第二輪:算各 industry 平均 PE(只用候選池內 PE 為正的)
    valid_pe = cands_df[cands_df["pe"] > 0]
    industry_pe = valid_pe.groupby("industry")["pe"].mean().to_dict()
    cands_df["industry_avg_pe"] = cands_df["industry"].map(industry_pe)

    # 第三輪:套用四條件
    def _passes(row: pd.Series) -> bool:
        cond_a = row["avg_roe"] > p["roe_threshold"]
        cond_b = (
            row["pe"] < p["pe_max"]
            or (
                pd.notna(row["industry_avg_pe"])
                and row["pe"] < row["industry_avg_pe"]
            )
        )
        cond_c = (
            row["consecutive_dividend_years"]
            >= p["consecutive_dividend_years"]
        )
        cond_d = row["dividend_yield"] > p["dividend_yield_min"]
        return bool(cond_a and cond_b and cond_c and cond_d)

    selected = cands_df[cands_df.apply(_passes, axis=1)].copy()
    return selected[OUTPUT_COLUMNS].reset_index(drop=True)


def _data_availability() -> tuple[int, int]:
    """查 financials.quarterly 與 dividend 是否有資料。dividend 表不存在當作 0 筆。"""
    with db.get_conn() as conn:
        fin_count = conn.execute(
            "SELECT COUNT(*) AS c FROM financials WHERE period_type='quarterly'"
        ).fetchone()["c"]
        try:
            div_count = conn.execute(
                "SELECT COUNT(*) AS c FROM dividend"
            ).fetchone()["c"]
        except sqlite3.OperationalError:
            div_count = 0
    return fin_count, div_count


def _evaluate_long(stock_id: str, p: dict) -> dict | None:
    """算單檔長線指標。資料任一缺失則回 None。"""
    with db.get_conn() as conn:
        roe_rows = conn.execute(
            """SELECT period, roe FROM financials
               WHERE stock_id=? AND period_type='quarterly' AND roe IS NOT NULL
               ORDER BY period DESC LIMIT ?""",
            (stock_id, p["roe_years"] * 4),
        ).fetchall()
        eps_rows = conn.execute(
            """SELECT period, eps FROM financials
               WHERE stock_id=? AND period_type='quarterly' AND eps IS NOT NULL
               ORDER BY period DESC LIMIT 4""",
            (stock_id,),
        ).fetchall()
        div_rows = conn.execute(
            """SELECT year, cash_dividend FROM dividend
               WHERE stock_id=? ORDER BY year DESC""",
            (stock_id,),
        ).fetchall()
        price_row = conn.execute(
            """SELECT close FROM daily_prices
               WHERE stock_id=? ORDER BY date DESC LIMIT 1""",
            (stock_id,),
        ).fetchone()

    if not roe_rows or not eps_rows or not div_rows or not price_row:
        return None

    avg_roe = sum(r["roe"] for r in roe_rows) / len(roe_rows)
    ttm_eps = sum(r["eps"] for r in eps_rows)
    if ttm_eps <= 0:
        return None
    close = float(price_row["close"])
    if close <= 0:
        return None
    pe = close / ttm_eps

    # 連續配息(從最近年往回算未中斷的年數)
    div_by_year = {int(r["year"]): float(r["cash_dividend"] or 0) for r in div_rows}
    consecutive = 0
    for y in sorted(div_by_year.keys(), reverse=True):
        if div_by_year[y] > 0:
            consecutive += 1
        else:
            break

    # 近 1 年現金殖利率(用最新一年 cash_dividend)
    latest_year = max(div_by_year.keys())
    last_year_div = div_by_year[latest_year]
    yield_pct = last_year_div / close * 100.0

    return {
        "close": close,
        "avg_roe": float(avg_roe),
        "pe": float(pe),
        "consecutive_dividend_years": consecutive,
        "dividend_yield": float(yield_pct),
    }


__all__ = ["screen_long", "DEFAULT_LONG_PARAMS", "OUTPUT_COLUMNS"]
