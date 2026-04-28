"""src/discord_notifier.py 單元測試。

策略:mock requests.post,確保不打 Discord 真實 webhook。
"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pandas as pd
import pytest
import requests

from src import config
from src import discord_notifier as dn


@pytest.fixture
def with_webhook(monkeypatch):
    monkeypatch.setattr(
        config, "DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/12345/abcdef",
    )


def _make_pick_row(stock_id="2330", name="台積電", **kw):
    base = {
        "stock_id": stock_id, "name": name,
        "close": 779.0, "volume": 30000, "ma_volume_5": 20000,
        "k": 52.0, "d": 48.0, "inst_total_3d": 5_500_000,
        "matched_at": "2026-04-25",
    }
    base.update(kw)
    return base


# === send_discord_message ===

def test_send_discord_success(with_webhook):
    fake = Mock(); fake.status_code = 204  # Discord 通常回 204
    with patch("src.discord_notifier.requests.post", return_value=fake) as m:
        ok = dn.send_discord_message("hello")
    assert ok is True
    args, kwargs = m.call_args
    assert "discord.com" in args[0]
    body = kwargs["json"]
    assert body["content"] == "hello"
    assert body["username"] == "Stock Screener"


def test_send_discord_missing_webhook_returns_false(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    with patch("src.discord_notifier.requests.post") as m:
        ok = dn.send_discord_message("hello")
    assert ok is False
    m.assert_not_called()


def test_send_discord_explicit_url_overrides(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    fake = Mock(); fake.status_code = 200
    with patch("src.discord_notifier.requests.post", return_value=fake) as m:
        ok = dn.send_discord_message(
            "hi", webhook_url="https://discord.com/api/webhooks/explicit",
        )
    assert ok is True
    assert "explicit" in m.call_args.args[0]


def test_send_discord_truncates_long_message(with_webhook):
    """Discord 訊息上限 2000 字,過長要截斷。"""
    long = "x" * 3000
    fake = Mock(); fake.status_code = 204
    with patch("src.discord_notifier.requests.post", return_value=fake) as m:
        dn.send_discord_message(long)
    body = m.call_args.kwargs["json"]
    assert len(body["content"]) <= 2000
    assert "已截斷" in body["content"]


def test_send_discord_http_error_returns_false(with_webhook):
    fake = Mock(); fake.status_code = 401; fake.text = "Unauthorized"
    with patch("src.discord_notifier.requests.post", return_value=fake):
        ok = dn.send_discord_message("hi")
    assert ok is False


def test_send_discord_network_error_returns_false(with_webhook):
    """requests 失敗 → fallback httpx,httpx 也失敗 → 回 False。"""
    with patch(
        "src.discord_notifier.requests.post",
        side_effect=requests.ConnectionError("net boom"),
    ), patch(
        "httpx.Client",
        side_effect=RuntimeError("httpx boom"),
    ):
        ok = dn.send_discord_message("hi")
    assert ok is False


# === format_short_picks_discord ===

def test_format_short_picks_discord_with_data():
    df = pd.DataFrame([
        _make_pick_row("2330", "台積電"),
        _make_pick_row("2454", "聯發科", close=1200, volume=5000,
                       ma_volume_5=3000, k=35, d=30),
    ])
    msg = dn.format_short_picks_discord(df, "2026-04-25")
    assert "stock-screener" in msg
    assert "2026-04-25" in msg
    assert "(2 檔)" in msg
    assert "2330" in msg and "台積電" in msg
    assert "2454" in msg
    # Discord Markdown 粗體
    assert "**" in msg
    assert "⚠️" in msg


def test_format_short_picks_discord_empty():
    msg = dn.format_short_picks_discord(pd.DataFrame(), "2026-04-25")
    assert "無符合條件" in msg
    assert "2026-04-25" in msg
    assert "stock-screener" in msg


# === notify_short_picks_discord ===

def test_notify_short_picks_discord_calls_send(with_webhook, monkeypatch):
    fake_picks = pd.DataFrame([_make_pick_row()])
    monkeypatch.setattr(
        dn, "screen_short",
        lambda d, params=None, stock_ids=None: fake_picks,
    )
    fake = Mock(); fake.status_code = 204
    with patch("src.discord_notifier.requests.post", return_value=fake) as m:
        ok = dn.notify_short_picks_discord(date="2026-04-25")
    assert ok is True
    sent_text = m.call_args.kwargs["json"]["content"]
    assert "2330" in sent_text


# === format_multi_strategy_picks_discord ===

def test_format_multi_strategy_discord_sorted_by_signals():
    """信號多的排前 + 🔥 數量對應。"""
    agg = {
        "A": {"name": "甲", "signals": ["量價KD"],
              "details": {"volume_kd": {"close": 100}}},
        "B": {"name": "乙", "signals": ["量價KD", "多頭排列"],
              "details": {"volume_kd": {"close": 200}}},
    }
    msg = dn.format_multi_strategy_picks_discord(agg, "2026-04-25")
    # B 該排在 A 之前(信號數多)
    pos_b = msg.find("**B 乙**")
    pos_a = msg.find("**A 甲**")
    assert 0 < pos_b < pos_a
    # 🔥 數量
    assert "🔥🔥" in msg


def test_format_multi_strategy_discord_empty():
    msg = dn.format_multi_strategy_picks_discord({}, "2026-04-25")
    assert "無任一策略" in msg


def test_format_multi_strategy_discord_includes_targets():
    """有 target_low/high/stop 該印「🎯 目標 ... / 🛑 停損 ...」行。"""
    agg = {
        "2880": {
            "name": "華南金", "signals": ["乖離收斂"],
            "details": {
                "bias_convergence": {
                    "close": 33.05,
                    "target_low": 34.20,
                    "target_high": 36.50,
                    "stop_loss": 31.90,
                    "risk_reward": 2.0,
                },
            },
        },
    }
    msg = dn.format_multi_strategy_picks_discord(agg, "2026-04-28")
    assert "🎯" in msg and "🛑" in msg
    assert "34.20" in msg
    assert "36.50" in msg
    assert "31.90" in msg
    assert "R:R 2.0:1" in msg
    assert "ATR" in msg  # 風險警語提及 ATR
