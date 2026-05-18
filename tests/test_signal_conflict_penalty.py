"""反轉 × 趨勢衝突檢測(Phase 2 #P2-7)— signal_conflict_penalty 邏輯測試。

驗證:
1. has_signal_conflict 對純 reversal / 純 trend / 純 neutral 群皆回 False
2. has_signal_conflict 對 reversal + trend 混合回 True
3. neutral 跟 reversal/trend 任一同時不算衝突
4. unknown(未在 NATURE dict)當 neutral,不會 crash 也不會誤判衝突
5. 邊界:空 list / None / 單一策略 → False
6. consensus_multiplier 衝突時回 1.0(從原本的 1.5 / 1.8 變 1.0)
7. consensus_multiplier 非衝突路徑沒被打到 — 原邏輯保留
8. SIGNAL_CONFLICT_PENALTY_ENABLED=false → 衝突檢關閉,回到原邏輯
9. consensus_badge 衝突時 badge 空、tier=none(跟 multiplier 同步)
10. 端到端:compute_strategy_consensus → multiplier 走衝突路徑
"""
from __future__ import annotations

import pytest

from src import consensus as cs


# === STRATEGY_NATURE 完整性 ===

def test_strategy_nature_covers_all_known_strategies():
    """STRATEGY_NATURE 應覆蓋 STRATEGY_CATEGORIES 內每一個 strategy key —
    未覆蓋會 silently 當 neutral 處理,埋下「該衝突卻沒檢出」的雷。
    """
    missing = set(cs.STRATEGY_CATEGORIES) - set(cs.STRATEGY_NATURE)
    assert not missing, f"NATURE 沒覆蓋: {sorted(missing)}"


def test_strategy_nature_values_are_valid():
    """所有 value 必須是 reversal/trend/neutral 三選一。"""
    allowed = {"reversal", "trend", "neutral"}
    bad = {k: v for k, v in cs.STRATEGY_NATURE.items() if v not in allowed}
    assert not bad, f"非法 NATURE value: {bad}"


# === has_signal_conflict ===

def test_no_conflict_empty_or_none():
    assert cs.has_signal_conflict([]) is False
    assert cs.has_signal_conflict(None) is False


def test_no_conflict_single_strategy():
    assert cs.has_signal_conflict(["rsi_recovery"]) is False
    assert cs.has_signal_conflict(["volume_breakout"]) is False
    assert cs.has_signal_conflict(["inst_consensus"]) is False


def test_no_conflict_pure_reversal():
    """兩個 reversal 策略不算衝突(同方向訊號加總有意義)。"""
    assert cs.has_signal_conflict(["rsi_recovery", "bb_lower_rebound"]) is False
    assert cs.has_signal_conflict(
        ["bias_convergence", "inst_oversold_reversal"]
    ) is False


def test_no_conflict_pure_trend():
    """兩個 trend 策略不算衝突。"""
    assert cs.has_signal_conflict(["ma_alignment", "macd_golden"]) is False
    assert cs.has_signal_conflict(
        ["volume_breakout", "gap_up", "ma_squeeze_breakout"],
    ) is False


def test_no_conflict_pure_neutral():
    """純 neutral 群不衝突(籌碼/基本面內部不衝突)。"""
    assert cs.has_signal_conflict(
        ["inst_consensus", "high_yield_stable", "big_holder_inflow"],
    ) is False


def test_no_conflict_reversal_plus_neutral():
    """reversal + neutral → 不衝突(neutral 跟方向訊號正交)。"""
    assert cs.has_signal_conflict(["rsi_recovery", "inst_consensus"]) is False
    assert cs.has_signal_conflict(
        ["bias_convergence", "high_yield_stable"],
    ) is False


def test_no_conflict_trend_plus_neutral():
    """trend + neutral → 不衝突。"""
    assert cs.has_signal_conflict(
        ["volume_breakout", "big_holder_inflow"],
    ) is False
    assert cs.has_signal_conflict(["ma_alignment", "eps_acceleration"]) is False


def test_conflict_reversal_plus_trend():
    """reversal × trend 同時觸發 → 衝突。"""
    assert cs.has_signal_conflict(
        ["rsi_recovery", "volume_breakout"],
    ) is True
    assert cs.has_signal_conflict(
        ["bias_convergence", "macd_golden"],
    ) is True
    assert cs.has_signal_conflict(
        ["bb_lower_rebound", "ma_alignment"],
    ) is True


def test_conflict_reversal_trend_plus_neutral():
    """有 neutral 在內,只要含 reversal × trend 仍算衝突。"""
    assert cs.has_signal_conflict(
        ["rsi_recovery", "ma_alignment", "inst_consensus"],
    ) is True


def test_unknown_strategy_treated_as_neutral():
    """未在 NATURE dict 的策略當 neutral — 不誤判衝突,也不 crash。"""
    assert cs.has_signal_conflict(["mystery_future_strategy"]) is False
    # unknown + reversal → 視為 reversal+neutral → 不衝突
    assert cs.has_signal_conflict(
        ["mystery_future_strategy", "rsi_recovery"],
    ) is False
    # unknown + reversal + trend → 仍衝突(reversal × trend 還在)
    assert cs.has_signal_conflict(
        ["mystery_future_strategy", "rsi_recovery", "ma_alignment"],
    ) is True


# === consensus_multiplier — 衝突路徑 ===

def test_multiplier_conflict_returns_one():
    """跨類別 2 票本來 1.5,衝突時降回 1.0。"""
    meta = {
        "strategy_count": 2,
        "category_count": 2,
        "strategies": ["rsi_recovery", "volume_breakout"],
    }
    assert cs.consensus_multiplier(meta) == 1.0


