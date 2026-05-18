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
- list_tw_stocks()                       取得台股清單
- fetch_daily_price(stock_id, s, e)      日線(含快取)
- fetch_institutional(stock_id, s, e)    三大法人(含快取)
- fetch_monthly_revenue(stock_id, s, e)  月營收(含快取)
- fetch_quarterly_financials(stock_id, s, e)  季財報(EPS/ROE,需 token)
- fetch_dividend(stock_id, start, end)   年度配息(需 token)
- fetch_long_term_data(stock_ids, on_progress) 批次抓季財報 + 配息(長線選股用)

注意: 任何呼叫 FinMind 的函式內,日期格式皆為 'YYYY-MM-DD' 字串。

資料來源差異 / 已知限制請見 docs/DATA_NOTES.md
(重點:不要把 yfinance 拿來抓台股,實測 yfinance 與 TWSE 官方收盤有差,
 例 2330 2024-03-29:TWSE/FinMind=779.0、yfinance=776)。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Callable

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
DATASET_DIVIDEND = "TaiwanStockDividend"

# 全市場 bulk endpoint(免 token / 一次拿全市場 OHLCV,~30 秒解決 2360 檔)
TWSE_DAILY_BULK_URL = (
    "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
)
TPEX_DAILY_BULK_URL = (
    "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
)


class FinMindAPIError(RuntimeError):
    """FinMind API 回傳非 200 status 或網路錯誤時拋出。"""


class FinMindQuotaError(FinMindAPIError):
    """FinMind quota 爆(HTTP status=402)。

    跟一般 FinMindAPIError 區分是因為:402 在 retry / long-backoff 是浪費 —
    quota window 是小時級才 reset,不該占用 5+ 分鐘 sleep slot 卡住 batch。
    by `_api_call` 偵測 status==402 觸發,由 `with_retry(no_retry_exceptions=...)`
    跳過重試直接 fail-fast。
    """


# === 共用工具 ===

def _api_call(dataset: str, **params: Any) -> list[dict]:
    """呼叫 FinMind v4 API,含 retry。

    自動帶 token(若 .env 有設定);無 token 模式下不帶。
    回傳 data 陣列(list of dict)。response 驗證:status=200 且 data 是 list。

    Retry 策略由環境變數 FINMIND_LONG_BACKOFF 切換:
      - 預設(關):3 次嘗試,指數退避 1s → 2s → 4s
        (本機 / daily_fetch / 互動使用,失敗快回不要 hang)
      - 開(=1/true):6 次嘗試,長退避 60s → 120s → 300s → 600s → 900s
        (backfill workflow 用,FinMind 限額觸發後等夠久才有意義 —
        免費 token 600/hr,被擋通常 5-15 分鐘解。長等比短等成功率高。)
    """
    p: dict[str, Any] = {"dataset": dataset}
    if config.FINMIND_TOKEN:
        p["token"] = config.FINMIND_TOKEN
    p.update(params)

    def _attempt() -> list[dict]:
        logger.info("[FETCH-FINMIND] dataset=%s params=%s", dataset, params)
        print(
            f"[FETCH-FINMIND] dataset={dataset} params={params}",
            flush=True,
        )
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
        if payload.get("status") == 402:
            # quota 爆 — fail-fast,別讓 with_retry 浪費 long-backoff slot
            # (FinMind 免費 token 600/hr,quota window 是小時級才 reset)
            raise FinMindQuotaError(
                f"FinMind quota 爆 (status=402): dataset={dataset} "
                f"msg={payload.get('msg')} — 改日再跑或加 token"
            )
        if payload.get("status") != 200:
            raise FinMindAPIError(
                f"FinMind 回傳錯誤: dataset={dataset} "
                f"status={payload.get('status')} msg={payload.get('msg')}"
            )
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise FinMindAPIError(
                f"FinMind data 欄位非 list: type={type(data).__name__}"
            )
        return data

    from src._retry import with_retry
    import os
    long_backoff = os.environ.get("FINMIND_LONG_BACKOFF", "").lower() in (
        "1", "true", "yes",
    )
    if long_backoff:
        return with_retry(
            _attempt,
            delays=[60, 120, 300, 600, 900],
            label=f"FinMind {dataset} (long-backoff)",
            no_retry_exceptions=(FinMindQuotaError,),
        )
    return with_retry(
        _attempt, max_attempts=3, label=f"FinMind {dataset}",
        no_retry_exceptions=(FinMindQuotaError,),
    )


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


