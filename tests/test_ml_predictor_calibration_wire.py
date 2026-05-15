"""ml_predictor 走 calibrator 的 wiring 守住 — predict_batch / predict_for_strategy
/ predict_short_pick_winrate 三條都該:

  1. calibrator=None → 跟舊行為一致(raw prob 直接回)
  2. calibrator 給就 transform
  3. ML_CALIBRATION_ENABLED=false → 不論 calibrator 給不給都走 raw
  4. calibrator.transform 拋 → fallback raw(不 crash production)

不打 DB(extract_features monkeypatch),不訓 RF(FakeModel)— 純結構性驗證
predict 端有經過 ml_calibration.apply_calibration。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import ml_predictor


# 對齊 test_ml_predictor.py 的 fake — 一個對 X 每 row 給固定 [P0, P1] 的 mock。
class _FakeModel:
    def __init__(self, classes=(0, 1), proba_per_row=None):
        self.classes_ = list(classes)
        self._proba_per_row = proba_per_row or []

    def predict_proba(self, X: pd.DataFrame):
        n = len(X)
        if self._proba_per_row and len(self._proba_per_row) >= n:
            return np.array(self._proba_per_row[:n])
        return np.array([[0.5, 0.5]] * n)


class _Calibrator:
    """fake calibrator:把 raw 拉平到 raw * factor + offset。"""

    method = "isotonic"

    def __init__(self, factor=0.5, offset=0.1, raise_on_call=False):
        self.factor = factor
        self.offset = offset
        self.raise_on_call = raise_on_call

    def transform(self, raw):
        if self.raise_on_call:
            raise RuntimeError("synthetic calibrator fail")
        arr = np.asarray(raw, dtype=float)
        return np.clip(arr * self.factor + self.offset, 0.0, 1.0)


@pytest.fixture
def _stub_features(monkeypatch):
    """extract_features 給每 sid 都回固定 0.0 features(不打 DB)。"""
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, td, db_path=None: {f: 0.0 for f in ml_predictor.FEATURE_NAMES},
    )


# === predict_batch ===

def test_predict_batch_without_calibrator_returns_raw(_stub_features, monkeypatch):
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    model = _FakeModel(proba_per_row=[[0.3, 0.7], [0.4, 0.6]])
    out = ml_predictor.predict_batch(model, ["2330", "2317"], "2026-05-04")
    assert out["2330"] == pytest.approx(0.7)
    assert out["2317"] == pytest.approx(0.6)


def test_predict_batch_with_calibrator_transforms(_stub_features, monkeypatch):
    """raw 0.7 → 0.7 * 0.5 + 0.1 = 0.45;raw 0.6 → 0.4。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    model = _FakeModel(proba_per_row=[[0.3, 0.7], [0.4, 0.6]])
    cal = _Calibrator(factor=0.5, offset=0.1)
    out = ml_predictor.predict_batch(
        model, ["2330", "2317"], "2026-05-04", calibrator=cal,
    )
    assert out["2330"] == pytest.approx(0.45)
    assert out["2317"] == pytest.approx(0.4)


def test_predict_batch_killswitch_off_bypasses_calibrator(_stub_features, monkeypatch):
    """ML_CALIBRATION_ENABLED=false → 即便傳 calibrator 也走 raw。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "false")
    model = _FakeModel(proba_per_row=[[0.3, 0.7]])
    cal = _Calibrator(factor=0.0, offset=0.0)  # 該把任何 prob 變 0,如果有套
    out = ml_predictor.predict_batch(
        model, ["2330"], "2026-05-04", calibrator=cal,
    )
    assert out["2330"] == pytest.approx(0.7)


def test_predict_batch_calibrator_exception_falls_back(_stub_features, monkeypatch):
    """calibrator.transform 拋 → 回 raw(production 不 crash)。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    model = _FakeModel(proba_per_row=[[0.3, 0.7]])
    cal = _Calibrator(raise_on_call=True)
    out = ml_predictor.predict_batch(
        model, ["2330"], "2026-05-04", calibrator=cal,
    )
    assert out["2330"] == pytest.approx(0.7)  # fallback raw


# === predict_for_strategy 路由 ===

