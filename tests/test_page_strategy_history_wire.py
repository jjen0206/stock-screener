"""「📊 策略歷史」分頁結構性守住測試。

純結構性 — 不 mock streamlit、不跑頁面、用 inspect.getsource 看 function body
有沒有 wire 對。對齊 test_page_strong_follower_wire.py 的 pattern。

守住:
1. `app._page_strategy_history` 存在
2. `"📊 策略歷史"` 在 `app.PAGES`,且緊接「🧪 實測追蹤」後
   (前向 paper-trade → 後向 backtest 連著放,主公一眼對比)
3. 主路由 if/elif 對「📊 策略歷史」dispatch 對應正確
4. Source 含三個 strategy-history helper call
   (get_strategy_history_stats / get_pick_outcomes_by_date /
    get_pick_outcomes_raw)
5. Source 含 3 個 sub-tab 標籤(by-strategy / by-date / 明細)
"""
from __future__ import annotations

import inspect
import re

import app


# ============================================================================
# Function 存在
# ============================================================================

def test_page_strategy_history_function_exists():
    """app._page_strategy_history 必須是 module-level callable。"""
    assert hasattr(app, "_page_strategy_history"), (
        "app 缺 _page_strategy_history function"
    )
    assert callable(app._page_strategy_history), (
        "_page_strategy_history 必須是 callable"
    )


# ============================================================================
# PAGES 註冊
# ============================================================================

def test_strategy_history_in_pages_list():
    """「📊 策略歷史」必須在 app.PAGES 內(否則 segmented_control 看不到)。"""
    assert "📊 策略歷史" in app.PAGES, (
        f"PAGES 缺「📊 策略歷史」:{app.PAGES}"
    )


def test_strategy_history_after_paper_tracking():
    """「📊 策略歷史」必須緊接「🧪 實測追蹤」之後 — 前向 paper-trade 跟後向
    backtest 並列,主公一頁對比真實 vs 歷史。
    """
    idx_paper = app.PAGES.index("🧪 實測追蹤")
    idx_history = app.PAGES.index("📊 策略歷史")
    assert idx_history == idx_paper + 1, (
        f"「📊 策略歷史」必須緊接「🧪 實測追蹤」之後 "
        f"(paper at {idx_paper}, history at {idx_history})"
    )


# ============================================================================
# Dispatch 路由
# ============================================================================

def test_main_dispatch_routes_strategy_history():
    """主路由 if/elif chain 必須對「📊 策略歷史」dispatch 到
    _page_strategy_history。
    """
    src = inspect.getsource(app)
    pattern = re.compile(
        r'elif\s+page\s*==\s*"📊 策略歷史"\s*:\s*\n\s*_page_strategy_history\(\)'
    )
    assert pattern.search(src), (
        "主路由 if/elif chain 缺「📊 策略歷史」→ _page_strategy_history() dispatch"
    )


# ============================================================================
# Source 含三個 helper call
# ============================================================================

def test_page_strategy_history_uses_three_helpers():
    """_page_strategy_history 必須呼叫 3 個 pick_outcomes helper
    (each tab 一個資料源)。
    """
    src = inspect.getsource(app._page_strategy_history)
    expected_helpers = [
        "get_strategy_history_stats",   # by-strategy 聚合
        "get_pick_outcomes_by_date",    # by-date 聚合
        "get_pick_outcomes_raw",        # 明細列表
    ]
    for helper in expected_helpers:
        pattern = re.compile(rf"\b{re.escape(helper)}\b")
        assert pattern.search(src), (
            f"_page_strategy_history source 沒 call 到 {helper}"
        )


def test_page_strategy_history_has_three_tabs():
    """3 個 sub-tab 標籤都要在 source 內 — refactor 後砍掉某個 tab 立刻爆。"""
    src = inspect.getsource(app._page_strategy_history)
    for tab_label in ("📈 by-strategy", "📅 by-date", "📦 全部結算明細"):
        assert tab_label in src, (
            f"_page_strategy_history source 缺 tab 標籤「{tab_label}」"
        )
