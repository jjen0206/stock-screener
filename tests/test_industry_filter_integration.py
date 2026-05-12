"""Structural guards for the short-term industry filter wire-up.

Pure inspect.getsource + regex — never mocks streamlit (per lessons
learned from the prior 'mock streamlit chain' incident). These tests
make sure both `_page_dashboard` and `_page_short` keep calling the
industry filter helpers, and that the helper is invoked *before* the
`[:3]` Top-3 slice on the dashboard (otherwise filtering can starve
Top 3 to <3 items).
"""
from __future__ import annotations

import inspect
import re

import app


# ============================================================================
# Helpers are imported at module level
# ============================================================================

def test_app_imports_industry_filter_helpers():
    """app.py 必須匯入 get_available_industries / filter_picks_by_industry。"""
    src = inspect.getsource(app)
    assert "get_available_industries" in src
    assert "filter_picks_by_industry" in src
    assert "src.industry_filter" in src or "from src.industry_filter" in src


# ============================================================================
# _page_dashboard wire-up
# ============================================================================

def test_page_dashboard_calls_industry_helpers():
    src = inspect.getsource(app._page_dashboard)
    assert "get_available_industries" in src, (
        "_page_dashboard 沒 call get_available_industries"
    )
    assert "filter_picks_by_industry" in src, (
        "_page_dashboard 沒 call filter_picks_by_industry"
    )


def test_page_dashboard_has_industry_multiselect():
    src = inspect.getsource(app._page_dashboard)
    assert 'key="dash_short_industry"' in src, (
        "_page_dashboard multiselect 缺 key=dash_short_industry"
    )
    assert "st.multiselect" in src


def test_page_dashboard_filter_runs_before_top3_slice():
    """filter_picks_by_industry 必須在 [:3] 之前 call,否則過濾完不足 3 檔。"""
    src = inspect.getsource(app._page_dashboard)
    filter_idx = src.find("filter_picks_by_industry")
    slice_idx = src.find("[:3]")
    assert filter_idx > 0, "找不到 filter_picks_by_industry call"
    assert slice_idx > 0, "找不到 [:3] slice"
    assert filter_idx < slice_idx, (
        f"filter_picks_by_industry (@ {filter_idx}) 必須在 [:3] (@ {slice_idx}) 之前"
    )


# ============================================================================
# _page_short wire-up
# ============================================================================

def test_page_short_calls_industry_helpers():
    src = inspect.getsource(app._page_short)
    assert "get_available_industries" in src, (
        "_page_short 沒 call get_available_industries"
    )
    assert "filter_picks_by_industry" in src, (
        "_page_short 沒 call filter_picks_by_industry"
    )


def test_page_short_has_industry_multiselect_with_distinct_key():
    """key=page_short_industry,不可跟首頁 dash_short_industry 撞 key。"""
    src = inspect.getsource(app._page_short)
    assert 'key="page_short_industry"' in src
    # 確認不會誤用 dashboard 的 key
    assert 'key="dash_short_industry"' not in src


def test_page_short_filter_runs_after_enrich_before_tabs():
    """filter 必須在 enrich 完之後 + st.tabs() 之前 call。"""
    src = inspect.getsource(app._page_short)
    # 取第一個 enrich_with_analyst_target — main df enrich,不是 cat tab 內的
    enrich_idx = src.find("enrich_with_analyst_target")
    get_avail_idx = src.find("get_available_industries")
    filter_idx = src.find("filter_picks_by_industry")
    tabs_idx = src.find("st.tabs(")
    assert enrich_idx > 0 and get_avail_idx > 0 and filter_idx > 0 and tabs_idx > 0
    assert enrich_idx < get_avail_idx, (
        "get_available_industries 必須在 enrich_with_analyst_target 之後"
    )
    assert get_avail_idx < tabs_idx, (
        "multiselect (get_available_industries) 必須在 st.tabs() 之前"
    )
    assert filter_idx < tabs_idx, (
        "filter_picks_by_industry 必須在 st.tabs() 之前(全部 tab 才會吃到)"
    )


def test_page_short_filter_applied_to_sub_df_too():
    """category tabs 內的 sub_df 也要 filter,否則切到 'Top 3 趨勢' 等分類 tab
    看到的還是未過濾的清單。
    """
    src = inspect.getsource(app._page_short)
    sub_rows_filter = re.search(
        r"sub_rows\s*=\s*filter_picks_by_industry\(",
        src,
    )
    assert sub_rows_filter, (
        "_page_short 內 category tab 缺 sub_rows = filter_picks_by_industry(...)"
    )
