"""
TWSE OpenAPI 免費資料抓取(免 FinMind 付費 token)。

來源 endpoint(全免費、無 API key):
- /v1/exchangeReport/BWIBBU_d   個股 PE / PB / 殖利率 / 收盤(全市場一次給)
- /v1/opendata/t187ap14_L       上市公司季綜損(EPS / 營收,全市場一次給)

ROE 算法(Du Pont 簡化):
    EPS_TTM = close / PE
    BVPS    = close / PB
    ROE     = EPS_TTM / BVPS = PB / PE   (數學恆等)

驗證:2330 PE=32.99, PB=10.46 → ROE = 10.46/32.99 ≈ 31.7%
     (市場公認 TSMC 2024 ROE ≈ 28-30%,合理)

注意 SSL:
- TWSE 憑證缺 Subject Key Identifier,新版 OpenSSL 拒絕
- 用 verify=False(公開資料無 MITM 風險)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import pandas as pd
import requests
import urllib3

from src import database as db


logger = logging.getLogger(__name__)

# 抑制 InsecureRequestWarning(verify=False)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TWSE_BWIBBU_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d"
TWSE_INC_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap14_L"

# 全市場資料記憶體 cache(5 分鐘 TTL,避免 batch 50 檔重複拉)
_metrics_cache: dict | None = None
_metrics_cache_time: float = 0.0
_eps_cache: dict | None = None
_eps_cache_time: float = 0.0
_CACHE_TTL_SECS = 300


# === 內部工具 ===

def _safe_float(v: Any) -> float | None:
    """字串 → float;空字串 / 非數字 → None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _twse_date_to_iso(d: str) -> str:
    """'20260424' → '2026-04-24'。"""
    if not d or len(d) != 8:
        return d
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _quarter_to_period(year_roc: Any, quarter: Any) -> str:
    """民國年 + 季 → 'YYYY-QN'(西元)。例 (114, 4) → '2025-Q4'。"""
    try:
        y = int(year_roc) + 1911
        q = int(quarter)
        return f"{y}-Q{q}"
    except (ValueError, TypeError):
        return ""


def _fetch_all_metrics(force_refresh: bool = False) -> dict[str, dict]:
    """拉全市場 BWIBBU_d 並轉成 {stock_id: raw_dict}。5 分鐘 cache。"""
    global _metrics_cache, _metrics_cache_time
    now = time.time()
    if (
        not force_refresh
        and _metrics_cache is not None
        and now - _metrics_cache_time < _CACHE_TTL_SECS
    ):
        return _metrics_cache
    logger.info("[FREE] 拉 TWSE BWIBBU_d 全市場...")
    r = requests.get(TWSE_BWIBBU_URL, timeout=30, verify=False)
    r.raise_for_status()
    raw = r.json()
    _metrics_cache = {x["Code"]: x for x in raw if x.get("Code")}
    _metrics_cache_time = now
    logger.info("[FREE] BWIBBU_d 拿到 %d 檔", len(_metrics_cache))
    return _metrics_cache


def _fetch_all_eps(force_refresh: bool = False) -> dict[str, dict]:
    """拉全市場 t187ap14_L 並轉成 {stock_id: raw_dict}。"""
    global _eps_cache, _eps_cache_time
    now = time.time()
    if (
        not force_refresh
        and _eps_cache is not None
        and now - _eps_cache_time < _CACHE_TTL_SECS
    ):
        return _eps_cache
    logger.info("[FREE] 拉 TWSE t187ap14_L 全市場(綜損/EPS)...")
    r = requests.get(TWSE_INC_URL, timeout=60, verify=False)
    r.raise_for_status()
    raw = r.json()
    _eps_cache = {x["公司代號"]: x for x in raw if x.get("公司代號")}
    _eps_cache_time = now
    logger.info("[FREE] t187ap14_L 拿到 %d 檔", len(_eps_cache))
    return _eps_cache


def _reset_caches() -> None:
    """測試用:清空記憶體 cache,確保每個測試獨立。"""
    global _metrics_cache, _metrics_cache_time, _eps_cache, _eps_cache_time
    _metrics_cache = None
    _metrics_cache_time = 0.0
    _eps_cache = None
    _eps_cache_time = 0.0


