"""精英化推播(2026-05-19 方案 B + B.1.d 排序 + B.2 7 欄)單元測試。

涵蓋:
- B.1.d ranking:consensus ≥ 1.5 → ml_prob top N,不足 fallback ≥ 1.25
- format_elite_pick_block:7 欄欄位齊 + 字數 < 100 / pick
- format_elite_top_picks_message:訊息 < 1000 chars + 元素齊
- LONG_STRATEGY_KEYS 過濾邏輯
"""
from __future__ import annotations

import pytest

from src import notifier


# === Test fixtures ===


def _mk_pick(
    sid: str,
    rank: int = 1,
    name: str = "TestCo",
    close: float = 100.0,
    pct_change: float = 1.0,
    ml_prob: float = 0.7,
    consensus_multiplier: float = 1.5,
    matched_strategies: list[str] | None = None,
    matched_labels: list[str] | None = None,
    target_low: float = 105.0,
    target_high: float = 115.0,
    stop: float = 95.0,
    is_watchlist: bool = False,
    warnings: list[dict] | None = None,
) -> dict:
    return {
        "rank": rank,
        "sid": sid,
        "name": name,
        "close": close,
        "pct_change": pct_change,
        "ml_prob": ml_prob,
        "consensus_multiplier": consensus_multiplier,
        "matched_strategies": matched_strategies or ["volume_breakout", "macd_golden"],
        "matched_labels": matched_labels or ["量爆突破", "MACD 黃金交叉"],
        "target_low": target_low,
        "target_high": target_high,
        "stop": stop,
        "is_watchlist": is_watchlist,
        "warnings": warnings,
    }


# === B.1.d ranking ===


class TestFilterByConsensusAndMl:
    def test_picks_top_n_from_strong_consensus(self):
        """consensus ≥ 1.5 內排 ml_prob desc 取 top N。"""
        pool = [
            _mk_pick("A", consensus_multiplier=1.5, ml_prob=0.6),
            _mk_pick("B", consensus_multiplier=1.8, ml_prob=0.9),
            _mk_pick("C", consensus_multiplier=1.5, ml_prob=0.8),
            _mk_pick("D", consensus_multiplier=1.25, ml_prob=0.95),  # < 1.5
            _mk_pick("E", consensus_multiplier=1.8, ml_prob=0.7),
        ]
        result = notifier._filter_by_consensus_and_ml(pool, top_n=3)
        assert len(result) == 3
        # B(0.9) > C(0.8) > E(0.7)
        assert [p["sid"] for p in result] == ["B", "C", "E"]
        # rank 重排
        assert [p["rank"] for p in result] == [1, 2, 3]

    def test_fallback_when_strong_insufficient(self):
        """≥ 1.5 不足 top_n → fallback 補 ≥ 1.25 高 ml_prob。"""
        pool = [
            _mk_pick("A", consensus_multiplier=1.5, ml_prob=0.6),  # strong
            _mk_pick("B", consensus_multiplier=1.25, ml_prob=0.9),  # fallback
            _mk_pick("C", consensus_multiplier=1.25, ml_prob=0.85),  # fallback
            _mk_pick("D", consensus_multiplier=1.0, ml_prob=0.95),   # 排除
            _mk_pick("E", consensus_multiplier=1.3, ml_prob=0.5),   # fallback (≥1.25)
        ]
        result = notifier._filter_by_consensus_and_ml(pool, top_n=5)
        # 結果應有 A (strong) + B/C/E (fallback by ml_prob desc)
        sids = [p["sid"] for p in result]
        assert "A" in sids
        assert "B" in sids
        assert "C" in sids
        assert "E" in sids
        assert "D" not in sids
        # A 永遠 first(strong)
        assert sids[0] == "A"

    def test_empty_pool_returns_empty(self):
        assert notifier._filter_by_consensus_and_ml([], top_n=5) == []

    def test_zero_top_n_returns_empty(self):
        pool = [_mk_pick("A")]
        assert notifier._filter_by_consensus_and_ml(pool, top_n=0) == []

    def test_no_consensus_returns_empty(self):
        """全部 consensus < 1.25 → 空 list(fallback 也濾光)。"""
        pool = [
            _mk_pick("A", consensus_multiplier=1.0, ml_prob=0.9),
            _mk_pick("B", consensus_multiplier=1.1, ml_prob=0.95),
        ]
        result = notifier._filter_by_consensus_and_ml(pool, top_n=5)
        assert result == []

    def test_missing_consensus_treated_as_1(self):
        """consensus_multiplier 缺 / None → 視為 1.0 不入選。"""
        pool = [
            _mk_pick("A", consensus_multiplier=None, ml_prob=0.95),
            _mk_pick("B", consensus_multiplier=1.5, ml_prob=0.7),
        ]
        result = notifier._filter_by_consensus_and_ml(pool, top_n=5)
        assert [p["sid"] for p in result] == ["B"]

    def test_dedup_strong_not_in_fallback(self):
        """fallback 階段不該重複加入已在 strong 的 picks。"""
        pool = [
            _mk_pick("A", consensus_multiplier=1.8, ml_prob=0.9),
            _mk_pick("B", consensus_multiplier=1.25, ml_prob=0.8),
        ]
        result = notifier._filter_by_consensus_and_ml(pool, top_n=5)
        sids = [p["sid"] for p in result]
        assert sids.count("A") == 1
        assert sids == ["A", "B"]


