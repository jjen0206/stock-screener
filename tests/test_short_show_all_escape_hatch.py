"""「📋 顯示全部 N 檔」逃生口的結構性守住測試。

純結構性檢查 — 不 mock streamlit、不跑頁面、只用 inspect.getsource 看
function body 有沒有正確接上 checkbox 與 bypass 邏輯。教訓自前次「mock
streamlit chain 死循環」事故,固守此 pattern。

守住:
1. `_page_short` 內至少 2 處 `st.checkbox(...)` 帶 "顯示全部" 文字
   (全部 tab 卡片模式 + 每個 category 分頁各 1)
2. source 中有 `show_all` 變數,且在 `show_all` 出現後有
   `filtered_all = all_rows` 或 `filtered_sub = sub_rows` 的 bypass 賦值
3. checkbox 預設值是 False(不是預設打開,不會破壞 confidence filter 預設行為)
"""
from __future__ import annotations

import inspect
import re

import app


# ============================================================================
# Function 存在
# ============================================================================

def test_page_short_function_exists():
    """app._page_short 必須是 module-level function。"""
    assert hasattr(app, "_page_short"), "app 缺 _page_short function"
    assert callable(app._page_short), "_page_short 必須是 callable"


# ============================================================================
# Checkbox 數量與文字
# ============================================================================

def test_show_all_checkbox_at_least_two_occurrences():
    """`_page_short` source 至少 2 處 `st.checkbox(...)` 帶 "顯示全部"。

    一處在全部 tab 卡片模式,一處在 category 分頁迴圈內。
    """
    src = inspect.getsource(app._page_short)
    # st.checkbox(...) 跨多行,需要 DOTALL
    pattern = re.compile(r"st\.checkbox\([^)]*顯示全部[^)]*\)", re.DOTALL)
    matches = pattern.findall(src)
    assert len(matches) >= 2, (
        f"_page_short 應有 ≥2 處 st.checkbox(..顯示全部..),實際 {len(matches)} 處"
    )


# ============================================================================
# Bypass 邏輯接上
# ============================================================================

def test_show_all_variable_present():
    """source 內必須有 `show_all` 變數(checkbox 的回傳值)。"""
    src = inspect.getsource(app._page_short)
    assert "show_all" in src, "_page_short 缺 show_all 變數"


def test_show_all_bypasses_confidence_filter():
    """`show_all` 出現後,必須有把 filtered_* 改回原始 rows 的 bypass 賦值。

    全部 tab:`filtered_all = all_rows`
    Category 分頁:`filtered_sub = sub_rows`
    兩種至少各出現一次。
    """
    src = inspect.getsource(app._page_short)
    # 先找到第一個 show_all 出現位置,後面要有 bypass 賦值
    first_show_all = src.find("show_all")
    assert first_show_all != -1, "_page_short 找不到 show_all"
    after = src[first_show_all:]
    assert "filtered_all = all_rows" in after, (
        "全部 tab 卡片模式缺 bypass 賦值 `filtered_all = all_rows`"
    )
    assert "filtered_sub = sub_rows" in after, (
        "Category 分頁缺 bypass 賦值 `filtered_sub = sub_rows`"
    )


# ============================================================================
# 預設關閉(逃生口不能破壞 high_confidence_mode 預設行為)
# ============================================================================

def test_show_all_checkbox_defaults_to_false():
    """每個 `st.checkbox(..顯示全部..)` 都要 `value=False`,不可預設打開。"""
    src = inspect.getsource(app._page_short)
    pattern = re.compile(r"st\.checkbox\([^)]*顯示全部[^)]*\)", re.DOTALL)
    for m in pattern.findall(src):
        assert "value=False" in m, (
            f"st.checkbox 缺 value=False 預設值:{m!r}"
        )


# ============================================================================
# Key 必須獨立(全部 tab 與 category 分頁不可共用 session_state)
# ============================================================================

def test_show_all_keys_are_distinct():
    """兩處 checkbox 的 key 必須不同(否則 session_state 互相干擾)。"""
    src = inspect.getsource(app._page_short)
    # 抓 key="..." 或 key=f"..." 在 顯示全部 checkbox 區塊內的字串
    pattern = re.compile(
        r"st\.checkbox\([^)]*顯示全部[^)]*key\s*=\s*(f?\"[^\"]+\"|f?'[^']+')[^)]*\)",
        re.DOTALL,
    )
    keys = pattern.findall(src)
    assert len(keys) >= 2, f"應有 ≥2 個 checkbox key,實際 {len(keys)}"
    # 全部 tab 是靜態 key、category 是 f-string 帶 {cat},兩者形式必然不同
    assert len(set(keys)) == len(keys), f"checkbox keys 重複:{keys}"
