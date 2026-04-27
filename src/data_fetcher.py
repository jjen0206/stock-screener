"""
資料抓取與快取模組(FinMind v4 API 介接)。

策略:
- 先查 SQLite,有快取就直接回。
- 缺的部分才打 FinMind API,抓回後寫入 DB,並更新 sync_log。
- sync_log 記錄各 (stock_id, dataset) 已同步的日期區間,
  使「整個區間已涵蓋」的請求可完全不打 API(快取命中)。
- 無 token 模式下,部分 dataset(尤其季財報)可能受限或拒絕,
  函式會 raise FinMindAPIError 或 log warn 後回空 DataFrame。

提供:
- list_tw_stocks()                   取得台股清單
- fetch_daily_price(stock_id, s, e)  日線(含快取)
- fetch_institutional(stock_id, s, e) 三大法人(含快取)
- fetch_monthly_revenue(stock_id, s, e) 月營收(含快取)
- fetch_quarterly_financials(stock_id, s, e) 季財報(無 token 可能失效)

注意: 任何呼叫 FinMind 的函式內,日期格式皆為 'YYYY-MM-DD' 字串。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import pandas as pd
import requests

from src import config, database as db


logger = logging.getLogger(__name__)

# FinMind v4 endpoint(無 token 也能呼叫,但會受嚴格頻率限制)
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

# Dataset 名稱常數(對應 sync_log.dataset 欄位,改名請小心,會跟既有快取對不上)
DATASET_PRICE = "TaiwanStockPrice"
DATASET_INFO = "TaiwanStockInfo"
DATASET_INST = "TaiwanStockInstitutionalInvestorsBuySell"
DATASET_REVENUE = "TaiwanStockMonthRevenue"
DATASET_FINANCIAL = "TaiwanStockFinancialStatements"


class FinMindAPIError(RuntimeError):
    """FinMind API 回傳非 200 status 或網路錯誤時拋出。"""


# === 共用工具 ===

def _api_call(dataset: str, **params: Any) -> list[dict]:
    """呼叫 FinMind v4 API。

    自動帶 token(若 .env 有設定);無 token 模式下不帶。
    回傳 data 陣列(list of dict)。
    """
    p: dict[str, Any] = {"dataset": dataset}
    if config.FINMIND_TOKEN:
        p["token"] = config.FINMIND_TOKEN
    p.update(params)
    logger.info("[FETCH] FinMind API call: dataset=%s params=%s", dataset, params)
    print(f"[FETCH] FinMind API call: dataset={dataset} params={params}", flush=True)
    try:
        r = requests.get(FINMIND_URL, params=p, timeout=30)
    except requests.RequestException as ex:
        raise FinMindAPIError(f"網路錯誤: {ex}") from ex
    try:
        payload = r.json()
    except ValueError as ex:
        raise FinMindAPIError(
            f"FinMind 回傳非 JSON (status={r.status_code}): {r.text[:200]}"
        ) from ex
    if payload.get("status") != 200:
        raise FinMindAPIError(
            f"FinMind 回傳錯誤: dataset={dataset} status={payload.get('status')} "
            f"msg={payload.get('msg')}"
        )
    return payload.get("data", [])


def _next_day(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def _prev_day(d: str) -> str:
    return (date.fromisoformat(d) - timedelta(days=1)).isoformat()


def _missing_ranges(
    start: str,
    end: str,
    synced: tuple[str, str] | None,
) -> list[tuple[str, str]]:
    """計算需要打 API 補齊的日期區段。

    - synced=None: 全部都要抓
    - synced=(S, E):
        若 [start, end] 完全在 [S, E] 內 → 回 [](快取命中)
        否則只補頭尾差的部分 ([start, S-1] 與 [E+1, end])
    """
    if synced is None:
        return [(start, end)]
    s_old, e_old = synced
    missing: list[tuple[str, str]] = []
    if start < s_old:
        missing.append((start, _prev_day(s_old)))
    if end > e_old:
        missing.append((_next_day(e_old), end))
    return missing


# === Normalizer ===

def _normalize_price_row(r: dict) -> dict:
    """把 FinMind TaiwanStockPrice 的欄位轉成 daily_prices 表的格式。"""
    return {
        "stock_id": r["stock_id"],
        "date": r["date"],
        "open": r.get("open"),
        "high": r.get("max"),
        "low": r.get("min"),
        "close": r.get("close"),
        "volume": r.get("Trading_Volume"),
        "trading_money": r.get("Trading_money"),
        "trading_turnover": r.get("Trading_turnover"),
        "spread": r.get("spread"),
    }


def _pivot_institutional(raw: list[dict]) -> list[dict]:
    """把 FinMind 三大法人的明細(每筆一個法人)pivot 成單筆/股/日。"""
    grouped: dict[tuple[str, str], dict] = {}
    for r in raw:
        key = (r["stock_id"], r["date"])
        if key not in grouped:
            grouped[key] = {
                "stock_id": r["stock_id"],
                "date": r["date"],
                "foreign_buy_sell": 0,
                "trust_buy_sell": 0,
                "dealer_buy_sell": 0,
            }
        net = (r.get("buy") or 0) - (r.get("sell") or 0)
        name = (r.get("name") or "").lower()
        # FinMind 法人別: Foreign_Investor / Foreign_Dealer_Self
        #                Investment_Trust
        #                Dealer_self / Dealer_Hedging
        if "foreign" in name:
            grouped[key]["foreign_buy_sell"] += net
        elif "trust" in name:
            grouped[key]["trust_buy_sell"] += net
        elif "dealer" in name:
            grouped[key]["dealer_buy_sell"] += net
    for v in grouped.values():
        v["total_buy_sell"] = (
            v["foreign_buy_sell"] + v["trust_buy_sell"] + v["dealer_buy_sell"]
        )
    return list(grouped.values())


def _date_to_quarter(d: str) -> str:
    """'2024-05-15' → '2024-Q2'。"""
    dt = date.fromisoformat(d)
    q = (dt.month - 1) // 3 + 1
    return f"{dt.year}-Q{q}"


# === 對外 API ===

def list_tw_stocks(force_refresh: bool = False) -> pd.DataFrame:
    """取得台股清單。預設先查 DB,空表才打 API;force_refresh=True 強制重抓。"""
    db.init_db()
    if not force_refresh:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT stock_id, name, market, industry, type, updated_at "
                "FROM stocks WHERE market=?",
                ("TW",),
            ).fetchall()
        if rows:
            return pd.DataFrame([dict(r) for r in rows])

    raw = _api_call(DATASET_INFO)
    norm = [
        {
            "stock_id": r["stock_id"],
            "name": r.get("stock_name") or r.get("name") or "",
            "market": "TW",
            "industry": r.get("industry_category"),
            "type": r.get("type"),
        }
        for r in raw
        if r.get("stock_id")
    ]
    db.upsert_stocks(norm)
    return pd.DataFrame(norm)


def fetch_daily_price(stock_id: str, start: str, end: str) -> pd.DataFrame:
    """取得日線價格(含快取)。

    流程:
      1. 確保 DB 與 sync_log 已建立。
      2. 查 sync_log,看 [start, end] 是否已涵蓋。
      3. 算缺的區段,只對缺的呼叫 FinMind。
      4. 寫入 daily_prices,更新 sync_log。
      5. 從 DB 撈 [start, end] 區間的資料回傳。

    參數:
      stock_id: 例 '2330'
      start, end: 'YYYY-MM-DD'
    """
    db.init_db()
    synced = db.get_synced_range(stock_id, DATASET_PRICE)
    missing = _missing_ranges(start, end, synced)

    if not missing:
        logger.info("[CACHE] daily_price 命中: %s [%s ~ %s]", stock_id, start, end)
        print(f"[CACHE] daily_price 命中: {stock_id} [{start} ~ {end}]", flush=True)
    else:
        for s, e in missing:
            raw = _api_call(DATASET_PRICE, data_id=stock_id, start_date=s, end_date=e)
            db.upsert_daily_prices([_normalize_price_row(r) for r in raw])
        db.update_synced_range(stock_id, DATASET_PRICE, start, end)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_prices WHERE stock_id=? AND date BETWEEN ? AND ? "
            "ORDER BY date",
            (stock_id, start, end),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def fetch_institutional(stock_id: str, start: str, end: str) -> pd.DataFrame:
    """取得三大法人買賣超(含快取)。"""
    db.init_db()
    synced = db.get_synced_range(stock_id, DATASET_INST)
    missing = _missing_ranges(start, end, synced)

    if not missing:
        logger.info("[CACHE] institutional 命中: %s [%s ~ %s]", stock_id, start, end)
        print(
            f"[CACHE] institutional 命中: {stock_id} [{start} ~ {end}]", flush=True
        )
    else:
        for s, e in missing:
            raw = _api_call(DATASET_INST, data_id=stock_id, start_date=s, end_date=e)
            db.upsert_institutional(_pivot_institutional(raw))
        db.update_synced_range(stock_id, DATASET_INST, start, end)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM institutional WHERE stock_id=? AND date BETWEEN ? AND ? "
            "ORDER BY date",
            (stock_id, start, end),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def fetch_monthly_revenue(stock_id: str, start: str, end: str) -> pd.DataFrame:
    """取得月營收(含快取)。

    FinMind 的 TaiwanStockMonthRevenue 每月一筆,欄位含 revenue / revenue_year_growth。
    """
    db.init_db()
    synced = db.get_synced_range(stock_id, DATASET_REVENUE)
    missing = _missing_ranges(start, end, synced)

    if not missing:
        logger.info(
            "[CACHE] monthly_revenue 命中: %s [%s ~ %s]", stock_id, start, end
        )
        print(
            f"[CACHE] monthly_revenue 命中: {stock_id} [{start} ~ {end}]", flush=True
        )
    else:
        for s, e in missing:
            raw = _api_call(DATASET_REVENUE, data_id=stock_id, start_date=s, end_date=e)
            norm = [
                {
                    "stock_id": r["stock_id"],
                    "period_type": "monthly_revenue",
                    "period": f"{int(r['revenue_year']):04d}-{int(r['revenue_month']):02d}",
                    "revenue": r.get("revenue"),
                    "revenue_yoy": r.get("revenue_year_growth"),
                    "eps": None,
                    "roe": None,
                }
                for r in raw
                if r.get("revenue_year") and r.get("revenue_month")
            ]
            db.upsert_financials(norm)
        db.update_synced_range(stock_id, DATASET_REVENUE, start, end)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM financials WHERE stock_id=? AND period_type='monthly_revenue' "
            "ORDER BY period",
            (stock_id,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def fetch_quarterly_financials(stock_id: str, start: str, end: str) -> pd.DataFrame:
    """取得季財報(EPS / ROE)。

    ⚠️ 已知限制(2026-04 觀察):
      FinMind 的 TaiwanStockFinancialStatements 屬會員 / 付費 dataset,
      無 token 模式可能直接被拒絕。本函式遇到 FinMindAPIError 會 log warning
      並回空 DataFrame,讓上層降級而不中斷整個流程。

    待主公升級 FinMind token 後重新驗證(對應 TASKS.md T1.5,標 [!] 待測)。
    """
    db.init_db()
    synced = db.get_synced_range(stock_id, DATASET_FINANCIAL)
    missing = _missing_ranges(start, end, synced)

    if not missing:
        logger.info(
            "[CACHE] quarterly_financials 命中: %s [%s ~ %s]", stock_id, start, end
        )
        print(
            f"[CACHE] quarterly_financials 命中: {stock_id} [{start} ~ {end}]",
            flush=True,
        )
    else:
        for s, e in missing:
            try:
                raw = _api_call(
                    DATASET_FINANCIAL, data_id=stock_id, start_date=s, end_date=e
                )
            except FinMindAPIError as ex:
                logger.warning(
                    "季財報抓取失敗(可能需要 FinMind token):%s", ex
                )
                return pd.DataFrame()
            grouped: dict[tuple[str, str], dict] = {}
            for r in raw:
                key = (r["stock_id"], r["date"])
                if key not in grouped:
                    grouped[key] = {
                        "stock_id": r["stock_id"],
                        "period_type": "quarterly",
                        "period": _date_to_quarter(r["date"]),
                        "revenue": None,
                        "revenue_yoy": None,
                        "eps": None,
                        "roe": None,
                    }
                t = (r.get("type") or "").upper()
                v = r.get("value")
                if t == "EPS":
                    grouped[key]["eps"] = v
                elif t == "ROE":
                    grouped[key]["roe"] = v
            db.upsert_financials(list(grouped.values()))
        db.update_synced_range(stock_id, DATASET_FINANCIAL, start, end)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM financials WHERE stock_id=? AND period_type='quarterly' "
            "ORDER BY period",
            (stock_id,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


__all__ = [
    "FinMindAPIError",
    "list_tw_stocks",
    "fetch_daily_price",
    "fetch_institutional",
    "fetch_monthly_revenue",
    "fetch_quarterly_financials",
    "DATASET_PRICE",
    "DATASET_INFO",
    "DATASET_INST",
    "DATASET_REVENUE",
    "DATASET_FINANCIAL",
]
