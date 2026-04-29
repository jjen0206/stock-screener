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


def test_format_short_picks_empty_includes_cache_health(monkeypatch):
    """0 入選時該附 cache 健康度,並在歷史不足時加註「累積中」。"""
    monkeypatch.setattr(
        notifier.db, "cache_health_summary",
        lambda: {
            "total_stocks": 2700, "with_prices": 2700,
            "buckets": {"<14": 2600, "14-19": 50, "20-59": 30, "60+": 20},
        },
    )
    msg = notifier.format_short_picks(pd.DataFrame(), "2026-04-25")
    assert "Cache" in msg
    assert "60+" in msg
    # eligible (60+ + 20-59) = 50 < 100 → 該加「累積中」
    assert "累積中" in msg


def test_format_short_picks_empty_no_warning_when_healthy(monkeypatch):
    """eligible >= 100 時不該加「累積中」(避免誤導已健康的 cache)。"""
    monkeypatch.setattr(
        notifier.db, "cache_health_summary",
        lambda: {
            "total_stocks": 2700, "with_prices": 2700,
            "buckets": {"<14": 100, "14-19": 0, "20-59": 100, "60+": 2500},
        },
    )
    msg = notifier.format_short_picks(pd.DataFrame(), "2026-04-25")
    assert "Cache" in msg
    assert "累積中" not in msg


def test_format_multi_strategy_empty_includes_cache_health(monkeypatch):
    """multi-strategy 空 aggregated 也該附 cache 健康度。"""
    monkeypatch.setattr(
        notifier.db, "cache_health_summary",
        lambda: {
            "total_stocks": 2700, "with_prices": 2700,
            "buckets": {"<14": 2700, "14-19": 0, "20-59": 0, "60+": 0},
        },
    )
    msg = notifier.format_multi_strategy_picks({}, "2026-04-25")
    assert "Cache" in msg
    assert "累積中" in msg


def test_format_multi_strategy_telegram_includes_targets():
    """有 target_low/high/stop_loss 該印「🎯 目標 / 🛑 停損」。"""
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
    msg = notifier.format_multi_strategy_picks(agg, "2026-04-28")
    assert "🎯" in msg and "🛑" in msg
    assert "34.20" in msg and "36.50" in msg and "31.90" in msg
    assert "R:R 2.0:1" in msg
    assert "ATR" in msg  # 風險警語


# === notify_short_picks ===

def test_notify_short_picks_calls_send(with_token, monkeypatch):
    fake_picks = pd.DataFrame([_make_pick_row()])
    monkeypatch.setattr(
        notifier, "screen_short",
        lambda d, params=None, stock_ids=None: fake_picks,
    )
    fake = Mock(); fake.status_code = 200
    with patch("src.notifier.requests.post", return_value=fake) as m:
        results = notifier.notify_short_picks(
            date="2026-04-25", send_discord=False,
        )
    # 新版回 dict
    assert results == {"telegram": True}
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
        results = notifier.notify_short_picks(
            date="2026-04-25", send_discord=False,
        )
    assert results.get("telegram") is True
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
        notifier.notify_short_picks(date="2026-04-25", send_discord=False)

    assert captured["stock_ids"] is not None
    assert len(captured["stock_ids"]) == 50  # TW_TOP_50
    assert "2330" in captured["stock_ids"]


def test_notify_short_picks_returns_false_on_send_failure(
    with_token, monkeypatch,
):
    """send 失敗(API 401)→ telegram 通道回 False。"""
    monkeypatch.setattr(
        notifier, "screen_short",
        lambda d, params=None, stock_ids=None: pd.DataFrame(),
    )
    fake = Mock(); fake.status_code = 401; fake.text = "Unauthorized"
    with patch("src.notifier.requests.post", return_value=fake):
        results = notifier.notify_short_picks(
            date="2026-04-25", send_discord=False,
        )
    assert results.get("telegram") is False


def test_notify_short_picks_no_secrets_returns_empty(monkeypatch):
    """兩個通道都沒設 secrets → 回空 dict。"""
    from src import config as cfg
    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(cfg, "TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(cfg, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(
        notifier, "screen_short",
        lambda d, params=None, stock_ids=None: pd.DataFrame(),
    )
    results = notifier.notify_short_picks(date="2026-04-25")
    assert results == {}


def test_notify_short_picks_both_channels_when_both_configured(
    with_token, monkeypatch,
):
    """Telegram + Discord 都有 secrets → 兩個通道都送。

    `requests.post` 是 module-level singleton,patch 會被全域共享 —
    用一個 patch 觀察兩次呼叫(URL 不同)。
    """
    from src import config as cfg
    monkeypatch.setattr(cfg, "DISCORD_WEBHOOK_URL", "https://discord/webhook")

    monkeypatch.setattr(
        notifier, "screen_short",
        lambda d, params=None, stock_ids=None: pd.DataFrame(
            [_make_pick_row()]
        ),
    )

    fake = Mock(); fake.status_code = 200  # Telegram 200 / Discord 也 OK
    with patch("requests.post", return_value=fake) as m:
        results = notifier.notify_short_picks(date="2026-04-25")

    assert results == {"telegram": True, "discord": True}
    assert m.call_count == 2
    # 兩個 call 應該分別打到 Telegram 與 Discord
    urls = [c.args[0] for c in m.call_args_list]
    assert any("telegram" in u for u in urls)
    assert any("discord" in u for u in urls)
