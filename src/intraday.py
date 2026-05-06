"""盤中追蹤(即時漲跌)— 用 yfinance 抓台股盤中即時價。

設計:
- 範圍只 watchlist + 今日 picks(yfinance 對 TW 個股有 rate limit,不抓全市場)
- 5 分鐘 cache(@st.cache_data ttl=300),App 端 user-pull
- 只在台股交易時段(09:00-13:30 台北)有意義,盤後/週末只顯昨收
- 失敗(rate limit / 假日 / 個股下市)→ 該 sid 回 None,App 端 fallback 不顯

注意:**yfinance 對台股 close 價有偏差**(vs TWSE 官方),只當「即時參考」,
不當「收盤」用。盤後的 regularMarketPrice 是當日收盤,跟 TWSE 仍可能差幾分。
"""
from __future__ import annotations

import logging
from datetime import datetime, time as _time
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# 台股交易時段(台北時區)。盤前(08:30-09:00)+ 盤中 09:00-13:30 + 盤後 5 分撮合。
_TPE_TZ = ZoneInfo("Asia/Taipei")
_MARKET_OPEN = _time(9, 0)
_MARKET_CLOSE = _time(13, 35)  # 含 5 分撮合


def is_market_hours(now: datetime | None = None) -> bool:
    """是否在台股盤中交易時段(台北 09:00-13:35,週一至週五,不算國定假日)。

    國定假日 → 還是回 True(不額外查;反正 yfinance 會回 stale 資料,App 端 fallback)。
    now=None 取現在時間。
    """
    if now is None:
        now = datetime.now(_TPE_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TPE_TZ)
    elif now.tzinfo != _TPE_TZ:
        now = now.astimezone(_TPE_TZ)
    if now.weekday() >= 5:  # 週末
        return False
    t = now.time()
    return _MARKET_OPEN <= t <= _MARKET_CLOSE


def _yf_symbol(stock_id: str) -> str:
    """台股 sid → yfinance symbol(2330 → 2330.TW;上櫃用 .TWO,但本工具範圍主要上市)。"""
    return f"{stock_id}.TW"


def get_intraday_quote(
    sids: list[str],
    timeout: float = 8.0,
) -> dict[str, dict | None]:
    """批次抓多檔即時價(透過 yfinance Ticker.info)。

    回 {sid: {prev_close, current, change_pct, volume, timestamp} | None}。
    None 表示該檔抓失敗(yfinance rate limit / sid 不存在 / 網路掛)。

    timeout 是給單檔的,total = timeout × len(sids)。caller 該 cache 結果。
    """
    import yfinance as yf

    out: dict[str, dict | None] = {}
    for sid in sids:
        sym = _yf_symbol(sid)
        try:
            t = yf.Ticker(sym)
            info = t.info or {}
            prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
            current = info.get("regularMarketPrice") or info.get("currentPrice")
            volume = info.get("regularMarketVolume") or info.get("volume")
            if not prev_close or not current:
                out[sid] = None
                continue
            change_pct = (
                (float(current) - float(prev_close)) / float(prev_close) * 100
                if float(prev_close) > 0 else None
            )
            ts = info.get("regularMarketTime")
            out[sid] = {
                "prev_close": float(prev_close),
                "current": float(current),
                "change_pct": float(change_pct) if change_pct is not None else None,
                "volume": int(volume) if volume else None,
                "timestamp": int(ts) if ts else None,
            }
        except Exception as e:  # noqa: BLE001
            logger.debug("[INTRADAY] %s 抓失敗 %s: %s", sid, type(e).__name__, e)
            out[sid] = None
    return out


def format_intraday_line(
    quote: dict | None,
    fallback_close: float | None = None,
) -> str:
    """組單行盤中行字串給 UI 顯。quote=None / 非交易時段 → 回空字串。

    格式:`📡 9.15 (↑14.7%)`(漲)/ `📡 9.15 (↓1.5%)`(跌)/ `📡 9.15 (→0.0%)`(平)。
    """
    if not quote or quote.get("current") is None:
        return ""
    current = float(quote["current"])
    change_pct = quote.get("change_pct")
    if change_pct is None:
        return f"📡 {current:.2f}"
    arrow = "↑" if change_pct > 0 else ("↓" if change_pct < 0 else "→")
    return f"📡 {current:.2f} ({arrow}{abs(change_pct):.1f}%)"


__all__ = [
    "is_market_hours",
    "get_intraday_quote",
    "format_intraday_line",
]