def _prev_year_quarter(period: str) -> str | None:
    """'2024-Q3' → '2023-Q3'。format 不對則回 None。"""
    try:
        year_str, q = period.split("-Q", 1)
        return f"{int(year_str) - 1}-Q{int(q)}"
    except (ValueError, AttributeError):
        return None


def _compute_eps_yoy(curr: float | None, prev: float | None) -> float | None:
    """(curr - prev) / abs(prev) * 100。

    邊界:
    - prev = 0 或 ~0(|prev| < 1e-9):無意義,回 None(分母爆 / 數學上 undefined)
    - 任一邊 None:回 None
    """
    if curr is None or prev is None:
        return None
    if abs(prev) < 1e-9:
        return None
    return (curr - prev) / abs(prev) * 100.0


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
            except FinMindQuotaError:
                # 402 quota 爆 — 不能 swallow,batch caller(backfill_financials)
                # 要看到才能 fail-fast 中斷整批
                raise
            except FinMindAPIError as ex:
                logger.warning(
                    "季財報抓取失敗(可能需要 FinMind token):%s", ex
                )
                return pd.DataFrame()
            # P2-4 PEAD prerequisite(2026-05-19):key 用 (stock_id, announce_date)
            # 保留 r["date"](FinMind 原始公佈日),寫入新 announce_date 欄。
            # 舊版 key collapse 成 quarter bucket 後丟掉了公佈日,PEAD 無法算
            # 「公佈後 N 日進場窗口」。
            grouped: dict[tuple[str, str], dict] = {}
            for r in raw:
                ann = r["date"]
                key = (r["stock_id"], ann)
                if key not in grouped:
                    grouped[key] = {
                        "stock_id": r["stock_id"],
                        "period_type": "quarterly",
                        "period": _date_to_quarter(ann),
                        "announce_date": ann,
                        "revenue": None,
                        "revenue_yoy": None,
                        "eps": None,
                        "eps_yoy": None,
                        "roe": None,
                    }
                t = (r.get("type") or "").upper()
                v = r.get("value")
                if t == "EPS":
                    grouped[key]["eps"] = v
                elif t == "ROE":
                    grouped[key]["roe"] = v

            # 算 eps_yoy:同 stock_id 比前年同季 EPS。
            # 為了處理本 batch 沒涵蓋前一年的 case,也讀 DB 既有 EPS 進 lookup table。
            eps_by_period: dict[str, float] = {}
            with db.get_conn() as conn:
                existing = conn.execute(
                    "SELECT period, eps FROM financials "
                    "WHERE stock_id=? AND period_type='quarterly' "
                    "AND eps IS NOT NULL",
                    (stock_id,),
                ).fetchall()
            for row in existing:
                eps_by_period[row["period"]] = row["eps"]
            # 本 batch 新值覆蓋既有(EPS 可能 restated)
            for v in grouped.values():
                if v["eps"] is not None:
                    eps_by_period[v["period"]] = v["eps"]
            for v in grouped.values():
                prev_period = _prev_year_quarter(v["period"])
                if prev_period is not None:
                    v["eps_yoy"] = _compute_eps_yoy(
                        v["eps"], eps_by_period.get(prev_period),
                    )

            db.upsert_financials(list(grouped.values()))
        db.update_synced_range(stock_id, DATASET_FINANCIAL, start, end)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM financials WHERE stock_id=? AND period_type='quarterly' "
            "ORDER BY period",
            (stock_id,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def fetch_dividend(
    stock_id: str,
    start: str = "2015-01-01",
    end: str | None = None,
    *,
    strict: bool = False,
) -> pd.DataFrame:
    """取得年度配息(含快取)。

    使用 FinMind `TaiwanStockDividend` dataset。每年可能 1–2 筆(中間配息 + 期末配息),
    本函式會按 (stock_id, year) 加總現金股利與股票股利。

    ⚠️ 已知限制(2026-04 觀察):
      此 dataset 在無 token 模式可能被 FinMind 限制或拒絕。
      預設 strict=False:遇到 FinMindAPIError 會 log warning 並回空 DataFrame
      讓互動 / 批次降級用例不中斷。

    Bug 2026-05-18 衍生:weekly backfill 2060 檔報 ok=2060 但實際 dividend.csv
      只 32 行 / 9 檔。原因是 swallow 把 quota / network 錯都吞掉,backfill
      看不到 → 每週白跑。新加 strict=True 給 backfill 用,errors propagate
      讓 backfill 真的能 count fail + log。

    參數:
        stock_id: 例 '2330'
        start, end: 'YYYY-MM-DD';預設抓 2015-01-01 ~ 今日(夠 5 年配息檢查)
        strict: True 時遇 FinMindAPIError 不 swallow,直接 raise 讓 caller
            自己處理(weekly backfill_dividend.py 必加,免再 silent fail)
    """
    db.init_db()
    if end is None:
        end = date.today().isoformat()

    synced = db.get_synced_range(stock_id, DATASET_DIVIDEND)
    missing = _missing_ranges(start, end, synced)

    if not missing:
        logger.info("[CACHE] dividend 命中: %s [%s ~ %s]", stock_id, start, end)
        print(f"[CACHE] dividend 命中: {stock_id} [{start} ~ {end}]", flush=True)
    else:
        for s, e in missing:
            try:
                raw = _api_call(
                    DATASET_DIVIDEND, data_id=stock_id, start_date=s, end_date=e,
                )
            except FinMindQuotaError:
                # 402 quota 永遠 raise — 跟 fetch_quarterly_financials 同模式,
                # batch caller 才能 fail-fast 不浪費 long-backoff slot
                raise
            except FinMindAPIError as ex:
                if strict:
                    raise
                logger.warning(
                    "配息抓取失敗(可能需要 FinMind token):%s", ex
                )
                return pd.DataFrame()
            normalized = _normalize_dividend_rows(stock_id, raw)
            db.upsert_dividend(normalized)
        db.update_synced_range(stock_id, DATASET_DIVIDEND, start, end)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM dividend WHERE stock_id=? ORDER BY year",
            (stock_id,),
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _normalize_dividend_rows(stock_id: str, raw: list[dict]) -> list[dict]:
    """把 FinMind dividend 多筆(年中 + 年末)按 year 加總成單筆/年。

    欄位名容錯:嘗試 FinMind 官方 (CashEarningsDistribution / StockEarningsDistribution)
    與口語化備援 (cash_dividend / stock_dividend / cash / stock)。
    """
    by_year: dict[int, dict] = {}
    for r in raw:
        year = _extract_dividend_year(r)
        if year is None:
            continue
        cash = _first_numeric(r, [
            "CashEarningsDistribution",
            "cash_dividend",
            "cash",
        ])
        stock = _first_numeric(r, [
            "StockEarningsDistribution",
            "stock_dividend",
            "stock",
        ])
        ex_date = (
            r.get("CashExDividendTradingDate")
            or r.get("StockExDividendTradingDate")
            or r.get("date")
        )
        agg = by_year.setdefault(year, {
            "stock_id": stock_id,
            "year": year,
            "cash_dividend": 0.0,
            "stock_dividend": 0.0,
            "ex_dividend_date": None,
        })
        agg["cash_dividend"] += float(cash or 0)
        agg["stock_dividend"] += float(stock or 0)
        # 取最近的除息日
        if ex_date:
            cur = agg["ex_dividend_date"]
            if cur is None or str(ex_date) > str(cur):
                agg["ex_dividend_date"] = ex_date
    return list(by_year.values())


def _extract_dividend_year(r: dict) -> int | None:
    """從 FinMind dividend row 取「配息所屬年度」(int)。

    FinMind 的 year 欄位有時是字串、有時是 'YYYYQn'、有時不存在;若不存在用 date 推。
    """
    for key in ("year", "Year", "data_year"):
        v = r.get(key)
        if v is None or v == "":
            continue
        try:
            return int(str(v)[:4])
        except (ValueError, TypeError):
            continue
    d = r.get("date")
    if d:
        try:
            return int(str(d)[:4])
        except ValueError:
            pass
    return None


def _first_numeric(r: dict, keys: list[str]) -> float | None:
    """依序嘗試多個 key,回第一個能轉 float 的值。"""
    for k in keys:
        v = r.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (ValueError, TypeError):
            continue
    return None


# === 批次:長線資料 (季財報 + 配息) ===

ProgressCallback = Callable[[int, int, str, "Exception | None"], None]


def fetch_long_term_data(
    stock_ids: list[str],
    on_progress: ProgressCallback | None = None,
    financial_start: str = "2015-01-01",
) -> dict[str, list[str]]:
    """批次抓多檔股票的季財報 + 配息(長線選股用)。

    容錯:單檔失敗(FinMindAPIError 或其他例外)不中斷整批,記入 failed 清單。
    on_progress callback 簽名: (idx_1based, total, stock_id, error_or_None) -> None

    回傳:
        {
            "success_financials": [stock_id, ...],  # 季財報拿到資料的
            "success_dividend":   [stock_id, ...],  # 配息拿到資料的
            "failed":             [stock_id, ...],  # 兩者都空 / raise 的
        }
    """
    today = date.today().isoformat()
    success_fin: list[str] = []
    success_div: list[str] = []
    failed: list[str] = []
    n = len(stock_ids)

    for i, sid in enumerate(stock_ids):
        err: Exception | None = None
        got_any = False
        try:
            fin_df = fetch_quarterly_financials(sid, financial_start, today)
            div_df = fetch_dividend(sid, financial_start, today)
            if not fin_df.empty:
                success_fin.append(sid)
                got_any = True
            if not div_df.empty:
                success_div.append(sid)
                got_any = True
        except Exception as e:  # noqa: BLE001 — 容錯,任何例外都記 failed
            err = e
        if not got_any:
            failed.append(sid)
        if on_progress is not None:
            try:
                on_progress(i + 1, n, sid, err)
            except Exception:  # noqa: BLE001 — callback 自己出錯不能影響主流程
                pass

    return {
        "success_financials": success_fin,
        "success_dividend": success_div,
        "failed": failed,
    }


# === 全市場 bulk OHLCV 抓取 ===

# 60 秒 cache,避免 daily_fetch 跟 daily_notify 重複打
_bulk_cache: dict[str, pd.DataFrame] = {}
_bulk_cache_time: dict[str, float] = {}
_BULK_CACHE_TTL_SECS = 60


def _reset_bulk_cache() -> None:
    """測試用。"""
    _bulk_cache.clear()
    _bulk_cache_time.clear()


def _parse_roc_date(roc: str) -> str:
    """民國 'YYYMMDD' (7 字元) → 西元 'YYYY-MM-DD'。例 '1150428' → '2026-04-28'。"""
    if not roc or len(str(roc)) < 7:
        return ""
    s = str(roc)
    try:
        year = int(s[:3]) + 1911
        return f"{year}-{s[3:5]}-{s[5:7]}"
    except (ValueError, TypeError):
        return ""


def _safe_int(v: Any) -> int | None:
    """字串 → int;空 / 非數 / 含 '+'-',' → 處理。"""
    if v is None or v == "":
        return None
    try:
        # TWSE/TPEx 的數字偶有 ',' 千分位
        return int(float(str(v).replace(",", "")))
    except (ValueError, TypeError):
        return None


def _safe_float_loose(v: Any) -> float | None:
    """寬鬆轉 float;支援 '+/-' 開頭與 ',' 千分位。"""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return None


def _normalize_twse_bulk_row(r: dict) -> dict | None:
    """TWSE STOCK_DAY_ALL 一筆 → daily_prices schema。"""
    sid = r.get("Code")
    iso_date = _parse_roc_date(r.get("Date", ""))
    if not sid or not iso_date:
        return None
    return {
        "stock_id": sid,
        "date": iso_date,
        "open": _safe_float_loose(r.get("OpeningPrice")),
        "high": _safe_float_loose(r.get("HighestPrice")),
        "low": _safe_float_loose(r.get("LowestPrice")),
        "close": _safe_float_loose(r.get("ClosingPrice")),
        "volume": _safe_int(r.get("TradeVolume")),
        "trading_money": _safe_float_loose(r.get("TradeValue")),
        "trading_turnover": _safe_int(r.get("Transaction")),
        "spread": _safe_float_loose(r.get("Change")),
    }


def _normalize_tpex_bulk_row(r: dict) -> dict | None:
    """TPEx tpex_mainboard_quotes 一筆 → daily_prices schema。

    TPEx 與 TWSE 欄位名不同 — 用 SecuritiesCompanyCode、Open/High/Low/Close。
    """
    sid = r.get("SecuritiesCompanyCode")
    iso_date = _parse_roc_date(r.get("Date", ""))
    if not sid or not iso_date:
        return None
    return {
        "stock_id": sid,
        "date": iso_date,
        "open": _safe_float_loose(r.get("Open")),
        "high": _safe_float_loose(r.get("High")),
        "low": _safe_float_loose(r.get("Low")),
        "close": _safe_float_loose(r.get("Close")),
        "volume": _safe_int(r.get("TradingShares")),
        "trading_money": _safe_float_loose(r.get("TransactionAmount")),
        "trading_turnover": _safe_int(r.get("TransactionNumber")),
        "spread": _safe_float_loose(r.get("Change")),
    }


def _fetch_bulk_url(url: str, timeout: int = 30) -> list[dict]:
    """robust GET(requests + httpx fallback);回 JSON list 或 raise。"""
    # 共用 financial_fetcher_free 的 _twse_get 思路:requests 失敗試 httpx
    try:
        from src.financial_fetcher_free import _twse_get
        # _twse_get 對 openapi.twse.com.tw 有 SSL adapter;對 tpex 也適用(都 verify=False)
        r = _twse_get(url, timeout=timeout)
        return r.json()
    except Exception:
        pass
    # fallback 直接 httpx
    import httpx
    with httpx.Client(verify=False, timeout=timeout, follow_redirects=True) as c:
        resp = c.get(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) stock-screener/1.0",
            "Accept": "application/json, text/plain, */*",
        })
        resp.raise_for_status()
        return resp.json()


