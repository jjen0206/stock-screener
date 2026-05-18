"""把使用者打的 Telegram 訊息字串 → 結構化 intent。

設計原則:
- pure function,沒副作用,可單測試
- 解析錯誤一律 fallback 到 HELP(不 raise)
- 不打 API、不讀 DB(交給 handlers.py)

公開:
- parse_intent(text: str) -> Intent

Intent 種類:
- STOCK_QUERY    純股票代號 (4-6 位數字,可含 .TW / .TWO 後綴)
- PAGE_DIGEST    中文/英文頁面別名(強者跟蹤、今天表現、關注 …)
- HELP           /help、/start、空字串、不認得
- FREEFORM       其他自然語言問題(走 Gemini)
- CALLBACK       internal:callback_query 走 src.notifier.handle_callback_query
                 (此檔不解析,只是給 handlers.py 區別)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Page-digest 中英文別名 → 內部代號
# key 是 normalize 後的字串(lowercase + strip),value 是 handlers.py 認得的代號
_PAGE_ALIASES: dict[str, str] = {
    # 強者跟蹤(法人共識榜)
    "強者跟蹤": "strong_follower",
    "強者": "strong_follower",
    "follower": "strong_follower",
    "strong": "strong_follower",
    # 今天表現 / 大盤(regime + sentiment + picks 數)
    "今天表現": "today_perf",
    "今天大盤": "today_perf",
    "大盤": "today_perf",
    "today": "today_perf",
    "market": "today_perf",
    # 關注列表
    "關注": "watchlist",
    "關注列表": "watchlist",
    "watchlist": "watchlist",
    # 持倉
    "持倉": "positions",
    "持倉部位": "positions",
    "positions": "positions",
    # 健康診斷
    "健康": "stats",
    "系統健康": "stats",
    "stats": "stats",
    "status": "stats",
    # 今日推薦 picks
    "推薦": "picks",
    "picks": "picks",
    "今日推薦": "picks",
}

INTENT_STOCK_QUERY = "STOCK_QUERY"
INTENT_PAGE_DIGEST = "PAGE_DIGEST"
INTENT_HELP = "HELP"
INTENT_FREEFORM = "FREEFORM"


@dataclass
class Intent:
    kind: str  # 上面四個常數之一
    sid: str | None = None           # STOCK_QUERY 時帶
    page: str | None = None          # PAGE_DIGEST 時帶 (strong_follower / today_perf / ...)
    raw_text: str = ""               # 原始字串
    extra: dict = field(default_factory=dict)


# 純 4-6 位數字(允許 .TW / .TWO 後綴),純股票代號 → STOCK_QUERY
_SID_PATTERN = re.compile(r"^(?P<sid>\d{4,6})(?:\.(?:TW|TWO))?$", re.IGNORECASE)

# 用於 FREEFORM 內偵測 sid(handlers 用)
_SID_INLINE_PATTERN = re.compile(r"\b(\d{4,6})\b")


def detect_sid(text: str) -> str | None:
    """從 free-form 文字撈 4-6 位數字當 sid。回 None 表示沒撈到。"""
    if not text:
        return None
    m = _SID_INLINE_PATTERN.search(text)
    return m.group(1) if m else None


def parse_intent(text: str | None) -> Intent:
    """純函式:user message → Intent dataclass。失敗一律 HELP(不 raise)。"""
    raw = (text or "").strip()
    if not raw:
        return Intent(kind=INTENT_HELP, raw_text=raw)

    # 1. /help / /start / /ping
    low = raw.lower()
    if low in ("/help", "/start", "help", "?", "？"):
        return Intent(kind=INTENT_HELP, raw_text=raw)

    # 2. 純股票代號(去掉 leading $ 等常見前綴)
    cleaned = raw.lstrip("$#").strip()
    m = _SID_PATTERN.match(cleaned)
    if m:
        return Intent(
            kind=INTENT_STOCK_QUERY,
            sid=m.group("sid"),
            raw_text=raw,
        )

    # 3. Page alias(strip slash if any)
    alias_key = low.lstrip("/").strip()
    page = _PAGE_ALIASES.get(alias_key)
    if page:
        return Intent(
            kind=INTENT_PAGE_DIGEST,
            page=page,
            raw_text=raw,
        )

    # 4. 其他 → FREEFORM(可能內含 sid,handlers 自己再 detect_sid)
    sid_inline = detect_sid(raw)
    return Intent(
        kind=INTENT_FREEFORM,
        sid=sid_inline,
        raw_text=raw,
    )


def help_text() -> str:
    """`/help` 訊息 — 列可用指令。Markdown-safe(不用底線等 Telegram special)。"""
    return (
        "🤖 *軍師指令清單*\n"
        "\n"
        "📈 *股票查詢*\n"
        "  • 直接打代號:`2330` / `2317`\n"
        "  • Bot 會回最新價 + 軍師判讀\n"
        "\n"
        "📊 *頁面摘要*\n"
        "  • `強者跟蹤`  — 法人共識 + 千張大戶 Top 5\n"
        "  • `今天表現`  — 大盤 regime + sentiment\n"
        "  • `關注`      — 你的 ☆ 關注列表現價\n"
        "  • `持倉`      — 未平倉部位 + 損益\n"
        "  • `推薦`      — 今日 picks Top 10\n"
        "  • `健康`      — 系統健康 + 30 天 hit rate\n"
        "\n"
        "💬 *自然語言*\n"
        "  • 任何問題會走 Gemini(例:「半導體最近怎麼樣」)\n"
        "  • 帶代號的問題會走個股軍師(例:「2330 怎麼樣」)\n"
        "\n"
        "⚠️ 僅供研究,非投資建議"
    )


__all__ = [
    "Intent",
    "INTENT_STOCK_QUERY",
    "INTENT_PAGE_DIGEST",
    "INTENT_HELP",
    "INTENT_FREEFORM",
    "parse_intent",
    "detect_sid",
    "help_text",
]
