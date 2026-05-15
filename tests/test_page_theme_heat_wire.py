"""題材熱度排行 UI section 結構性守住測試。

純結構性,不 mock streamlit、不 render — 用 inspect.getsource 看 app.py
function source 內有沒有 wire 對。對齊 test_dynamic_weighting_wire.py
test_system_brief_page_shows_weights_expander 模式。

守住:
1. app._render_theme_heat_section helper 存在
2. _render_theme_heat_section 撈 src.theme_heat.compute_theme_heat
3. _render_theme_heat_section source 有「題材熱度」label + multiplier badge
4. _page_strong_follower(高信心精選 tab)source 有 _render_theme_heat_section call
5. _page_system_brief source 有 _render_theme_heat_section call
6. Mobile-first:_render_theme_heat_section 不用 st.columns 多欄分區塊
"""
from __future__ import annotations

import inspect

import app


# === Helper exists ===

def test_render_theme_heat_section_exists():
    """app._render_theme_heat_section 必須是 module-level helper。"""
    assert hasattr(app, "_render_theme_heat_section"), (
        "app.py 缺 _render_theme_heat_section helper"
    )
    assert callable(app._render_theme_heat_section)


def test_helper_calls_compute_theme_heat():
    """_render_theme_heat_section 必須呼叫 compute_theme_heat。"""
    src = inspect.getsource(app._render_theme_heat_section)
    assert "compute_theme_heat" in src, (
        "_render_theme_heat_section 沒呼叫 compute_theme_heat"
    )


def test_helper_shows_label_and_badges():
    """source 內含 label + multiplier badge 字眼。"""
    src = inspect.getsource(app._render_theme_heat_section)
    assert "題材熱度" in src
    # heat / cold / neutral 三種 badge
    assert "🔥" in src or "🧊" in src or "➖" in src, (
        "_render_theme_heat_section 沒加 multiplier badge 顯示"
    )


def test_helper_respects_kill_switch():
    """source 必須讀 _is_enabled / THEME_HEAT_ENABLED kill-switch。"""
    src = inspect.getsource(app._render_theme_heat_section)
    assert "_is_enabled" in src or "THEME_HEAT_ENABLED" in src, (
        "_render_theme_heat_section 沒讀 kill-switch"
    )


def test_helper_is_mobile_first():
    """Mobile-first:不該在程式碼裡 call st.columns(docstring 提及不算)。

    去掉 docstring + 註解 行後再 grep,避免「不用 st.columns」這類說明
    讓檢查誤殺。
    """
    src = inspect.getsource(app._render_theme_heat_section)
    code_lines = []
    in_docstring = False
    for line in src.splitlines():
        stripped = line.lstrip()
        # 三引號 docstring toggle
        if stripped.startswith('"""') or stripped.startswith("'''"):
            # 同行開閉(單行 docstring)→ skip
            quote = stripped[:3]
            rest = stripped[3:]
            if rest.endswith(quote) and len(rest) >= 3:
                continue
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        # 純註解行 skip
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)
    assert "st.columns" not in code, (
        "_render_theme_heat_section code body 用了 st.columns(違反 mobile-first)"
    )


# === Wire 進兩個頁面 ===

def test_strong_follower_page_wires_theme_heat():
    """_page_strong_follower(高信心精選 tab)source 內含 _render_theme_heat_section。"""
    src = inspect.getsource(app._page_strong_follower)
    assert "_render_theme_heat_section" in src, (
        "_page_strong_follower 沒 wire _render_theme_heat_section"
    )


def test_system_brief_page_wires_theme_heat():
    """_page_system_brief source 內含 _render_theme_heat_section。"""
    src = inspect.getsource(app._page_system_brief)
    assert "_render_theme_heat_section" in src, (
        "_page_system_brief 沒 wire _render_theme_heat_section"
    )
