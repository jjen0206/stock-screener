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


# === format_manual_picks / notify_manual_picks(雲端 App 手動推播按鈕) ===


def _short_page_picks_df(n: int = 3) -> pd.DataFrame:
    """模擬短線頁 aggregated_to_dataframe 的 schema。"""
    rows = []
    for i in range(n):
        rows.append({
            "stock_id": f"23{i:02d}",
            "name": f"股{i}",
            "close": 100.0 + i,
            "信號數": (i % 3) + 1,
            "信號": "量價突破" if i == 0 else "KD 黃金交叉",
            "target_low": 105.0 + i,
            "target_high": 115.0 + i,
            "stop_loss": 95.0 + i,
            "risk_reward": 2.5,
            "atr14": 3.0,
        })
    return pd.DataFrame(rows)


def test_format_manual_picks_includes_manual_footer():
    df = _short_page_picks_df(2)
    msg = notifier.format_manual_picks(df, date="2026-04-30")
    assert "📲 來源:雲端 App 手動推播" in msg
    assert "短線推薦" in msg
    assert "2300" in msg


def test_format_manual_picks_truncates_to_limit():
    """超過 limit 該截斷,訊息該標『顯示前 N / 共 M』。"""
    df = _short_page_picks_df(15)
    msg = notifier.format_manual_picks(df, date="2026-04-30", limit=7)
    assert "顯示前 7 / 共 15" in msg
    # 2307 在前 7 內(2300~2306),2308 不該在
    assert "2308" not in msg


def test_format_manual_picks_empty_dataframe():
    msg = notifier.format_manual_picks(pd.DataFrame(), date="2026-04-30")
    assert "當前無推薦" in msg


def test_notify_manual_picks_no_secrets_returns_empty(monkeypatch):
    """兩通道都沒設 secrets → 回 {},不打任何 HTTP。"""
    from src import config as cfg
    monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(cfg, "DISCORD_WEBHOOK_URL", "")
    with patch("src.notifier.requests.post") as m:
        results = notifier.notify_manual_picks(_short_page_picks_df(2))
    assert results == {}
    m.assert_not_called()


def test_notify_manual_picks_pushes_to_both(with_token, monkeypatch):
    """secrets 都有 → Telegram + Discord 各推一次,訊息含手動推播 footer。"""
    from src import config as cfg
    monkeypatch.setattr(cfg, "DISCORD_WEBHOOK_URL", "https://discord/webhook")
    fake = Mock(); fake.status_code = 200
    with patch("requests.post", return_value=fake) as m:
        results = notifier.notify_manual_picks(
            _short_page_picks_df(3), date="2026-04-30",
        )
    assert results == {"telegram": True, "discord": True}
    assert m.call_count == 2
    urls = [c.args[0] for c in m.call_args_list]
    assert any("telegram" in u for u in urls)
    assert any("discord" in u for u in urls)
    # 確認 Telegram payload 含手動推播 footer
    tg_call = next(c for c in m.call_args_list if "telegram" in c.args[0])
    assert "手動推播" in tg_call.kwargs["json"]["text"]


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


# === Top picks(高信心 + confluence ≥2)推播 ===

def _make_top_pick(
    rank: int = 1, sid: str = "2330", name: str = "台積電",
    close: float = 850.0, pct_change: float = 1.2,
    matched: list[str] | None = None,
    ml_prob: float | None = 0.72,
    target_low: float = 893.0, target_high: float = 935.0, stop: float = 825.0,
    rr: float = 2.0,
) -> dict:
    if matched is None:
        matched = ["macd_golden", "ma_alignment", "volume_breakout"]
    from src.strategies import STRATEGY_LABELS
    return {
        "rank": rank, "sid": sid, "name": name,
        "close": close, "pct_change": pct_change,
        "matched_strategies": matched,
        "matched_labels": [STRATEGY_LABELS.get(s, s) for s in matched],
        "ml_prob": ml_prob,
        "target_low": target_low, "target_high": target_high, "stop": stop,
        "ev": (ml_prob * 0.05 - (1 - ml_prob) * 0.03) if ml_prob else None,
        "risk_reward": rr,
    }


def test_format_pick_block_telegram_includes_all_fields():
    """單張 pick 區塊含 sid 名 / 收盤 / 命中策略 list / ML / 目標 / 期望值。"""
    pick = _make_top_pick()
    block = notifier.format_pick_block(pick, channel="telegram")

    assert "#1" in block
    assert "2330 台積電" in block
    assert "850.00" in block          # 收盤
    assert "↑1.2%" in block           # 漲跌方向
    assert "命中 3 策略" in block
    assert "MACD 黃金交叉" in block   # 中文 label 不是 key
    assert "多頭排列" in block
    assert "量爆突破" in block
    assert "ML 機率 72%" in block
    assert "保守 893" in block
    assert "停損 825" in block
    assert "期望值" in block
    assert "R:R 2.0:1" in block
    # Telegram bold = single asterisk
    assert "*#1*" in block