def fetch_all_daily_prices_bulk(
    date_str: str | None = None,
) -> pd.DataFrame:
    """一次拿全市場 OHLCV(TWSE + TPEx),約 2360 檔 < 30 秒。

    參數 date_str 預留;實際 endpoint 永遠回「最近一個交易日」資料,
    傳 date_str 不影響回傳內容(但會分開 cache)。

    回 DataFrame[stock_id, date, open, high, low, close, volume,
                  trading_money, trading_turnover, spread]
    任一邊 fetch 失敗,仍回另一邊資料(不擋全流程)。
    兩邊都失敗回空 DataFrame + log error。
    """
    cache_key = date_str or "today"
    now = time.time()
    if (
        cache_key in _bulk_cache
        and now - _bulk_cache_time.get(cache_key, 0) < _BULK_CACHE_TTL_SECS
    ):
        return _bulk_cache[cache_key]

    rows: list[dict] = []

    # TWSE bulk
    try:
        twse_raw = _fetch_bulk_url(TWSE_DAILY_BULK_URL)
        for r in twse_raw:
            norm = _normalize_twse_bulk_row(r)
            if norm:
                rows.append(norm)
        logger.info("[BULK] TWSE 拿到 %d 筆", sum(1 for _ in twse_raw))
    except Exception as e:  # noqa: BLE001
        logger.error("[BULK] TWSE STOCK_DAY_ALL 失敗: %s", e)

    # TPEx bulk
    try:
        tpex_raw = _fetch_bulk_url(TPEX_DAILY_BULK_URL)
        for r in tpex_raw:
            norm = _normalize_tpex_bulk_row(r)
            if norm:
                rows.append(norm)
        logger.info("[BULK] TPEx 拿到 %d 筆", sum(1 for _ in tpex_raw))
    except Exception as e:  # noqa: BLE001
        logger.error("[BULK] TPEx daily quotes 失敗: %s", e)

    df = pd.DataFrame(
        rows,
        columns=[
            "stock_id", "date", "open", "high", "low", "close",
            "volume", "trading_money", "trading_turnover", "spread",
        ],
    )
    _bulk_cache[cache_key] = df
    _bulk_cache_time[cache_key] = now
    logger.info("[BULK] 合併 %d 筆 (TWSE + TPEx)", len(df))
    return df


