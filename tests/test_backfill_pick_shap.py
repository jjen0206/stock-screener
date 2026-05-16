"""scripts/backfill_pick_shap.py 單元測試。

scripts/ 不是 package,用 importlib 載入。Mock extract_features / compute_pick_shap
驗 routing + idempotent + 跳過 missing model。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src import config, database as db

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "backfill_pick_shap.py"
)
_spec = importlib.util.spec_from_file_location(
    "backfill_pick_shap", _SCRIPT,
)
bps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bps)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """tmp DB,init schema。"""
    db_file = tmp_path / "shap.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    return db_file


def _seed_daily_picks(rows: list[tuple[str, str, str]]) -> None:
    """rows: list of (trade_date, sid, strategy)."""
    with db.get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO daily_picks
                (trade_date, universe, strategy, sid, score, rank,
                 params_hash, payload, ml_prob, computed_at)
            VALUES (?, 'pure_stock', ?, ?, NULL, NULL,
                    'default_v1', NULL, NULL, '2026-05-15T00:00:00Z')
            """,
            [(d, s, sid) for (d, sid, s) in rows],
        )


def test_fetch_picks_skips_existing(tmp_db):
    """已有 SHAP 的 (date, sid, strategy) 不會被列入待補。"""
    _seed_daily_picks([
        ("2026-05-01", "2330", "ma_alignment"),
        ("2026-05-01", "2317", "ma_alignment"),
        ("2026-05-02", "2330", "macd_golden"),
    ])
    # 已有 1 筆 SHAP
    db.save_shap_explanation(
        "2026-05-01", "2330", "ma_alignment",
        [{"feature": "x", "value": 1.0, "contribution": 0.5,
          "contribution_pct": 50.0, "direction": "+"}],
    )

    todo = bps._fetch_picks_to_backfill(
        "2026-05-01", "2026-05-02", force=False,
    )
    assert len(todo) == 2
    keys = {(d, sid, s) for (d, sid, s) in todo}
    assert ("2026-05-01", "2330", "ma_alignment") not in keys
    assert ("2026-05-01", "2317", "ma_alignment") in keys
    assert ("2026-05-02", "2330", "macd_golden") in keys


def test_fetch_picks_force_includes_existing(tmp_db):
    """--force 把已有 SHAP 的也列入。"""
    _seed_daily_picks([("2026-05-01", "2330", "ma_alignment")])
    db.save_shap_explanation(
        "2026-05-01", "2330", "ma_alignment",
        [{"feature": "x", "value": 1.0, "contribution": 0.5,
          "contribution_pct": 50.0, "direction": "+"}],
    )
    todo = bps._fetch_picks_to_backfill(
        "2026-05-01", "2026-05-01", force=True,
    )
    assert todo == [("2026-05-01", "2330", "ma_alignment")]


def test_backfill_one_no_model(tmp_db):
    """general_model=None + 非 per-strategy → no_model。"""
    res = bps.backfill_one(
        "2026-05-01", "2330", "some_random_strategy",
        general_model=None,
        strategy_models={},
        strategy_ml_thresholds={},
        extract_features_fn=lambda *a, **kw: {"x": 1.0},
        compute_pick_shap_fn=lambda *a, **kw: [{"feature": "x"}],
        load_strategy_model_fn=lambda s: None,
    )
    assert res == "no_model"


def test_backfill_one_no_feats(tmp_db):
    """extract_features 回 None → no_feats,不寫表。"""
    res = bps.backfill_one(
        "2026-05-01", "2330", "ma_alignment",
        general_model=object(),
        strategy_models={},
        strategy_ml_thresholds={},
        extract_features_fn=lambda *a, **kw: None,
        compute_pick_shap_fn=lambda *a, **kw: [],
        load_strategy_model_fn=lambda s: None,
    )
    assert res == "no_feats"
    assert db.get_shap_explanation("2026-05-01", "2330") is None


