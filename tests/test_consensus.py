"""跨策略共識核心邏輯測試。

驗證:
1. compute_strategy_consensus 正確算 strategy_count / category_count
2. 同 sid 在同一策略多次出現只算 1 票(set 去重)
3. consensus_multiplier 對各 tier 給出正確倍率
4. consensus_badge 對各 tier 給出正確 badge / tier 字串
5. STRATEGY_CONSENSUS_ENABLED env var kill-switch 生效
6. 未分類策略不會 crash
7. summarize_consensus_counts 統計結果正確
"""
from __future__ import annotations

import os

import pytest

from src import consensus as cs


# === compute_strategy_consensus ===

def test_compute_consensus_empty_input():
    assert cs.compute_strategy_consensus({}) == {}
    assert cs.compute_strategy_consensus(None) == {}


def test_compute_consensus_single_strategy_single_sid():
    """1 個策略命中 1 個 sid → count=1, category_count=1。"""
    result = cs.compute_strategy_consensus({"macd_golden": ["2330"]})
    assert "2330" in result
    meta = result["2330"]
    assert meta["strategy_count"] == 1
    assert meta["strategies"] == ["macd_golden"]
    assert meta["category_count"] == 1
    assert meta["categories"] == ["趨勢"]


def test_compute_consensus_cross_category():
    """跨類別共識 — macd_golden(趨勢)+ inst_consensus(籌碼)。"""
    result = cs.compute_strategy_consensus({
        "macd_golden": ["2330"],
        "inst_consensus": ["2330", "2454"],
    })
    meta = result["2330"]
    assert meta["strategy_count"] == 2
    assert meta["category_count"] == 2
    # 2454 只命中 1 策略 → count=1
    assert result["2454"]["strategy_count"] == 1
    assert result["2454"]["category_count"] == 1


def test_compute_consensus_same_category_only():
    """同類別 2+ 票 — macd_golden + ma_alignment 都是「趨勢」→ count=2, cat=1。"""
    result = cs.compute_strategy_consensus({
        "macd_golden": ["2330"],
        "ma_alignment": ["2330"],
    })
    meta = result["2330"]
    assert meta["strategy_count"] == 2
    assert meta["category_count"] == 1
    assert meta["categories"] == ["趨勢"]


def test_compute_consensus_cross_three_categories():
    """3 個不同類別 → category_count=3(觸發強共識)。"""
    result = cs.compute_strategy_consensus({
        "macd_golden": ["2330"],         # 趨勢
        "inst_consensus": ["2330"],      # 籌碼
        "volume_breakout": ["2330"],     # 動能
    })
    meta = result["2330"]
    assert meta["strategy_count"] == 3
    assert meta["category_count"] == 3


def test_compute_consensus_dedupe_same_strategy_multiple_times():
    """同 sid 在同一策略內出現多次 → 只算 1 票(set 去重保險)。"""
    result = cs.compute_strategy_consensus({
        "macd_golden": ["2330", "2330", "2330"],
    })
    assert result["2330"]["strategy_count"] == 1


def test_compute_consensus_accepts_pick_dicts():
    """input value 可以是 pick dict(.sid / .stock_id) 也接。"""
    result = cs.compute_strategy_consensus({
        "macd_golden": [{"sid": "2330"}, {"stock_id": "2454"}],
        "inst_consensus": [{"sid": "2330"}],
    })
    assert result["2330"]["strategy_count"] == 2
    assert result["2454"]["strategy_count"] == 1


def test_compute_consensus_unknown_strategy_uses_unclassified():
    """沒在 STRATEGY_CATEGORIES 的策略 → 分到 '未分類',不 crash。"""
    result = cs.compute_strategy_consensus({
        "macd_golden": ["2330"],
        "mystery_new_strategy": ["2330"],
    })
    meta = result["2330"]
    assert meta["strategy_count"] == 2
    assert meta["category_count"] == 2  # 趨勢 + 未分類
    assert "未分類" in meta["categories"]


def test_compute_consensus_ignores_empty_or_none_items():
    """list 內混 None / empty string → 過濾不算。"""
    result = cs.compute_strategy_consensus({
        "macd_golden": ["2330", None, "", {"sid": None}],
    })
    assert "2330" in result
    assert result["2330"]["strategy_count"] == 1
    # None / empty / dict-without-sid 都不該出現
    assert "" not in result
    assert None not in result


# === consensus_multiplier ===

def test_multiplier_single_strategy():
    """單策略 → 1.0(無加成)。"""
    assert cs.consensus_multiplier(
        {"strategy_count": 1, "category_count": 1}
    ) == 1.0


def test_multiplier_same_category_2():
    """同類 2+ 票 → ×1.3。"""
    assert cs.consensus_multiplier(
        {"strategy_count": 2, "category_count": 1}
    ) == 1.3


def test_multiplier_cross_category_2():
    """跨類 2 票 → ×1.5。"""
    assert cs.consensus_multiplier(
        {"strategy_count": 2, "category_count": 2}
    ) == 1.5


