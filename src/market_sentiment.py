"""
大盤情緒資料抓取模組。

四個指標:
1. 加權指數 (TAIEX) — FinMind TaiwanStockPrice 'TAIEX' 近 90 日
2. 三大法人合計買賣超 — FinMind TaiwanStockTotalInstitutionalInvestors 近 30 日
3. 融資融券餘額(全市場) — FinMind TaiwanStockTotalMarginPurchaseShortSale 近 30 日
4. VIX 恐慌指數 — yfinance ^VIX 近 90 日

每個 fetch 60 秒記憶體 cache,失敗回空 DataFrame,UI 自己處理顯示。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from src.data_fetcher import FinMindAPIError, _api_call, fetch_daily_price


logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_TTL_SECS = 60


def _reset_cache() -> None:
    """測試用。"""
    _CACHE.clear()


def _get_cached(key: str, fetcher: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    """簡易 60 秒 cache wrapper。"""
    now = time.time()
    if key in _CACHE:
        ts, val = _CACHE[key]
        if now - ts < _CACHE_TTL_SECS:
            return val
    val = fetcher()
    _CACHE[key] = (now, val)
    return val


def fetch_taiex(days: int = 90) -> pd.DataFrame:
    """加權指數近 N 日線(欄位 date / close)。失敗回空 DF。"""
    def _do() -> pd.DataFrame:
        try:
            today = date.today().isoformat()
            start = (date.today() - timedelta(days=days)).isoformat()
            return fetch_daily_price("TAIEX", start, today)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SENTIMENT] TAIEX 抓取失敗: %s", e)
            return pd.DataFrame()
    return _get_cached(f"taiex_{days}", _do)


def fetch_institutional_total(days: int = 30) -> pd.DataFrame:
    """三大法人合計買賣超(全市場)近 N 日。

    FinMind dataset: TaiwanStockTotalInstitutionalInvestors
    回欄位 date / name / buy / sell / 等;若失敗回空 DF。
    """
    def _do() -> pd.DataFrame:
        try:
            today = date.today().isoformat()
            start = (date.today() - timedelta(days=days)).isoformat()
            raw = _api_call(
                "TaiwanStockTotalInstitutionalInvestors",
                start_date=start, end_date=today,
            )
            return pd.DataFrame(raw)
        except FinMindAPIError as e:
            logger.warning("[SENTIMENT] 法人總額抓取失敗: %s", e)
            return pd.DataFrame()
        except Exception as e:  # noqa: BLE001
            logger.warning("[SENTIMENT] 法人總額抓取失敗(其他): %s", e)
            return pd.DataFrame()
    return _get_cached(f"inst_{days}", _do)


def fetch_margin_balance(days: int = 30) -> pd.DataFrame:
    """融資融券餘額(全市場)近 N 日。

    FinMind dataset: TaiwanStockTotalMarginPurchaseShortSale
    """
    def _do() -> pd.DataFrame:
        try:
            today = date.today().isoformat()
            start = (date.today() - timedelta(days=days)).isoformat()
            raw = _api_call(
                "TaiwanStockTotalMarginPurchaseShortSale",
                start_date=start, end_date=today,
            )
            return pd.DataFrame(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SENTIMENT] 融資券總額抓取失敗: %s", e)
            return pd.DataFrame()
    return _get_cached(f"margin_{days}", _do)


def fetch_vix(days: int = 90) -> pd.DataFrame:
    """美股 VIX 恐慌指數(yfinance)。

    雖然是美股指數,對全球風險偏好仍有指標性 — 高 VIX = 市場恐慌。
    回欄位 Date / Close;失敗回空 DF。
    """
    def _do() -> pd.DataFrame:
        try:
            import yfinance as yf
            t = yf.Ticker("^VIX")
            df = t.history(period=f"{days}d")
            if df.empty:
                return pd.DataFrame()
            return df.reset_index()
        except Exception as e:  # noqa: BLE001
            logger.warning("[SENTIMENT] VIX 抓取失敗: %s", e)
            return pd.DataFrame()
    return _get_cached(f"vix_{days}", _do)


__all__ = [
    "fetch_taiex",
    "fetch_institutional_total",
    "fetch_margin_balance",
    "fetch_vix",
]
