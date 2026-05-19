"""src/telegram_bot/intent.py 單元測試(2026-05-18 加,Telegram 雙向問答 daemon)。

純函式測試,不打 API、不讀 DB。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.telegram_bot import intent  # noqa: E402


def test_empty_text_is_help():
    i = intent.parse_intent("")
    assert i.kind == intent.INTENT_HELP

    i = intent.parse_intent(None)
    assert i.kind == intent.INTENT_HELP


def test_whitespace_only_is_help():
    i = intent.parse_intent("   \n\t ")
    assert i.kind == intent.INTENT_HELP


def test_slash_help_is_help():
    for s in ("/help", "/start", "help", "?", "？"):
        assert intent.parse_intent(s).kind == intent.INTENT_HELP, f"failed: {s}"


def test_pure_sid_is_stock_query():
    for txt in ("2330", "2317", "00878", "$2330", "  2330  ", "2330.TW", "2330.tw"):
        i = intent.parse_intent(txt)
        assert i.kind == intent.INTENT_STOCK_QUERY, f"failed: {txt!r}"
        assert i.sid is not None
        assert i.sid.isdigit()


def test_sid_with_twoboard_suffix():
    i = intent.parse_intent("6488.TWO")
    assert i.kind == intent.INTENT_STOCK_QUERY
    assert i.sid == "6488"


def test_page_aliases_strong_follower():
    for txt in ("強者跟蹤", "強者", "follower", "/follower"):
        i = intent.parse_intent(txt)
        assert i.kind == intent.INTENT_PAGE_DIGEST, f"failed: {txt}"
        assert i.page == "strong_follower"


def test_page_aliases_today_perf():
    for txt in ("今天表現", "今天大盤", "大盤", "today", "market"):
        i = intent.parse_intent(txt)
        assert i.kind == intent.INTENT_PAGE_DIGEST, f"failed: {txt}"
        assert i.page == "today_perf"


def test_page_aliases_watchlist():
    for txt in ("關注", "關注列表", "watchlist", "/watchlist"):
        i = intent.parse_intent(txt)
        assert i.kind == intent.INTENT_PAGE_DIGEST
        assert i.page == "watchlist"


def test_page_aliases_positions_picks_stats():
    assert intent.parse_intent("持倉").page == "positions"
    assert intent.parse_intent("推薦").page == "picks"
    assert intent.parse_intent("健康").page == "stats"
    assert intent.parse_intent("status").page == "stats"


def test_freeform_with_sid_detection():
    """自由問題內若含 sid,intent.sid 撈出來;kind 仍是 FREEFORM。"""
    i = intent.parse_intent("2330 怎麼樣")
    assert i.kind == intent.INTENT_FREEFORM
    assert i.sid == "2330"


def test_freeform_without_sid():
    i = intent.parse_intent("半導體最近熱嗎")
    assert i.kind == intent.INTENT_FREEFORM
    assert i.sid is None


def test_detect_sid_helper():
    assert intent.detect_sid("2330 怎麼樣") == "2330"
    assert intent.detect_sid("聊聊台積") is None
    assert intent.detect_sid("") is None
    assert intent.detect_sid(None) is None


def test_help_text_lists_commands():
    txt = intent.help_text()
    # 黃金路徑必含 key 字眼
    assert "軍師指令" in txt
    assert "強者跟蹤" in txt
    assert "關注" in txt
    assert "2330" in txt


def test_intent_preserves_raw_text():
    """raw_text 是 strip 後的版本(parse_intent 先 strip,便於 logging)。"""
    i = intent.parse_intent("  2330  ")
    assert i.raw_text == "2330"
    assert i.sid == "2330"