def test_backfill_one_ok_uses_per_strategy_model(tmp_db):
    """strategy ∈ STRATEGY_ML_THRESHOLDS + per-strategy model 存在 → 用 per-strategy
    並用 strategy 當 strategy_key 寫表。"""
    per_strategy = object()
    general = object()
    captured = {}

    def compute_shap(sid, model, feats, top_k=3):
        captured["model"] = model
        return [{"feature": "x", "value": 1.0, "contribution": 0.5,
                 "contribution_pct": 50.0, "direction": "+"}]

    res = bps.backfill_one(
        "2026-05-01", "2330", "ma_alignment",
        general_model=general,
        strategy_models={},
        strategy_ml_thresholds={"ma_alignment": 0.55},
        extract_features_fn=lambda *a, **kw: {"x": 1.0},
        compute_pick_shap_fn=compute_shap,
        load_strategy_model_fn=lambda s: per_strategy,
    )
    assert res == "ok"
    assert captured["model"] is per_strategy
    # 寫進 (pick_date, sid, strategy='ma_alignment')
    got = db.get_shap_explanation("2026-05-01", "2330", "ma_alignment")
    assert got and got[0]["feature"] == "x"


def test_backfill_one_fallback_to_general(tmp_db):
    """strategy 不在 STRATEGY_ML_THRESHOLDS → 用 general,strategy_key='general'。"""
    general = object()
    captured = {}

    def compute_shap(sid, model, feats, top_k=3):
        captured["model"] = model
        return [{"feature": "y", "value": 0.5, "contribution": 0.3,
                 "contribution_pct": 30.0, "direction": "+"}]

    res = bps.backfill_one(
        "2026-05-01", "2330", "rsi_recovery",
        general_model=general,
        strategy_models={},
        strategy_ml_thresholds={"ma_alignment": 0.55},
        extract_features_fn=lambda *a, **kw: {"x": 1.0},
        compute_pick_shap_fn=compute_shap,
        load_strategy_model_fn=lambda s: None,
    )
    assert res == "ok"
    assert captured["model"] is general
    got = db.get_shap_explanation("2026-05-01", "2330", "general")
    assert got and got[0]["feature"] == "y"


def test_backfill_one_idempotent_via_save_upsert(tmp_db):
    """連跑 2 次 backfill_one,(pick_date, sid, strategy) 只一筆(UPSERT)。"""
    general = object()
    res1 = bps.backfill_one(
        "2026-05-01", "2330", "ma_alignment",
        general_model=general,
        strategy_models={},
        strategy_ml_thresholds={"ma_alignment": 0.55},
        extract_features_fn=lambda *a, **kw: {"x": 1.0},
        compute_pick_shap_fn=lambda *a, **kw: [
            {"feature": "a", "value": 1.0, "contribution": 0.5,
             "contribution_pct": 50.0, "direction": "+"},
        ],
        load_strategy_model_fn=lambda s: object(),
    )
    res2 = bps.backfill_one(
        "2026-05-01", "2330", "ma_alignment",
        general_model=general,
        strategy_models={},
        strategy_ml_thresholds={"ma_alignment": 0.55},
        extract_features_fn=lambda *a, **kw: {"x": 1.0},
        compute_pick_shap_fn=lambda *a, **kw: [
            {"feature": "b", "value": 2.0, "contribution": 0.7,
             "contribution_pct": 70.0, "direction": "+"},
        ],
        load_strategy_model_fn=lambda s: object(),
    )
    assert res1 == "ok" and res2 == "ok"
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM pick_shap_explanations "
            "WHERE pick_date=? AND sid=? AND strategy=?",
            ("2026-05-01", "2330", "ma_alignment"),
        ).fetchone()["c"]
    assert n == 1
    # 第二次的內容覆寫(top_features 是 'b')
    got = db.get_shap_explanation("2026-05-01", "2330", "ma_alignment")
    assert got[0]["feature"] == "b"


def test_main_no_todo_exits_zero(tmp_db, capsys):
    """範圍內無 daily_picks → exit 0,印出『無待補』。"""
    rc = bps.main(["--start", "2026-05-01", "--end", "2026-05-02"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "無待補" in out


def test_main_bad_dates_exits_two(tmp_db):
    """--start > --end → exit 2。"""
    rc = bps.main(["--start", "2026-05-10", "--end", "2026-05-01"])
    assert rc == 2
