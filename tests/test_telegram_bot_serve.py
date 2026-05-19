"""scripts/telegram_bot_serve.py 的 dispatch + run_once 整合測試。

getUpdates / send_telegram_message 全部 mock,確保:
  - run_once 在無 token 時 graceful 結束(回 max_update_id=0)
  - run_once 拿到 update → dispatch → 推進 last_update_id
  - dry-run 不推進 offset、不 send
  - ACL:不同 chat_id 的訊息被過濾
  - callback_query 走 notifier.handle_callback_query path
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db, notifier  # noqa: E402
from src.telegram_bot import state  # noqa: E402
from scripts import telegram_bot_serve as bot_serve  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    """乾淨 fixture:tmp_db + bot token 設好 + getUpdates / send_* 全 mock。"""
    snap = tmp_path / "twse_snapshot"
    snap.mkdir()
    db_file = tmp_path / "bot.db"
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(state, "SNAPSHOT_DIR", snap)
    monkeypatch.setattr(state, "STATE_CSV", snap / "telegram_bot_state.csv")
    db._reset_path_cache()
    db.init_db()
    yield {"snap": snap, "db": db_file}
    db._reset_path_cache()


def test_run_once_no_token_aborts(monkeypatch, tmp_path):
    """缺 token → 不打 API,直接 return。"""
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    res = bot_serve.run_once(dry=True)
    assert res["polled"] == 0
    assert res["handled"] == 0


def test_run_once_with_help_message(env, monkeypatch):
    """收到 `/help` → 推 help text(mock send),推進 last_update_id。"""
    sent: list[dict] = []

    def _fake_get(offset, **kw):
        return [
            {
                "update_id": 101,
                "message": {
                    "chat": {"id": 12345},
                    "text": "/help",
                },
            }
        ]

    def _fake_send(text, keyboard, **kw):
        sent.append({"text": text, "kb": keyboard, "kw": kw})
        return True

    monkeypatch.setattr(bot_serve, "get_updates", _fake_get)
    monkeypatch.setattr(notifier, "send_telegram_message_with_keyboard", _fake_send)

    res = bot_serve.run_once(dry=False)
    assert res["polled"] == 1
    assert res["handled"] == 1
    assert res["max_update_id"] == 101
    assert state.get_last_update_id() == 101
    # help 訊息真的有送
    assert sent
    assert "軍師指令" in sent[0]["text"]


def test_run_once_dry_does_not_advance_offset(env, monkeypatch):
    """dry-run → 不更新 offset、不 send。"""
    sent: list = []

    def _fake_get(offset, **kw):
        return [
            {
                "update_id": 202,
                "message": {"chat": {"id": 12345}, "text": "/help"},
            }
        ]

    def _fake_send(*a, **kw):
        sent.append(1)
        return True

    monkeypatch.setattr(bot_serve, "get_updates", _fake_get)
    monkeypatch.setattr(notifier, "send_telegram_message_with_keyboard", _fake_send)

    res = bot_serve.run_once(dry=True)
    assert res["max_update_id"] == 202
    # state 不應該被推進(dry)
    assert state.get_last_update_id() == 0
    # 也不應該真送
    assert not sent


def test_acl_filters_wrong_chat_id(env, monkeypatch):
    """不同 chat_id 的訊息 → handled=False(filtered),但 update_id 仍推進。"""
    sent: list = []

    def _fake_get(offset, **kw):
        return [
            {
                "update_id": 303,
                "message": {
                    "chat": {"id": 99999},   # 不在 allow-list
                    "text": "2330",
                },
            }
        ]

    monkeypatch.setattr(bot_serve, "get_updates", _fake_get)
    monkeypatch.setattr(
        notifier, "send_telegram_message_with_keyboard",
        lambda *a, **kw: sent.append(1) or True,
    )

    res = bot_serve.run_once(dry=False)
    # poll 收到 1 筆,但 ACL 擋掉 → handled=0
    assert res["polled"] == 1
    assert res["handled"] == 0
    # update_id 還是要推進,否則下次 cron 又會看到這條
    assert state.get_last_update_id() == 303
    assert not sent  # 沒送任何訊息


def test_callback_query_dispatch_to_notifier(env, monkeypatch):
    """callback_query 路徑 → answer + reply,走 notifier.handle_callback_query。"""
    answer_calls = []
    send_calls = []

    monkeypatch.setattr(
        notifier, "answer_callback_query",
        lambda cb_id, text="", **kw: answer_calls.append((cb_id, text)) or True,
    )
    monkeypatch.setattr(
        notifier, "send_telegram_message_with_keyboard",
        lambda text, keyboard, **kw: send_calls.append({"text": text, "kw": kw}) or True,
    )

    # 用 watch action,因為 it's deterministic(直接寫 watchlist)
    def _fake_get(offset, **kw):
        return [
            {
                "update_id": 404,
                "callback_query": {
                    "id": "cb-1",
                    "data": f"{notifier.CB_PREFIX_WATCH}:2330",
                    "message": {"chat": {"id": 12345}},
                },
            }
        ]

    monkeypatch.setattr(bot_serve, "get_updates", _fake_get)

    res = bot_serve.run_once(dry=False)
    assert res["handled"] == 1
    assert state.get_last_update_id() == 404
    # answerCallbackQuery 必須被呼叫(< 30s 限制)
    assert answer_calls and answer_calls[0][0] == "cb-1"
    # 跟著 reply text 推回 chat
    assert send_calls
    assert "2330" in send_calls[0]["text"]


def test_state_dump_after_run(env, monkeypatch):
    """run_once 結尾應該 dump_to_csv 持久化 offset。"""
    def _fake_get(offset, **kw):
        return [
            {
                "update_id": 505,
                "message": {"chat": {"id": 12345}, "text": "/help"},
            }
        ]

    monkeypatch.setattr(bot_serve, "get_updates", _fake_get)
    monkeypatch.setattr(
        notifier, "send_telegram_message_with_keyboard",
        lambda *a, **kw: True,
    )
    bot_serve.run_once(dry=False)
    # CSV 應該存在且帶 last_update_id
    assert state.STATE_CSV.exists()
    body = state.STATE_CSV.read_text(encoding="utf-8")
    assert "505" in body
