"""SHAP ML 解釋性 helper + cache schema 單元測試。

用 production schema(db.init_db)+ mock RF model(訓練超小 dataset)避免測試慢。
shap.TreeExplainer 對 RF 的呼叫不 mock — 真的跑一次確認 wire 對。
"""
from __future__ import annotations

import numpy as np
import pytest

from src import config, database as db
from src import ml_shap


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """每個測試一份乾淨 DB(production schema via db.init_db)。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    return db_file


@pytest.fixture
def trained_rf():
    """訓練極小 RF(11 features 對齊 v2 FEATURE_NAMES 前 11 欄)。"""
    from sklearn.ensemble import RandomForestClassifier
    from src.ml_predictor import FEATURE_NAMES

    rng = np.random.RandomState(42)
    n_feat = len(FEATURE_NAMES)
    X = rng.rand(60, n_feat)
    # 讓 class 1 跟 feature 0 有相關性,確保 SHAP 不是全 0
    y = (X[:, 0] > 0.5).astype(int)
    m = RandomForestClassifier(
        n_estimators=10, max_depth=3, random_state=42,
    )
    m.fit(X, y)
    return m


# === compute_pick_shap 正常 case ===

def test_compute_pick_shap_returns_top3_dicts(trained_rf):
    from src.ml_predictor import FEATURE_NAMES

    feats = {name: 0.5 for name in FEATURE_NAMES}
    feats[FEATURE_NAMES[0]] = 0.9  # 該 feature 在 train 時跟 y 有相關

    out = ml_shap.compute_pick_shap("2330", trained_rf, feats, top_k=3)

    assert isinstance(out, list)
    assert len(out) == 3
    for e in out:
        assert "feature" in e
        assert "value" in e
        assert "contribution" in e
        assert "contribution_pct" in e
        assert e["direction"] in ("+", "-")
        assert 0.0 <= e["contribution_pct"] <= 100.0
        assert e["feature"] in FEATURE_NAMES


def test_compute_pick_shap_top3_sorted_by_abs_contribution(trained_rf):
    from src.ml_predictor import FEATURE_NAMES

    feats = {name: 0.5 for name in FEATURE_NAMES}
    feats[FEATURE_NAMES[0]] = 0.95

    out = ml_shap.compute_pick_shap("2330", trained_rf, feats, top_k=3)

    # |shap| descending
    abs_vals = [abs(e["contribution"]) for e in out]
    assert abs_vals == sorted(abs_vals, reverse=True)


# === compute_pick_shap edge cases ===

def test_compute_pick_shap_none_features_returns_empty(trained_rf):
    assert ml_shap.compute_pick_shap("2330", trained_rf, None) == []


def test_compute_pick_shap_none_model_returns_empty():
    assert ml_shap.compute_pick_shap("2330", None, {"kd_k": 0.5}) == []


def test_compute_pick_shap_all_zero_features(trained_rf):
    """所有 feature 都 0 → SHAP 大概率有非零 contribution,但若全 0 應 graceful。"""
    from src.ml_predictor import FEATURE_NAMES

    feats = {name: 0.0 for name in FEATURE_NAMES}
    out = ml_shap.compute_pick_shap("2330", trained_rf, feats, top_k=3)

    # SHAP 對全 0 input 仍會算貢獻(因為 tree 內部仍有 split),所以 len 通常 > 0
    # 至少不應該 crash,且若有結果則 pct sum 合理
    assert isinstance(out, list)
    if out:
        # contribution_pct 加總應該 ≤ 100(top 3 不一定 100%)
        assert sum(e["contribution_pct"] for e in out) <= 100.01


# === format_shap_reason ===

def test_format_shap_reason_basic():
    explanations = [
        {"feature": "holders_delta_w_zscore", "contribution_pct": 12.3, "direction": "+"},
        {"feature": "inst_5d_zscore", "contribution_pct": 8.7, "direction": "+"},
        {"feature": "is_theme_member", "contribution_pct": 3.2, "direction": "-"},
    ]
    s = ml_shap.format_shap_reason(explanations)
    assert s.startswith("🧠 SHAP: ")
    assert "holders_delta_w_zscore +12%" in s
    assert "inst_5d_zscore +9%" in s  # 8.7 → round → 9
    assert "is_theme_member -3%" in s
    assert s.count("/") == 2  # 3 features → 2 separators


def test_format_shap_reason_empty_returns_empty_string():
    assert ml_shap.format_shap_reason([]) == ""
    assert ml_shap.format_shap_reason(None) == ""  # type: ignore[arg-type]


# === Cache schema: save_shap_explanation / get_shap_explanation ===

def test_save_and_get_shap_explanation_roundtrip(tmp_db):
    top = [
        {"feature": "kd_k", "value": 75.0, "contribution": 0.12,
         "contribution_pct": 25.0, "direction": "+"},
        {"feature": "macd_dif", "value": 1.2, "contribution": -0.08,
         "contribution_pct": 16.6, "direction": "-"},
    ]
    db.save_shap_explanation("2026-05-14", "2330", "general", top)

    got = db.get_shap_explanation("2026-05-14", "2330", "general")
    assert got is not None
    assert len(got) == 2
    assert got[0]["feature"] == "kd_k"
    assert got[0]["contribution_pct"] == 25.0
    assert got[1]["direction"] == "-"


def test_get_shap_explanation_cache_miss_returns_none(tmp_db):
    got = db.get_shap_explanation("2026-05-14", "9999", "general")
    assert got is None


def test_save_shap_explanation_upsert_overrides(tmp_db):
    """同 (pick_date, sid, strategy) 重存應該覆蓋。"""
    db.save_shap_explanation(
        "2026-05-14", "2330", "general",
        [{"feature": "kd_k", "contribution_pct": 50.0, "direction": "+"}],
    )
    db.save_shap_explanation(
        "2026-05-14", "2330", "general",
        [{"feature": "vol_ratio", "contribution_pct": 30.0, "direction": "-"}],
    )
    got = db.get_shap_explanation("2026-05-14", "2330", "general")
    assert got is not None
    assert len(got) == 1
    assert got[0]["feature"] == "vol_ratio"


def test_get_shap_explanation_strategy_none_returns_any(tmp_db):
    """strategy=None 撈該 (date, sid) 任一筆。"""
    db.save_shap_explanation(
        "2026-05-14", "2330", "big_holder_inflow",
        [{"feature": "holders_delta_w_zscore", "contribution_pct": 40.0, "direction": "+"}],
    )
    got = db.get_shap_explanation("2026-05-14", "2330")  # strategy=None
    assert got is not None
    assert got[0]["feature"] == "holders_delta_w_zscore"
