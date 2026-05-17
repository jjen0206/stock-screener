"""src/discord_bot.py 單元測試(2026-05-17 加,C 任務)。

涵蓋:
  - is_enabled kill-switch / config 缺欄回 False
  - get_slash_command_definitions schema 對齊 Discord 規格
  - handle_interaction dispatch picks / watchlist / chart / stats / positions
    / alert / ask / 未知指令
  - verify_signature graceful 處理缺 key / 缺 PyNaCl
  - _detect_sid + _sparkline 純函式
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from src import discord_bot, config, database as db  # noqa: E402


def _force_enable(monkeypatch):
    """讓 is_enabled() 回 True(grant 3 個 token)。"""
    monkeypatch.setenv("DISCORD_BOT_ENABLED", "true")
    monkeypatch.setattr(config, "DISCORD_APPLICATION_ID", "app123")
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "bot456")
    monkeypatch.setattr(config, "DISCORD_PUBLIC_KEY", "pk789")


# === is_enabled ===

def test_is_enabled_requires_all_tokens(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_ENABLED", "true")
    monkeypatch.setattr(config, "DISCORD_APPLICATION_ID", "")
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "")
    monkeypatch.setattr(config, "DISCORD_PUBLIC_KEY", "")
    assert discord_bot.is_enabled() is False


def test_is_enabled_kill_switch(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_ENABLED", "false")
    monkeypatch.setattr(config, "DISCORD_APPLICATION_ID", "app")
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "bot")
    monkeypatch.setattr(config, "DISCORD_PUBLIC_KEY", "pk")
    assert discord_bot.is_enabled() is False


def test_is_enabled_happy_path(monkeypatch):
    _force_enable(monkeypatch)
    assert discord_bot.is_enabled() is True


# === slash command definitions ===

def test_get_slash_command_definitions_required_set():
    defs = discord_bot.get_slash_command_definitions()
    names = {d["name"] for d in defs}
    # 7 個必備指令
    assert names == {"picks", "watchlist", "chart", "stats", "positions", "alert", "ask"}
    # 全部 type=1 (CHAT_INPUT)
    assert all(d["type"] == 1 for d in defs)


def test_chart_command_has_required_sid():
    defs = discord_bot.get_slash_command_definitions()
    chart = next(d for d in defs if d["name"] == "chart")
    assert chart["options"][0]["name"] == "sid"
    assert chart["options"][0]["required"] is True


def test_alert_command_type_choices():
    defs = discord_bot.get_slash_command_definitions()
    alert = next(d for d in defs if d["name"] == "alert")
    type_opt = next(o for o in alert["options"] if o["name"] == "type")
    values = {c["value"] for c in type_opt["choices"]}
    assert values == {"price_above", "price_below"}


# === verify_signature graceful ===

def test_verify_signature_no_public_key(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_PUBLIC_KEY", "")
    assert discord_bot.verify_signature(None, "sig", "ts", b"body") is False


def test_verify_signature_bad_hex(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_PUBLIC_KEY", "deadbeef")
    # PyNaCl 可能沒裝 → False;裝了會走 verify 路徑也 False。
    assert discord_bot.verify_signature(None, "garbage", "0", b"body") is False


# === handle_interaction dispatch ===

def test_handle_ping_returns_pong(monkeypatch):
    _force_enable(monkeypatch)
    resp = discord_bot.handle_interaction({"type": discord_bot.INTERACTION_PING})
    assert resp == {"type": discord_bot.RESP_PONG}


def test_handle_interaction_disabled_returns_message(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_ENABLED", "false")
    resp = discord_bot.handle_interaction({"type": discord_bot.INTERACTION_PING})
    assert resp["type"] == discord_bot.RESP_CHANNEL_MESSAGE
    assert "停用" in resp["data"]["content"] or "下班" in resp["data"]["content"]


def test_handle_unknown_command(monkeypatch):
    _force_enable(monkeypatch)
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {"name": "totally_not_a_command"},
    }
    resp = discord_bot.handle_interaction(payload)
    assert resp["type"] == discord_bot.RESP_CHANNEL_MESSAGE
    assert "未知" in resp["data"]["content"]


def test_handle_picks_empty_db(monkeypatch, tmp_db):
    _force_enable(monkeypatch)
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {"name": "picks"},
    }
    resp = discord_bot.handle_interaction(payload)
    assert resp["type"] == discord_bot.RESP_CHANNEL_MESSAGE
    # 空表時友善訊息
    assert "📭" in resp["data"]["content"] or "無" in resp["data"]["content"]


def test_handle_picks_with_data(monkeypatch, tmp_db):
    _force_enable(monkeypatch)
    # seed daily_picks
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_picks (trade_date, universe, strategy, sid, "
            "score, params_hash, computed_at, ml_prob) VALUES "
            "('2026-04-30', 'top50', 'volume_breakout', '2330', 0.9, 'h', "
            "'2026-04-30T00:00:00', 0.85)"
        )
        conn.commit()
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {"name": "picks"},
    }
    resp = discord_bot.handle_interaction(payload)
    assert "2330" in resp["data"]["content"]


def test_handle_watchlist_empty(monkeypatch, tmp_db):
    _force_enable(monkeypatch)
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {"name": "watchlist"},
    }
    resp = discord_bot.handle_interaction(payload)
    assert "空的" in resp["data"]["content"] or "📭" in resp["data"]["content"]


def test_handle_chart_no_data(monkeypatch, tmp_db):
    _force_enable(monkeypatch)
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {
            "name": "chart",
            "options": [{"name": "sid", "value": "9999"}],
        },
    }
    resp = discord_bot.handle_interaction(payload)
    assert "找不到" in resp["data"]["content"]


def test_handle_chart_with_data(monkeypatch, tmp_db):
    _force_enable(monkeypatch)
    with db.get_conn() as conn:
        for i, d in enumerate(
            ["2026-04-28", "2026-04-29", "2026-04-30"]
        ):
            conn.execute(
                "INSERT INTO daily_prices (stock_id, date, open, high, low, "
                "close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2330", d, 500, 510, 495, 500 + i, 10000),
            )
        conn.commit()
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {
            "name": "chart",
            "options": [{"name": "sid", "value": "2330"}],
        },
    }
    resp = discord_bot.handle_interaction(payload)
    content = resp["data"]["content"]
    assert "2330" in content
    # ASCII sparkline 用 unicode block
    assert any(ch in content for ch in "▁▂▃▄▅▆▇█")


def test_handle_alert_writes_row(monkeypatch, tmp_db):
    _force_enable(monkeypatch)
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {
            "name": "alert",
            "options": [
                {"name": "sid", "value": "2330"},
                {"name": "type", "value": "price_above"},
                {"name": "value", "value": 700.0},
            ],
        },
    }
    resp = discord_bot.handle_interaction(payload)
    assert "已設定" in resp["data"]["content"]
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM price_alerts WHERE stock_id='2330'"
        ).fetchone()
        assert row is not None
        assert row["alert_type"] == "price_above"
        assert row["target_value"] == 700.0
        assert row["is_active"] == 1


def test_handle_alert_rejects_bad_type(monkeypatch, tmp_db):
    _force_enable(monkeypatch)
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {
            "name": "alert",
            "options": [
                {"name": "sid", "value": "2330"},
                {"name": "type", "value": "not_supported"},
                {"name": "value", "value": 700.0},
            ],
        },
    }
    resp = discord_bot.handle_interaction(payload)
    assert "不支援" in resp["data"]["content"]


def test_handle_positions_empty(monkeypatch, tmp_db):
    _force_enable(monkeypatch)
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {"name": "positions"},
    }
    resp = discord_bot.handle_interaction(payload)
    assert "未平倉" in resp["data"]["content"] or "📭" in resp["data"]["content"]


def test_handle_stats(monkeypatch, tmp_db):
    _force_enable(monkeypatch)
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {"name": "stats"},
    }
    resp = discord_bot.handle_interaction(payload)
    assert "系統健康" in resp["data"]["content"]
    assert "Cache" in resp["data"]["content"]


def test_handle_ask_calls_ai_assistant(monkeypatch, tmp_db):
    _force_enable(monkeypatch)
    from src import ai_assistant
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "true")
    # mock 出回應
    monkeypatch.setattr(
        ai_assistant, "ask_about_stock",
        lambda sid, q, conn=None: {
            "ok": True, "answer": "📊 多頭",
            "context_summary": "", "model": "x", "error": None,
        },
    )
    monkeypatch.setattr(
        ai_assistant, "ask_about_market",
        lambda q, conn=None: {
            "ok": True, "answer": "📊 大盤強",
            "context_summary": "", "model": "x", "error": None,
        },
    )
    # 帶 sid 走 stock
    payload = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {
            "name": "ask",
            "options": [{"name": "question", "value": "2330 怎麼樣"}],
        },
    }
    resp = discord_bot.handle_interaction(payload)
    assert "多頭" in resp["data"]["content"]
    # 不帶 sid 走 market
    payload2 = {
        "type": discord_bot.INTERACTION_APP_COMMAND,
        "data": {
            "name": "ask",
            "options": [{"name": "question", "value": "今天大盤怎樣"}],
        },
    }
    resp2 = discord_bot.handle_interaction(payload2)
    assert "大盤強" in resp2["data"]["content"]


# === 純函式 ===

def test_detect_sid():
    from src.discord_bot import _detect_sid
    assert _detect_sid("2330 怎麼樣") == "2330"
    assert _detect_sid("看一下 0050") == "0050"
    assert _detect_sid("沒有 sid") is None
    assert _detect_sid("") is None


def test_sparkline_basic():
    from src.discord_bot import _sparkline
    out = _sparkline([1.0, 2.0, 3.0, 4.0, 5.0])
    # 5 字元
    assert len(out) == 5
    # 上升 → 越來越高的 block
    blocks = "▁▂▃▄▅▆▇█"
    assert blocks.index(out[0]) < blocks.index(out[-1])


def test_sparkline_flat():
    from src.discord_bot import _sparkline
    out = _sparkline([3.0, 3.0, 3.0])
    # 全一樣 → 中間 block × 3
    assert len(out) == 3