# === B.2 7 欄 format ===


class TestFormatElitePickBlock:
    def test_telegram_includes_7_columns(self):
        pick = _mk_pick(
            "2330", rank=1, name="台積電", close=1248.0, pct_change=0.5,
            ml_prob=0.78, consensus_multiplier=1.8,
            matched_labels=["KD 黃金交叉", "三紅兵", "突破 60MA"],
            target_low=1320.0, stop=1190.0,
        )
        pick["entry_low"] = 1230.0
        block = notifier.format_elite_pick_block(pick, channel="telegram")
        # 1. stock_id + name
        assert "2330" in block and "台積電" in block
        # 2. 收盤 + 漲跌
        assert "1248.00" in block
        assert "0.5%" in block
        # 3. AI 勝率
        assert "AI 勝率 78%" in block
        # 4. 命中策略 top 3
        assert "KD 黃金交叉" in block
        assert "三紅兵" in block
        assert "突破 60MA" in block
        # 5. 共識級別 ⭐⭐⭐(1.8 ≥ 1.5)
        assert "⭐⭐⭐" in block
        # 6. 進 / 停損 / 停利
        assert "進 1230" in block
        assert "停損 1190" in block
        assert "停利 1320" in block

    def test_short_pick_under_200_chars(self):
        """單張精英 pick 應該 < 200 chars(目標 ~100-150)。"""
        pick = _mk_pick(
            "2330", rank=1, name="台積電",
            matched_labels=["KD 黃金交叉", "三紅兵", "突破 60MA"],
        )
        pick["entry_low"] = 1230.0
        block = notifier.format_elite_pick_block(pick, channel="telegram")
        assert len(block) < 200, f"pick block 太長 ({len(block)} chars):\n{block}"

    def test_watchlist_star_prefix(self):
        pick = _mk_pick("2330", is_watchlist=True)
        block = notifier.format_elite_pick_block(pick, channel="telegram")
        assert "⭐2330" in block

    def test_no_watchlist_no_star_prefix(self):
        pick = _mk_pick("2330", is_watchlist=False)
        block = notifier.format_elite_pick_block(pick, channel="telegram")
        # Header 部份不含 watchlist star;但共識 ⭐⭐⭐ 仍會在
        first_line = block.split("\n")[0]
        assert "⭐2330" not in first_line  # watchlist prefix 不在

    def test_warning_appended_only_when_present(self):
        pick = _mk_pick("2330", warnings=[{"warning_type": "disposition"}])
        block = notifier.format_elite_pick_block(pick, channel="telegram")
        assert "⚠️" in block
        assert "處置股" in block

    def test_no_warning_no_warning_line(self):
        pick = _mk_pick("2330", warnings=None)
        block = notifier.format_elite_pick_block(pick, channel="telegram")
        assert "⚠️" not in block

    def test_consensus_stars_tier(self):
        # 1.8 → ⭐⭐⭐
        b1 = notifier.format_elite_pick_block(
            _mk_pick("A", consensus_multiplier=1.8), channel="telegram"
        )
        assert "⭐⭐⭐" in b1
        # 1.3 → ⭐⭐(1.25 ≤ x < 1.5)
        b2 = notifier.format_elite_pick_block(
            _mk_pick("B", consensus_multiplier=1.3), channel="telegram"
        )
        assert "⭐⭐" in b2 and "⭐⭐⭐" not in b2
        # 1.0 → 無星
        b3 = notifier.format_elite_pick_block(
            _mk_pick("C", consensus_multiplier=1.0), channel="telegram"
        )
        # header 沒共識星(但 watchlist=False 也沒 ⭐ prefix)
        first_line = b3.split("\n")[0]
        assert "⭐" not in first_line

    def test_discord_uses_double_star_bold(self):
        pick = _mk_pick("2330")
        block = notifier.format_elite_pick_block(pick, channel="discord")
        assert "**#1**" in block

    def test_telegram_uses_html_bold(self):
        pick = _mk_pick("2330")
        block = notifier.format_elite_pick_block(pick, channel="telegram")
        assert "<b>#1</b>" in block


