"""src/ml_ensemble.py 單元測試 — 不打 DB / 純 numpy 合成資料,跑 stacking +
multi-task + backward compat 介面。

涵蓋:
  - StackingEnsembleModel sklearn-compat API(predict_proba / classes_ /
    n_features_in_)— 確保 ml_predictor.predict_batch 不需改就能跑
  - train_stacking_ensemble 基本路徑(3 base learners + meta blend)
  - 樣本不足 raise ValueError(MIN_STACKING_SAMPLES gate)
  - 單類 raise ValueError
  - train_multitask_lgbm 多 horizon heads + NaN mask 跳過
  - predict_multitask 回 DataFrame with h{N}d columns
  - is_ensemble duck-type check
  - load_model 對 ensemble pickle 的 round-trip
  - predict_batch 對 ensemble 的 backward-compat(共用 ml_predictor 既有路徑)
"""
from __future__ import annotations

import joblib
import numpy as np
import pandas as pd
import pytest

from src import ml_ensemble, ml_predictor


# ---------- fixtures ----------

@pytest.fixture
def synthetic_training_data():
    """合成 600 個 sample × 5 個 feature 的 binary classification dataset。

    label 跟 feature_0 + feature_1 - feature_2 有線性相關 + 一點 noise,讓
    LightGBM/LR/RF 都能學到信號(AUC > 0.55 即可,不必逼近 1)。
    """
    rng = np.random.default_rng(42)
    n = 600
    feature_names = [f"feat_{i}" for i in range(5)]
    X = pd.DataFrame(
        rng.normal(0, 1, size=(n, 5)),
        columns=feature_names,
    )
    # logit = w·x + noise → sigmoid → bernoulli
    logit = 0.8 * X["feat_0"] + 0.6 * X["feat_1"] - 0.5 * X["feat_2"] + 0.3 * rng.normal(0, 1, n)
    prob = 1.0 / (1.0 + np.exp(-logit))
    y = pd.Series((rng.uniform(0, 1, n) < prob).astype(int))
    return X, y, feature_names


@pytest.fixture
def synthetic_multitask_data():
    """600 sample dataset + 4 horizons of labels(some NaN 在 1d / 10d 模擬資料不足)。"""
    rng = np.random.default_rng(7)
    n = 600
    feature_names = [f"feat_{i}" for i in range(5)]
    X = pd.DataFrame(rng.normal(0, 1, size=(n, 5)), columns=feature_names)
    base_logit = 0.5 * X["feat_0"] + 0.3 * X["feat_1"]
    y5 = pd.Series((rng.uniform(0, 1, n) < 1.0 / (1.0 + np.exp(-base_logit))).astype(int))
    y3 = pd.Series((rng.uniform(0, 1, n) < 1.0 / (1.0 + np.exp(-(base_logit * 0.8)))).astype(int))
    y1 = pd.Series((rng.uniform(0, 1, n) < 1.0 / (1.0 + np.exp(-(base_logit * 0.5)))).astype(int))
    y10 = pd.Series((rng.uniform(0, 1, n) < 1.0 / (1.0 + np.exp(-(base_logit * 1.2)))).astype(int))
    # 模擬 10d 後段資料不夠 → 後 100 row NaN
    y10_f = y10.astype(float)
    y10_f.iloc[-100:] = float("nan")
    return X, {1: y1.astype(float), 3: y3.astype(float), 5: y5.astype(float), 10: y10_f}, feature_names


# ---------- StackingEnsembleModel sklearn-compat ----------

def test_stacking_model_exposes_sklearn_classifier_api(synthetic_training_data):
    X, y, feat_names = synthetic_training_data
    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )
    # sklearn-compat attributes
    assert hasattr(ensemble, "predict_proba")
    assert hasattr(ensemble, "classes_")
    assert hasattr(ensemble, "n_features_in_")
    assert list(ensemble.classes_) == [0, 1]
    assert ensemble.n_features_in_ == len(feat_names)


def test_stacking_predict_proba_shape_and_range(synthetic_training_data):
    X, y, feat_names = synthetic_training_data
    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )
    proba = ensemble.predict_proba(X.head(10))
    assert proba.shape == (10, 2)
    assert (proba >= 0).all() and (proba <= 1).all()
    # rows sum to ~1
    assert np.allclose(proba.sum(axis=1), 1.0, atol=1e-6)


def test_stacking_predict_returns_binary(synthetic_training_data):
    X, y, feat_names = synthetic_training_data
    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )
    pred = ensemble.predict(X.head(20))
    assert pred.shape == (20,)
    assert set(np.unique(pred).tolist()) <= {0, 1}


