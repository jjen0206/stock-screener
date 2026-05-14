"""動態策略權重 wire 結構性守住測試。

純結構性 — 不 mock streamlit、不跑邏輯、用 inspect.getsource +
inspect.signature 看 function 接口跟 source 內 token 有沒有 wire 對。
對齊 test_page_strategy_history_wire.py 的 pattern。

守住:
1. src.strategy_weighting.get_strategy_weights_30d / get_strategy_weight_details
   都 existing
2. notifier._compute_pick_score 接受 strategy_weights 參數
3. notifier._select_top_picks source 含 get_strategy_weights_30d call
4. notifier format_yesterday_recap source 含 get_strategy_weights_30d call
5. notifier 有 STRATEGY_DYNAMIC_WEIGHT_ENABLED flag
6. app._page_system_brief source 含「⚖️ 動態權重明細」expander
"""
from __future__ import annotations

import inspect

import app
from src import notifier, strategy_weighting


# === src/strategy_weighting.py ===

def test_get_strategy_weights_30d_exists():
    """src.strategy_weighting.get_strategy_weights_30d 必須是 module-level callable。"""
    assert hasattr(strategy_weighting, "get_strategy_weights_30d"), (
        "src.strategy_weighting 缺 get_strategy_weights_30d"
    )
    assert callable(strategy_weighting.get_strategy_weights_30d), (
        "get_strategy_weights_30d 必須 callable"
    )


def test_get_strategy_weight_details_exists():
    """details version 給 Streamlit page 用。"""
    assert hasattr(strategy_weighting, "get_strategy_weight_details"), (
        "src.strategy_weighting 缺 get_strategy_weight_details"
    )
    assert callable(strategy_weighting.get_strategy_weight_details)


# === notifier._compute_pick_score 接 strategy_weights ===

def test_compute_pick_score_accepts_strategy_weights():
    """_compute_pick_score 必須接 strategy_weights kwarg(動態權重 wire 點)。"""
    sig = inspect.signature(notifier._compute_pick_score)
    assert "strategy_weights" in sig.parameters, (
        f"_compute_pick_score 缺 strategy_weights 參數:{list(sig.parameters)}"
    )


# === notifier 內呼叫 helper ===

def test_select_top_picks_calls_get_strategy_weights_30d():
    """_select_top_picks 必須撈 weights 並傳給 _compute_pick_score。"""
    src = inspect.getsource(notifier._select_top_picks)
    assert "get_strategy_weights_30d" in src, (
        "_select_top_picks source 沒呼叫 get_strategy_weights_30d"
    )
    assert "strategy_weights" in src, (
        "_select_top_picks source 沒傳 strategy_weights 到 sort key"
    )


def test_format_yesterday_recap_calls_get_strategy_weights_30d():
    """format_yesterday_recap 必須讀同一份 weights 才能保 recap 順序一致(M4/U1)。"""
    src = inspect.getsource(notifier.format_yesterday_recap)
    assert "get_strategy_weights_30d" in src, (
        "format_yesterday_recap 沒呼叫 get_strategy_weights_30d — "
        "recap 順序會跟實際推播分歧"
    )


# === Config flag ===

def test_notifier_has_dynamic_weight_flag():
    """STRATEGY_DYNAMIC_WEIGHT_ENABLED flag 必須存在(kill-switch)。"""
    assert hasattr(notifier, "STRATEGY_DYNAMIC_WEIGHT_ENABLED"), (
        "notifier 缺 STRATEGY_DYNAMIC_WEIGHT_ENABLED kill-switch"
    )
    # 預設 ON
    assert notifier.STRATEGY_DYNAMIC_WEIGHT_ENABLED is True, (
        "預設應 ON;改 False 退回 legacy 純 ml_prob 排序"
    )


# === Streamlit 系統結論頁顯示 weights ===

def test_system_brief_page_shows_weights_expander():
    """_page_system_brief source 必須含「⚖️ 動態權重明細」expander label +
    get_strategy_weight_details call。"""
    src = inspect.getsource(app._page_system_brief)
    assert "動態權重明細" in src, (
        "_page_system_brief 缺「動態權重明細」expander label"
    )
    assert "get_strategy_weight_details" in src, (
        "_page_system_brief source 沒呼叫 get_strategy_weight_details"
    )