def test_format_pick_block_discord_uses_double_asterisk():
    """Discord 用 **bold**(Telegram 用 *bold*)。"""
    pick = _make_top_pick()
    block = notifier.format_pick_block(pick, channel="discord")
    assert "**#1**" in block
    assert "*#1*" not in block.replace("**#1**", "")


def test_format_pick_block_negative_change_shows_down_arrow():
    pick = _make_top_pick(pct_change=-2.5)
    block = notifier.format_pick_block(pick)
    assert "↓2.5%" in block


def test_format_pick_block_no_ml_prob_skips_line():
    pick = _make_top_pick(ml_prob=None)
    block = notifier.format_pick_block(pick)
    assert "ML 機率" not in block
    assert "期望值" not in block  # ev 也算不出


# === 產業 badge(2026-05-06 主公拍板加) ===

def test_format_pick_block_includes_hot_industry():
    """industry_heat ≥ 3 → 顯 🔥 [類別] 加 bold + (今日 N 檔同類)。"""
    pick = _make_top_pick()
    pick["industry"] = "半導體業"
    pick["industry_heat"] = 5
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "🔥" in block, "industry_heat ≥ 3 該顯 🔥 emoji"
    assert "半導體業" in block
    assert "今日 5 檔同類" in block
    # Telegram bold(單星)
    assert "*半導體業*" in block


def test_format_pick_block_includes_normal_industry():
    """industry_heat < 3 → 顯 🏭 [類別](灰色 normal label,沒「同類」尾段)。"""
    pick = _make_top_pick()
    pick["industry"] = "水泥工業"
    pick["industry_heat"] = 2
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "🏭" in block
    assert "水泥工業" in block
    assert "🔥" not in block
    assert "同類" not in block  # heat < 3 不加「同類」尾段


def test_format_pick_block_skip_when_no_industry():
    """沒 industry 欄(舊 caller / 個股 industry IS NULL)→ 不顯 industry 行。"""
    pick = _make_top_pick()
    # 不設 industry / industry_heat
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "🔥" not in block
    assert "🏭" not in block


# === win_rate 推播(2026-05-06 主公拍板加,接 backtest 結果到推播) ===

def test_format_pick_block_includes_win_rate_when_high():
    """win_rate ≥ 55% → 🎯 emoji + 加粗顯。"""
    pick = _make_top_pick()
    pick["win_rate"] = 0.62
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "🎯" in block, "≥55% 該用 🎯 emoji"
    assert "勝率" in block
    assert "62%" in block
    assert "126d 回測" in block
    # Telegram bold(單星)
    assert "*62%*" in block


def test_format_pick_block_includes_win_rate_when_low():
    """win_rate < 55% → 📊 emoji(中性 / 不到 55% 不算高勝率)。

    註:🎯 emoji 也出現在「目標」行(🎯 保守 / 積極 / 停損)— 用具體
    勝率行 substring 斷言才不會跟它撞。
    """
    pick = _make_top_pick()
    pick["win_rate"] = 0.40
    block = notifier.format_pick_block(pick, channel="telegram")
    # 找含「勝率 40%」的那行
    assert "📊 勝率" in block, f"低勝率該用 📊 emoji,實際: {block}"
    # 「勝率」行不該用 🎯(只 ≥55% 才用)
    assert "🎯 勝率" not in block
    assert "40%" in block


def test_format_pick_block_skips_win_rate_when_none():
    """win_rate=None → 不顯該行(向下相容,舊 caller / 命中策略全沒 backtest)。"""
    pick = _make_top_pick()
    pick["win_rate"] = None
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "勝率" not in block
    assert "126d 回測" not in block


def test_format_pick_block_skips_win_rate_when_zero():
    """win_rate=0 視同無資料(避免顯「勝率 0%」誤導)。"""
    pick = _make_top_pick()
    pick["win_rate"] = 0.0
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "勝率" not in block


# === 千張大戶推播(TDCC 週快照,主公拍板:不納入 ML 只當附加資訊)===

def test_format_pick_block_includes_shareholder_line_with_delta_and_pct():
    """有完整千張戶資料 → 顯「👥 千張戶 N (週變 +M, 占比 X.X%)」。"""
    pick = _make_top_pick()
    pick["holders_1000up_count"] = 2050
    pick["holders_delta_w"] = 15
    pick["holders_pct"] = 0.00674
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "👥 千張戶 2050" in block
    assert "週變 +15" in block
    assert "占比 0.7%" in block


def test_format_pick_block_shareholder_delta_negative_shows_minus():
    """delta_w 是 - 號(大戶減少 → 籌碼鬆動)。"""
    pick = _make_top_pick()
    pick["holders_1000up_count"] = 1980
    pick["holders_delta_w"] = -23
    pick["holders_pct"] = 0.00650
    block = notifier.format_pick_block(pick)
    assert "👥 千張戶 1980" in block
    assert "週變 -23" in block