# === 公開 API ===

def fetch_daily_metrics(
    stock_id: str,
    date: str | None = None,  # 預留參數,實務上 BWIBBU_d 只回最新日
) -> dict | None:
    """取得個股當日 PE / PB / 殖利率 / 收盤。

    從全市場 cache 篩;沒在 cache 或 API 失敗回 None。
    """
    try:
        all_metrics = _fetch_all_metrics()
    except Exception as e:  # noqa: BLE001
        logger.warning("[FREE] BWIBBU_d 抓取失敗: %s", e)
        return None
    raw = all_metrics.get(stock_id)
    if raw is None:
        return None
    return {
        "stock_id": stock_id,
        "date": _twse_date_to_iso(raw.get("Date", "")),
        "close": _safe_float(raw.get("ClosePrice")),
        "pe": _safe_float(raw.get("PEratio")),
        "pb": _safe_float(raw.get("PBratio")),
        "dividend_yield": _safe_float(raw.get("DividendYield")),
        # FiscalYearQuarter ('2025Q4') 留著供 compute_roe / 寫 financials
        "_fiscal_quarter": raw.get("FiscalYearQuarter"),
    }


def fetch_quarterly_eps(stock_id: str) -> pd.DataFrame:
    """取得個股最新季 EPS(累計值,單筆 DataFrame)。

    TWSE OpenAPI 一次只給最新一季,要累積歷史得跨期更新本機 SQLite。
    回傳 DataFrame 欄位:year, quarter, eps_quarterly(累計到該季), eps_ttm(=eps_quarterly)
    """
    try:
        all_eps = _fetch_all_eps()
    except Exception as e:  # noqa: BLE001
        logger.warning("[FREE] t187ap14_L 抓取失敗: %s", e)
        return pd.DataFrame(
            columns=["year", "quarter", "eps_quarterly", "eps_ttm"]
        )
    raw = all_eps.get(stock_id)
    if raw is None:
        return pd.DataFrame(
            columns=["year", "quarter", "eps_quarterly", "eps_ttm"]
        )
    # 民國年轉西元
    year_roc = raw.get("年度")
    quarter = raw.get("季別")
    eps = _safe_float(raw.get("基本每股盈餘(元)"))
    try:
        year_ad = int(year_roc) + 1911
        q = int(quarter)
    except (ValueError, TypeError):
        return pd.DataFrame(
            columns=["year", "quarter", "eps_quarterly", "eps_ttm"]
        )
    return pd.DataFrame([{
        "year": year_ad,
        "quarter": q,
        "eps_quarterly": eps,  # TWSE 給的是「累計到該季」
        "eps_ttm": eps,        # 簡化:Q4 時 = 全年 = TTM;Q1-Q3 略低估,可接受
    }])


def compute_roe(stock_id: str) -> float | None:
    """估算 ROE(% 已乘 100)。

    公式(Du Pont 簡化):
        EPS_TTM = close / PE
        BVPS    = close / PB
        ROE     = EPS_TTM / BVPS  (= PB / PE,數學恆等)
    任一資料缺 / 為 0 / 為負 → 回 None。
    """
    metrics = fetch_daily_metrics(stock_id)
    if metrics is None:
        return None
    close = metrics.get("close")
    pe = metrics.get("pe")
    pb = metrics.get("pb")
    if not close or close <= 0:
        return None
    if not pe or pe <= 0:
        return None
    if not pb or pb <= 0:
        return None
    eps_ttm = close / pe
    bvps = close / pb
    if bvps <= 0:
        return None
    return eps_ttm / bvps * 100.0  # 回百分比(對齊 financials.roe 欄位慣例)


# === 批次更新 ===

ProgressCallback = Callable[[int, int, str, "Exception | None"], None]