def test_predict_for_strategy_uses_strategy_calibrator(_stub_features, monkeypatch):
    """strategy_model 命中 → 用 strategy_calibrator(不用 fallback_calibrator)。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    sm = _FakeModel(proba_per_row=[[0.2, 0.8]])
    fb = _FakeModel(proba_per_row=[[0.5, 0.5]])
    sc = _Calibrator(factor=0.0, offset=0.1)  # 0.8 * 0 + 0.1 = 0.1
    fc = _Calibrator(factor=2.0, offset=0.0)  # 應該不會被叫
    out = ml_predictor.predict_for_strategy(
        strategy_name="ma_alignment",
        stock_ids=["2330"],
        target_date="2026-05-04",
        fallback_model=fb,
        strategy_model=sm,
        strategy_calibrator=sc,
        fallback_calibrator=fc,
    )
    assert out["2330"] == pytest.approx(0.1)


def test_predict_for_strategy_uses_fallback_calibrator_when_no_strategy_model(
    _stub_features, monkeypatch,
):
    """strategy_model=None → 走 fallback,並用 fallback_calibrator。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    fb = _FakeModel(proba_per_row=[[0.4, 0.6]])
    fc = _Calibrator(factor=0.5, offset=0.0)  # 0.6 * 0.5 = 0.3
    out = ml_predictor.predict_for_strategy(
        strategy_name="ma_alignment",
        stock_ids=["2330"],
        target_date="2026-05-04",
        fallback_model=fb,
        strategy_calibrator=None,
        fallback_calibrator=fc,
    )
    assert out["2330"] == pytest.approx(0.3)


def test_predict_for_strategy_no_calibrators_keeps_raw(_stub_features, monkeypatch):
    """全沒傳 calibrator → raw prob(完全 backward-compat,沒呼叫 ml_calibration)。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    fb = _FakeModel(proba_per_row=[[0.4, 0.6]])
    out = ml_predictor.predict_for_strategy(
        strategy_name="ma_alignment",
        stock_ids=["2330"],
        target_date="2026-05-04",
        fallback_model=fb,
    )
    assert out["2330"] == pytest.approx(0.6)


# === predict_short_pick_winrate(單檔)===

def test_predict_short_pick_winrate_with_calibrator(_stub_features, monkeypatch):
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    model = _FakeModel(proba_per_row=[[0.2, 0.8]])
    cal = _Calibrator(factor=0.5, offset=0.0)  # 0.8 → 0.4
    out = ml_predictor.predict_short_pick_winrate(
        model, "2330", "2026-05-04", calibrator=cal,
    )
    assert out == pytest.approx(0.4)


def test_predict_short_pick_winrate_no_calibrator_keeps_raw(
    _stub_features, monkeypatch,
):
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    model = _FakeModel(proba_per_row=[[0.2, 0.8]])
    out = ml_predictor.predict_short_pick_winrate(model, "2330", "2026-05-04")
    assert out == pytest.approx(0.8)


def test_predict_short_pick_winrate_killswitch_off(
    _stub_features, monkeypatch,
):
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "false")
    model = _FakeModel(proba_per_row=[[0.2, 0.8]])
    cal = _Calibrator(factor=0.0, offset=0.0)
    out = ml_predictor.predict_short_pick_winrate(
        model, "2330", "2026-05-04", calibrator=cal,
    )
    assert out == pytest.approx(0.8)


# === load helpers fallback graceful ===

def test_load_strategy_calibrator_returns_none_when_missing(
    monkeypatch, tmp_path,
):
    """models/calibrators/<name>.pkl 不存在 → 回 None(不 raise)。"""
    from src import ml_calibration

    monkeypatch.setattr(
        ml_calibration, "_default_calibrators_dir", lambda: tmp_path,
    )
    out = ml_predictor.load_strategy_calibrator("nonexistent_strat")
    assert out is None


def test_load_short_pick_calibrator_returns_none_when_missing(
    monkeypatch, tmp_path,
):
    from src import ml_calibration

    monkeypatch.setattr(
        ml_calibration, "_default_calibrators_dir", lambda: tmp_path,
    )
    out = ml_predictor.load_short_pick_calibrator()
    assert out is None


def test_load_strategy_calibrator_loads_existing(monkeypatch, tmp_path):
    """save + load → 拿回 Calibrator 物件。"""
    from src import ml_calibration

    monkeypatch.setattr(
        ml_calibration, "_default_calibrators_dir", lambda: tmp_path,
    )
    cal = ml_calibration.Calibrator(method="platt")
    cal.fit([0.1, 0.3, 0.6, 0.8, 0.9], [0, 0, 1, 1, 1])
    ml_calibration.save_calibrator(cal, "my_strat", base_dir=tmp_path)

    loaded = ml_predictor.load_strategy_calibrator("my_strat")
    assert loaded is not None
    assert loaded.method == "platt"
