"""「🎲 參數最佳化」tab 結構性守住測試。

純結構性 — 不 mock streamlit、不跑頁面、用 inspect.getsource 看 function body
有沒有 wire 對。對齊 test_page_strategy_history_wire.py 的 pattern。

守住:
1. `app._render_vbt_grid_tab` 存在
2. `_page_strategy_history` source 有 4 個 sub-tab 標籤(原 3 + 🎲 參數最佳化)
3. `_page_strategy_history` source 呼叫 `_render_vbt_grid_tab()`
4. `_render_vbt_grid_tab` source 呼叫 `db.load_vbt_grid_results`
5. database.py 有 vbt_grid_results 表 + 對應 helper functions
"""
from __future__ import annotations

import inspect
import re

import app
from src import database as db


# ============================================================================
# Function 存在
# ============================================================================

def test_render_vbt_grid_tab_function_exists():
    """app._render_vbt_grid_tab 必須是 module-level callable。"""
    assert hasattr(app, "_render_vbt_grid_tab"), (
        "app 缺 _render_vbt_grid_tab function"
    )
    assert callable(app._render_vbt_grid_tab), (
        "_render_vbt_grid_tab 必須是 callable"
    )


# ============================================================================
# Sub-tab 標籤 + dispatch
# ============================================================================

def test_strategy_history_has_four_tabs():
    """_page_strategy_history source 必須含 4 個 sub-tab 標籤(原 3 + 新增 🎲)。"""
    src = inspect.getsource(app._page_strategy_history)
    expected_tabs = (
        "📈 by-strategy",
        "📅 by-date",
        "📦 全部結算明細",
        "🎲 參數最佳化",
    )
    for tab_label in expected_tabs:
        assert tab_label in src, (
            f"_page_strategy_history source 缺 sub-tab 標籤「{tab_label}」"
        )


def test_strategy_history_calls_render_vbt_grid_tab():
    """_page_strategy_history 必須呼叫 _render_vbt_grid_tab()。"""
    src = inspect.getsource(app._page_strategy_history)
    pattern = re.compile(r"_render_vbt_grid_tab\s*\(")
    assert pattern.search(src), (
        "_page_strategy_history source 沒 call _render_vbt_grid_tab()"
    )


# ============================================================================
# Render function 對接 DB helper
# ============================================================================

def test_render_vbt_grid_tab_uses_load_helper():
    """_render_vbt_grid_tab 必須呼叫 db.load_vbt_grid_results。"""
    src = inspect.getsource(app._render_vbt_grid_tab)
    assert "load_vbt_grid_results" in src, (
        "_render_vbt_grid_tab source 沒 call db.load_vbt_grid_results"
    )


def test_render_vbt_grid_tab_warns_not_auto_default():
    """守住「不自動覆蓋既有 production default」安全聲明,主公手動採用。"""
    src = inspect.getsource(app._render_vbt_grid_tab)
    # 任一關鍵詞 hit 即可(保守拆兩種同義 wording)
    has_warning = (
        "不自動覆蓋" in src
        or "不自動推進" in src
        or "建議" in src and "手動" in src
    )
    assert has_warning, (
        "_render_vbt_grid_tab 必須明示「結果不自動推進 production」,只是建議"
    )


# ============================================================================
# DB helpers + schema
# ============================================================================

def test_database_has_vbt_grid_helpers():
    """database 模組必須暴露 upsert_vbt_grid_results / load_vbt_grid_results。"""
    assert hasattr(db, "upsert_vbt_grid_results"), (
        "src.database 缺 upsert_vbt_grid_results"
    )
    assert hasattr(db, "load_vbt_grid_results"), (
        "src.database 缺 load_vbt_grid_results"
    )


def test_vbt_grid_results_table_created(tmp_path, monkeypatch):
    """init_db 建表 — schema 含 strategy / params_hash 雙 PK 必要欄位。"""
    from src import config

    db_file = tmp_path / "wire.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    try:
        db.init_db()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='vbt_grid_results'"
            ).fetchone()
            assert row is not None, "vbt_grid_results 表沒建出來"

            # 確認必要欄位
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(vbt_grid_results)").fetchall()
            }
        required = {
            "strategy", "params_hash", "params_json",
            "period_start", "period_end",
            "n_trades", "total_return", "sharpe", "max_drawdown",
            "win_rate", "generated_at",
        }
        missing = required - cols
        assert not missing, f"vbt_grid_results 缺欄位:{missing}"
    finally:
        db._reset_path_cache()
