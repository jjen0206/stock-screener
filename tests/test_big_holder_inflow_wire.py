"""Structural guards for big_holder_inflow strategy registration.

純 inspect.getsource + regex — 不 mock streamlit、不 mock strategy 跑真邏輯,
只守住:
1. screen_big_holder_inflow callable 存在於 src.strategies
2. ALL_STRATEGIES 含 "big_holder_inflow" key → registry 有掛
3. STRATEGY_LABELS 含 "big_holder_inflow" → 顯示名「千張戶進場」
4. strategies.py STRATEGY_CATEGORY 標 "籌碼"
5. app.py _STRATEGY_CATEGORY 標 "籌碼"
"""
from __future__ import annotations

import inspect
import re

import app
from src import strategies as strat


# ============================================================================
# 1. strategy 函式存在 + callable
# ============================================================================

def test_screen_big_holder_inflow_callable_exists():
    """src.strategies.screen_big_holder_inflow 必須存在且 callable。"""
    fn = getattr(strat, "screen_big_holder_inflow", None)
    assert fn is not None, "src.strategies 缺 screen_big_holder_inflow"
    assert callable(fn), "screen_big_holder_inflow 不是 callable"


def test_screen_big_holder_inflow_in_dunder_all():
    """__all__ 該 export screen_big_holder_inflow,模組合約穩定。"""
    assert "screen_big_holder_inflow" in strat.__all__, (
        "strategies.__all__ 缺 screen_big_holder_inflow"
    )


# ============================================================================
# 2. ALL_STRATEGIES registry 有掛
# ============================================================================

def test_all_strategies_dict_contains_big_holder_inflow():
    """ALL_STRATEGIES["big_holder_inflow"] = screen_big_holder_inflow。"""
    assert "big_holder_inflow" in strat.ALL_STRATEGIES, (
        "ALL_STRATEGIES 缺 'big_holder_inflow' key"
    )
    assert strat.ALL_STRATEGIES["big_holder_inflow"] is strat.screen_big_holder_inflow


def test_all_strategies_source_contains_big_holder_inflow_line():
    """inspect ALL_STRATEGIES 定義段 source → 確認 big_holder_inflow line 有寫入。

    避免 dict 被 monkeypatch / runtime 改但 source 沒同步的詭異情況。
    """
    src = inspect.getsource(strat)
    # 抓 ALL_STRATEGIES: dict[...] = { ... } 整段 block
    match = re.search(
        r"ALL_STRATEGIES:\s*dict\[[^\]]+\]\s*=\s*\{(.+?)\n\}",
        src,
        re.DOTALL,
    )
    assert match, "找不到 ALL_STRATEGIES dict 定義 block"
    block = match.group(1)
    assert '"big_holder_inflow"' in block, (
        "ALL_STRATEGIES dict 定義缺 'big_holder_inflow' key"
    )
    assert "screen_big_holder_inflow" in block, (
        "ALL_STRATEGIES dict 沒掛 screen_big_holder_inflow callable"
    )


# ============================================================================
# 3. STRATEGY_LABELS 有顯示名
# ============================================================================

def test_strategy_labels_has_big_holder_inflow():
    """STRATEGY_LABELS["big_holder_inflow"] = "千張戶進場"。"""
    assert "big_holder_inflow" in strat.STRATEGY_LABELS, (
        "STRATEGY_LABELS 缺 'big_holder_inflow' 顯示名"
    )
    assert strat.STRATEGY_LABELS["big_holder_inflow"] == "千張戶進場"


# ============================================================================
# 4. strategies.py STRATEGY_CATEGORY = "籌碼"(model truth source)
# ============================================================================

def test_strategies_module_category_is_chip_flow():
    """src.strategies.STRATEGY_CATEGORY["big_holder_inflow"] = "籌碼"。"""
    assert "big_holder_inflow" in strat.STRATEGY_CATEGORY, (
        "src.strategies.STRATEGY_CATEGORY 缺 big_holder_inflow"
    )
    assert strat.STRATEGY_CATEGORY["big_holder_inflow"] == "籌碼", (
        "src.strategies.STRATEGY_CATEGORY 必須標『籌碼』"
    )