# === 個股基本資料 helper(自動補 stocks 表的 name / industry) ===

# 1 小時記憶體 cache,避免每查未知 stock_id 都打全市場 API(4093 筆 ~500KB)
_stock_info_cache: dict[str, dict] | None = None
_stock_info_cache_time: float = 0.0
_STOCK_INFO_TTL_SECS = 3600


def _reset_stock_info_cache() -> None:
    """測試用:清空 stock_info 記憶體 cache。"""
    global _stock_info_cache, _stock_info_cache_time
    _stock_info_cache = None
    _stock_info_cache_time = 0.0


def _fetch_all_stock_info() -> dict[str, dict]:
    """打 FinMind TaiwanStockInfo 拉全市場 (上市+上櫃) 個股基本資料。"""
    global _stock_info_cache, _stock_info_cache_time
    now = time.time()
    if (
        _stock_info_cache is not None
        and now - _stock_info_cache_time < _STOCK_INFO_TTL_SECS
    ):
        return _stock_info_cache
    raw = _api_call(DATASET_INFO)
    _stock_info_cache = {r["stock_id"]: r for r in raw if r.get("stock_id")}
    _stock_info_cache_time = now
    logger.info("[STOCK_INFO] 拿到 %d 檔基本資料", len(_stock_info_cache))
    return _stock_info_cache


