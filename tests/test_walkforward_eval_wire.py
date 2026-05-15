"""scripts/eval_walkforward.py + ml-weekly-retrain.yml 結構性守住測試。

純結構性 — 不跑 walk-forward(那已在 test_ml_walkforward.py 跑過 5 unit),
不跑 workflow(GitHub Actions 才能跑)。用 inspect / 文字搜尋確保:

1. scripts/eval_walkforward.py 模組可 import,有 main / build_short_pick_dataset_with_dates
   / evaluate_model 三個 callable
2. eval_walkforward.py source 含 ml_walkforward_results INSERT(寫表 wire 對)
3. eval_walkforward.py 預設跑 short_pick + 8 個 per_strategy
4. 既有 ml_walkforward_results 表在 SCHEMA(production schema fixture,
   tmp_path init_db 出來必有此表)
5. ml-weekly-retrain.yml workflow:cron 對 / 5 個關鍵 step / A/B gate inline 邏輯
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from src import config, database as db, ml_walkforward as wf


_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _ROOT / "scripts" / "eval_walkforward.py"
_WORKFLOW_PATH = _ROOT / ".github" / "workflows" / "ml-weekly-retrain.yml"


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """production-schema fixture — init_db 後保證 ml_walkforward_results 在。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def _load_eval_module():
    spec = importlib.util.spec_from_file_location(
        "eval_walkforward", _SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_walkforward"] = module
    spec.loader.exec_module(module)
    return module


# === scripts/eval_walkforward.py ===

def test_eval_walkforward_module_imports_and_exposes_callables():
    m = _load_eval_module()
    for name in ("main", "build_short_pick_dataset_with_dates",
                 "evaluate_model", "DEFAULT_PER_STRATEGY"):
        assert hasattr(m, name), f"eval_walkforward 缺 {name}"
    assert callable(m.main)
    assert callable(m.build_short_pick_dataset_with_dates)
    assert callable(m.evaluate_model)


def test_eval_walkforward_default_models_cover_short_pick_and_7_per_strategy():
    """2026-05-15:gap_up 從 DEFAULT 拿掉(已下架 ML 過濾,見
    docs/gap-up-decision-2026-05-15.md);7 個剩下的 per_strategy 仍要在內。
    """
    m = _load_eval_module()
    expected = {
        "ma_alignment", "bias_convergence", "macd_golden", "bb_lower_rebound",
        "volume_breakout", "taiex_alpha", "big_holder_inflow",
    }
    assert set(m.DEFAULT_PER_STRATEGY) == expected, (
        f"DEFAULT_PER_STRATEGY 應為 7 個 trained per_strategy(gap_up 已下架),"
        f"實際 {set(m.DEFAULT_PER_STRATEGY)}"
    )
    assert "gap_up" not in set(m.DEFAULT_PER_STRATEGY), (
        "gap_up 應從 DEFAULT_PER_STRATEGY 移除(已下架 ML 過濾)"
    )


def test_eval_walkforward_writes_to_ml_walkforward_results_table():
    """source 內必須有 INSERT INTO ml_walkforward_results — 防止意外改表名。"""
    src = _SCRIPT_PATH.read_text(encoding="utf-8")
    assert "ml_walkforward_results" in src, "eval_walkforward.py 缺寫表 SQL"
    assert "INSERT" in src and "ml_walkforward_results" in src
    # 必填欄位都應在 INSERT 中(對齊 SCHEMA)
    for col in ("model_name", "split_idx", "roc_auc", "pr_auc",
                "log_loss", "evaluated_at"):
        assert col in src, f"INSERT 缺欄 {col}"


# === DB schema(production schema fixture)===

def test_ml_walkforward_results_table_exists_after_init_db(tmp_db):
    """init_db 後必須有 ml_walkforward_results 表 — 對齊 production schema。"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='ml_walkforward_results'"
        ).fetchall()
        assert len(rows) == 1, "ml_walkforward_results 表沒建出來"

        # 欄位也要對齊
        cols = {
            r[1] for r in conn.execute(
                "PRAGMA table_info(ml_walkforward_results)"
            ).fetchall()
        }
        for required in ("model_name", "split_idx", "train_start",
                         "train_end", "test_start", "test_end",
                         "train_n", "test_n", "roc_auc", "pr_auc",
                         "log_loss", "train_roc_auc", "evaluated_at"):
            assert required in cols, f"表缺欄 {required}"


def test_ml_walkforward_results_index_exists(tmp_db):
    """idx_ml_walkforward_model 必須在 — 給 GROUP BY model_name 加速。"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_ml_walkforward_model'"
        ).fetchall()
        assert len(rows) == 1, "idx_ml_walkforward_model index 沒建"


