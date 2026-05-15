"""src/ml_calibration.py 單元測試 — 不打 DB / 不訓 RF,純 helper 邏輯。

涵蓋:
  - Calibrator fit / transform roundtrip(isotonic + platt)
  - fit_calibrator 自動 method 切換(n < 500 fallback platt)
  - compute_calibration_metrics:Brier score + reliability bins
  - save_calibrator / load_calibrator(tmp_path roundtrip)
  - calibration_enabled env kill-switch
  - apply_calibration 邊界(None calibrator passthrough、kill-switch passthrough、
    transform 失敗 fallback raw)
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from src import ml_calibration


class _FakeModel:
    """Mock sklearn classifier:每個 row 直接回 [1-p, p] 的 predict_proba。"""

    def __init__(self, classes=(0, 1)):
        self.classes_ = list(classes)
        # 內部存「下一次要回的 prob」(call-site 設好)
        self.probs: np.ndarray = np.array([])

    def predict_proba(self, X):
        n = len(X)
        if 1 in self.classes_:
            p = self.probs[:n] if len(self.probs) >= n else np.full(n, 0.5)
            return np.column_stack([1 - p, p])
        # 只有 class=0(罕見)
        return np.ones((n, 1))


# === Calibrator basic fit/transform ===

def test_calibrator_isotonic_fit_transform_roundtrip():
    """isotonic fit 後 transform 給單調回單調(不會把 0.9 拉到 0.1)。"""
    rng = np.random.default_rng(42)
    # 構造一個 raw 偏高的分布:raw 大 → 真實 y=1 比率高,但 raw 過度自信
    n = 1000
    raw = rng.uniform(0, 1, size=n)
    # 真實機率 = raw * 0.6(模型 overconfident — 校正後應壓低)
    y = (rng.uniform(0, 1, size=n) < raw * 0.6).astype(int)

    cal = ml_calibration.Calibrator(method="isotonic")
    cal.fit(raw, y)
    out = cal.transform(np.array([0.1, 0.5, 0.9]))
    # 校正後仍應單調(0.1 ≤ 0.5 ≤ 0.9 對應的 out 也單調 ≤)
    assert out[0] <= out[1] <= out[2]
    # 校正後高分被壓下來(0.9 raw → < 0.9 calibrated,因 raw 太自信)
    assert out[2] < 0.9
    # n_train 紀錄正確
    assert cal.n_train == n


def test_calibrator_platt_fit_transform_returns_probabilities():
    """Platt sigmoid:輸出仍在 [0, 1] 區間,且單調對應。"""
    rng = np.random.default_rng(7)
    n = 200
    raw = rng.uniform(0, 1, size=n)
    y = (rng.uniform(0, 1, size=n) < raw).astype(int)

    cal = ml_calibration.Calibrator(method="platt")
    cal.fit(raw, y)
    out = cal.transform(np.linspace(0, 1, 20))
    assert (out >= 0).all() and (out <= 1).all()
    # 單調(sigmoid 對單調 input 出單調 output)
    assert all(out[i] <= out[i + 1] for i in range(len(out) - 1))


def test_calibrator_unfit_transform_returns_raw():
    """未 fit 直接 transform → 原樣回(safety fallback,不 crash)。"""
    cal = ml_calibration.Calibrator(method="isotonic")
    out = cal.transform(np.array([0.1, 0.5, 0.9]))
    assert np.allclose(out, [0.1, 0.5, 0.9])


def test_calibrator_rejects_unknown_method():
    with pytest.raises(ValueError, match="未知 calibration method"):
        ml_calibration.Calibrator(method="bogus")


def test_calibrator_fit_rejects_length_mismatch():
    cal = ml_calibration.Calibrator(method="isotonic")
    with pytest.raises(ValueError, match="長度不一致"):
        cal.fit([0.1, 0.2, 0.3], [0, 1])


def test_calibrator_fit_rejects_single_class():
    """y_true 只有單一類 → 無法 fit calibrator。"""
    cal = ml_calibration.Calibrator(method="isotonic")
    with pytest.raises(ValueError, match="單一類別"):
        cal.fit([0.1, 0.5, 0.9], [0, 0, 0])


def test_calibrator_fit_rejects_empty():
    cal = ml_calibration.Calibrator(method="isotonic")
    with pytest.raises(ValueError, match="樣本為空"):
        cal.fit([], [])


# === fit_calibrator API + method 自動切換 ===

def test_fit_calibrator_small_sample_falls_back_to_platt(monkeypatch):
    """n=200 < 500 → isotonic fallback platt(自動降階)。"""
    rng = np.random.default_rng(13)
    n = 200
    model = _FakeModel(classes=(0, 1))
    model.probs = rng.uniform(0, 1, size=n)
    X = np.zeros((n, 1))
    y = (rng.uniform(0, 1, size=n) < model.probs).astype(int)

    cal = ml_calibration.fit_calibrator(model, X, y, method="isotonic")
    assert cal.method == "platt", "n<500 應自動 fallback platt"


def test_fit_calibrator_large_sample_keeps_isotonic():
    """n=600 ≥ 500 → 保持 isotonic。"""
    rng = np.random.default_rng(13)
    n = 600
    model = _FakeModel(classes=(0, 1))
    model.probs = rng.uniform(0, 1, size=n)
    X = np.zeros((n, 1))
    y = (rng.uniform(0, 1, size=n) < model.probs).astype(int)

    cal = ml_calibration.fit_calibrator(model, X, y, method="isotonic")
    assert cal.method == "isotonic"


def test_fit_calibrator_explicit_platt_kept_regardless_of_n():
    """method='platt' 手動指定 → 不做 fallback,即使 n 很大。"""
    rng = np.random.default_rng(13)
    n = 600
    model = _FakeModel(classes=(0, 1))
    model.probs = rng.uniform(0, 1, size=n)
    X = np.zeros((n, 1))
    y = (rng.uniform(0, 1, size=n) < model.probs).astype(int)
    cal = ml_calibration.fit_calibrator(model, X, y, method="platt")
    assert cal.method == "platt"


# === compute_calibration_metrics ===

def test_brier_score_perfect_prediction_is_zero():
    """y_prob == y_true(完美校準)→ brier = 0。"""
    y_true = [0, 0, 1, 1, 1]
    y_prob = [0.0, 0.0, 1.0, 1.0, 1.0]
    m = ml_calibration.compute_calibration_metrics(y_true, y_prob)
    assert m["brier_score"] == pytest.approx(0.0)
    assert m["n_samples"] == 5


def test_brier_score_worst_prediction_is_one():
    """y_prob 完全反方向 → brier = 1。"""
    y_true = [0, 0, 1, 1]
    y_prob = [1.0, 1.0, 0.0, 0.0]
    m = ml_calibration.compute_calibration_metrics(y_true, y_prob)
    assert m["brier_score"] == pytest.approx(1.0)


def test_brier_score_random_at_half():
    """y_prob 全 0.5,y_true 半半 → brier = 0.25(random benchmark)。"""
    y_true = [0, 0, 1, 1]
    y_prob = [0.5, 0.5, 0.5, 0.5]
    m = ml_calibration.compute_calibration_metrics(y_true, y_prob)
    assert m["brier_score"] == pytest.approx(0.25)


def test_reliability_bins_default_10_bins():
    """default 10 個 bins,每個 bin 有 bin_lower / bin_upper / n / mean_predicted / actual_rate。"""
    y_true = [0, 1, 0, 1, 1, 0, 1, 0, 1, 1]
    y_prob = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    m = ml_calibration.compute_calibration_metrics(y_true, y_prob, n_bins=10)
    assert m["n_bins"] == 10
    assert len(m["reliability_bins"]) == 10
    for b in m["reliability_bins"]:
        assert "bin_lower" in b
        assert "bin_upper" in b
        assert "n" in b
        assert "mean_predicted" in b
        assert "actual_rate" in b


def test_reliability_bins_empty_bin_actual_rate_is_none():
    """沒落到的 bin → actual_rate = None(避免拿空 list mean 噴 warning)。"""
    y_true = [0, 1]
    y_prob = [0.05, 0.95]  # 只用到 bin 0 + bin 9
    m = ml_calibration.compute_calibration_metrics(y_true, y_prob, n_bins=10)
    empty_bins = [b for b in m["reliability_bins"] if b["n"] == 0]
    for b in empty_bins:
        assert b["actual_rate"] is None


def test_compute_metrics_empty_input_graceful():
    """空 input → 全 NaN dict,不 raise。"""
    m = ml_calibration.compute_calibration_metrics([], [])
    assert m["n_samples"] == 0
    # NaN check
    assert m["brier_score"] != m["brier_score"]
    assert m["reliability_bins"] == []


def test_compute_metrics_length_mismatch_raises():
    with pytest.raises(ValueError, match="長度不一致"):
        ml_calibration.compute_calibration_metrics([0, 1], [0.5])


# === save / load roundtrip ===

def test_save_load_calibrator_roundtrip(tmp_path):
    """save → load 出來的 calibrator transform 結果跟原本一致。"""
    rng = np.random.default_rng(99)
    n = 600
    raw = rng.uniform(0, 1, size=n)
    y = (rng.uniform(0, 1, size=n) < raw).astype(int)

    cal = ml_calibration.Calibrator(method="isotonic")
    cal.fit(raw, y)

    saved_path = ml_calibration.save_calibrator(cal, "test_strat", base_dir=tmp_path)
    assert saved_path.exists()

    loaded = ml_calibration.load_calibrator("test_strat", base_dir=tmp_path)
    assert loaded is not None
    assert loaded.method == "isotonic"

    # transform 結果一致
    inputs = np.array([0.1, 0.5, 0.9])
    assert np.allclose(cal.transform(inputs), loaded.transform(inputs))


def test_load_calibrator_returns_none_when_missing(tmp_path):
    """檔不存在 → None(caller 走 raw prob fallback)。"""
    out = ml_calibration.load_calibrator("does_not_exist", base_dir=tmp_path)
    assert out is None


def test_load_calibrator_returns_none_on_corrupt(tmp_path):
    """檔損壞 → 印 log + 回 None(不 raise,讓 production 不會掛)。"""
    p = ml_calibration.calibrator_path("corrupt", base_dir=tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not a pickle", encoding="utf-8")
    out = ml_calibration.load_calibrator("corrupt", base_dir=tmp_path)
    assert out is None


# === calibration_enabled env kill-switch ===

def test_calibration_enabled_default_true(monkeypatch):
    """env 未設 → True(預設 on)。"""
    monkeypatch.delenv("ML_CALIBRATION_ENABLED", raising=False)
    assert ml_calibration.calibration_enabled() is True


def test_calibration_enabled_false_values(monkeypatch):
    """false / 0 / off / no(任意大小寫)→ False。"""
    for v in ("false", "FALSE", "0", "off", "no", "No"):
        monkeypatch.setenv("ML_CALIBRATION_ENABLED", v)
        assert ml_calibration.calibration_enabled() is False, (
            f"ML_CALIBRATION_ENABLED={v!r} 應視為 disabled"
        )


def test_calibration_enabled_true_values(monkeypatch):
    """true / 1 / on / yes / 隨便字串 → True(只有明確 false 才關)。"""
    for v in ("true", "1", "on", "yes", "anything", ""):
        monkeypatch.setenv("ML_CALIBRATION_ENABLED", v)
        # 空字串 → 預設 True
        assert ml_calibration.calibration_enabled() is True


# === apply_calibration ===

def test_apply_calibration_passes_through_when_calibrator_none():
    """calibrator=None → 原樣回。"""
    raw = np.array([0.1, 0.5, 0.9])
    out = ml_calibration.apply_calibration(model=None, calibrator=None, raw_probs=raw)
    assert np.allclose(out, raw)


def test_apply_calibration_passes_through_when_disabled(monkeypatch):
    """kill-switch off → 不套 calibrator,原樣回。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "false")
    raw = np.array([0.1, 0.5, 0.9])

    # 構造一個會把所有 input 變 0.0 的 calibrator
    class _AllZero:
        method = "isotonic"

        def transform(self, x):
            return np.zeros_like(np.asarray(x, dtype=float))

    out = ml_calibration.apply_calibration(
        model=None, calibrator=_AllZero(), raw_probs=raw,
    )
    # kill-switch 關掉應該無視 calibrator → 仍回 raw
    assert np.allclose(out, raw)


def test_apply_calibration_uses_calibrator_when_enabled(monkeypatch):
    """kill-switch on + calibrator 存在 → 套 transform。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    raw = np.array([0.1, 0.5, 0.9])

    class _Doubler:
        method = "isotonic"

        def transform(self, x):
            return np.minimum(np.asarray(x, dtype=float) * 2, 1.0)

    out = ml_calibration.apply_calibration(
        model=None, calibrator=_Doubler(), raw_probs=raw,
    )
    assert np.allclose(out, [0.2, 1.0, 1.0])


def test_apply_calibration_falls_back_to_raw_on_transform_exception(monkeypatch):
    """transform 拋例外 → 回 raw,不 crash(production safety)。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    raw = np.array([0.1, 0.5, 0.9])

    class _Boom:
        method = "isotonic"

        def transform(self, x):
            raise RuntimeError("synthetic transform fail")

    out = ml_calibration.apply_calibration(
        model=None, calibrator=_Boom(), raw_probs=raw,
    )
    assert np.allclose(out, raw)
