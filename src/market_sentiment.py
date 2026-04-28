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


def compute_total_net_per_day(raw_df: pd.DataFrame) -> pd.DataFrame:
    """從 fetch_institutional_total 的 raw 抽出「每日法人合計買賣超」(億元)。

    Raw schema: 每日 6 筆 → 5 個法人(Foreign_Investor / Investment_Trust /
    Dealer_self / Dealer_Hedging / Foreign_Dealer_Self)+ 1 筆 name='total'(已是加總)。

    取 'total' 那筆即可;若 FinMind 某天漏給 total → fallback 對 5 法人 SUM。
    回傳 DataFrame[date, net] (淨買超億元,正 = 買超、負 = 賣超)。

    驗證:正確 net 範圍應在 ±1000 億內,典型日約 ±300 億。
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=["date", "net"])

    total_rows = raw_df[raw_df["name"] == "total"]
    if not total_rows.empty:
        out = total_rows[["date"]].copy()
        out["net"] = (
            total_rows["buy"].fillna(0).values
            - total_rows["sell"].fillna(0).values
        ) / 1e8
    else:
        # Fallback:FinMind 沒給 'total' 列 → 對 5 個分項法人 SUM
        agg = raw_df.groupby("date").agg(
            buy_sum=("buy", "sum"),
            sell_sum=("sell", "sum"),
        ).reset_index()
        out = agg[["date"]].copy()
        out["net"] = (agg["buy_sum"] - agg["sell_sum"]) / 1e8

    return out.sort_values("date").reset_index(drop=True)


__all__ = [
    "fetch_taiex",
    "fetch_institutional_total",
    "fetch_margin_balance",
    "fetch_vix",
    "compute_total_net_per_day",
]
