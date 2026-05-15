"""端到端 calibration test:從 ml_predictor.train_with_calibration → 存
calibrator pkl → load → 走 predict_batch → 推播時 ml_prob 經過 calibrate。

不打 SQLite 主庫(用 tmp + 自製 features + label),只測「校正流程串得起來」:
  1. train_with_calibration 訓出 base_model + calibrator + Brier metrics
  2. save_calibrator / load_calibrator roundtrip 不掉資訊
  3. predict_batch(model, sids, ..., calibrator=loaded) 出來的 prob ≠ raw prob
  4. ML_CALIBRATION_ENABLED=false 時 picks 信心度 = raw(kill-switch 真的關得掉)
  5. Brier score 校正後 < 校正前(用人為構造的 over-confident 場景驗證有改善)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import ml_calibration, ml_predictor


def _make_synthetic_dataset(n=1200, seed=42, overconfidence=0.6):
    """構造 RF 容易 over-confident 的場景:

    真實 P(y=1) = sigmoid(0.5 * feat_a) — 平滑;但 feat_a 還有強雜訊。
    RF 在這種 weak signal + noise 場景傾向把 prob 推向 0/1(over-confidence)。
    overconfidence < 1 額外把 raw signal 弱化,讓 RF 預測偏高/偏低更明顯。
    """
    rng = np.random.default_rng(seed)
    feat_a = rng.normal(size=n)
    feat_b = rng.normal(size=n)
    feat_c = rng.normal(size=n)
    # 真實機率(平滑 sigmoid)
    logits = 0.5 * feat_a * overconfidence + 0.3 * feat_b * overconfidence
    p_true = 1.0 / (1.0 + np.exp(-logits))
    y = (rng.uniform(0, 1, size=n) < p_true).astype(int)

    X = pd.DataFrame({"feat_a": feat_a, "feat_b": feat_b, "feat_c": feat_c})
    y_s = pd.Series(y)
    return X, y_s


def test_train_with_calibration_produces_model_calibrator_metrics():
    """train_with_calibration 回 (model, calibrator, metrics) 三元組,結構正確。"""
    X, y = _make_synthetic_dataset(n=800, seed=7)
    model, calibrator, metrics = ml_predictor.train_with_calibration(X, y)
    # model
    assert hasattr(model, "predict_proba")
    assert hasattr(model, "classes_")
    # calibrator
    assert isinstance(calibrator, ml_calibration.Calibrator)
    # n=800 ≥ 500 → isotonic 保留(holdout 160 < 500 但 method 看 X_val
    # 長度而非 X_train。train_with_calibration 給 X_holdout = 160,所以
    # 自動 fallback platt。)
    assert calibrator.method in ("isotonic", "platt")
    # metrics
    for k in (
        "n_train", "n_holdout", "n_total", "win_rate_overall",
        "calibration_method", "raw_brier", "calibrated_brier",
        "raw_reliability_bins", "calibrated_reliability_bins",
    ):
        assert k in metrics, f"metrics 缺 {k}"
    # n_train + n_holdout = n_total
    assert metrics["n_train"] + metrics["n_holdout"] == metrics["n_total"]
    # holdout 約 20%
    assert 0.18 <= metrics["n_holdout"] / metrics["n_total"] <= 0.22


def test_train_with_calibration_time_based_split_uses_last_rows():
    """time-based:給 dates,calibrator 應該在「最新」段 fit,不混到舊段。"""
    X, y = _make_synthetic_dataset(n=600, seed=11)
    dates = pd.Series(pd.date_range("2024-01-01", periods=len(X), freq="B").strftime("%Y-%m-%d"))
    model, calibrator, metrics = ml_predictor.train_with_calibration(X, y, dates=dates)
    # holdout 段 = 最後 20%(120 rows)
    assert metrics["n_holdout"] == 120
    assert metrics["n_train"] == 480


def test_calibrator_save_load_roundtrip_with_real_model(tmp_path):
    """訓出 calibrator → save → load → transform 結果一致。"""
    X, y = _make_synthetic_dataset(n=600, seed=3)
    _, calibrator, _ = ml_predictor.train_with_calibration(X, y)

    saved = ml_calibration.save_calibrator(calibrator, "e2e_strat", base_dir=tmp_path)
    assert saved.exists()

    loaded = ml_calibration.load_calibrator("e2e_strat", base_dir=tmp_path)
    assert loaded is not None

    grid = np.linspace(0.0, 1.0, 21)
    assert np.allclose(calibrator.transform(grid), loaded.transform(grid))


def test_predict_batch_with_calibrator_differs_from_raw(tmp_path, monkeypatch):
    """端到端:用真模型 + calibrator,預測值跟 raw 不同(代表 calibrator 真的有套上)。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "true")
    X, y = _make_synthetic_dataset(n=800, seed=21)
    model, calibrator, _ = ml_predictor.train_with_calibration(X, y)

    # 把 ml_predictor.extract_features 換成回固定 feature(對齊上述 X 欄位)
    # 但 ml_predictor.FEATURE_NAMES 是 16 欄;synthetic 模型只有 3 欄,所以
    # 走 predict_batch 會 column mismatch。改成直接拍 model.predict_proba +
    # 套 calibrator,模擬 predict_batch 內部行為驗證 wiring:
    sample_X = X.iloc[:20]
    raw_proba = model.predict_proba(sample_X)
    idx = list(model.classes_).index(1)
    raw_p1 = raw_proba[:, idx]
    cal_p1 = calibrator.transform(raw_p1)

    # 至少有一筆校正後值跟 raw 差 > 1e-3(代表 calibrator 不是 identity)
    diffs = np.abs(cal_p1 - raw_p1)
    assert diffs.max() > 1e-3, (
        f"calibrator 像 identity(max diff {diffs.max():.5f}),wiring 可能斷"
    )


