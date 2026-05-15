"""app.py regime gating badge wire 結構性測試。

純結構性(對齊 test_dynamic_weighting_wire.py / test_page_stock_detail_wire.py):
用 inspect.getsource 看 _page_short / _page_long / _page_system_brief 是否
都呼叫了 _render_regime_gating_badge,以及 badge helper 自身結構是否正確。

不 mock streamlit、不跑頁面。
"""
from __future__ import annotations

import inspect

import app


# === Badge helper 自身存在 + 結構 ===

def test_render_regime_gating_badge_exists():
    """_render_regime_gating_badge helper 必須是 module-level callable。"""
    assert hasattr(app, "_render_regime_gating_badge"), (
        "app 缺 _render_regime_gating_badge helper"
    )
    assert callable(app._render_regime_gating_badge)


def test_render_regime_gating_badge_uses_regime_gating_module():
    """badge helper source 必須呼叫 get_regime_gating_params。"""
    src = inspect.getsource(app._render_regime_gating_badge)
    assert "get_regime_gating_params" in src, (
        "_render_regime_gating_badge 沒呼叫 get_regime_gating_params"
    )


def test_render_regime_gating_badge_renders_all_three_colors():
    """badge helper source 必須涵蓋 bull(綠)/ range(黃)/ bear(紅)三種顏色。"""
    src = inspect.getsource(app)
    # 主公拍板綠 / 黃 / 紅 — 顏色 dict 在 module level
    assert "_REGIME_BADGE_COLORS" in src, (
        "app 缺 _REGIME_BADGE_COLORS dict(三色 mapping)"
    )
    # 3 種 regime 都該有對應顏色 key
    for regime in ("bull", "range", "bear"):
        assert f'"{regime}"' in src, (
            f"_REGIME_BADGE_COLORS 缺 {regime} key"
        )


def test_render_regime_gating_badge_explains_max_count_and_threshold():
    """badge helper expander 應顯示「目前推薦最多 N 檔 / 信心 ≥ X」說明。"""
    src = inspect.getsource(app._render_regime_gating_badge)
    assert "short_pick_max_count" in src, "badge 沒顯示短線推薦上限"
    assert "confidence_threshold_uplift" in src, "badge 沒顯示信心 uplift"


def test_render_regime_gating_badge_silent_skip_on_failure():
    """badge helper 必須吃掉所有例外(避免 regime_gating import 失敗就擋頁面)。"""
    src = inspect.getsource(app._render_regime_gating_badge)
    assert "except" in src, "badge helper 沒 try/except,失敗會擋頁面渲染"


# === 三個 pages 都 wire 上 badge ===

def test_page_short_renders_regime_badge():
    """_page_short 標題下方應呼叫 _render_regime_gating_badge。"""
    src = inspect.getsource(app._page_short)
    assert "_render_regime_gating_badge" in src, (
        "_page_short 缺 _render_regime_gating_badge 呼叫"
    )


def test_page_long_renders_regime_badge():
    """_page_long 標題下方應呼叫 _render_regime_gating_badge。"""
    src = inspect.getsource(app._page_long)
    assert "_render_regime_gating_badge" in src, (
        "_page_long 缺 _render_regime_gating_badge 呼叫"
    )


def test_page_system_brief_renders_regime_badge():
    """_page_system_brief 標題下方應呼叫 _render_regime_gating_badge。"""
    src = inspect.getsource(app._page_system_brief)
    assert "_render_regime_gating_badge" in src, (
        "_page_system_brief 缺 _render_regime_gating_badge 呼叫"
    )