def test_strategies_module_category_source_contains_chip_flow_line():
    """source 也檢查,避開 runtime monkeypatch hiding。"""
    src = inspect.getsource(strat)
    # 抓 STRATEGY_CATEGORY: dict[...] = { ... } 區塊(注意:strategies 內名字
    # 就叫 STRATEGY_CATEGORY,前面沒底線,跟 app.py 的 _STRATEGY_CATEGORY 不同)
    match = re.search(
        r"^STRATEGY_CATEGORY:\s*dict\[[^\]]+\]\s*=\s*\{(.+?)\n\}",
        src,
        re.DOTALL | re.MULTILINE,
    )
    assert match, "找不到 strategies.STRATEGY_CATEGORY dict 定義 block"
    block = match.group(1)
    # block 內必須有 "big_holder_inflow": "籌碼"
    pat = re.compile(r'"big_holder_inflow"\s*:\s*"籌碼"')
    assert pat.search(block), (
        "strategies.STRATEGY_CATEGORY block 缺 \"big_holder_inflow\": \"籌碼\""
    )


# ============================================================================
# 5. app.py _STRATEGY_CATEGORY = "籌碼"(UI tab 顏色用)
# ============================================================================

def test_app_strategy_category_is_chip_flow():
    """app._STRATEGY_CATEGORY["big_holder_inflow"] = "籌碼"。"""
    assert "big_holder_inflow" in app._STRATEGY_CATEGORY, (
        "app._STRATEGY_CATEGORY 缺 big_holder_inflow"
    )
    assert app._STRATEGY_CATEGORY["big_holder_inflow"] == "籌碼", (
        "app._STRATEGY_CATEGORY 必須標『籌碼』"
    )


def test_app_strategy_category_source_contains_chip_flow_line():
    """app.py source 內 _STRATEGY_CATEGORY 定義段也檢查。"""
    src = inspect.getsource(app)
    match = re.search(
        r"_STRATEGY_CATEGORY:\s*dict\[[^\]]+\]\s*=\s*\{(.+?)\n\}",
        src,
        re.DOTALL,
    )
    assert match, "找不到 app._STRATEGY_CATEGORY dict 定義 block"
    block = match.group(1)
    pat = re.compile(r'"big_holder_inflow"\s*:\s*"籌碼"')
    assert pat.search(block), (
        "app._STRATEGY_CATEGORY block 缺 \"big_holder_inflow\": \"籌碼\""
    )


# ============================================================================
# 6. Phase 2 標記 — 確認 source 已升級(mean / std / sigma 字串 + 無 TODO)
# ============================================================================

def test_screen_big_holder_inflow_source_contains_phase2_markers():
    """screen_big_holder_inflow 的 source 必須含 Phase 2 三件套字串:
    mean、std、sigma — 避免被 monkeypatch / 回退到 Phase 1 而沒人發現。"""
    src = inspect.getsource(strat.screen_big_holder_inflow)
    for marker in ("mean", "std", "sigma"):
        assert marker in src, (
            f"screen_big_holder_inflow source 缺 Phase 2 marker: {marker!r}"
        )


def test_screen_big_holder_inflow_source_has_no_phase2_todo():
    """source 內不該再留『Phase 2 TODO』或『不做』這類字樣 — 升級後該清乾淨。"""
    src = inspect.getsource(strat.screen_big_holder_inflow)
    forbidden_patterns = [
        r"TODO\s+Phase\s+2",
        r"Phase\s+2.*TODO",
        r"Phase\s+2.*不做",
    ]
    for pat in forbidden_patterns:
        assert not re.search(pat, src, re.IGNORECASE), (
            f"screen_big_holder_inflow source 仍含 Phase 2 TODO 註解(pattern: {pat})"
        )
