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
import ssl
import sys
import time
from typing import Any, Callable

import pandas as pd
import requests
import urllib3
from requests.adapters import HTTPAdapter

from src import database as db


logger = logging.getLogger(__name__)

# 抑制 InsecureRequestWarning(verify=False)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TWSE_BWIBBU_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d"
TWSE_INC_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap14_L"
TWSE_BASIC_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"

# TWSE 產業別代碼 → 中文(依 TWSE 公開分類)
INDUSTRY_CODE_MAP: dict[str, str] = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業",
    "04": "紡織纖維", "05": "電機機械", "06": "電器電纜",
    "08": "玻璃陶瓷", "09": "造紙工業", "10": "鋼鐵工業",
    "11": "橡膠工業", "12": "汽車工業", "14": "建材營造",
    "15": "航運業", "16": "觀光餐旅", "17": "金融保險",
    "18": "貿易百貨", "19": "綜合", "20": "其他",
    "21": "化學工業", "22": "生技醫療", "23": "油電燃氣",
    "24": "半導體業", "25": "電腦及週邊設備業", "26": "光電業",
    "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業",
    "33": "農業科技業", "34": "電子商務", "35": "綠能環保",
    "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}


# === Robust HTTP 客戶端 ===
# 為什麼要這層:
# - TWSE 的 SSL 憑證缺 Subject Key Identifier,新版 OpenSSL 拒絕 → verify=False
# - Streamlit Cloud (Debian 12 + OpenSSL 3.x) 預設 SECLEVEL=2,某些 cipher 被擋
#   → set_ciphers('DEFAULT@SECLEVEL=1') 開放更舊的 cipher
# - 部分 CDN 對 python-requests 預設 UA 擋 → 設成像瀏覽器的 UA