def test_stacking_alignment_reorders_columns(synthetic_training_data):
    """X 給的 columns 順序混亂 → ensemble 內部按 feature_names 對齊。"""
    X, y, feat_names = synthetic_training_data
    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )
    shuffled_cols = list(reversed(feat_names))
    X_shuffled = X.head(10)[shuffled_cols]
    proba = ensemble.predict_proba(X_shuffled)
    assert proba.shape == (10, 2)


def test_stacking_missing_feature_raises(synthetic_training_data):
    X, y, feat_names = synthetic_training_data
    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )
    X_missing = X.drop(columns=["feat_0"]).head(5)
    with pytest.raises(ValueError, match="缺 features"):
        ensemble.predict_proba(X_missing)


# ---------- train_stacking_ensemble ----------

def test_train_stacking_ensemble_returns_model_and_metrics(synthetic_training_data):
    X, y, feat_names = synthetic_training_data
    ensemble, metrics = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )
    assert isinstance(ensemble, ml_ensemble.StackingEnsembleModel)
    assert "oof_auc_per_learner" in metrics
    assert set(metrics["oof_auc_per_learner"].keys()) == {"lgbm", "lr", "rf"}
    assert "meta_oof_auc" in metrics
    assert "meta_oof_brier" in metrics
    assert metrics["n_train"] == len(X)
    assert "feature_importances" in metrics


def test_train_stacking_below_min_samples_raises():
    """樣本 < MIN_STACKING_SAMPLES → ValueError(caller fallback 到 RF)。"""
    rng = np.random.default_rng(0)
    X = pd.DataFrame(rng.normal(0, 1, size=(50, 3)), columns=["a", "b", "c"])
    y = pd.Series((rng.uniform(0, 1, 50) < 0.5).astype(int))
    with pytest.raises(ValueError, match="樣本太少"):
        ml_ensemble.train_stacking_ensemble(X, y)


def test_train_stacking_single_class_raises(synthetic_training_data):
    X, _, feat_names = synthetic_training_data
    y_const = pd.Series([1] * len(X))  # 全 win
    with pytest.raises(ValueError, match="全同類"):
        ml_ensemble.train_stacking_ensemble(X, y_const, feature_names=feat_names)


def test_train_stacking_learns_signal_better_than_chance(synthetic_training_data):
    """合成 data 帶顯著線性信號,OOF AUC 應 > 0.55(不到 0.55 = 模型出問題)。"""
    X, y, feat_names = synthetic_training_data
    _, metrics = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )
    # 至少一個 base learner OOF AUC > 0.55
    aucs = metrics["oof_auc_per_learner"]
    assert max(aucs.values()) > 0.55, f"All OOF AUC ≤ 0.55: {aucs}"
    # Meta-learner 整體 OOF AUC 也要 > 0.55
    assert metrics["meta_oof_auc"] > 0.55


# ---------- train_multitask_lgbm ----------

def test_train_multitask_lgbm_returns_dict_per_horizon(synthetic_multitask_data):
    X, y_dict, feat_names = synthetic_multitask_data
    heads = ml_ensemble.train_multitask_lgbm(X[feat_names], y_dict)
    # 1/3/5 都該有(10 後段 NaN 仍有 500 個非 NaN → 也該有)
    assert set(heads.keys()) >= {1, 3, 5, 10}
    for h, model in heads.items():
        assert hasattr(model, "predict_proba")


def test_train_multitask_lgbm_skips_horizon_with_too_few_samples():
    rng = np.random.default_rng(0)
    n = 100
    X = pd.DataFrame(rng.normal(0, 1, size=(n, 3)), columns=["a", "b", "c"])
    # horizon 1 全 NaN → 跳過;horizon 3 只有 20 個非 NaN → 跳過(< 30)
    y_1 = pd.Series([float("nan")] * n)
    y_3 = pd.Series([float("nan")] * (n - 20) + [0] * 10 + [1] * 10)
    y_5 = pd.Series((rng.uniform(0, 1, n) < 0.5).astype(int)).astype(float)
    heads = ml_ensemble.train_multitask_lgbm(X, {1: y_1, 3: y_3, 5: y_5})
    assert 1 not in heads
    assert 3 not in heads
    assert 5 in heads


# ---------- predict_multitask ----------

def test_predict_multitask_returns_dataframe_with_h5d_and_aux(synthetic_multitask_data):
    X, y_dict, feat_names = synthetic_multitask_data
    # 5d 是主 label;1/3/10d 是 aux
    main_y = y_dict[5].astype(int)
    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X[feat_names], main_y,
        feature_names=feat_names,
        multitask_y={1: y_dict[1], 3: y_dict[3], 10: y_dict[10]},
    )
    mt = ensemble.predict_multitask(X.head(15))
    assert isinstance(mt, pd.DataFrame)
    assert "h5d" in mt.columns
    # aux heads 應該有(NaN 樣本不夠 5d 那些直接由 train_multitask_lgbm 跳)
    assert any(c.startswith("h1d") or c.startswith("h3d") or c.startswith("h10d") for c in mt.columns)
    # 每個 column 值都在 [0, 1]
    for c in mt.columns:
        assert (mt[c] >= 0).all() and (mt[c] <= 1).all()


