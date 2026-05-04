"""src/ml_predictor.py 測試 — 主要測 predict_batch 行為(Stage 1 加的 batch 介面)。

不測 train_short_pick_model(已在 train_ml_model.py 端到端跑過),也不測
extract_features 的 SQL 細節(那是 backfill 範疇)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import ml_predictor


class _FakeModel:
    """Mock sklearn classifier — 每個 sample 回 predict_proba 固定 row。"""

    def __init__(self, classes=(0, 1), proba_per_row=None):
        self.classes_ = list(classes)
        # proba_per_row: list[list[float]], 對應每筆 sample 的 [P(class=0), P(class=1)]
        self._proba_per_row = proba_per_row or []

    def predict_proba(self, X: pd.DataFrame):
        n = len(X)
        if self._proba_per_row and len(self._proba_per_row) >= n:
            return np.array(self._proba_per_row[:n])
        # default: 全 0.5
        return np.array([[0.5, 0.5]] * n)


def test_predict_batch_returns_dict_keyed_by_sid(monkeypatch):
    """basic — extract_features 給每 sid 假 features,model 回 [0.3, 0.7]。"""
    fake_features = {f: 0.0 for f in ml_predictor.FEATURE_NAMES}
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, target_date, db_path=None: dict(fake_features),
    )
    model = _FakeModel(classes=(0, 1), proba_per_row=[[0.3, 0.7], [0.4, 0.6]])

    out = ml_predictor.predict_batch(model, ["2330", "2317"], "2026-05-04")
    assert out == {"2330": pytest.approx(0.7), "2317": pytest.approx(0.6)}


def test_predict_batch_handles_missing_history(monkeypatch):
    """extract_features 回 None 的 sid → 對應 ml_prob = None。"""
    def _ext(sid, target_date, db_path=None):
        if sid == "no_data":
            return None
        return {f: 0.0 for f in ml_predictor.FEATURE_NAMES}

    monkeypatch.setattr(ml_predictor, "extract_features", _ext)
    model = _FakeModel(classes=(0, 1), proba_per_row=[[0.4, 0.6]])

    out = ml_predictor.predict_batch(
        model, ["2330", "no_data"], "2026-05-04",
    )
    assert out["2330"] == pytest.approx(0.6)
    assert out["no_data"] is None


def test_predict_batch_empty_input_returns_empty_dict():
    out = ml_predictor.predict_batch(_FakeModel(), [], "2026-05-04")
    assert out == {}


def test_predict_batch_no_model_returns_empty_dict():
    out = ml_predictor.predict_batch(None, ["2330"], "2026-05-04")
    assert out == {}


def test_predict_batch_falls_back_to_zero_when_class_1_missing(
    monkeypatch,
):
    """model.classes_=(0,) 沒 win class → 全 0.0(而非 None,讓 caller 還能 filter)。"""
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, td, db_path=None: {f: 0.0 for f in ml_predictor.FEATURE_NAMES},
    )
    model = _FakeModel(classes=(0,), proba_per_row=[[1.0]])
    out = ml_predictor.predict_batch(model, ["2330"], "2026-05-04")
    assert out["2330"] == 0.0


def test_predict_batch_handles_predict_proba_exception(monkeypatch):
    """sklearn predict_proba 拋 exception → 回全 None(不 raise)。"""
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, td, db_path=None: {f: 0.0 for f in ml_predictor.FEATURE_NAMES},
    )

    class _Boom:
        classes_ = [0, 1]

        def predict_proba(self, X):
            raise RuntimeError("synthetic sklearn fail")

    out = ml_predictor.predict_batch(_Boom(), ["2330", "2317"], "2026-05-04")
    assert out == {"2330": None, "2317": None}


# === Stage 2B per-strategy 路由 ===

def test_load_strategy_model_returns_none_when_pkl_missing(monkeypatch, tmp_path):
    """models/per_strategy/<name>.pkl 不存在 → 回 None。"""
    monkeypatch.setattr(
        ml_predictor, "per_strategy_model_path",
        lambda name: tmp_path / f"{name}.pkl",
    )
    assert ml_predictor.load_strategy_model("nonexistent_strategy") is None


def test_load_strategy_model_loads_existing_pkl(monkeypatch, tmp_path):
    """pkl 存在 → joblib load 出 model 物件(不為 None)。"""
    import joblib

    pkl_path = tmp_path / "fake_strat.pkl"
    fake_obj = {"version": "v0", "classes_": [0, 1]}
    joblib.dump(fake_obj, pkl_path)

    monkeypatch.setattr(
        ml_predictor, "per_strategy_model_path",
        lambda name: tmp_path / f"{name}.pkl",
    )
    out = ml_predictor.load_strategy_model("fake_strat")
    assert out == fake_obj


def test_predict_for_strategy_uses_per_strategy_model_when_provided(monkeypatch):
    """strategy_model 直接傳入 → 用該 model 預測,不 load disk pkl。"""
    fake_features = {f: 0.0 for f in ml_predictor.FEATURE_NAMES}
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, td, db_path=None: dict(fake_features),
    )
    # load_strategy_model 不該被叫到 — caller 已預載並傳 strategy_model
    monkeypatch.setattr(
        ml_predictor, "load_strategy_model",
        lambda name: pytest.fail(f"load_strategy_model 不應被叫到 (got {name})"),
    )
    strategy_model = _FakeModel(classes=(0, 1), proba_per_row=[[0.2, 0.8]])
    fallback = _FakeModel(classes=(0, 1), proba_per_row=[[0.5, 0.5]])

    out = ml_predictor.predict_for_strategy(
        strategy_name="ma_alignment",
        stock_ids=["2330"],
        target_date="2026-05-04",
        fallback_model=fallback,
        strategy_model=strategy_model,
    )
    assert out == {"2330": pytest.approx(0.8)}


def test_predict_for_strategy_uses_fallback_when_no_strategy_model(monkeypatch):
    """strategy_model=None → 直接用 fallback_model(不 disk load,不再 auto-load
    避免 N×N 次 IO)。"""
    fake_features = {f: 0.0 for f in ml_predictor.FEATURE_NAMES}
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, td, db_path=None: dict(fake_features),
    )
    monkeypatch.setattr(
        ml_predictor, "load_strategy_model",
        lambda name: pytest.fail(f"strategy_model=None 時不該觸發 disk load (got {name})"),
    )
    fallback = _FakeModel(classes=(0, 1), proba_per_row=[[0.4, 0.6]])

    out = ml_predictor.predict_for_strategy(
        strategy_name="missing_strat",
        stock_ids=["2330"],
        target_date="2026-05-04",
        fallback_model=fallback,
    )
    assert out == {"2330": pytest.approx(0.6)}


def test_predict_for_strategy_returns_all_none_when_no_models_at_all():
    """strategy_model=None + fallback_model=None → 全 None(不 raise)。"""
    out = ml_predictor.predict_for_strategy(
        strategy_name="missing_strat",
        stock_ids=["2330", "2317"],
        target_date="2026-05-04",
        fallback_model=None,
    )
    assert out == {"2330": None, "2317": None}


def test_predict_for_strategy_with_no_strategy_name_uses_fallback(monkeypatch):
    """strategy_name=None → 直接 fallback_model。"""
    fake_features = {f: 0.0 for f in ml_predictor.FEATURE_NAMES}
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, td, db_path=None: dict(fake_features),
    )
    fallback = _FakeModel(classes=(0, 1), proba_per_row=[[0.4, 0.6]])

    out = ml_predictor.predict_for_strategy(
        strategy_name=None,
        stock_ids=["2330"],
        target_date="2026-05-04",
        fallback_model=fallback,
    )
    assert out == {"2330": pytest.approx(0.6)}


def test_predict_for_strategy_empty_sids_returns_empty():
    out = ml_predictor.predict_for_strategy(
        strategy_name="ma_alignment",
        stock_ids=[],
        target_date="2026-05-04",
    )
    assert out == {}