def update_long_term_data_free(
    stock_ids: list[str],
    on_progress: ProgressCallback | None = None,
) -> dict:
    """批次更新長線資料(免費版)。

    流程:
      1. 拉一次 BWIBBU_d 全市場(cache 5 分鐘)
      2. 拉一次 t187ap14_L 全市場(cache 5 分鐘)
      3. 對每檔:篩 BWIBBU → 寫 daily_metrics + 算 ROE 寫 financials
                篩 t187ap14 → 寫 financials.eps(同期 period)

    容錯:單檔失敗不中斷;BWIBBU 全市場 API 失敗 → 全部 fail。
    回 {success_metrics: [...], success_eps: [...], failed: [...]}
    """
    db.init_db()
    success_metrics: list[str] = []
    success_eps: list[str] = []
    failed: list[str] = []

    # 預熱兩個 cache
    try:
        _fetch_all_metrics(force_refresh=True)
    except Exception as e:  # noqa: BLE001
        logger.error("[FREE] BWIBBU_d 全市場抓取失敗,全部視為 failed: %s", e)
        for sid in stock_ids:
            failed.append(sid)
            if on_progress is not None:
                try:
                    on_progress(
                        stock_ids.index(sid) + 1, len(stock_ids), sid, e
                    )
                except Exception:  # noqa: BLE001
                    pass
        return {
            "success_metrics": [], "success_eps": [], "failed": failed,
        }

    eps_available = True
    try:
        _fetch_all_eps(force_refresh=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[FREE] t187ap14_L 全市場抓取失敗,EPS 跳過: %s", e
        )
        eps_available = False

    n = len(stock_ids)
    for i, sid in enumerate(stock_ids):
        err: Exception | None = None
        got_metrics = False
        got_eps = False
        try:
            m = fetch_daily_metrics(sid)
            if m and m.get("date") and m.get("pe"):
                db.upsert_daily_metrics([{
                    "stock_id": sid,
                    "date": m["date"],
                    "close": m["close"],
                    "pe": m["pe"],
                    "pb": m["pb"],
                    "dividend_yield": m["dividend_yield"],
                }])
                got_metrics = True

                # 同時把 ROE / EPS_TTM 寫進 financials(period_type='quarterly')
                fq = m.get("_fiscal_quarter") or ""
                # 'YYYYQN' → 'YYYY-QN'
                if "Q" in fq:
                    period = fq.replace("Q", "-Q")
                    eps_ttm = (
                        m["close"] / m["pe"]
                        if m.get("pe") and m["pe"] > 0 else None
                    )
                    bvps = (
                        m["close"] / m["pb"]
                        if m.get("pb") and m["pb"] > 0 else None
                    )
                    roe = (
                        eps_ttm / bvps * 100.0
                        if eps_ttm and bvps and bvps > 0 else None
                    )
                    if roe is not None:
                        db.upsert_financials([{
                            "stock_id": sid,
                            "period_type": "quarterly",
                            "period": period,
                            "revenue": None,
                            "revenue_yoy": None,
                            "eps": eps_ttm,
                            "roe": roe,
                        }])

            if eps_available:
                eps_df = fetch_quarterly_eps(sid)
                if not eps_df.empty:
                    row = eps_df.iloc[0]
                    period = f"{int(row['year'])}-Q{int(row['quarter'])}"
                    db.upsert_financials([{
                        "stock_id": sid,
                        "period_type": "quarterly",
                        "period": period,
                        "revenue": None,
                        "revenue_yoy": None,
                        "eps": float(row["eps_quarterly"])
                              if pd.notna(row["eps_quarterly"]) else None,
                        "roe": None,  # 不蓋掉 PB 反推的 ROE(COALESCE 保留舊值)
                    }])
                    got_eps = True
        except Exception as e:  # noqa: BLE001
            err = e
        if not got_metrics:
            failed.append(sid)
        else:
            success_metrics.append(sid)
        if got_eps:
            success_eps.append(sid)
        if on_progress is not None:
            try:
                on_progress(i + 1, n, sid, err)
            except Exception:  # noqa: BLE001
                pass

    return {
        "success_metrics": success_metrics,
        "success_eps": success_eps,
        "failed": failed,
    }


__all__ = [
    "fetch_daily_metrics",
    "fetch_quarterly_eps",
    "compute_roe",
    "update_long_term_data_free",
]
