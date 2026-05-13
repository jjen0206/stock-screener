"""「📊 強者跟蹤」分頁結構性守住測試(報告 docs/dage-feature-scope.md 方案 D)。

純結構性檢查 — 不 mock streamlit、不跑頁面、只用 inspect.getsource 看
function body 有沒有 wire 到對應 helpers。教訓自前次「mock streamlit chain
死循環」事故,固守此 pattern。

守住:
1. `app._page_strong_follower` function 存在
2. `"📊 強者跟蹤"` 在 `app.PAGES` 內,且緊接「👥 大戶入場」之後
   (報告建議的籌碼面 thematic grouping)
3. 主路由 if/elif chain dispatch 對應正確
4. Source 含 3 個關鍵 helper call
   (get_top_inst_consensus / get_top_shareholder_movers /
    get_strong_follower_composite)
5. Reuse _render_table_with_inline_detail(同 _page_big_buyer pattern)
6. 三個 tab 標籤都在 source 內
7. 報告 7.2 法律警示:disclaimer 文案常駐顯示
"""
from __future__ import annotations

import inspect
import re

import app


# ============================================================================
# Function 存在
# ============================================================================

def test_page_strong_follower_function_exists():
    """app._page_strong_follower 必須是 module-level function。"""
    assert hasattr(app, "_page_strong_follower"), (
        "app 缺 _page_strong_follower function"
    )
    assert callable(app._page_strong_follower), (
        "_page_strong_follower 必須是 callable"
    )


# ============================================================================
# PAGES 註冊
# ============================================================================

def test_strong_follower_in_pages_list():
    """「📊 強者跟蹤」必須在 app.PAGES 內(否則 segmented_control 看不到)。"""
    assert "📊 強者跟蹤" in app.PAGES, (
        f"PAGES 缺「📊 強者跟蹤」:{app.PAGES}"
    )


def test_strong_follower_after_big_buyer():
    """報告 8.1 建議:「強者跟蹤」緊接「大戶入場」之後(籌碼 thematic group)。

    守住未來改 PAGES 時不會把它推到別處(e.g. 跑去設定旁邊)。
    """
    idx_bb = app.PAGES.index("👥 大戶入場")
    idx_sf = app.PAGES.index("📊 強者跟蹤")
    assert idx_sf == idx_bb + 1, (
        f"「強者跟蹤」必須緊接「大戶入場」之後 "
        f"(big_buyer at {idx_bb}, strong_follower at {idx_sf})"
    )


# ============================================================================
# Source 含 3 個 helper call
# ============================================================================

def test_page_strong_follower_calls_three_helpers():
    """_page_strong_follower 必須呼叫 3 個 helper(每 tab 一個資料源)。

    用 inspect.getsource 抓 function body,regex 比對 helper 名稱。
    確保未來 refactor 不誤砍 tab 的資料源。
    """
    src = inspect.getsource(app._page_strong_follower)

    expected_helpers = [
        "get_top_inst_consensus",       # 法人共識榜
        "get_top_shareholder_movers",   # 千張大戶進場榜
        "get_strong_follower_composite",  # 綜合排行
    ]
    for helper in expected_helpers:
        pattern = re.compile(rf"\b{re.escape(helper)}\b")
        assert pattern.search(src), (
            f"_page_strong_follower source 沒有 call 到 {helper}"
        )


def test_page_strong_follower_uses_inline_detail_helper():
    """reuse _render_table_with_inline_detail(對齊 _page_big_buyer pattern)。"""
    src = inspect.getsource(app._page_strong_follower)
    assert "_render_table_with_inline_detail" in src, (
        "_page_strong_follower 沒 reuse _render_table_with_inline_detail"
    )


def test_page_strong_follower_has_three_tabs():
    """報告方案 D 拆 3 個 tab:法人共識榜 / 千張大戶進場榜 / 綜合排行。

    每個 tab 的 emoji + 中性命名都該在 source 內(法律警示 7.2 — 不要
    「跟單 / 大哥 / 主力」等敏感字眼)。
    """
    src = inspect.getsource(app._page_strong_follower)
    for tab_label in ("🏛️ 法人共識榜", "🐋 千張大戶進場榜", "🎯 綜合排行"):
        assert tab_label in src, (
            f"_page_strong_follower source 缺 tab 標籤「{tab_label}」"
        )


def test_page_strong_follower_has_legal_disclaimer():
    """報告 7.2 強烈建議:UI 常駐顯示法律免責(中性語言)。

    比對 disclaimer 關鍵字 — 不要硬綁死整段字串,只要 source 含
    「非投資建議」+ 「僅供」這兩個語意核心即可。
    """
    src = inspect.getsource(app._page_strong_follower)
    assert "非投資建議" in src, "disclaimer 缺「非投資建議」語意"
    assert "僅供" in src, "disclaimer 缺「僅供」語意(個人參考用途)"


def test_page_strong_follower_avoids_legal_sensitive_terms():
    """報告 7.2 法律警示:tab 標籤 / caption 不可出現敏感字眼。

    禁:跟單 / 跟買 / 大哥 / 帶你飛
    這些是金管會「薦股」敏感字,即便這次只用既有訊號,UI 也要保持中性。
    """
    src = inspect.getsource(app._page_strong_follower)
    sensitive_terms = ["跟單", "跟買", "大哥", "帶你飛"]
    for term in sensitive_terms:
        assert term not in src, (
            f"_page_strong_follower source 不該出現敏感字「{term}」(報告 7.2)"
        )


# ============================================================================
# Dispatch 路由(主路由 if/elif chain)
# ============================================================================

def test_main_dispatch_routes_strong_follower():
    """app.main / 主路由必須對「📊 強者跟蹤」dispatch 到 _page_strong_follower。

    純 string 比對 app source(不執行 main 避免 streamlit context 死循環)。
    """
    src = inspect.getsource(app)
    pattern = re.compile(
        r'elif\s+page\s*==\s*"📊 強者跟蹤"\s*:\s*\n\s*_page_strong_follower\(\)'
    )
    assert pattern.search(src), (
        "主路由 if/elif chain 缺「📊 強者跟蹤」→ _page_strong_follower() dispatch"
    )