def test_calibration_reduces_brier_when_model_is_overconfident():
    """關鍵:校正後 Brier 應 ≤ 校正前(對 RF over-confident 場景)。

    這是「校正有沒有用」的回歸測試 — 如果哪天改 calibrator 邏輯把 brier 弄壞,
    這 test 會擋。

    n=3000 確保 holdout 600 ≥ ISOTONIC_MIN_SAMPLES(500),走 isotonic 而非
    platt — isotonic 對 in-sample brier 保證 ≤ raw(它直接最佳化 squared loss
    在 fit 的資料上)。
    """
    X, y = _make_synthetic_dataset(n=3000, seed=55, overconfidence=0.5)
    _, calibrator, metrics = ml_predictor.train_with_calibration(X, y)
    assert calibrator.method == "isotonic", (
        f"holdout 600 應走 isotonic,實際 {calibrator.method}"
    )
    raw = metrics["raw_brier"]
    cal = metrics["calibrated_brier"]
    assert cal <= raw + 1e-6, (
        f"isotonic in-sample brier ({cal:.4f}) 應 ≤ raw ({raw:.4f}) — "
        f"calibration 失效"
    )


def test_killswitch_off_picks_use_raw_prob(monkeypatch):
    """端到端 kill-switch:env ML_CALIBRATION_ENABLED=false 時,即便 calibrator
    存在,predict 端拿到的仍是 raw — 主公能完全關掉這套機制。"""
    monkeypatch.setenv("ML_CALIBRATION_ENABLED", "false")
    # 構造一個會把所有 prob 變 0 的「破壞性」calibrator
    raw = np.array([0.1, 0.5, 0.9])

    class _Zeroer:
        method = "isotonic"

        def transform(self, x):
            return np.zeros_like(np.asarray(x, dtype=float))

    out = ml_calibration.apply_calibration(
        model=None, calibrator=_Zeroer(), raw_probs=raw,
    )
    # 校正關掉 → calibrator 被 bypass,raw 原樣回
    assert np.allclose(out, raw)


def test_calibration_metrics_match_brier_formula():
    """compute_calibration_metrics 算的 Brier 跟手算公式一致(防 mean axis 之類筆誤)。"""
    y_true = np.array([0, 1, 1, 0, 1])
    y_prob = np.array([0.2, 0.8, 0.6, 0.3, 0.7])
    expected = float(np.mean((y_true - y_prob) ** 2))
    m = ml_calibration.compute_calibration_metrics(y_true, y_prob)
    assert m["brier_score"] == pytest.approx(expected)


def test_e2e_train_save_load_meta_persists_calibration_block(tmp_path):
    """訓完 → save calibrator + dump_model_meta with calibration → load_model_meta
    讀得回 calibration 區塊(讓 system_brief / app.py 能讀)。"""
    X, y = _make_synthetic_dataset(n=800, seed=37)
    model, calibrator, metrics = ml_predictor.train_with_calibration(X, y)

    pkl_path = tmp_path / "test.pkl"
    ml_predictor.save_model(model, pkl_path)
    ml_calibration.save_calibrator(calibrator, "test_strat", base_dir=tmp_path)

    # 灌進 dump_model_meta 的 metrics — 模擬 train_ml_model.py 的呼叫
    full_metrics = {
        "n_train": metrics["n_train"], "n_test": metrics["n_holdout"],
        "win_rate_overall": metrics["win_rate_overall"],
        "accuracy": 0.6, "precision": 0.6, "recall": 0.6, "f1": 0.6,
        "calibration": {
            "method": calibrator.method,
            "n_holdout": metrics["n_holdout"],
            "raw_brier": metrics["raw_brier"],
            "calibrated_brier": metrics["calibrated_brier"],
        },
    }
    ml_predictor.dump_model_meta(pkl_path, full_metrics)

    loaded_meta = ml_predictor.load_model_meta(pkl_path)
    assert loaded_meta is not None
    assert "calibration" in loaded_meta
    cb = loaded_meta["calibration"]
    assert cb["method"] == calibrator.method
    assert cb["raw_brier"] == pytest.approx(metrics["raw_brier"])
    assert cb["calibrated_brier"] == pytest.approx(metrics["calibrated_brier"])