def test_multiplier_conflict_with_three_strategies():
    """跨類別 3 票本來 1.8,衝突時降回 1.0。"""
    meta = {
        "strategy_count": 3,
        "category_count": 3,
        "strategies": ["rsi_recovery", "ma_alignment", "inst_consensus"],
    }
    assert cs.consensus_multiplier(meta) == 1.0


def test_multiplier_no_conflict_preserves_original_logic():
    """無衝突 → 原 tier 倍率不變。"""
    # 同類別共識 — 兩個 trend 策略
    meta_same = {
        "strategy_count": 2,
        "category_count": 1,
        "strategies": ["ma_alignment", "macd_golden"],
    }
    assert cs.consensus_multiplier(meta_same) == 1.3

    # 跨類別 2 — reversal + neutral
    meta_cross2 = {
        "strategy_count": 2,
        "category_count": 2,
        "strategies": ["rsi_recovery", "inst_consensus"],
    }
    assert cs.consensus_multiplier(meta_cross2) == 1.5

    # 跨類別 3 — 全 neutral 不同 category
    meta_cross3 = {
        "strategy_count": 3,
        "category_count": 3,
        "strategies": [
            "inst_consensus", "eps_acceleration", "high_yield_stable",
        ],
    }
    assert cs.consensus_multiplier(meta_cross3) == 1.8


def test_multiplier_missing_strategies_field_falls_back_safely():
    """meta 沒帶 strategies 欄位 → 沒法檢衝突 → 用原 tier 邏輯(防守)。"""
    meta = {"strategy_count": 2, "category_count": 2}
    # 沒 strategies → has_signal_conflict([]) = False → 走原邏輯回 1.5
    assert cs.consensus_multiplier(meta) == 1.5


# === SIGNAL_CONFLICT_PENALTY_ENABLED kill switch ===

@pytest.mark.parametrize("val", ["false", "0", "no", "off"])
def test_conflict_kill_switch_disables_check(monkeypatch, val):
    """關掉衝突檢 → 即使有衝突仍走原 tier 邏輯。"""
    monkeypatch.setenv("SIGNAL_CONFLICT_PENALTY_ENABLED", val)
    meta = {
        "strategy_count": 2,
        "category_count": 2,
        "strategies": ["rsi_recovery", "volume_breakout"],
    }
    # 沒衝突檢 → 回原 1.5
    assert cs.consensus_multiplier(meta) == 1.5


def test_conflict_kill_switch_default_on(monkeypatch):
    """env 沒設 → 預設 on → 衝突 → 1.0。"""
    monkeypatch.delenv("SIGNAL_CONFLICT_PENALTY_ENABLED", raising=False)
    meta = {
        "strategy_count": 2,
        "category_count": 2,
        "strategies": ["rsi_recovery", "volume_breakout"],
    }
    assert cs.consensus_multiplier(meta) == 1.0


def test_consensus_kill_switch_overrides_conflict_kill_switch(monkeypatch):
    """大開關關了 → 不論衝突檢開不開,都回 1.0(no consensus at all)。"""
    monkeypatch.setenv("STRATEGY_CONSENSUS_ENABLED", "false")
    monkeypatch.setenv("SIGNAL_CONFLICT_PENALTY_ENABLED", "true")
    meta = {
        "strategy_count": 3,
        "category_count": 3,
        "strategies": ["macd_golden", "inst_consensus", "high_yield_stable"],
    }
    assert cs.consensus_multiplier(meta) == 1.0


# === consensus_badge — 衝突同步消失 ===

def test_badge_conflict_returns_none():
    """衝突時 badge 跟 multiplier 同步消失。"""
    meta = {
        "strategy_count": 2,
        "category_count": 2,
        "strategies": ["rsi_recovery", "volume_breakout"],
    }
    assert cs.consensus_badge(meta) == ("", "none")


def test_badge_no_conflict_preserves_tiers():
    """無衝突 → 原 badge 不變。"""
    meta_cross2 = {
        "strategy_count": 2,
        "category_count": 2,
        "strategies": ["rsi_recovery", "inst_consensus"],
    }
    badge, tier = cs.consensus_badge(meta_cross2)
    assert tier == "cross_2"
    assert "共識" in badge


# === 端到端 compute → multiplier ===

def test_end_to_end_conflict_pick_gets_no_bonus():
    """模擬真實流程:同 sid 被 reversal + trend 命中 → 共識計算 → multiplier 1.0。"""
    picks_by_strategy = {
        "rsi_recovery": ["2330"],
        "volume_breakout": ["2330", "2454"],
    }
    consensus_map = cs.compute_strategy_consensus(picks_by_strategy)

    # 2330 被兩個策略命中且互衝 → multiplier 1.0
    assert cs.consensus_multiplier(consensus_map["2330"]) == 1.0
    # 2454 只命中 1 策略 → 1.0(本來就沒共識)
    assert cs.consensus_multiplier(consensus_map["2454"]) == 1.0


def test_end_to_end_non_conflict_pick_gets_bonus():
    """reversal + neutral 同檔命中 → 還是有共識加成(neutral 不衝突)。"""
    picks_by_strategy = {
        "rsi_recovery": ["2330"],
        "high_yield_stable": ["2330"],
    }
    consensus_map = cs.compute_strategy_consensus(picks_by_strategy)
    # 反轉 + 殖利率 跨 category 2 → 1.5
    assert cs.consensus_multiplier(consensus_map["2330"]) == 1.5
