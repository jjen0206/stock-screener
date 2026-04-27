"""src/notifier.py 單元測試。

策略:
- mock requests.post,不打真網路
- mock screen_short,讓 notify_short_picks 不真的查 SQLite
"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pandas as pd
import pytest
import requests

from src import config, notifier


# === fixtures ===

@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake_token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345")


def _make_pick_row(stock_id: str = "2330", name: str = "台積電", **kw) -> dict:
    base = {
        "stock_id": stock_id, "name": name,
        "close": 779.0, "volume": 30000, "ma_volume_5": 20000,
        "k": 52.0, "d": 48.0, "inst_total_3d": 5_500_000,
        "matched_at": "2026-04-25",
    }
    base.update(kw)
    return base


# === send_telegram_message ===

def test_send_telegram_success(with_token):
    fake = Mock()
    fake.status_code = 200
    with patch("src.notifier.requests.post", return_value=fake) as m:
        ok = notifier.send_telegram_message("hello")
    assert ok is True
    m.assert_called_once()
    args, kwargs = m.call_args
    assert "fake_token" in args[0]  # URL 含 token
    assert kwargs["json"]["chat_id"] == "12345"
    assert kwargs["json"]["text"] == "hello"
    assert kwargs["json"]["parse_mode"] == "Markdown"


def test_send_telegram_missing_token_returns_false(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    with patch("src.notifier.requests.post") as m:
        ok = notifier.send_telegram_message("hello")
    assert ok is False
    m.assert_not_called()  # 缺 token 連 API 都不該呼叫


def test_send_telegram_missing_chat_id_returns_false(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")  # 只缺 chat_id
    ok = notifier.send_telegram_message("hello")
    assert ok is False


def test_send_telegram_explicit_args_override_config(monkeypatch):
    """傳參數應覆蓋 config(沒設 config 也能跑)。"""
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    fake = Mock(); fake.status_code = 200
    with patch("src.notifier.requests.post", return_value=fake) as m:
        ok = notifier.send_telegram_message(
            "hi", bot_token="explicit_tok", chat_id="999",
        )
    assert ok is True
    assert m.call_args.kwargs["json"]["chat_id"] == "999"
    assert "explicit_tok" in m.call_args.args[0]


def test_send_telegram_http_error_returns_false(with_token):
    fake = Mock(); fake.status_code = 401; fake.text = "Unauthorized"
    with patch("src.notifier.requests.post", return_value=fake):
        ok = notifier.send_telegram_message("hello")
    assert ok is False


def test_send_telegram_network_error_returns_false(with_token):
    with patch(
        "src.notifier.requests.post",
        side_effect=requests.ConnectionError("boom"),
    ):
        ok = notifier.send_telegram_message("hello")
    assert ok is False


# === format_short_picks ===

def test_format_short_picks_with_data():
    df = pd.DataFrame([
        _make_pick_row("2330", "台積電"),
        _make_pick_row("2454", "聯發科", close=1200.0,
                       volume=5000, ma_volume_5=3000,
                       k=35.0, d=30.0, inst_total_3d=200_000),
    ])
    msg = notifier.format_short_picks(df, "2026-04-25")
    assert "2026-04-25" in msg
    assert "短線推薦" in msg
    assert "(2 檔)" in msg
    assert "2330" in msg and "台積電" in msg
    assert "2454" in msg and "聯發科" in msg
    # Markdown 粗體
    assert "*" in msg
    # 量比格式
    assert "1.5x" in msg  # 30000/20000 = 1.5
    assert "1.7x" in msg  # 5000/3000 ≈ 1.67 → 1.7
    # 風險警語
    assert "⚠️" in msg


def test_format_short_picks_empty():
    msg = notifier.format_short_picks(pd.DataFrame(), "2026-04-25")
    assert "無符合條件" in msg
    assert "2026-04-25" in msg


def test_format_short_picks_handles_none():
    msg = notifier.format_short_picks(None, "2026-04-25")
    assert "無符合條件" in msg


def test_format_short_picks_handles_zero_ma_volume():
    """ma_volume_5=0 不該除零炸。"""
    df = pd.DataFrame([_make_pick_row(ma_volume_5=0)])
    msg = notifier.format_short_picks(df, "2026-04-25")
    assert "0.0x" in msg  # 量比顯示 0.0x


# === notify_short_picks ===

def test_notify_short_picks_calls_send(with_token, monkeypatch):
    fake_picks = pd.DataFrame([_make_pick_row()])
    monkeypatch.setattr(
        notifier, "screen_short",
        lambda d, params=None, stock_ids=None: fake_picks,
    )
    fake = Mock(); fake.status_code = 200
    with patch("src.notifier.requests.post", return_value=fake) as m:
        ok = notifier.notify_short_picks(date="2026-04-25")
    assert ok is True
    sent_text = m.call_args.kwargs["json"]["text"]
    assert "2330" in sent_text
    assert "2026-04-25" in sent_text


def test_notify_short_picks_empty_still_sends(with_token, monkeypatch):
    """空 picks 也該推一則「無符合條件」訊息。"""
    monkeypatch.setattr(
        notifier, "screen_short",
        lambda d, params=None, stock_ids=None: pd.DataFrame(),
    )
    fake = Mock(); fake.status_code = 200
    with patch("src.notifier.requests.post", return_value=fake) as m:
        ok = notifier.notify_short_picks(date="2026-04-25")
    assert ok is True
    sent_text = m.call_args.kwargs["json"]["text"]
    assert "無符合條件" in sent_text


def test_notify_short_picks_uses_universe(with_token, monkeypatch):
    """確認 screen_short 被傳 stock_ids(限縮到 TW_TOP_50)。"""
    captured: dict = {}

    def fake_screen(d, params=None, stock_ids=None):
        captured["stock_ids"] = stock_ids
        return pd.DataFrame()

    monkeypatch.setattr(notifier, "screen_short", fake_screen)
    fake = Mock(); fake.status_code = 200
    with patch("src.notifier.requests.post", return_value=fake):
        notifier.notify_short_picks(date="2026-04-25")

    assert captured["stock_ids"] is not None
    assert len(captured["stock_ids"]) == 50  # TW_TOP_50
    assert "2330" in captured["stock_ids"]


def test_notify_short_picks_returns_false_on_send_failure(
    with_token, monkeypatch,
):
    """send 失敗(API 401)→ notify 也回 False。"""
    monkeypatch.setattr(
        notifier, "screen_short",
        lambda d, params=None, stock_ids=None: pd.DataFrame(),
    )
    fake = Mock(); fake.status_code = 401; fake.text = "Unauthorized"
    with patch("src.notifier.requests.post", return_value=fake):
        ok = notifier.notify_short_picks(date="2026-04-25")
    assert ok is False