class _LegacySSLAdapter(HTTPAdapter):
    """處理 TWSE 舊 SSL 憑證 / cipher,雲端 OpenSSL 較新環境也能連。"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            # 某些 OpenSSL build 不支援這個 cipher 字串,忽略繼續
            pass
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


_session: requests.Session | None = None


def _twse_session() -> requests.Session:
    """共用 session(robust SSL adapter + 偽裝 UA)。"""
    global _session
    if _session is None:
        s = requests.Session()
        s.mount("https://", _LegacySSLAdapter())
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 stock-screener/1.0"
            ),
            "Accept": "application/json, text/plain, */*",
        })
        _session = s
    return _session


def _twse_get_via_httpx(url: str, timeout: int = 30):
    """Fallback HTTP client (httpx 對舊 SSL / cipher 比 requests 寬鬆)。

    Streamlit Cloud 上有時 requests 抓 TWSE 回 200 但內容空,httpx 卻能拿到正常資料。
    回的 response 物件有 .json() 方法,介面與 requests.Response 兼容。
    """
    import httpx
    with httpx.Client(
        verify=False, timeout=timeout, follow_redirects=True,
    ) as c:
        r = c.get(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 stock-screener/1.0"
            ),
            "Accept": "application/json, text/plain, */*",
        })
        r.raise_for_status()
        return r


def _twse_get(url: str, timeout: int = 30):
    """先試 requests + _LegacySSLAdapter,失敗 fallback 到 httpx,兩個都失敗才 raise。

    回 response-like 物件(`.json()` 可呼叫),caller 不用區分 client。
    每次成敗都印一行到 stderr/log,雲端 logs 可看出走哪條路徑。
    """
    errors: list[tuple[str, Exception]] = []
    # try 1: requests + custom adapter
    try:
        r = _twse_session().get(url, timeout=timeout, verify=False)
        r.raise_for_status()
        return r
    except Exception as e:  # noqa: BLE001
        errors.append(("requests", e))
        msg = (
            f"[TWSE-WARN-REQUESTS] {url}: "
            f"({type(e).__name__}) {str(e)[:200]} — 試 httpx fallback"
        )
        logger.warning(msg)
        print(msg, file=sys.stderr, flush=True)

    # try 2: httpx fallback
    try:
        r = _twse_get_via_httpx(url, timeout)
        msg = f"[TWSE-OK-HTTPX] {url} via httpx fallback"
        logger.info(msg)
        print(msg, file=sys.stderr, flush=True)
        return r
    except Exception as e:  # noqa: BLE001
        errors.append(("httpx", e))
        msg = (
            f"[TWSE-ERROR-HTTPX] {url}: "
            f"({type(e).__name__}) {str(e)[:200]}"
        )
        logger.error(msg)
        print(msg, file=sys.stderr, flush=True)

    # 兩個都失敗 → 拋第一個 exception(通常是 requests 的,主公比較熟)
    raise errors[0][1]

# 全市場資料記憶體 cache(5 分鐘 TTL,避免 batch 50 檔重複拉)
_metrics_cache: dict | None = None
_metrics_cache_time: float = 0.0
_eps_cache: dict | None = None
_eps_cache_time: float = 0.0
_industry_cache: dict | None = None
_industry_cache_time: float = 0.0
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
    r = _twse_get(TWSE_BWIBBU_URL, timeout=30)
    raw = r.json()
    # 防雲端「靜默失敗」:HTTP 200 但回空 list / 非 list
    if not isinstance(raw, list) or len(raw) == 0:
        sample = str(raw)[:200]
        raise RuntimeError(
            f"TWSE BWIBBU_d 回傳格式異常: type={type(raw).__name__} "
            f"len={len(raw) if hasattr(raw, '__len__') else '?'} — "
            f"前 200 字: {sample}"
        )
    _metrics_cache = {x["Code"]: x for x in raw if x.get("Code")}
    if not _metrics_cache:
        raise RuntimeError(
            f"TWSE BWIBBU_d 回了 {len(raw)} 筆但都沒 'Code' 欄位"
        )
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
    r = _twse_get(TWSE_INC_URL, timeout=60)
    raw = r.json()
    if not isinstance(raw, list) or len(raw) == 0:
        sample = str(raw)[:200]
        raise RuntimeError(
            f"TWSE t187ap14_L 回傳格式異常: type={type(raw).__name__} "
            f"len={len(raw) if hasattr(raw, '__len__') else '?'} — "
            f"前 200 字: {sample}"
        )
    _eps_cache = {x["公司代號"]: x for x in raw if x.get("公司代號")}
    _eps_cache_time = now
    logger.info("[FREE] t187ap14_L 拿到 %d 檔", len(_eps_cache))
    return _eps_cache


def _reset_caches() -> None:
    """測試用:清空記憶體 cache + session,確保每個測試獨立。"""
    global _metrics_cache, _metrics_cache_time, _eps_cache, _eps_cache_time
    global _industry_cache, _industry_cache_time, _session
    _metrics_cache = None
    _metrics_cache_time = 0.0
    _eps_cache = None
    _eps_cache_time = 0.0
    _industry_cache = None
    _industry_cache_time = 0.0
    _session = None


def fetch_industry_classification() -> dict[str, str]:
    """從 TWSE t187ap03_L 拉所有上市公司的產業別,翻譯成中文。

    回 {stock_id: industry_zh},例 {"2330": "半導體業", "2317": "其他電子業", ...}。
    抓取失敗 / 代碼找不到對照 → 該檔 industry 為 "其他"。
    記憶體 5 分鐘 cache。
    """
    global _industry_cache, _industry_cache_time
    now = time.time()
    if (
        _industry_cache is not None
        and now - _industry_cache_time < _CACHE_TTL_SECS
    ):
        return _industry_cache

    logger.info("[FREE] 拉 TWSE t187ap03_L 全市場(產業別)...")
    try:
        r = _twse_get(TWSE_BASIC_URL, timeout=60)
        raw = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("[FREE] 產業別抓取失敗: %s", e)
        return {}
    if not isinstance(raw, list) or len(raw) == 0:
        logger.warning("[FREE] 產業別回空清單")
        return {}

    out: dict[str, str] = {}
    for r in raw:
        sid = r.get("公司代號")
        code = (r.get("產業別") or "").strip().zfill(2)
        if not sid:
            continue
        out[sid] = INDUSTRY_CODE_MAP.get(code, "其他")
    _industry_cache = out
    _industry_cache_time = now
    logger.info("[FREE] 產業別拿到 %d 檔", len(out))
    return out


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
            "success_metrics": [],
            "success_eps": [],
            "failed": failed,
            "error": e,  # ← 給 UI 顯示具體 error type + 訊息
        }

    eps_available = True
    try:
        _fetch_all_eps(force_refresh=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[FREE] t187ap14_L 全市場抓取失敗,EPS 跳過: %s", e
        )
        eps_available = False

    # 一次拉產業別,寫進 stocks 表
    industry_map = fetch_industry_classification()
    if industry_map:
        rows_to_update = []
        for sid in stock_ids:
            ind = industry_map.get(sid)
            if ind:
                rows_to_update.append({"stock_id": sid, "industry": ind})
        if rows_to_update:
            with db.get_conn() as conn:
                conn.executemany(
                    "UPDATE stocks SET industry=:industry "
                    "WHERE stock_id=:stock_id",
                    rows_to_update,
                )
            logger.info("[FREE] 更新 %d 檔的 industry", len(rows_to_update))

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
        "error": None,
    }


__all__ = [
    "fetch_daily_metrics",
    "fetch_quarterly_eps",
    "compute_roe",
    "fetch_industry_classification",
    "update_long_term_data_free",
    "INDUSTRY_CODE_MAP",
]