# === src.ml_walkforward 模組存在 + 對外介面 ===

def test_ml_walkforward_module_exposes_required_callables():
    for name in ("walkforward_train_test", "walkforward_summary",
                 "DEFAULT_N_SPLITS", "DEFAULT_TEST_SIZE", "DEFAULT_MIN_TRAIN_SIZE"):
        assert hasattr(wf, name), f"src.ml_walkforward 缺 {name}"


# === .github/workflows/ml-weekly-retrain.yml ===

def test_ml_weekly_retrain_workflow_exists_and_parseable():
    import yaml
    assert _WORKFLOW_PATH.exists(), "ml-weekly-retrain.yml 不存在"
    parsed = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert parsed is not None
    # PyYAML 對 workflow yml 把 'on:' 解成 boolean True key(YAML 1.1
    # 'on' aliasing)— 兩個 key 都接受
    assert ("on" in parsed) or (True in parsed), "workflow 缺 on: 觸發定義"
    assert "jobs" in parsed, "workflow 缺 jobs"


def test_ml_weekly_retrain_workflow_has_correct_cron():
    """週六 19:00 UTC = 週日 03:00 TW — 主公拍板的時間。"""
    src = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert 'cron: "0 19 * * 6"' in src or "cron: '0 19 * * 6'" in src, (
        "workflow cron 必須是 '0 19 * * 6'(週日 03:00 TW)"
    )


def test_ml_weekly_retrain_workflow_runs_5_critical_steps():
    """五個關鍵 step 在 source — backup / OLD eval / train / NEW eval / A/B gate。"""
    src = _WORKFLOW_PATH.read_text(encoding="utf-8")
    # backup
    assert ".pre_retrain.bak" in src, "workflow 缺 pkl backup step"
    # OLD baseline 評估
    assert "OLD walk-forward" in src or "OLD_EVAL_TS" in src, (
        "workflow 缺 OLD walk-forward baseline"
    )
    # train(reuse 既有 train script)
    assert "train_ml_model.py" in src, "workflow 缺 train_ml_model.py"
    assert "train_per_strategy_ml.py" in src, "workflow 缺 train_per_strategy_ml.py"
    # NEW walk-forward
    assert "eval_walkforward.py" in src, "workflow 缺 eval_walkforward.py call"
    # A/B gate + rollback
    assert "ROLLBACK" in src and "TOLERANCE" in src, "workflow 缺 A/B gate / rollback 邏輯"
    assert "0.02" in src, "A/B gate tolerance 應為 0.02"


def test_ml_weekly_retrain_workflow_pushes_telegram_summary():
    """A/B summary 必須走 Telegram 推播 — 主公需要週日早上看摘要。"""
    src = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN" in src
    assert "sendMessage" in src or "telegram" in src.lower()
    # AB_SUMMARY env var 用於 Telegram body
    assert "AB_SUMMARY" in src, "workflow 缺 AB_SUMMARY env var(Telegram body)"


def test_ml_weekly_retrain_workflow_has_force_keep_dispatch_input():
    """workflow_dispatch 必須有 force_keep input 給主公手動 override gate。"""
    src = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "force_keep" in src, "workflow 缺 force_keep workflow_dispatch input"


def test_ml_weekly_retrain_workflow_uses_concurrency_group():
    """concurrency.group 防 cron + manual 雙觸發 race(對齊 retrain-ml.yml)。"""
    src = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "concurrency:" in src, "workflow 缺 concurrency: block"
    assert "ml-weekly-retrain" in src
