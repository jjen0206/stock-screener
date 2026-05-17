"""src/notifier.py inline keyboard 支援單元測試(2026-05-17 加,C 任務)。

涵蓋:
  - build_stock_inline_keyboard 結構
  - send_telegram_message_with_keyboard payload 帶 reply_markup
  - handle_callback_query dispatch 各 action
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from src import notifier, config, database as db  # noqa: E402


# === build_stock_inline_keyboard ===

def test_build_stock_inline_keyboard_structure():
    kb = notifier.build_stock_inline_keyboard("2330")
    # 必有 inline_keyboard key
    assert "inline_keyboard" in kb
    # 一排 4 顆按鈕
    row = kb["inline_keyboard"][0]
    assert len(row) == 4
    # 每顆有 text + callback_data
    for btn in row:
        assert "text" in btn
        assert "callback_data" in btn
        assert ":" in btn["callback_data"]
        # callback_data 不能超過 64 byte(Telegram 規格)
        assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_build_stock_inline_keyboard_actions():
    kb = notifier.build_stock_inline_keyboard("2330")
    actions = {b["callback_data"].split(":")[0] for b in kb["inline_keyboard"][0]}
    assert actions == {
        notifier.CB_PREFIX_CHART,
        notifier.CB_PREFIX_WATCH,
        notifier.CB_PREFIX_ALERT,
        notifier.CB_PREFIX_ASK,
    }


# === send_telegram_message_with_keyboard ===

def test_send_telegram_with_keyboard_skips_when_no_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    kb = notifier.build_stock_inline_keyboard("2330")
    assert notifier.send_telegram_message_with_keyboard("msg", kb) is False


def test_send_telegram_with_keyboard_includes_reply_markup(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "cid")
    kb = notifier.build_stock_inline_keyboard("2330")
    captured = {}

    def _fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        m = MagicMock()
        m.status_code = 200
        return m

    monkeypatch.setattr(notifier.requests, "post", _fake_post)
    ok = notifier.send_telegram_message_with_keyboard("msg", kb)
    assert ok is True
    assert captured["payload"]["chat_id"] == "cid"
    assert captured["payload"]["text"] == "msg"
    assert captured["payload"]["reply_markup"] == kb


def test_send_telegram_no_keyboard_works(monkeypatch):
    """keyboard=None → 不帶 reply_markup,行為等同 send_telegram_message。"""
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "cid")
    captured = {}

    def _fake_post(url, json=None, timeout=None):
        captured["payload"] = json
        m = MagicMock()
        m.status_code = 200
        return m

    monkeypatch.setattr(notifier.requests, "post", _fake_post)
    notifier.send_telegram_message_with_keyboard("msg", None)
    assert "reply_markup" not in captured["payload"]


# === answer_callback_query ===

def test_answer_callback_query_skips_no_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    assert notifier.answer_callback_query("cb-id-123") is False


def test_answer_callback_query_posts(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    captured = {}

    def _fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        m = MagicMock()
        m.status_code = 200
        return m

    monkeypatch.setattr(notifier.requests, "post", _fake_post)
    ok = notifier.answer_callback_query("cb-id-123", text="ok")
    assert ok is True
    assert "answerCallbackQuery" in captured["url"]
    assert captured["json"]["callback_query_id"] == "cb-id-123"
    assert captured["json"]["text"] == "ok"


# === handle_callback_query ===

def test_handle_callback_query_invalid(monkeypatch, tmp_db):
    update = {"callback_query": {"id": "cb1", "data": "garbage_no_colon"}}
    res = notifier.handle_callback_query(update)
    assert res["ok"] is False


def test_handle_callback_query_watch_adds_to_watchlist(monkeypatch, tmp_db):
    update = {
        "callback_query": {
            "id": "cb1",
            "data": f"{notifier.CB_PREFIX_WATCH}:2330",
        }
    }
    res = notifier.handle_callback_query(update)
    assert res["ok"] is True
    assert res["action"] == notifier.CB_PREFIX_WATCH
    assert res["sid"] == "2330"
    # watchlist 內應該已經有 2330
    wl = db.get_watchlist()
    assert any(w["stock_id"] == "2330" for w in wl)


def test_handle_callback_query_alert_returns_guidance(monkeypatch, tmp_db):
    update = {
        "callback_query": {
            "id": "cb1",
            "data": f"{notifier.CB_PREFIX_ALERT}:2330",
        }
    }
    res = notifier.handle_callback_query(update)
    assert res["ok"] is True
    assert "警報" in res["reply_text"]
    assert "2330" in res["reply_text"]


def test_handle_callback_query_chart_renders(monkeypatch, tmp_db):
    """有資料時 → chart action 渲染 sparkline。"""
    with db.get_conn() as conn:
        for i, d in enumerate(["2026-04-28", "2026-04-29", "2026-04-30"]):
            conn.execute(
                "INSERT INTO daily_prices (stock_id, date, open, high, low, "
                "close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2330", d, 500, 510, 495, 500 + i, 10000),
            )
        conn.commit()
    update = {
        "callback_query": {
            "id": "cb1",
            "data": f"{notifier.CB_PREFIX_CHART}:2330",
        }
    }
    res = notifier.handle_callback_query(update)
    assert res["ok"] is True
    assert "2330" in res["reply_text"]


def test_handle_callback_query_ask_uses_ai(monkeypatch, tmp_db):
    """ask action 走 ai_assistant.ask_about_stock。"""
    from src import ai_assistant
    monkeypatch.setattr(
        ai_assistant, "ask_about_stock",
        lambda sid, q, conn=None: {
            "ok": True, "answer": "📊 多頭", "context_summary": "",
            "model": "x", "error": None,
        },
    )
    update = {
        "callback_query": {
            "id": "cb1",
            "data": f"{notifier.CB_PREFIX_ASK}:2330",
        }
    }
    res = notifier.handle_callback_query(update)
    assert res["ok"] is True
    assert "軍師" in res["reply_text"]
    assert "多頭" in res["reply_text"]


def test_handle_callback_query_unknown_action(monkeypatch, tmp_db):
    update = {
        "callback_query": {
            "id": "cb1",
            "data": "weird_action:2330",
        }
    }
    res = notifier.handle_callback_query(update)
    assert res["ok"] is True  # 框架接住,只是回 reply_text 含未知字樣
    assert "未知" in res["reply_text"]
