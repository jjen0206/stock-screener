"""結構性 wire test + AppTest smoke:確認個股深度頁的「🎯 軍師判讀」section 接線。

涵蓋:
  - render_stock_verdict / compute_verdict / verdict_tag_for_card 公開 API 存在
  - _page_stock_detail 確實 call render_stock_verdict
  - ui_cards.render_pick_card 走 _verdict_tag_cached(卡片短標)
  - notifier.format_pick_block 帶軍師判讀字串(推播 pick block)
  - AppTest smoke:帶 sid + 灌違約警示 → 詳細頁出現「軍師判讀」+「不進場」字樣
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src import config, database as db, individual_stock_verdict as isv
from src import ui_cards
from src import notifier as nt


APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _read_app() -> str:
    return Path(APP_PATH).read_text(encoding="utf-8")


# ============================================================================
# 結構性 guard:公開 API + caller wiring
# ============================================================================

def test_isv_public_api_exists():
    """軍師判讀模組必有的 5 個公開 API。"""
    for name in (
        "is_enabled", "compute_verdict", "render_stock_verdict",
        "verdict_tag_for_card", "latest_pattern_phrase",
    ):
        assert hasattr(isv, name), f"individual_stock_verdict 缺 API: {name}"


def test_page_stock_detail_renders_verdict():
    """_page_stock_detail() 必須 call render_stock_verdict,否則主公看不到結論。"""
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", APP_PATH)
    assert spec is not None and spec.loader is not None
    app_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_mod)
    fn_src = inspect.getsource(app_mod._page_stock_detail)
    assert "render_stock_verdict" in fn_src or "individual_stock_verdict" in fn_src, (
        "_page_stock_detail 必須 call individual_stock_verdict.render_stock_verdict"
    )


def test_ui_cards_uses_verdict_tag():
    """render_pick_card 必須 call _verdict_tag_cached (cached wrapper),
    否則 138 卡片各跑 6 SQL queries 會 timeout。"""
    fn_src = inspect.getsource(ui_cards.render_pick_card)
    assert "_verdict_tag_cached" in fn_src or "verdict_tag" in fn_src, (
        "render_pick_card 應顯示「🎯 軍師判讀」短標"
    )


def test_notifier_pick_block_emits_verdict_line():
    """format_pick_block 內必須有「軍師判讀」字串,主公推播訊息一眼看到結論。"""
    src = inspect.getsource(nt.format_pick_block)
    assert "軍師判讀" in src, "format_pick_block 必須輸出「軍師判讀」行"


def test_pattern_section_uses_plain_language_helper():
    """K 線形態 section 必須 call latest_pattern_phrase 把術語翻成白話。"""
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", APP_PATH)
    assert spec is not None and spec.loader is not None
    app_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_mod)
    fn_src = inspect.getsource(app_mod._render_detail_patterns_section)
    assert "latest_pattern_phrase" in fn_src or "PATTERN_MEANINGS" in fn_src, (
        "_render_detail_patterns_section 必須用白話 helper"
    )


# ============================================================================
# AppTest smoke
# ============================================================================

@pytest.fixture
def isolated_db_with_red_flag(monkeypatch, tmp_path):
    """獨立 tmp DB,灌違約交割警示給 sid=9999。"""
    import streamlit as st

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "verdict.db"))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    monkeypatch.setenv("STOCK_VERDICT_ENABLED", "true")
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    db.upsert_stock_warnings([
        {
            "stock_id": "9999", "warning_type": "default_settlement",
            "announced_date": "2026-05-12", "effective_to": None,
            "reason": "違約交割金額 NT$1,830,000,000",
        },
    ])
    sys.modules.pop("app", None)
    st.cache_data.clear()
    yield tmp_path
    db._reset_path_cache()  # type: ignore[attr-defined]


def test_detail_page_red_flag_renders_no_entry_verdict(isolated_db_with_red_flag):
    """sid=9999 有違約交割 active → 詳細頁應出現「軍師判讀」+「不進場」字樣。"""
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["active_page"] = "📊 個股深度"
    at.session_state["detail_sid"] = "9999"
    at.session_state["high_confidence_mode"] = False
    at.run()

    assert not at.exception, (
        f"detail page with red flag raised: "
        f"{[str(e.value)[:300] for e in at.exception]}"
    )

    all_md = "\n".join(getattr(m, "value", "") or "" for m in at.markdown)
    assert "軍師判讀" in all_md, "詳細頁應 render「🎯 軍師判讀」title"
    assert "不進場" in all_md, "違約警示股應顯示「不進場」結論"


def test_detail_page_kill_switch_off_skips_verdict(monkeypatch, tmp_path):
    """STOCK_VERDICT_ENABLED=false → 不該顯軍師判讀 banner。"""
    import streamlit as st

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "ks.db"))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    monkeypatch.setenv("STOCK_VERDICT_ENABLED", "false")
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    sys.modules.pop("app", None)
    st.cache_data.clear()

    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["active_page"] = "📊 個股深度"
    at.session_state["detail_sid"] = "0050"
    at.session_state["high_confidence_mode"] = False
    at.run()

    assert not at.exception, (
        f"detail page with verdict off raised: "
        f"{[str(e.value)[:300] for e in at.exception]}"
    )
    all_md = "\n".join(getattr(m, "value", "") or "" for m in at.markdown)
    # 大字 banner 不該出現(只該有停用 caption)
    assert "🎯 軍師判讀" not in all_md or "停用" in all_md

    db._reset_path_cache()  # type: ignore[attr-defined]
