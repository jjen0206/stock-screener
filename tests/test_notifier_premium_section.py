"""src.notifier: 高信心精選 section 格式 + skip 行為單元測試。

對齊既有 test_strong_follower_premium.py pattern,用 db.init_db() 建 production
schema 不自編 CREATE TABLE。守住:

1. format_premium_picks_block:三維資料 → 包含 sid / name / close / 法人 / 千張 / ML
2. format_premium_picks_block:空輸入 → 空字串(caller graceful skip)
3. format_premium_picks_block:無 ML(2D fallback)→ 不顯 🎯 ML 段
4. format_top_picks_message:premium_picks 非空 → 訊息包含 ✨ 高信心精選 section
5. format_top_picks_message:premium_picks None / 空 → 不出現 ✨ 字樣(skip)
6. format_top_picks_message:picks 空 + premium 非空 → 仍顯精選 section
7. Telegram channel 用 *bold*(Markdown legacy)
8. Discord channel 用 **bold**

並含結構性 test 守住 notify_top_picks 內部呼叫 get_strong_follower_premium。
"""
from __future__ import annotations

import inspect

import pytest

from src import config, database as db, notifier


# ============================================================================
# fixtures(production schema,對齊 test_strong_follower_premium pattern)
# ============================================================================

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """每 case 獨立 SQLite,init_db 建 production schema 後 yield。"""
    db_file = tmp_path / "notifier_premium.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    yield tmp_path
    db._reset_path_cache()


# ============================================================================
# format_premium_picks_block 單元
# ============================================================================

def test_premium_block_empty_returns_empty_string():
    """空 list → 回空 string,caller falsy check graceful skip。"""
    assert notifier.format_premium_picks_block([], channel="telegram") == ""
    assert notifier.format_premium_picks_block([], channel="discord") == ""


def test_premium_block_basic_telegram_format():
    """三維齊全 → 標題 + 編號行 + 三大訊號(法人/千張/ML)。"""
    rows = [
        {
            "sid": "2330", "name": "台積電", "close": 779.5,
            "consensus_days": 3, "holders_delta_w": 12,
            "ml_prob": 0.71, "composite_score": 0.92,
        },
    ]
    out = notifier.format_premium_picks_block(rows, channel="telegram")
    # 標題段(Telegram Markdown legacy → *bold*)
    assert "✨" in out
    assert "高信心精選" in out
    assert "法人連買 ≥ 3" in out and "千張戶進場" in out and "ML 過門檻" in out
    # 編號行
    assert "1. [2330]" in out
    assert "台積電" in out
    assert "779.5" in out  # close 兩位小數
    # 三大訊號子行
    assert "🏛️ 法人連買 3 天" in out
    assert "🐋 千張戶 +12" in out
    assert "🎯 ML 0.71" in out


def test_premium_block_skips_ml_when_none():
    """ml_prob=None(2D fallback)→ 不顯 🎯 ML 段,其他仍顯。"""
    rows = [
        {
            "sid": "2454", "name": "聯發科", "close": 1010.0,
            "consensus_days": 2, "holders_delta_w": 5,
            "ml_prob": None, "composite_score": 0.75,
        },
    ]
    out = notifier.format_premium_picks_block(rows, channel="telegram")
    assert "🏛️ 法人連買 2 天" in out
    assert "🐋 千張戶 +5" in out
    assert "🎯 ML" not in out


def test_premium_block_discord_uses_double_star_bold():
    """Discord channel → **bold** 而非 Telegram 的 *bold*。"""
    rows = [
        {
            "sid": "2330", "name": "台積電", "close": 779.5,
            "consensus_days": 3, "holders_delta_w": 10,
            "ml_prob": 0.65, "composite_score": 0.80,
        },
    ]
    out = notifier.format_premium_picks_block(rows, channel="discord")
    # Discord 用 **bold**(Markdown 標準)
    assert "**高信心精選" in out or "**台積電**" in out
    # 不該出現 escape backslash(Telegram-only)
    assert "\\*" not in out


def test_premium_block_handles_missing_close():
    """close=None → 顯「—」不 crash。"""
    rows = [
        {
            "sid": "9999", "name": "Test", "close": None,
            "consensus_days": 2, "holders_delta_w": 3,
            "ml_prob": 0.60,
        },
    ]
    out = notifier.format_premium_picks_block(rows, channel="telegram")
    assert "[9999]" in out
    assert "—" in out  # close 缺失顯 em dash


def test_premium_block_multiple_rows_keeps_order():
    """多 row 保持入榜順序(caller 已 sort by composite_score desc)。"""
    rows = [
        {"sid": "2330", "name": "台積電", "close": 779.0,
         "consensus_days": 3, "holders_delta_w": 100, "ml_prob": 0.85},
        {"sid": "2317", "name": "鴻海", "close": 150.0,
         "consensus_days": 2, "holders_delta_w": 50, "ml_prob": 0.70},
    ]
    out = notifier.format_premium_picks_block(rows, channel="telegram")
    assert out.index("1. [2330]") < out.index("2. [2317]")


# ============================================================================
# format_top_picks_message 整合(premium 段嵌入)
# ============================================================================

