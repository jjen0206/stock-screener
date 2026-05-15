"""結構性 wire test:確認個股深度頁的接線沒被未來重構不小心拆掉。

不跑 UI(boot smoke 在 test_e2e_smoke 覆蓋),只 inspect.getsource() / 字串
比對,避免「callee 重新命名 → caller 還在叫舊名稱」這類 silent break。

涵蓋:
- _page_stock_detail() 存在於 app.py
- PAGES 含 "📊 個股深度"
- main() router dispatch 該頁
- _page_stock_detail 內呼叫 ≥ 5 個新加的 db.get_* helper
- 各 page 的跳轉接線(短線 / inline_detail / 策略歷史)寫了
  session_state['detail_sid'] + pending_nav

外加一個 AppTest smoke 確認帶 ?sid=2330 個股深度頁 0 exception 且
主要 component 真的渲染(tabs / button / header markdown)。
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _read_app() -> str:
    return Path(APP_PATH).read_text(encoding="utf-8")


# === 結構性 guard:函式 / PAGES / router ===


def test_page_stock_detail_function_exists():
    src = _read_app()
    assert "def _page_stock_detail()" in src, (
        "_page_stock_detail() 必須存在於 app.py"
    )


def test_pages_constant_contains_detail_page():
    src = _read_app()
    # 該字串出現在 PAGES 內 + e2e PAGE_KEYS 內
    assert "📊 個股深度" in src, "PAGES 必須含 \"📊 個股深度\""


def test_main_router_dispatches_detail_page():
    src = _read_app()
    # 路由必須真的 elif 到該函式(不是只塞進 PAGES 卻沒接)
    assert "_page_stock_detail()" in src, (
        "main() 必須呼叫 _page_stock_detail()"
    )


# === _page_stock_detail 內部 helper 呼叫 ===


def test_detail_page_calls_at_least_5_new_helpers():
    """確認 _page_stock_detail() + 它呼叫的 4 個 tab render helper 加起來
    至少 call 到 5 個新加的 db.get_* helper(階段 1 的 6 個 helper)。

    用 inspect.getsource 把該函式跟它 call 的 render helper source 拼起來
    grep helper name — 比 dynamic mock 簡單可靠。
    """
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", APP_PATH)
    assert spec is not None and spec.loader is not None
    app_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_mod)

    # 收集 _page_stock_detail + 4 個 _render_detail_*_tab + _render_detail_header
    func_names = [
        "_page_stock_detail",
        "_render_detail_header",
        "_render_detail_kline_tab",
        "_render_detail_chip_tab",
        "_render_detail_ml_tab",
        "_render_detail_news_tab",
        "_render_detail_paper_trade_status",
    ]
    combined_src = ""
    for fn in func_names:
        assert hasattr(app_mod, fn), f"app.py 缺函式 {fn}"
        combined_src += inspect.getsource(getattr(app_mod, fn))

    expected_helpers = [
        "get_stock_kline_with_indicators",
        "get_inst_history",
        "get_shareholder_history",
        "get_news_for_sid",
        "get_pick_history_for_sid",
        "get_shap_for_sid_latest",
    ]
    found = [h for h in expected_helpers if h in combined_src]
    assert len(found) >= 5, (
        f"個股深度頁應 call ≥ 5 個新 helper,實際 call 到:{found}"
    )


# === 跳轉接線 ===


def test_short_page_has_detail_jump_button():
    src = _read_app()
    # 短線頁的「📊 看細節」button(short_detail_one key)
    assert "short_detail_one" in src, (
        "短線頁應有 short_detail_one 跳轉 button"
    )
    # 設 pending_nav 跳該頁
    assert "\"pending_nav\"] = \"📊 個股深度\"" in src or \
           "'pending_nav'] = '📊 個股深度'" in src, (
        "跳轉接線應 set pending_nav 到「📊 個股深度」"
    )


def test_inline_detail_helper_has_jump_button():
    """_render_table_with_inline_detail 的 detail mode 應 render
    「📊 看完整深度頁」button — 一個改 propagate 到 5 頁 inline detail(關注 /
    市場熱度 / 大戶入場 / 強者跟蹤 / 短線 inline)。
    """
    src = _read_app()
    assert "📊 看完整深度頁" in src, (
        "_render_table_with_inline_detail 應加跳「📊 個股深度」button"
    )
    # button 必須 set detail_sid + pending_nav(不只顯字)
    assert "st.session_state[\"detail_sid\"]" in src, (
        "跳轉接線應 set session_state['detail_sid']"
    )


def test_strategy_history_has_detail_jump():
    src = _read_app()
    # 策略歷史 tab_raw 加的 sid 跳轉 selectbox + button
    assert "strategy_history_jump_sid" in src, (
        "策略歷史頁 tab_raw 應加 sid 跳轉 selectbox"
    )
    assert "strategy_history_jump_btn" in src, (
        "策略歷史頁 tab_raw 應加跳轉 button"
    )


# === AppTest smoke:0 exception + 主要 component 渲染 ===


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """跟 test_e2e_smoke 同樣的 tmp-db / clear-cache fixture(簡化版)。"""
    import streamlit as st
    from src import config, database as db

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "e2e.db"))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    sys.modules.pop("app", None)
    st.cache_data.clear()
    yield tmp_path
    db._reset_path_cache()  # type: ignore[attr-defined]


def test_detail_page_with_sid_2330_renders_no_exception(isolated_db):
    """帶 detail_sid=2330 開深度頁:0 exception + 4 個 tab + header markdown。

    沒灌任何 daily_prices,所以 K 線/籌碼/新聞會走 fallback「無資料」訊息,
    但 page 本身應該 boot 不炸,且 4 個 tab 真的渲染出來。
    """
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["active_page"] = "📊 個股深度"
    at.session_state["detail_sid"] = "2330"
    at.session_state["high_confidence_mode"] = False
    at.run()

    assert not at.exception, (
        f"deep detail page raised: {[str(e.value)[:300] for e in at.exception]}"
    )

    # 4 個 tab 應該渲染出來(st.tabs 在 at.tabs 內)
    tab_labels = {t.label for t in at.tabs}
    assert "📈 K 線" in tab_labels, f"tabs={tab_labels}"
    assert "🚦 籌碼" in tab_labels, f"tabs={tab_labels}"
    assert "🧠 ML 解釋" in tab_labels, f"tabs={tab_labels}"
    assert "📰 新聞" in tab_labels, f"tabs={tab_labels}"


def test_detail_page_default_sid_when_no_session_state(isolated_db):
    """直接開深度頁(無 detail_sid)— 應 fallback 預設 2330,仍 0 exception。"""
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["active_page"] = "📊 個股深度"
    at.session_state["high_confidence_mode"] = False
    at.run()

    assert not at.exception, (
        f"default deep detail page raised: "
        f"{[str(e.value)[:300] for e in at.exception]}"
    )
    # text_input 應有預設值 2330
    sid_input = at.text_input(key="detail_sid_input")
    assert sid_input is not None, "detail_sid_input 應渲染"
    assert sid_input.value == "2330"
