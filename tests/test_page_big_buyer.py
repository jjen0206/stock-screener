"""「👥 大戶入場」分頁結構性守住測試。

純結構性檢查 — 不 mock streamlit、不跑頁面、只用 inspect.getsource 看
function body 有沒有 call 到對應的 3 個 db helpers。教訓自前次「mock streamlit
chain 死循環」事故,固守此 pattern。

守住:
1. `app._page_big_buyer` function 存在
2. `"👥 大戶入場"` 在 `app.PAGES` 內
3. `_page_big_buyer` source 含 3 個 db helper call
   (get_top_shareholder_movers / get_top_shareholder_concentration /
    get_consecutive_shareholder_increases)
4. PAGES 中「大戶入場」必須緊接「市場熱度」之後(主公拍板的順序)
"""
from __future__ import annotations

import inspect
import re

import app


# ============================================================================
# Function 存在
# ============================================================================

def test_page_big_buyer_function_exists():
    """app._page_big_buyer 必須是 module-level function。"""
    assert hasattr(app, "_page_big_buyer"), "app 缺 _page_big_buyer function"
    assert callable(app._page_big_buyer), "_page_big_buyer 必須是 callable"


# ============================================================================
# PAGES 註冊
# ============================================================================

def test_big_buyer_in_pages_list():
    """「👥 大戶入場」必須在 app.PAGES 內(否則 segmented_control 看不到)。"""
    assert "👥 大戶入場" in app.PAGES, (
        f"PAGES 缺「👥 大戶入場」:{app.PAGES}"
    )


def test_big_buyer_after_market_heat():
    """主公拍板:「大戶入場」緊接「市場熱度」之後。

    這個順序測試守住未來改 PAGES 時不會把它推到別處去
    (e.g. 不小心放到「設定」後面)。
    """
    idx_heat = app.PAGES.index("🌡️ 市場熱度")
    idx_big_buyer = app.PAGES.index("👥 大戶入場")
    assert idx_big_buyer == idx_heat + 1, (
        f"「大戶入場」必須緊接「市場熱度」之後 "
        f"(market_heat at {idx_heat}, big_buyer at {idx_big_buyer})"
    )


# ============================================================================
# Source 含 3 個 db helper call
# ============================================================================

def test_page_big_buyer_calls_three_helpers():
    """_page_big_buyer 必須呼叫 3 個 ranking helper(不靠死記 dispatch 名稱)。

    用 inspect.getsource 抓 function body,regex 看 helper 名稱有沒有出現。
    確保未來 refactor 不會誤把 3 個 tab 的資料源砍掉一個。
    """
    src = inspect.getsource(app._page_big_buyer)

    expected_helpers = [
        "get_top_shareholder_movers",
        "get_top_shareholder_concentration",
        "get_consecutive_shareholder_increases",
    ]
    for helper in expected_helpers:
        # 比對 db.get_top_... 或 直接 get_top_... 都接受
        pattern = re.compile(rf"\b{re.escape(helper)}\b")
        assert pattern.search(src), (
            f"_page_big_buyer source 沒有 call 到 {helper}"
        )


def test_page_big_buyer_uses_inline_detail_helper():
    """_page_big_buyer 必須 reuse _render_table_with_inline_detail
    (對齊關注 / 市場熱度頁 pattern,點 row → 展開卡片)。"""
    src = inspect.getsource(app._page_big_buyer)
    assert "_render_table_with_inline_detail" in src, (
        "_page_big_buyer 沒 reuse _render_table_with_inline_detail"
    )


def test_page_big_buyer_has_three_tabs():
    """三個 tab 標籤都該在 source 內(主公拍板的設計)。"""
    src = inspect.getsource(app._page_big_buyer)
    for tab_label in ("🚀 本週暴增", "📈 連續增加", "🏰 絕對占比"):
        assert tab_label in src, (
            f"_page_big_buyer source 缺 tab 標籤「{tab_label}」"
        )


def test_page_big_buyer_shows_data_accumulating_info_on_streak_tab():
    """「連續增加」tab 必須顯資料累積中提示(資料只一週時的預期)。"""
    src = inspect.getsource(app._page_big_buyer)
    assert "資料累積中" in src, (
        "_page_big_buyer 沒在「連續增加」tab 顯資料累積中提示"
    )


# ============================================================================
# Dispatch 路由(主路由 if/elif chain)
# ============================================================================

def test_main_dispatch_routes_big_buyer():
    """app.main / 主路由必須對「👥 大戶入場」dispatch 到 _page_big_buyer。

    純 string 比對 app source(不執行 main 避免 streamlit context 死循環)。
    """
    src = inspect.getsource(app)
    # 主路由結構:elif page == "👥 大戶入場":\n    _page_big_buyer()
    pattern = re.compile(
        r'elif\s+page\s*==\s*"👥 大戶入場"\s*:\s*\n\s*_page_big_buyer\(\)'
    )
    assert pattern.search(src), (
        "主路由 if/elif chain 缺「👥 大戶入場」→ _page_big_buyer() dispatch"
    )