def test_format_pick_block_shareholder_skips_delta_when_none():
    """第一次抓沒上週基準 → delta_w=None → 省略「週變」段但仍顯人數 + 占比。"""
    pick = _make_top_pick()
    pick["holders_1000up_count"] = 720
    pick["holders_delta_w"] = None
    pick["holders_pct"] = 0.00551
    block = notifier.format_pick_block(pick)
    assert "👥 千張戶 720" in block
    assert "週變" not in block
    assert "占比 0.6%" in block


def test_format_pick_block_shareholder_skips_entire_line_when_no_data():
    """無 holders_1000up_count(該檔當週沒公布 / 還沒抓過)→ 整行 graceful skip。"""
    pick = _make_top_pick()
    # 不設任何 holders_* 欄位
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "👥" not in block
    assert "千張戶" not in block


def test_format_pick_block_shareholder_handles_zero_count():
    """holders_1000up_count=0(該檔小公司無千張戶)→ 仍顯該行(0 也是有意義的資訊)。"""
    pick = _make_top_pick()
    pick["holders_1000up_count"] = 0
    pick["holders_delta_w"] = 0
    pick["holders_pct"] = 0.0
    block = notifier.format_pick_block(pick)
    assert "👥 千張戶 0" in block


def test_format_pick_block_shareholder_does_not_break_other_fields():
    """加千張戶行不該影響原有欄位(ML / 目標 / 期望值 / 勝率)。"""
    pick = _make_top_pick()
    pick["holders_1000up_count"] = 1500
    pick["holders_delta_w"] = 10
    pick["holders_pct"] = 0.005
    pick["win_rate"] = 0.62
    block = notifier.format_pick_block(pick, channel="telegram")
    # 既有欄位仍在
    assert "ML 機率 72%" in block
    assert "保守 893" in block
    assert "停損 825" in block
    assert "期望值" in block
    assert "勝率" in block
    # 新欄位也在
    assert "千張戶 1500" in block


# === U3 進場區間建議(ATR / BB based)===

def test_format_pick_block_includes_entry_range():
    """有 entry_low + entry_high → 顯「💰 進場區間 X ~ Y」。"""
    pick = _make_top_pick()
    pick["entry_low"] = 1232.5
    pick["entry_high"] = 1245.0
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "💰 進場區間" in block
    assert "1232.50" in block
    assert "1245.00" in block


def test_format_pick_block_skips_entry_range_when_missing():
    """無 entry_low / entry_high(資料不足)→ 整行 graceful skip。"""
    pick = _make_top_pick()
    # 不設 entry_low / entry_high
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "💰" not in block
    assert "進場區間" not in block


def test_format_pick_block_entry_range_does_not_break_other_fields():
    """加進場區間不影響既有欄位。"""
    pick = _make_top_pick()
    pick["entry_low"] = 893.0
    pick["entry_high"] = 850.0
    pick["win_rate"] = 0.62
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "💰 進場區間" in block
    assert "ML 機率 72%" in block
    assert "保守 893" in block
    assert "停損 825" in block
    assert "勝率" in block


def test_format_top_picks_message_includes_separator_and_stats():
    picks = [_make_top_pick(rank=i, sid=f"233{i}") for i in range(1, 4)]
    msg = notifier.format_top_picks_message(
        picks, "2026-05-05", channel="telegram",
    )
    # 標題
    assert "短線精選" in msg
    assert "2026-05-05" in msg
    # 分隔線(出現在 picks 之間 + 結尾)
    assert "━━━━━━━━━━━━━━━━" in msg
    # 統計區塊
    assert "今日 picks 統計" in msg
    assert "高信心 + ≥2 策略:  3 張" in msg
    assert "平均 ML 機率" in msg
    assert "平均期望值" in msg
    # 警語
    assert "僅供研究" in msg


def test_format_top_picks_message_empty_shows_friendly_msg():
    msg = notifier.format_top_picks_message(
        [], "2026-05-05", channel="telegram",
    )
    assert "無符合" in msg or "0" in msg
    assert "僅供研究" in msg
    # 不該有 picks 統計區
    assert "高信心 + ≥2 策略:" not in msg


def test_notify_top_picks_dry_run_prints_to_stdout(monkeypatch, capsys):
    """dry_run=True 不打 channel API,只 print 兩 channel 訊息。"""
    monkeypatch.setattr(
        notifier, "_select_top_picks",
        lambda d, top_n=5, confluence_n=2, params=None, universe=None: [
            _make_top_pick(rank=1, sid="2330", name="台積電"),
        ],
    )
    # network 也 mock 防漏網
    with patch("requests.post") as m:
        results = notifier.notify_top_picks(
            date="2026-05-05", dry_run=True,
        )
    assert results == {"telegram": True, "discord": True}
    assert m.call_count == 0  # dry_run 不送

    captured = capsys.readouterr()
    assert "Telegram" in captured.out
    assert "Discord" in captured.out
    assert "2330 台積電" in captured.out