def test_multiplier_cross_category_3plus():
    """跨類 3+ 票 → ×1.8(罕見強訊號)。"""
    assert cs.consensus_multiplier(
        {"strategy_count": 3, "category_count": 3}
    ) == 1.8
    assert cs.consensus_multiplier(
        {"strategy_count": 5, "category_count": 4}
    ) == 1.8


def test_multiplier_none_input():
    assert cs.consensus_multiplier(None) == 1.0


def test_multiplier_malformed_meta_safe():
    """meta 缺欄 / 型別錯 → 1.0,不 crash。"""
    assert cs.consensus_multiplier({}) == 1.0
    assert cs.consensus_multiplier(
        {"strategy_count": "bad", "category_count": "bad"}
    ) == 1.0


def test_multiplier_kill_switch(monkeypatch):
    """STRATEGY_CONSENSUS_ENABLED=false → multiplier 一律 1.0。"""
    monkeypatch.setenv("STRATEGY_CONSENSUS_ENABLED", "false")
    assert cs.consensus_multiplier(
        {"strategy_count": 3, "category_count": 3}
    ) == 1.0


# === consensus_badge ===

def test_badge_single():
    assert cs.consensus_badge(
        {"strategy_count": 1, "category_count": 1}
    ) == ("", "none")


def test_badge_same_category():
    """同類 2 票 → ⭐ / same_cat。"""
    badge, tier = cs.consensus_badge(
        {"strategy_count": 2, "category_count": 1}
    )
    assert badge == "⭐"
    assert tier == "same_cat"


def test_badge_cross_2():
    """跨類 2 → ⭐⭐ 共識。"""
    badge, tier = cs.consensus_badge(
        {"strategy_count": 2, "category_count": 2}
    )
    assert "⭐⭐" in badge
    assert "共識" in badge
    assert tier == "cross_2"


def test_badge_cross_3():
    """跨類 3+ → ⭐⭐⭐ 強共識。"""
    badge, tier = cs.consensus_badge(
        {"strategy_count": 4, "category_count": 3}
    )
    assert "⭐⭐⭐" in badge
    assert "強共識" in badge
    assert tier == "cross_3"


def test_badge_kill_switch(monkeypatch):
    """kill switch off → badge 空 / tier none(即使有共識也不顯)。"""
    monkeypatch.setenv("STRATEGY_CONSENSUS_ENABLED", "0")
    badge, tier = cs.consensus_badge(
        {"strategy_count": 3, "category_count": 3}
    )
    assert badge == ""
    assert tier == "none"


def test_badge_none_input():
    assert cs.consensus_badge(None) == ("", "none")


# === summarize_consensus_counts ===

def test_summary_all_tiers():
    consensus_map = {
        "A": {"strategy_count": 3, "category_count": 3},   # cross_3
        "B": {"strategy_count": 2, "category_count": 2},   # cross_2
        "C": {"strategy_count": 2, "category_count": 2},   # cross_2
        "D": {"strategy_count": 2, "category_count": 1},   # same_cat
        "E": {"strategy_count": 1, "category_count": 1},   # none
    }
    summary = cs.summarize_consensus_counts(consensus_map)
    assert summary["cross_3"] == 1
    assert summary["cross_2"] == 2
    assert summary["same_cat"] == 1
    assert summary["none"] == 1


def test_summary_empty():
    """空 input → 全 0,不 crash。"""
    summary = cs.summarize_consensus_counts({})
    assert summary == {"cross_3": 0, "cross_2": 0, "same_cat": 0, "none": 0}
    summary = cs.summarize_consensus_counts(None)
    assert summary["cross_3"] == 0


# === Categories 覆蓋率 sanity ===

def test_categories_cover_main_strategies():
    """STRATEGY_CATEGORIES 必須涵蓋目前 strategies.py 的主要策略 keys。"""
    expected = {
        "macd_golden", "ma_alignment", "rsi_recovery", "bb_lower_rebound",
        "inst_consensus", "inst_silent_accum", "big_holder_inflow",
        "volume_breakout", "gap_up", "eps_acceleration",
        "high_yield_stable", "taiex_alpha",
    }
    missing = expected - set(cs.STRATEGY_CATEGORIES.keys())
    assert not missing, f"STRATEGY_CATEGORIES 缺主要策略: {missing}"


# === Kill switch reads env var live ===

def test_kill_switch_default_on(monkeypatch):
    """沒設 env var → 預設 on。"""
    monkeypatch.delenv("STRATEGY_CONSENSUS_ENABLED", raising=False)
    assert cs._kill_switch() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "FALSE"])
def test_kill_switch_off_values(monkeypatch, val):
    monkeypatch.setenv("STRATEGY_CONSENSUS_ENABLED", val)
    assert cs._kill_switch() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "anything-else"])
def test_kill_switch_on_values(monkeypatch, val):
    monkeypatch.setenv("STRATEGY_CONSENSUS_ENABLED", val)
    assert cs._kill_switch() is True
