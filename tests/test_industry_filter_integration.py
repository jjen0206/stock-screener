"""Structural guards for the short-term industry pre-filter wire-up.

Pure inspect.getsource + regex — never mocks streamlit (per lessons
learned from the prior 'mock streamlit chain' incident). These tests
make sure both `_page_dashboard` and `_page_short` use the new
pre-filter universe pattern: pick industry tags via st.pills, filter
sids via filter_sids_by_industry, THEN run strategies. The old
post-filter pattern (multiselect + filter_picks_by_industry) is gone.
"""
from __future__ import annotations

import inspect
import re

import app
from src.industry_filter import MAINSTREAM_INDUSTRIES


def _strip_comments(src: str) -> str:
    """Strip '# ...' Python comments from each line so call-order assertions
    don't false-match function names that happen to appear in comments."""
    return "\n".join(re.sub(r"\s*#.*$", "", line) for line in src.splitlines())


# ============================================================================
# Module-level imports
# ============================================================================

def test_app_imports_pre_filter_helpers():
    """app.py must import the new pre-filter helpers + constant."""
    src = inspect.getsource(app)
    assert "MAINSTREAM_INDUSTRIES" in src
    assert "filter_sids_by_industry" in src
    assert "get_other_industries" in src
    assert "src.industry_filter" in src or "from src.industry_filter" in src


def test_app_no_longer_imports_post_filter_helper():
    """The old post-filter helper must NOT be imported anymore in app.py.
    (It still exists in src.industry_filter for backward compat, but app.py
    should not depend on it.)"""
    src = inspect.getsource(app)
    assert "filter_picks_by_industry" not in src, (
        "app.py still references the deprecated post-filter helper"
    )


def test_mainstream_industries_constant_size_15():
    """The Top 15 constant must exist and have exactly 15 entries."""
    assert len(MAINSTREAM_INDUSTRIES) == 15


# ============================================================================
# _page_dashboard wire-up (pre-filter pattern)
# ============================================================================

def test_page_dashboard_uses_pills_not_multiselect():
    src = inspect.getsource(app._page_dashboard)
    # New UI: st.pills
    assert "st.pills" in src, "_page_dashboard 沒用 st.pills"
    # Old UI: must be gone
    assert 'key="dash_short_industry"' not in src, (
        "_page_dashboard 還留著舊 multiselect key"
    )


def test_page_dashboard_has_mainstream_and_other_pills():
    src = inspect.getsource(app._page_dashboard)
    assert 'key="dash_mainstream"' in src
    assert 'key="dash_other"' in src
    assert "MAINSTREAM_INDUSTRIES" in src
    assert "get_other_industries" in src


def test_page_dashboard_calls_filter_sids_by_industry():
    src = inspect.getsource(app._page_dashboard)
    assert "filter_sids_by_industry" in src, (
        "_page_dashboard 沒 call filter_sids_by_industry"
    )


def test_page_dashboard_filter_runs_before_strategies():
    """filter_sids_by_industry 必須在 _run_all_strategies_cached( call 之前
    執行 (這是 pre-filter pattern 的核心:strategy 只跑被選產業的 sids)。
    先 strip comments,否則 Chinese 註解內的「_run_all_strategies_cached(同 day...)」
    會誤匹配。"""
    src = _strip_comments(inspect.getsource(app._page_dashboard))
    filter_idx = src.find("filter_sids_by_industry(")
    run_idx = src.find("_run_all_strategies_cached(")
    assert filter_idx > 0, "找不到 filter_sids_by_industry( call"
    assert run_idx > 0, "找不到 _run_all_strategies_cached( call"
    assert filter_idx < run_idx, (
        f"filter_sids_by_industry call (@ {filter_idx}) 必須在 "
        f"_run_all_strategies_cached call (@ {run_idx}) 之前"
    )


def test_page_dashboard_no_post_filter():
    src = inspect.getsource(app._page_dashboard)
    assert "filter_picks_by_industry" not in src, (
        "_page_dashboard 還在用 post-filter,應改 pre-filter"
    )


# ============================================================================
# _page_short wire-up (pre-filter pattern)
# ============================================================================

def test_page_short_uses_pills_not_multiselect_for_industry():
    src = inspect.getsource(app._page_short)
    assert "st.pills" in src, "_page_short 沒用 st.pills"
    assert 'key="page_short_industry"' not in src, (
        "_page_short 還留著舊 multiselect key"
    )


def test_page_short_has_mainstream_and_other_pills():
    src = inspect.getsource(app._page_short)
    assert 'key="page_short_mainstream"' in src
    assert 'key="page_short_other"' in src
    assert "MAINSTREAM_INDUSTRIES" in src
    assert "get_other_industries" in src


def test_page_short_calls_filter_sids_by_industry():
    src = inspect.getsource(app._page_short)
    assert "filter_sids_by_industry" in src, (
        "_page_short 沒 call filter_sids_by_industry"
    )


def test_page_short_filter_runs_before_strategies():
    """filter_sids_by_industry 必須在 _run_all_strategies_cached( call 之前
    執行。先 strip comments 避免註解內提及誤匹配。"""
    src = _strip_comments(inspect.getsource(app._page_short))
    filter_idx = src.find("filter_sids_by_industry(")
    run_idx = src.find("_run_all_strategies_cached(")
    assert filter_idx > 0 and run_idx > 0
    assert filter_idx < run_idx, (
        f"filter_sids_by_industry call (@ {filter_idx}) 必須在 "
        f"_run_all_strategies_cached call (@ {run_idx}) 之前"
    )


def test_page_short_pills_above_submit_button():
    """pills 必須出現在「執行選股」按鈕之前(source order = render order)。"""
    src = inspect.getsource(app._page_short)
    pills_idx = src.find("page_short_mainstream")
    submit_idx = src.find('"執行選股"')
    assert pills_idx > 0, "找不到 page_short_mainstream pills"
    assert submit_idx > 0, "找不到「執行選股」按鈕"
    assert pills_idx < submit_idx, (
        "產業 pills 必須在「執行選股」按鈕上方"
    )


def test_page_short_no_post_filter():
    src = inspect.getsource(app._page_short)
    assert "filter_picks_by_industry" not in src, (
        "_page_short 還在用 post-filter,應改 pre-filter"
    )
