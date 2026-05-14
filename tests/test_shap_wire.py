"""SHAP 解釋性 wire 結構性測試(2026-05-14)。

純結構性檢查 — 不 mock streamlit,只用 inspect.getsource 確認 wire 對。
教訓自「mock streamlit chain 死循環」事故,固守此 pattern。

守住:
1. src/ml_shap.py 模組存在:compute_pick_shap + format_shap_reason
2. database.py 含 save_shap_explanation / get_shap_explanation
3. database.py SCHEMA 含 pick_shap_explanations 表
4. notifier.notify_top_picks source 含 SHAP enrich call(_enrich_picks_with_shap)
5. notifier._enrich_picks_with_shap source 含 db.save_shap_explanation
6. notifier.format_pick_block source 含 shap_reason 取值與輸出
7. ui_cards.render_pick_card source 含 _render_shap_explanation_expander call
8. ui_cards._render_shap_explanation_expander source 含 get_shap_explanation
   + "ML 信心拆解" expander 字串
"""
from __future__ import annotations

import inspect


# ============================================================================
# 1. src/ml_shap.py 模組存在
# ============================================================================

def test_ml_shap_module_exists():
    from src import ml_shap

    assert hasattr(ml_shap, "compute_pick_shap")
    assert callable(ml_shap.compute_pick_shap)
    assert hasattr(ml_shap, "format_shap_reason")
    assert callable(ml_shap.format_shap_reason)


# ============================================================================
# 2. database.py 含 save_shap_explanation / get_shap_explanation
# ============================================================================

def test_database_shap_helpers_exist():
    from src import database as db

    assert hasattr(db, "save_shap_explanation")
    assert callable(db.save_shap_explanation)
    assert hasattr(db, "get_shap_explanation")
    assert callable(db.get_shap_explanation)


def test_database_schema_includes_pick_shap_explanations():
    from src import database as db

    schema_blob = "\n".join(db.SCHEMA)
    assert "pick_shap_explanations" in schema_blob, (
        "SCHEMA 缺 pick_shap_explanations table"
    )
    assert "top_features" in schema_blob, (
        "pick_shap_explanations 缺 top_features 欄"
    )


# ============================================================================
# 3. notifier.notify_top_picks wire
# ============================================================================

def test_notify_top_picks_calls_shap_enrich():
    from src import notifier

    src = inspect.getsource(notifier.notify_top_picks)
    assert "_enrich_picks_with_shap" in src, (
        "notify_top_picks source 缺 _enrich_picks_with_shap call"
    )


def test_enrich_picks_with_shap_saves_to_db():
    from src import notifier

    assert hasattr(notifier, "_enrich_picks_with_shap")
    src = inspect.getsource(notifier._enrich_picks_with_shap)
    assert "save_shap_explanation" in src, (
        "_enrich_picks_with_shap source 缺 save_shap_explanation call"
    )
    assert "compute_pick_shap" in src, (
        "_enrich_picks_with_shap source 缺 compute_pick_shap call"
    )
    assert "shap_reason" in src, (
        "_enrich_picks_with_shap 沒注入 shap_reason"
    )


def test_format_pick_block_renders_shap_reason():
    from src import notifier

    src = inspect.getsource(notifier.format_pick_block)
    assert "shap_reason" in src, (
        "format_pick_block source 缺 shap_reason 取值"
    )


# ============================================================================
# 4. ui_cards SHAP expander wire
# ============================================================================

def test_render_pick_card_calls_shap_expander():
    from src import ui_cards

    src = inspect.getsource(ui_cards.render_pick_card)
    assert "_render_shap_explanation_expander" in src, (
        "render_pick_card source 缺 _render_shap_explanation_expander call"
    )


def test_render_shap_explanation_expander_exists():
    from src import ui_cards

    assert hasattr(ui_cards, "_render_shap_explanation_expander")
    src = inspect.getsource(ui_cards._render_shap_explanation_expander)
    assert "get_shap_explanation" in src, (
        "_render_shap_explanation_expander source 缺 get_shap_explanation"
    )
    assert "ML 信心拆解" in src, (
        "_render_shap_explanation_expander source 缺「ML 信心拆解」expander 字串"
    )


# ============================================================================
# 5. format_shap_reason 輸出格式
# ============================================================================

def test_format_shap_reason_output_has_shap_prefix():
    """Telegram pick block 顯示需要的格式:🧠 SHAP: ..."""
    from src.ml_shap import format_shap_reason

    s = format_shap_reason([
        {"feature": "kd_k", "contribution_pct": 50.0, "direction": "+"},
    ])
    assert "🧠 SHAP:" in s
    assert "kd_k" in s