def ensure_stock_info(stock_id: str) -> dict | None:
    """確保 stocks 表有此 stock_id 的 name/industry,沒有就抓 FinMind 補。

    回 {stock_id, name, industry} 或 None(FinMind 也找不到此股)。

    流程:
      1. 查 SQLite stocks 表;有 name 就回
      2. 沒有 → 抓全市場(含上櫃 tpex,例如 3680 家登)
      3. 篩出此 sid → upsert_stocks 寫進 cache
      4. 找不到 → 回 None(可能是無效代號)
    """
    sid = stock_id.strip() if stock_id else ""
    if not sid:
        return None

    db.init_db()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT stock_id, name, industry FROM stocks "
            "WHERE stock_id=? AND name IS NOT NULL AND name != ''",
            (sid,),
        ).fetchone()
    if row:
        return {
            "stock_id": row["stock_id"],
            "name": row["name"],
            "industry": row["industry"],
        }

    # SQLite 沒 → 抓 FinMind 全市場
    try:
        all_info = _fetch_all_stock_info()
    except Exception as e:  # noqa: BLE001
        logger.warning("[STOCK_INFO] FinMind 抓全市場失敗: %s", e)
        return None

    raw = all_info.get(sid)
    if raw is None:
        return None

    name = raw.get("stock_name") or ""
    industry = raw.get("industry_category") or None
    db.upsert_stocks([{
        "stock_id": sid,
        "name": name,
        "market": "TW",
        "industry": industry,
        "type": raw.get("type"),
    }])
    return {"stock_id": sid, "name": name, "industry": industry}


