"""src/telegram_bot/handlers.py 單元測試。

驗各 intent 的 dispatch 與基本 fallback。AI / Telegram 全部 mock,確保純邏輯。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.telegram_bot import handlers, intent as intent_mod  # noqa: E402


# === HELP ===

def test_help_intent_returns_help_text(tmp_db):
    i = intent_mod.parse_intent("/help")
    res = handlers.handle_intent(i)
    assert "軍師指令" in res["text"]
    # help 不帶 inline keyboard
    assert res.get("reply_markup") is None


# === STOCK_QUERY ===

def test_stock_query_no_data_returns_not_found(tmp_db):
    """SQLite 空 → 找不到 sid 歷史 → graceful 訊息。"""
    i = intent_mod.parse_intent("2330")
    res = handlers.handle_intent(i)
    assert "2330" in res["text"]
    assert "找不到" in res["text"] or "❓" in res["text"]


def test_stock_query_with_data_returns_price_and_keyboard(tmp_db):
    """有 daily_prices → 回最新價 + inline keyboard。"""
    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices (stock_id, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2330", "2026-05-18", 800, 810, 795, 805.5, 12345),
        )
        conn.commit()

    i = intent_mod.parse_intent("2330")
    res = handlers.handle_intent(i)
    assert "2330" in res["text"]
    assert "805.5" in res["text"] or "805.50" in res["text"]
    # inline keyboard 必須帶 K 線 / 加關注 / 警報 / 問軍師
    kb = res.get("reply_markup")
    assert kb is not None
    assert "inline_keyboard" in kb


# === PAGE_DIGEST ===

def test_page_digest_watchlist_empty(tmp_db):
    i = intent_mod.parse_intent("關注")
    res = handlers.handle_intent(i)
    assert "關注列表是空的" in res["text"]


def test_page_digest_watchlist_with_data(tmp_db):
    db.add_to_watchlist("2330")
    db.add_to_watchlist("2317")
    i = intent_mod.parse_intent("關注")
    res = handlers.handle_intent(i)
    assert "關注列表" in res["text"]
    assert "2330" in res["text"]


def test_page_digest_positions_empty(tmp_db):
    i = intent_mod.parse_intent("持倉")
    res = handlers.handle_intent(i)
    assert "沒有未平倉" in res["text"]


def test_page_digest_picks_empty(tmp_db):
    i = intent_mod.parse_intent("推薦")
    res = handlers.handle_intent(i)
    assert "無資料" in res["text"] or "📭" in res["text"]


def test_page_digest_stats(tmp_db):
    """sanity:健康 digest 不應該 raise,就算所有表都空。"""
    i = intent_mod.parse_intent("健康")
    res = handlers.handle_intent(i)
    assert "系統健康" in res["text"]


def test_page_digest_strong_follower_empty(tmp_db):
    i = intent_mod.parse_intent("強者跟蹤")
    res = handlers.handle_intent(i)
    # 沒資料 → graceful「目前無交集」
    assert "強者跟蹤" in res["text"] or "無交集" in res["text"]


def test_page_digest_today_perf(tmp_db):
    """sanity:今天表現 digest 不應該 raise。"""
    i = intent_mod.parse_intent("今天表現")
    res = handlers.handle_intent(i)
    assert "今天表現" in res["text"]


def test_page_digest_unknown_alias_falls_through_to_freeform(tmp_db, monkeypatch):
    """未知字串 → FREEFORM → 走 ai_assistant(monkeypatched 不真打 Gemini)。"""
    from src import ai_assistant
    monkeypatch.setattr(
        ai_assistant, "is_enabled", lambda: True,
    )
    monkeypatch.setattr(
        ai_assistant, "ask_about_market",
        lambda q, conn=None: {
            "ok": True, "answer": "test-market-answer",
            "context_summary": "", "model": "x", "error": None,
        },
    )
    i = intent_mod.parse_intent("半導體最近熱嗎")
    res = handlers.handle_intent(i)
    assert "test-market-answer" in res["text"]


# === FREEFORM ===

def test_freeform_with_sid_routes_to_stock_assistant(tmp_db, monkeypatch):
    from src import ai_assistant
    captured = {}

    def _fake(sid, q, conn=None):
        captured["sid"] = sid
        captured["q"] = q
        return {
            "ok": True, "answer": "stock-answer-for-test",
            "context_summary": "", "model": "x", "error": None,
        }

    monkeypatch.setattr(ai_assistant, "is_enabled", lambda: True)
    monkeypatch.setattr(ai_assistant, "ask_about_stock", _fake)
    i = intent_mod.parse_intent("2330 怎麼樣")
    res = handlers.handle_intent(i)
    assert captured["sid"] == "2330"
    assert "stock-answer-for-test" in res["text"]


def test_freeform_when_ai_disabled(tmp_db, monkeypatch):
    from src import ai_assistant
    monkeypatch.setattr(ai_assistant, "is_enabled", lambda: False)
    i = intent_mod.parse_intent("聊聊大盤")
    res = handlers.handle_intent(i)
    assert "下班" in res["text"] or "💤" in res["text"]


def test_freeform_when_ai_raises(tmp_db, monkeypatch):
    """ai_assistant raise → handler 必須 graceful 回錯誤訊息,不 propagate。"""
    from src import ai_assistant
    monkeypatch.setattr(ai_assistant, "is_enabled", lambda: True)

    def _boom(q, conn=None):
        raise RuntimeError("gemini quota")

    monkeypatch.setattr(ai_assistant, "ask_about_market", _boom)
    i = intent_mod.parse_intent("聊聊大盤")
    res = handlers.handle_intent(i)
    assert "失敗" in res["text"] or "RuntimeError" in res["text"]