# ---------- is_ensemble + pickle compat ----------

def test_is_ensemble_returns_true_for_stacking(synthetic_training_data):
    X, y, feat_names = synthetic_training_data
    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )
    assert ml_ensemble.is_ensemble(ensemble) is True


def test_is_ensemble_returns_false_for_rf():
    from sklearn.ensemble import RandomForestClassifier

    rf = RandomForestClassifier()
    assert ml_ensemble.is_ensemble(rf) is False


def test_is_ensemble_returns_false_for_none():
    assert ml_ensemble.is_ensemble(None) is False


def test_stacking_model_pickle_roundtrip(synthetic_training_data, tmp_path):
    """joblib dump → load → 仍能 predict_proba(對齊 ml_predictor.load_model 路徑)。"""
    X, y, feat_names = synthetic_training_data
    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )
    pkl = tmp_path / "ens.pkl"
    joblib.dump(ensemble, pkl)
    loaded = joblib.load(pkl)
    assert ml_ensemble.is_ensemble(loaded)
    proba_orig = ensemble.predict_proba(X.head(5))
    proba_loaded = loaded.predict_proba(X.head(5))
    assert np.allclose(proba_orig, proba_loaded, atol=1e-9)


def test_ml_predictor_load_model_handles_ensemble(synthetic_training_data, tmp_path):
    """ml_predictor.load_model 在 ensemble pkl 上 round-trip 後仍 callable。"""
    X, y, feat_names = synthetic_training_data
    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )
    pkl = tmp_path / "ens.pkl"
    ml_predictor.save_model(ensemble, pkl)
    loaded = ml_predictor.load_model(pkl)
    assert loaded is not None
    assert hasattr(loaded, "predict_proba")
    assert list(loaded.classes_) == [0, 1]
    assert loaded.n_features_in_ == len(feat_names)


# ---------- predict_batch backward-compat with ensemble ----------

def test_predict_batch_works_on_stacking_ensemble(monkeypatch, synthetic_training_data):
    """ml_predictor.predict_batch 不需要任何修改就能跑 stacking ensemble。

    這個測試是 backward-compat 保險:只要 ensemble 仍實作 sklearn classifier API
    (predict_proba / classes_ / n_features_in_),既有 caller 完全不用動。
    """
    # 把訓練 features 對齊到 ml_predictor.FEATURE_NAMES 的前 5 個
    feat_names = ml_predictor.FEATURE_NAMES[:5]
    X, y, _ = synthetic_training_data
    X.columns = feat_names

    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=feat_names,
    )

    # 假 extract_features:回 dict of {feat_name: 0.0} for FEATURE_NAMES[:5]
    # _aligned_feature_names 會看 ensemble.n_features_in_ = 5 → slice FEATURE_NAMES[:5]
    fake_feats = {f: 0.0 for f in feat_names}
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, target_date, db_path=None: dict(fake_feats),
    )

    out = ml_predictor.predict_batch(ensemble, ["2330", "2317"], "2026-05-04")
    # 所有 sid 應回 float ∈ [0, 1]
    assert set(out.keys()) == {"2330", "2317"}
    for sid, prob in out.items():
        assert prob is not None
        assert 0.0 <= prob <= 1.0


# ---------- multi-task labels NaN handling ----------

def test_train_stacking_with_multitask_stores_aux_heads(synthetic_multitask_data):
    X, y_dict, feat_names = synthetic_multitask_data
    main_y = y_dict[5].astype(int)
    ensemble, metrics = ml_ensemble.train_stacking_ensemble(
        X[feat_names], main_y,
        feature_names=feat_names,
        multitask_y={1: y_dict[1], 3: y_dict[3], 10: y_dict[10]},
    )
    # ensemble 儲存了 aux heads
    assert ensemble.multitask_heads
    assert set(ensemble.multitask_heads.keys()) <= {1, 3, 10}
    assert "multitask_horizons" in metrics


# ---------- min_samples override ----------

def test_train_stacking_min_samples_override_allows_small_set():
    """min_samples=10 → 50-sample 也能訓(供測試 / debug 用)。"""
    rng = np.random.default_rng(99)
    n = 50
    X = pd.DataFrame(rng.normal(0, 1, size=(n, 3)), columns=["a", "b", "c"])
    y = pd.Series((rng.uniform(0, 1, n) < 0.5).astype(int))
    ensemble, _ = ml_ensemble.train_stacking_ensemble(
        X, y, feature_names=["a", "b", "c"], min_samples=10,
    )
    assert isinstance(ensemble, ml_ensemble.StackingEnsembleModel)
