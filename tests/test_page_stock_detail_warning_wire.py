"""結構性 wire test:確認個股深度頁的「⚠️ 警示紀錄」section 接線沒被拆掉。

涵蓋:
  - _render_detail_warnings_section() 函式存在
  - _page_stock_detail() 確實 call _render_detail_warnings_section
  - _render_detail_warnings_section call db.get_warning_history_for_sid
  - render_pick_card 走 _build_card_html 帶 warnings 參數(card 紅色 ⚠️ badge)
  - render_picks_cards 走 enrich_rows_with_warnings(批次 enrich)
  - AppTest smoke:帶 detail_sid + 灌警示紀錄,確認 section 真的被 render
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src import config, database as db, ui_cards


APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _read_app() -> str:
    return Path(APP_PATH).read_text(encoding="utf-8")


# ============================================================================
# 結構性 guard
# ============================================================================

def test_warnings_section_function_exists():
    src = _read_app()
    assert "def _render_detail_warnings_section(" in src, (
        "_render_detail_warnings_section() 必須存在於 app.py"
    )


def test_page_stock_detail_calls_warnings_section():
    """_page_stock_detail() 必須 call _render_detail_warnings_section,
    否則「⚠️ 警示紀錄」section 根本不會出現。"""
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", APP_PATH)
    assert spec is not None and spec.loader is not None
    app_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_mod)
    fn_src = inspect.getsource(app_mod._page_stock_detail)
    assert "_render_detail_warnings_section" in fn_src, (
        "_page_stock_detail 必須 call _render_detail_warnings_section"
    )


def test_warnings_section_calls_db_helper():
    """_render_detail_warnings_section 必須 call db.get_warning_history_for_sid,
    否則 section 沒資料來源,違約交割教訓白學。"""
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", APP_PATH)
    assert spec is not None and spec.loader is not None
    app_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_mod)
    fn_src = inspect.getsource(app_mod._render_detail_warnings_section)
    assert "get_warning_history_for_sid" in fn_src, (
        "_render_detail_warnings_section 必須 call get_warning_history_for_sid"
    )


def test_build_card_html_supports_warnings_parameter():
    """_build_card_html 簽名必須含 warnings 參數(card 紅色 ⚠️ badge 來源)。"""
    sig = inspect.signature(ui_cards._build_card_html)
    assert "warnings" in sig.parameters, (
        "_build_card_html 必須有 warnings 參數,否則卡片無法顯紅色 ⚠️ badge"
    )


def test_enrich_rows_with_warnings_exported():
    """ui_cards 必須 export enrich_rows_with_warnings(批次 enrich)。"""
    assert hasattr(ui_cards, "enrich_rows_with_warnings"), (
        "ui_cards 必須 export enrich_rows_with_warnings 給 app.py 批次 enrich"
    )


def test_render_picks_cards_calls_enrich():
    """render_picks_cards 必須 call enrich_rows_with_warnings(批次 enrich
    避免每張卡單次 query)。"""
    fn_src = inspect.getsource(ui_cards.render_picks_cards)
    assert "enrich_rows_with_warnings" in fn_src, (
        "render_picks_cards 必須 call enrich_rows_with_warnings"
    )


# ============================================================================
# AppTest smoke:帶 sid + 灌警示紀錄,verify section render
# ============================================================================

@pytest.fixture
def isolated_db_with_warning(monkeypatch, tmp_path):
    """獨立 tmp DB,灌一筆 active 違約交割警示給 sid=2330。"""
    import streamlit as st

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "warn.db"))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    # 灌一筆 active 違約交割
    db.upsert_stock_warnings([
        {
            "stock_id": "2330", "warning_type": "default_settlement",
            "announced_date": "2026-05-12", "effective_to": None,
            "reason": "違約交割金額 NT$1,234,567",
        },
    ])
    sys.modules.pop("app", None)
    st.cache_data.clear()
    yield tmp_path
    db._reset_path_cache()  # type: ignore[attr-defined]


def test_detail_page_with_warning_renders_section(isolated_db_with_warning):
    """sid=2330 有 active 違約交割警示 → 詳細頁 section 應出現「警示紀錄」字樣
    且訊息含「違約交割」label。
    """
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["active_page"] = "📊 個股深度"
    at.session_state["detail_sid"] = "2330"
    at.session_state["high_confidence_mode"] = False
    at.run()

    assert not at.exception, (
        f"detail page raised: "
        f"{[str(e.value)[:300] for e in at.exception]}"
    )

    # markdown blocks 內應有「警示紀錄」字樣
    all_md = "\n".join(
        getattr(m, "value", "") or "" for m in at.markdown
    )
    assert "警示紀錄" in all_md, (
        "詳細頁應 render 「⚠️ 警示紀錄」section,實際 markdown 內容沒包含"
    )
    assert "違約交割" in all_md, (
        "section 應顯示警示類別中文「違約交割」,實際 markdown 沒包含"
    )


def test_detail_page_clean_stock_skips_warning_section(monkeypatch, tmp_path):
    """乾淨股(無警示)→ section 整段 graceful skip 不顯,避免空 section 雜訊。"""
    import streamlit as st

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "clean.db"))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()  # 不灌任何警示
    sys.modules.pop("app", None)
    st.cache_data.clear()

    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["active_page"] = "📊 個股深度"
    at.session_state["detail_sid"] = "0050"  # 乾淨股
    at.session_state["high_confidence_mode"] = False
    at.run()

    assert not at.exception, (
        f"clean stock page raised: "
        f"{[str(e.value)[:300] for e in at.exception]}"
    )
    # 「⚠️ 警示紀錄」字樣不該出現(乾淨股不顯整段)
    all_md = "\n".join(
        getattr(m, "value", "") or "" for m in at.markdown
    )
    assert "⚠️ 警示紀錄" not in all_md, (
        "乾淨股不該 render 警示紀錄 section,實際 markdown 卻包含"
    )

    db._reset_path_cache()  # type: ignore[attr-defined]