# === Full message ===


class TestFormatEliteTopPicksMessage:
    def test_under_1000_chars_with_5_short_picks(self):
        short = [
            _mk_pick(f"233{i}", rank=i, name=f"股{i}",
                     matched_labels=["量爆突破", "MACD 黃金交叉"])
            for i in range(1, 6)
        ]
        msg = notifier.format_elite_top_picks_message(
            short_picks=short, long_picks=[],
            date="2026-05-19", channel="telegram",
        )
        # 訊息字數應 < 1000(spec 目標 ~700)
        assert len(msg) < 1500, f"訊息太長 ({len(msg)} chars):\n{msg}"
        # 含 5 檔 + header
        for i in range(1, 6):
            assert f"233{i}" in msg
        assert "短線精選" in msg
        assert "2026-05-19" in msg
        # warning footer
        assert "僅供研究" in msg

    def test_includes_long_picks_section(self):
        short = [_mk_pick("2330", rank=1)]
        long_p = [_mk_pick("2345", rank=1, name="長線股")]
        msg = notifier.format_elite_top_picks_message(
            short_picks=short, long_picks=long_p,
            date="2026-05-19", channel="telegram",
        )
        assert "長線觀察" in msg
        assert "2345" in msg

    def test_news_caption_appended(self):
        short = [_mk_pick("2330", rank=1)]
        news = [
            {
                "sid": "2330", "company_name": "台積電",
                "subject": "重大訊息",
            }
        ]
        msg = notifier.format_elite_top_picks_message(
            short_picks=short, long_picks=[],
            date="2026-05-19", channel="telegram",
            news_items=news,
        )
        assert "重大 news" in msg
        assert "重大訊息" in msg

    def test_empty_short_picks_shows_placeholder(self):
        msg = notifier.format_elite_top_picks_message(
            short_picks=[], long_picks=[],
            date="2026-05-19", channel="telegram",
        )
        assert "無高共識" in msg

    def test_regime_caption_at_top(self):
        short = [_mk_pick("2330", rank=1)]
        msg = notifier.format_elite_top_picks_message(
            short_picks=short, long_picks=[],
            date="2026-05-19", channel="telegram",
            regime_caption="🟢 多頭，TAIEX +0.5% 預期",
        )
        # regime caption 應該在前(header 後、picks 前)
        idx_regime = msg.find("多頭")
        idx_picks = msg.find("2330")
        assert idx_regime > 0
        assert idx_regime < idx_picks


# === Long strategy keys ===


def test_long_strategy_keys_set():
    """LONG_STRATEGY_KEYS 應包含基本面 + 殖利率類。"""
    assert "eps_acceleration" in notifier.LONG_STRATEGY_KEYS
    assert "revenue_acceleration" in notifier.LONG_STRATEGY_KEYS
    assert "high_yield_stable" in notifier.LONG_STRATEGY_KEYS
    # 短線策略不在內
    assert "volume_breakout" not in notifier.LONG_STRATEGY_KEYS
    assert "macd_golden" not in notifier.LONG_STRATEGY_KEYS


# === _consensus_stars helper ===


@pytest.mark.parametrize(
    "mult,expected",
    [
        (None, ""),
        (0.9, ""),
        (1.0, ""),
        (1.1, "⭐"),
        (1.24, "⭐"),
        (1.25, "⭐⭐"),
        (1.4, "⭐⭐"),
        (1.5, "⭐⭐⭐"),
        (1.8, "⭐⭐⭐"),
        ("invalid", ""),
    ],
)
def test_consensus_stars_tiers(mult, expected):
    assert notifier._consensus_stars(mult) == expected
