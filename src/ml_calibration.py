"""ML 機率校準 (probability calibration) — 把 RandomForest 的 raw predict_proba
轉成接近真實機率的校正值。

背景:RandomForest 的 predict_proba 天生會把分數集中在 0/1 兩端,實測上信心
0.7 的 pick 真實命中可能只有 55%。把 raw_prob 經過 isotonic / sigmoid mapping
拉回對齊真實分布後,UI / 推播顯示的「AI 勝率 N%」才是可靠的決策依據。

設計:
- 訓練流程:base RF 訓完後,用最後 20% time-based holdout 樣本 fit calibrator
- 推論流程:base_model.predict_proba → calibrator.transform → 校正 prob
- 持久化:`models/calibrators/{strategy}.pkl`,跟 base model 同生命週期

method 選擇邏輯:
- isotonic(預設):非參數、靈活,需要 ≥ 500 樣本才穩定
- platt(sigmoid):參數化,小樣本(< 500)更穩,fallback 用

不直接用 sklearn `CalibratedClassifierCV(cv='prefit')`:
1. 該物件把 base model 也包進去,pickle 出來檔案大、跟 base pkl 重複
2. 1.6+ 起 `cv='prefit'` 被 `FrozenEstimator` 取代,API 變動風險高
3. 自己包薄薄一層 IsotonicRegression / LogisticRegression 更乾淨可控

Kill-switch:env `ML_CALIBRATION_ENABLED=true` 預設 on;設 false 一切如舊。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# 預設 method 切換門檻:< 500 樣本 isotonic 容易抖,fallback platt(sigmoid)
ISOTONIC_MIN_SAMPLES = 500

DEFAULT_RELIABILITY_BINS = 10

# Kill-switch env var
_CALIBRATION_ENV_VAR = "ML_CALIBRATION_ENABLED"


def calibration_enabled() -> bool:
    """讀 env ML_CALIBRATION_ENABLED;預設 true。

    任何值除了 'false' / '0' / 'off' / 'no'(不分大小寫) → 視為 enabled。
    """
    val = os.environ.get(_CALIBRATION_ENV_VAR)
    if val is None or val == "":
        return True
    return val.strip().lower() not in ("false", "0", "off", "no")


class Calibrator:
    """機率校準器 — wraps IsotonicRegression or Platt sigmoid。

    .fit(raw_probs, y_true):用 raw model probabilities + 真實 label 學一個
        raw → calibrated 的單調映射。
    .transform(raw_probs) → calibrated_probs:推論時呼叫。

    method 紀錄在 .method 屬性,給 metrics / log 看(isotonic 還是 platt fallback)。
    """

    __slots__ = ("method", "_inner", "n_train")

    def __init__(self, method: str = "isotonic") -> None:
        if method not in ("isotonic", "platt"):
            raise ValueError(
                f"未知 calibration method: {method}(可選 isotonic / platt)"
            )
        self.method = method
        self._inner: Any = None
        self.n_train: int = 0

    def fit(self, raw_probs, y_true) -> "Calibrator":
        raw_probs = np.asarray(raw_probs, dtype=float).ravel()
        y_true = np.asarray(y_true, dtype=int).ravel()
        if len(raw_probs) != len(y_true):
            raise ValueError(
                f"raw_probs / y_true 長度不一致:{len(raw_probs)} vs {len(y_true)}"
            )
        if len(raw_probs) == 0:
            raise ValueError("樣本為空,無法 fit calibrator")
        if len(set(y_true.tolist())) < 2:
            raise ValueError(
                "y_true 只有單一類別,無法 fit calibrator(需 win + loss 都有樣本)"
            )

        self.n_train = int(len(raw_probs))
        if self.method == "isotonic":
            from sklearn.isotonic import IsotonicRegression

            self._inner = IsotonicRegression(
                out_of_bounds="clip", y_min=0.0, y_max=1.0,
            )
            self._inner.fit(raw_probs, y_true)
        else:
            # Platt sigmoid:LogisticRegression on raw_prob(clip 防 0/1 邊界)
            from sklearn.linear_model import LogisticRegression

            X = np.clip(raw_probs, 1e-7, 1 - 1e-7).reshape(-1, 1)
            self._inner = LogisticRegression()
            self._inner.fit(X, y_true)
        return self

    def transform(self, raw_probs) -> np.ndarray:
        """轉換 raw_probs → calibrated_probs。

        未 fit / inner None → 直接回 raw(safety fallback,讓 caller 不會 crash)。
        """
        arr = np.asarray(raw_probs, dtype=float).ravel()
        if self._inner is None:
            return arr
        if self.method == "isotonic":
            out = self._inner.predict(arr)
        else:
            X = np.clip(arr, 1e-7, 1 - 1e-7).reshape(-1, 1)
            out = self._inner.predict_proba(X)[:, 1]
        # clip 防小數誤差使 prob 跑出 [0, 1]
        return np.clip(np.asarray(out, dtype=float), 0.0, 1.0)


def _extract_positive_proba(model, X) -> np.ndarray:
    """從 sklearn-like classifier 拿 P(class=1) 的 1D ndarray。

    classes_ 沒包含 1 → 全 0.0(對齊 ml_predictor.predict_batch 邊界行為)。
    """
    proba = model.predict_proba(X)
    proba = np.asarray(proba)
    classes = list(getattr(model, "classes_", []))
    if 1 in classes:
        idx = classes.index(1)
        return proba[:, idx]
    return np.zeros(proba.shape[0], dtype=float)


def fit_calibrator(
    base_model,
    X_val: pd.DataFrame | np.ndarray,
    y_val: pd.Series | np.ndarray,
    method: str = "isotonic",
) -> Calibrator:
    """訓 calibrator:base_model.predict_proba(X_val) → y_val 的映射。

    method='isotonic'(預設):樣本 < ISOTONIC_MIN_SAMPLES(500)自動 fallback
    'platt'(sigmoid),避免小樣本 isotonic 抖動。傳 method='platt' 強制走 sigmoid。
    """
    n = len(y_val)
    actual_method = method
    if method == "isotonic" and n < ISOTONIC_MIN_SAMPLES:
        actual_method = "platt"
        logger.info(
            "[CAL] n=%d < %d → fallback isotonic → platt sigmoid",
            n, ISOTONIC_MIN_SAMPLES,
        )

    raw_prob = _extract_positive_proba(base_model, X_val)
    cal = Calibrator(method=actual_method)
    cal.fit(raw_prob, y_val)
    return cal


def compute_calibration_metrics(
    y_true,
    y_prob,
    n_bins: int = DEFAULT_RELIABILITY_BINS,
) -> dict[str, Any]:
    """算 Brier score + reliability diagram(10 bins)。

    Brier score = mean((y_true - y_prob)^2),範圍 [0, 1];越低越好。
    Random guess(p=base_rate)的 brier 約 0.25;perfect calibration 接近 0。
    一般健康門檻 < 0.25;> 0.3 視為偏離校準。

    reliability_bins:每個 bin 一個 dict,鍵:
        bin_lower / bin_upper:bin 區間
        n:落在此 bin 的樣本數
        mean_predicted:該 bin 內 y_prob 平均
        actual_rate:該 bin 內 y_true 平均(命中率);n=0 → None
    """
    y_true = np.asarray(y_true, dtype=float).ravel()
    y_prob = np.asarray(y_prob, dtype=float).ravel()

    if len(y_true) != len(y_prob):
        raise ValueError(
            f"y_true / y_prob 長度不一致:{len(y_true)} vs {len(y_prob)}"
        )

    if len(y_true) == 0:
        return {
            "brier_score": float("nan"),
            "n_samples": 0,
            "base_rate": float("nan"),
            "n_bins": n_bins,
            "reliability_bins": [],
        }

    brier = float(np.mean((y_true - y_prob) ** 2))
    base_rate = float(np.mean(y_true))

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: list[dict[str, Any]] = []
    for i in range(n_bins):
        lo, hi = float(edges[i]), float(edges[i + 1])
        if i == n_bins - 1:
            # 最右 bin 含右端點,避免 prob=1.0 跑出去
            mask = (y_prob >= lo) & (y_prob <= hi)
        else:
            mask = (y_prob >= lo) & (y_prob < hi)
        n = int(mask.sum())
        if n > 0:
            mean_pred = float(y_prob[mask].mean())
            actual = float(y_true[mask].mean())
        else:
            mean_pred = (lo + hi) / 2
            actual = None
        bins.append({
            "bin_lower": lo,
            "bin_upper": hi,
            "n": n,
            "mean_predicted": mean_pred,
            "actual_rate": actual,
        })

    return {
        "brier_score": brier,
        "n_samples": int(len(y_true)),
        "base_rate": base_rate,
        "n_bins": n_bins,
        "reliability_bins": bins,
    }


# === 持久化 ===

def _default_calibrators_dir() -> Path:
    from src import config
    return Path(config.PROJECT_ROOT) / "models" / "calibrators"


def calibrator_path(strategy: str, base_dir: Path | None = None) -> Path:
    """回 models/calibrators/{strategy}.pkl 絕對路徑(不檢查存不存在)。"""
    if base_dir is None:
        base_dir = _default_calibrators_dir()
    return Path(base_dir) / f"{strategy}.pkl"


def save_calibrator(
    calibrator: Calibrator,
    strategy: str,
    base_dir: Path | None = None,
) -> Path:
    """joblib dump 到 models/calibrators/{strategy}.pkl;parent 不存在會建。"""
    p = calibrator_path(strategy, base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(calibrator, p)
    return p


def load_calibrator(
    strategy: str,
    base_dir: Path | None = None,
) -> Calibrator | None:
    """joblib load。檔不存在或 load 失敗 → 回 None(caller fallback raw prob)。

    print 診斷 log 讓雲端看得到(對齊 ml_predictor.load_model 行為)。
    """
    p = calibrator_path(strategy, base_dir)
    if not p.exists():
        return None
    try:
        cal = joblib.load(p)
        return cal
    except Exception as e:  # noqa: BLE001
        print(
            f"[CAL] load_calibrator {strategy} 失敗:{type(e).__name__}: {e}",
            flush=True,
        )
        logger.warning("[CAL] load_calibrator(%s)失敗:%s", strategy, e)
        return None


def apply_calibration(
    model,
    calibrator: Calibrator | None,
    raw_probs: np.ndarray,
) -> np.ndarray:
    """把 raw probs 餵 calibrator.transform,kill-switch / None calibrator 自動 passthrough。

    這是 predict 端共用入口;model 參數目前不直接用(預留給將來 method 依 model
    type 切換),但保留簽名讓 caller 不需要記哪些路徑要傳哪些。
    """
    arr = np.asarray(raw_probs, dtype=float).ravel()
    if not calibration_enabled() or calibrator is None:
        return arr
    try:
        return calibrator.transform(arr)
    except Exception as e:  # noqa: BLE001
        print(
            f"[CAL] apply_calibration 失敗,回 raw probs:{type(e).__name__}: {e}",
            flush=True,
        )
        logger.warning("[CAL] apply_calibration 失敗:%s", e)
        return arr


__all__ = [
    "Calibrator",
    "ISOTONIC_MIN_SAMPLES",
    "DEFAULT_RELIABILITY_BINS",
    "calibration_enabled",
    "fit_calibrator",
    "compute_calibration_metrics",
    "save_calibrator",
    "load_calibrator",
    "calibrator_path",
    "apply_calibration",
]