def _make_short_pick() -> dict:
    """組一張合規短線 pick(過 ≥2 共識 + ML),給整合測試用。"""
    return {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 779.0, "pct_change": 1.2,
        "matched_strategies": ["volume_kd", "ma_alignment"],
        "matched_labels": ["量增 KD 黃金交叉", "均線多頭排列"],
        "ml_prob": 0.65, "target_low": 800.0, "target_high": 850.0,
        "stop": 760.0, "ev": 0.025, "risk_reward": 2.5,
        "industry": "半導體", "industry_heat": 2,
        "win_rate": 0.60,
        "analyst_target_mean": None, "analyst_num": None,
        "analyst_source": None, "analyst_target_prev_mean": None,
        "analyst_target_high": None, "analyst_target_low": None,
        "holders_1000up_count": None,
        "holders_delta_w": None, "holders_pct": None,
    }


def test_top_picks_message_with_premium_section_shows_block():
    """premium_picks 非空 → 訊息含 ✨ 高信心精選 section。"""
    picks = [_make_short_pick()]
    premium = [
        {"sid": "2454", "name": "聯發科", "close": 1010.0,
         "consensus_days": 3, "holders_delta_w": 8, "ml_prob": 0.70},
    ]
    msg = notifier.format_top_picks_message(
        picks, "2026-05-13", channel="telegram", premium_picks=premium,
    )
    assert "✨" in msg
    assert "高信心精選" in msg
    assert "[2454]" in msg
    assert "🏛️ 法人連買 3 天" in msg
    # picks 的 footer 統計仍在
    assert "今日 picks 統計" in msg


def test_top_picks_message_skips_premium_section_when_none():
    """premium_picks=None / [] → 不出現 ✨ 字樣(空白訊息防呆)。"""
    picks = [_make_short_pick()]
    msg_none = notifier.format_top_picks_message(
        picks, "2026-05-13", channel="telegram", premium_picks=None,
    )
    msg_empty = notifier.format_top_picks_message(
        picks, "2026-05-13", channel="telegram", premium_picks=[],
    )
    assert "✨" not in msg_none, "premium_picks=None 不該顯 ✨ section"
    assert "✨" not in msg_empty, "premium_picks=[] 不該顯 ✨ section"
    # 既有 footer 仍在(沒 regression)
    assert "今日 picks 統計" in msg_none


def test_top_picks_message_empty_picks_with_premium_still_shows_section():
    """picks 空 但 premium 非空 → 不能整段消失,精選 section 仍要顯。"""
    premium = [
        {"sid": "2330", "name": "台積電", "close": 779.0,
         "consensus_days": 3, "holders_delta_w": 12, "ml_prob": 0.71},
    ]
    msg = notifier.format_top_picks_message(
        [], "2026-05-13", channel="telegram", premium_picks=premium,
    )
    assert "📭 今日無符合" in msg, "empty branch fallback 文字仍在"
    assert "✨" in msg, "premium 非空時 empty branch 仍要顯精選 section"
    assert "[2330]" in msg


def test_top_picks_message_backward_compat_no_premium_arg():
    """不傳 premium_picks → 維持舊行為(無新 section,不影響既有 caller)。"""
    picks = [_make_short_pick()]
    msg = notifier.format_top_picks_message(picks, "2026-05-13", channel="telegram")
    assert "✨" not in msg
    assert "今日 picks 統計" in msg  # footer 仍在


# ============================================================================
# notify_top_picks 整合(用 production schema fixture,跑真 DB call)
# ============================================================================

def test_notify_top_picks_dry_run_with_empty_db(tmp_db, capsys):
    """空 DB → premium helper 自動回 [] → dry_run 訊息含 empty fallback 文字,
    且不含 ✨(graceful skip)。守住整條 wire 不會 crash。"""
    result = notifier.notify_top_picks(
        date="2026-05-13", dry_run=True,
    )
    assert result == {"telegram": True, "discord": True}
    captured = capsys.readouterr()
    assert "📭 今日無符合" in captured.out
    assert "✨" not in captured.out  # 無資料 → skip


# ============================================================================
# 結構性 test 守住 notify_top_picks 內呼叫 get_strong_follower_premium
# ============================================================================

def test_notify_top_picks_source_calls_get_strong_follower_premium():
    """notify_top_picks source 必含 db.get_strong_follower_premium(...) 呼叫,
    避免將來被靜默移除導致精選 section 消失。"""
    src = inspect.getsource(notifier.notify_top_picks)
    assert "get_strong_follower_premium(" in src, (
        "notify_top_picks 必須呼叫 db.get_strong_follower_premium(...) — "
        "高信心精選 section wire 斷掉"
    )


def test_notify_top_picks_source_uses_min_inst_days_3():
    """守住主公拍板的門檻參數(min_inst_days=3 / min_delta_w=1 / top_n=5)。"""
    src = inspect.getsource(notifier.notify_top_picks)
    assert "min_inst_days=3" in src, "min_inst_days 必須為 3(法人連 3 日共識)"
    assert "min_delta_w=1" in src, "min_delta_w 必須為 1"
    assert "top_n=5" in src, "精選 section top_n 必須為 5"


def test_format_top_picks_message_signature_has_premium_picks():
    """format_top_picks_message 必須有 premium_picks 參數。"""
    sig = inspect.signature(notifier.format_top_picks_message)
    assert "premium_picks" in sig.parameters, (
        "format_top_picks_message 缺 premium_picks kwarg"
    )