def validate_daily_price_sanity(df: pd.DataFrame) -> list[tuple[str, str]]:
    """偵測 daily_price 異常,回 [(stock_id, reason), ...]。

    當前 check:
    - close <= 0 (或 NaN):全部視為異常
    - high < low:資料矛盾
    - close 不在 [low, high] 之間:資料矛盾
    不阻擋寫入,僅 print warning 給 daily_fetch summary 用。
    """
    if df is None or df.empty:
        return []
    issues: list[tuple[str, str]] = []
    for _, r in df.iterrows():
        sid = str(r.get("stock_id", "?"))
        c = r.get("close")
        h = r.get("high")
        low = r.get("low")
        if c is None or pd.isna(c) or float(c) <= 0:
            issues.append((sid, f"close={c}"))
            continue
        if h is not None and low is not None:
            try:
                if float(h) < float(low):
                    issues.append((sid, f"high {h} < low {low}"))
                elif not (float(low) <= float(c) <= float(h)):
                    issues.append((sid, f"close {c} 不在 [{low}, {h}] 區間"))
            except (TypeError, ValueError):
                pass
    return issues


def fetch_all_daily_prices_via_finmind(
    stock_ids: list[str],
    target_date: str,
    sleep_secs: float = 0.05,
) -> pd.DataFrame:
    """用 FinMind TaiwanStockPrice 對 universe 各檔抓 target_date 的 OHLCV。

    跟 TWSE bulk endpoint 比:
      + FinMind publication 較準時(2026-05-04 事件:TWSE OpenAPI 還在服務 4/30
        舊資料時 FinMind 已有 5/4 個股資料)
      - 慢:每檔 1 次 API call,~2700 檔 × ~1s = ~45 分鐘
      - 受 token quota 限制(600/hr 預設,有 token 1500/hr)

    sleep_secs 在每檔之間 throttle,避免一次性燒太快。

    重用 `fetch_daily_price` 的 cache 邏輯(sync_log)— 已撈過的日期 cache hit,
    不重打 API。回 concat DataFrame(stock_id, date, OHLCV ...);全失敗 → 空。
    """
    import time as _time

    rows: list[pd.DataFrame] = []
    n = len(stock_ids)
    for i, sid in enumerate(stock_ids, start=1):
        try:
            df = fetch_daily_price(sid, target_date, target_date)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "[FETCH-FINMIND] fetch_daily_price(%s) 失敗: %s", sid, e,
            )
            continue
        if df is not None and not df.empty:
            rows.append(df)
        if i % 200 == 0:
            print(
                f"[FETCH-FINMIND]   {i}/{n} 完成"
                f"({len(rows)} 檔有資料)",
                flush=True,
            )
        if sleep_secs > 0:
            _time.sleep(sleep_secs)

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


__all__ = [
    "FinMindAPIError",
    "FinMindQuotaError",
    "list_tw_stocks",
    "ensure_stock_info",
    "fetch_all_daily_prices_bulk",
    "fetch_all_daily_prices_via_finmind",
    "validate_daily_price_sanity",
    "fetch_daily_price",
    "fetch_institutional",
    "fetch_monthly_revenue",
    "fetch_quarterly_financials",
    "fetch_dividend",
    "fetch_long_term_data",
    "DATASET_PRICE",
    "DATASET_INFO",
    "DATASET_INST",
    "DATASET_REVENUE",
    "DATASET_FINANCIAL",
    "DATASET_DIVIDEND",
]
