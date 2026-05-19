"""LightGBM + Multi-task + Stacking ensemble for short-pick classification。

# Phase 2 #P2-5(2026-05-18)— 把 production RandomForest 升級成 stacking。

## 架構
Base learners(全部 train 在主 label = 5d ATR target hit):
  - LightGBM(主力,gradient boosting tree;tabular small-data 公認比 RF 強 3-7pp AUC)
  - LogisticRegression(線性 baseline;StandardScaler pipeline,給 ensemble 多元性)
  - RandomForest(舊架構保留,讓 ensemble 對非線性 + 線性都覆蓋)

Meta-learner:
  - LogisticRegression(於 base learners 的 cross-validated OOF 機率上學一個 blend)

Optional multi-task heads(1d / 3d / 10d):
  - 主 base learner 仍是 5d label LightGBM,額外的 horizons 訓在 .multitask_heads dict。
  - 不影響 predict_proba(維持 5d 為主)— UI / verdict 想顯示多 horizon 預測可
    透過 predict_multitask() 拿。

## 持久化
StackingEnsembleModel 整包 joblib.dump 成單一 .pkl。實作 sklearn classifier
API(classes_ / n_features_in_ / predict_proba) → ml_predictor.predict_batch
跟 predict_short_pick_winrate **不需任何修改** 就能透過 duck typing 跑這個模型。

## 樣本量 fallback
train_stacking_ensemble(min_samples=MIN_STACKING_SAMPLES=300)。樣本 < 300 →
直接 raise ValueError,caller(scripts/train_per_strategy_ml.py)接住 fallback
到舊的單 RF 路徑(避免 stacking 在小樣本過擬合 + cross-validated OOF 不穩)。

## OOF 機率產生方式
用 StratifiedKFold(n_splits=5, shuffle=True, random_state=42)對每個 base
learner 跑 cross_val_predict(method='predict_proba'),把 OOF 機率餵 meta-
learner。最後 base learners 在全資料 refit 一次給 inference 用。

Note:OOF 機率有 fold-leak 之嫌(meta 看過 fold-i 的測試 prob,但 base learner
fold-i 沒看過 fold-i 樣本)— 標準 stacking practice。OOS 嚴格評估走
src/ml_walkforward.py framework,不用 OOF AUC 當生產評估。

## 為什麼不用 sklearn StackingClassifier
1. StackingClassifier 把 base estimators 重複 pickle(我們要省檔案大小)
2. 不能像本實作那樣直接 expose .multitask_heads / .train_metrics
3. 自己包薄一層 + 對齊既有 ml_predictor API 反而最乾淨
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


ENSEMBLE_TYPE = "stacking_v1"

# 樣本量低於此 → caller 應 fallback 到單 RF(避免 stacking 過擬合 + CV fold 太小)
MIN_STACKING_SAMPLES = 300

# Multi-task horizons:5d 仍是主 label,1d/3d/10d 是 auxiliary heads
MULTITASK_HORIZONS: tuple[int, ...] = (1, 3, 5, 10)

# LightGBM 預設 hyperparameters(spec 拍板)
DEFAULT_LGBM_PARAMS = dict(
    n_estimators=200,
    learning_rate=0.05,
    num_leaves=31,
    feature_fraction=0.8,
    bagging_fraction=0.8,
    bagging_freq=5,
    verbose=-1,
    random_state=42,
    n_jobs=-1,
)

# RF(保留 ensemble 多元性,跟 production per-strategy 對齊)
DEFAULT_RF_PARAMS = dict(
    n_estimators=100,
    max_depth=10,
    min_samples_leaf=5,
    class_weight="balanced",
    random_state=42,
    n_jobs=-1,
)

# LR(線性 baseline,需要 StandardScaler 配合)
DEFAULT_LR_PARAMS = dict(
    max_iter=1000,
    class_weight="balanced",
)

# Meta-learner LR(OOF blend,刻意不加 class_weight — OOF 已反映分布)
DEFAULT_META_PARAMS = dict(
    max_iter=1000,
)

# Stacking OOF CV folds(StratifiedKFold)
DEFAULT_CV_FOLDS = 5


class StackingEnsembleModel:
    """LightGBM + LR + RF base learners,LR meta-learner blend。

    對外 sklearn classifier API:
      .predict_proba(X) → ndarray shape (n, 2),columns = [P(y=0), P(y=1)]
      .predict(X)       → ndarray int 0/1
      .classes_         → np.array([0, 1])
      .n_features_in_   → int(對齊 _aligned_feature_names slicing 邏輯)

    額外屬性:
      .base_learners      dict {'lgbm': ..., 'lr': pipeline, 'rf': ...}
      .meta_learner       LR fitted on OOF predictions
      .feature_names      list[str](訓練時 column 順序)
      .ensemble_type      str(預設 'stacking_v1';未來改架構可 bump)
      .multitask_heads    dict {horizon_days: LGBMClassifier};可空
      .train_metrics      dict(OOF AUC / feature importances / win rate 等)

    predict_proba 路徑:
      X → 每個 base learner predict_proba → DataFrame{'lgbm','lr','rf'} of P(y=1)
        → meta_learner.predict_proba(base_df) → [P0, P1]
    """

    def __init__(
        self,
        base_learners: dict,
        meta_learner: Any,
        feature_names: Sequence[str],
        ensemble_type: str = ENSEMBLE_TYPE,
        multitask_heads: dict | None = None,
        train_metrics: dict | None = None,
    ) -> None:
        self.base_learners = base_learners
        self.meta_learner = meta_learner
        self.feature_names = list(feature_names)
        self.ensemble_type = ensemble_type
        # sklearn classifier compat
        self.classes_ = np.array([0, 1])
        self.n_features_in_ = len(self.feature_names)
        self.multitask_heads = multitask_heads or {}
        self.train_metrics = train_metrics or {}

    def _align_X(self, X) -> pd.DataFrame:
        """確保 X 是 DataFrame 且 columns 順序對齊 self.feature_names。

        如果 caller 給的 X 是 numpy 或 column 順序不同,base learners 會吃錯欄
        → 一定要先對齊。X 缺欄則 raise(對齊 sklearn 行為)。
        """
        if not isinstance(X, pd.DataFrame):
            X = pd.DataFrame(X, columns=self.feature_names)
        missing = [c for c in self.feature_names if c not in X.columns]
        if missing:
            raise ValueError(
                f"StackingEnsembleModel.predict_proba 缺 features: {missing}"
            )
        return X[self.feature_names]

    def _base_predictions(self, X) -> pd.DataFrame:
        """跑所有 base learners → DataFrame,one column per learner of P(y=1)。

        Caller 已 align X。columns 順序固定為 sorted(base_learners.keys())
        對齊 meta-learner training 時的 column order(_train_with_oof 那邊
        同樣用 sorted)。
        """
        X_aligned = self._align_X(X)
        cols: dict[str, np.ndarray] = {}
        for name in sorted(self.base_learners.keys()):
            learner = self.base_learners[name]
            proba = learner.predict_proba(X_aligned)
            classes = list(getattr(learner, "classes_", [0, 1]))
            if 1 in classes:
                idx = classes.index(1)
                cols[name] = np.asarray(proba)[:, idx]
            else:
                # base learner 訓練資料全 0 → P(y=1) = 0,讓 meta 自己處理
                cols[name] = np.zeros(len(X_aligned))
        return pd.DataFrame(cols)

    def predict_proba(self, X) -> np.ndarray:
        """sklearn-compat:回 (n, 2) ndarray of [P(y=0), P(y=1)]."""
        base_df = self._base_predictions(X)
        meta_proba = self.meta_learner.predict_proba(base_df)
        meta_classes = list(getattr(self.meta_learner, "classes_", [0, 1]))
        if 1 in meta_classes:
            idx = meta_classes.index(1)
            p1 = np.asarray(meta_proba)[:, idx]
        else:
            p1 = np.zeros(len(base_df))
        # clip 防小數誤差超出 [0, 1]
        p1 = np.clip(p1, 0.0, 1.0)
        return np.column_stack([1.0 - p1, p1])

    def predict(self, X) -> np.ndarray:
        proba = self.predict_proba(X)
        return (proba[:, 1] >= 0.5).astype(int)

    def predict_multitask(self, X) -> pd.DataFrame:
        """回 DataFrame 每列一檔 sample,每欄一個 horizon 的 P(y=1)。

        Column key 格式:'h{N}d' 例如 'h1d' / 'h3d' / 'h5d' / 'h10d'。

        - 'h5d' 永遠存在,取主 'lgbm' base learner(跟 predict_proba 的主信號一致)。
        - 其他 horizons:有 multitask_heads 那個 key 才有 column。

        empty DataFrame 若連 5d 主 head 都不在(理論不會發生 — 訓練時一定有)。
        """
        X_aligned = self._align_X(X)
        cols: dict[str, np.ndarray] = {}

        primary = self.base_learners.get("lgbm")
        if primary is not None:
            proba = primary.predict_proba(X_aligned)
            classes = list(getattr(primary, "classes_", [0, 1]))
            if 1 in classes:
                cols["h5d"] = np.asarray(proba)[:, classes.index(1)]

        for horizon, head in self.multitask_heads.items():
            key = f"h{int(horizon)}d"
            if head is None or key in cols:
                continue
            proba = head.predict_proba(X_aligned)
            classes = list(getattr(head, "classes_", [0, 1]))
            if 1 in classes:
                cols[key] = np.asarray(proba)[:, classes.index(1)]
        return pd.DataFrame(cols)


def is_ensemble(obj: Any) -> bool:
    """Duck-type:判斷 obj 是否為 stacking ensemble(用於 load_model 後分流)。"""
    return (
        hasattr(obj, "base_learners")
        and hasattr(obj, "meta_learner")
        and hasattr(obj, "ensemble_type")
    )


def _build_lr_pipeline(lr_params: dict) -> Any:
    """LR 需要 StandardScaler — 用 sklearn Pipeline 包起來 fit/predict 自動套。"""
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return Pipeline(
        [
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(**lr_params)),
        ]
    )


def _safe_auc(y_true, y_score) -> float:
    """roc_auc_score wrapper:單類 → NaN(不 raise)。"""
    from sklearn.metrics import roc_auc_score

    try:
        return float(roc_auc_score(y_true, y_score))
    except ValueError:
        return float("nan")


def _safe_brier(y_true, y_score) -> float:
    from sklearn.metrics import brier_score_loss

    try:
        return float(brier_score_loss(y_true, np.clip(y_score, 1e-7, 1 - 1e-7)))
    except Exception:  # noqa: BLE001
        return float("nan")


def _extract_positive_proba(model, X) -> np.ndarray:
    """從 sklearn-like classifier 拿 P(y=1) 的 1D ndarray;沒 class=1 → 全 0。"""
    proba = model.predict_proba(X)
    proba = np.asarray(proba)
    classes = list(getattr(model, "classes_", []))
    if 1 in classes:
        idx = classes.index(1)
        return proba[:, idx]
    return np.zeros(proba.shape[0], dtype=float)


def train_multitask_lgbm(
    X: pd.DataFrame,
    y_dict: dict[int, pd.Series],
    lgbm_params: dict | None = None,
) -> dict:
    """訓 LightGBM multi-task — 每 horizon 一個 model。

    Args:
      X:features DataFrame(每 row 一個訓練樣本,columns 對齊 caller 的 feature_names)
      y_dict:{horizon_days: y_series};y_series 用 NaN 表示「該 horizon 該 row
        label 不可得」(例如 entry+N 日 SQL 撈不到資料)
      lgbm_params:覆寫 DEFAULT_LGBM_PARAMS

    Returns:
      dict {horizon: LGBMClassifier};horizons 樣本太少(< 30 non-NaN)或單類
      自動跳過(key 不出現)。
    """
    import lightgbm as lgb

    params = dict(DEFAULT_LGBM_PARAMS)
    if lgbm_params:
        params.update(lgbm_params)

    heads: dict = {}
    X = X.reset_index(drop=True)
    for horizon, y in y_dict.items():
        y_series = pd.Series(y).reset_index(drop=True)
        mask = y_series.notna()
        if int(mask.sum()) < 30:
            continue
        X_h = X.loc[mask].copy()
        y_h = y_series.loc[mask].astype(int)
        if y_h.nunique() < 2:
            continue
        model = lgb.LGBMClassifier(**params)
        model.fit(X_h, y_h)
        heads[int(horizon)] = model
    return heads


def train_stacking_ensemble(
    X: pd.DataFrame,
    y: pd.Series,
    feature_names: Sequence[str] | None = None,
    cv: int = DEFAULT_CV_FOLDS,
    lgbm_params: dict | None = None,
    rf_params: dict | None = None,
    lr_params: dict | None = None,
    meta_params: dict | None = None,
    multitask_y: dict[int, pd.Series] | None = None,
    min_samples: int = MIN_STACKING_SAMPLES,
) -> tuple[StackingEnsembleModel, dict]:
    """訓 LightGBM + LR + RF base + LR meta — return (ensemble, metrics)。

    Args:
      X / y:training features / labels(y 為 0/1 binary)
      feature_names:None → 用 X.columns;明確傳更安全
      cv:OOF 折數(StratifiedKFold)
      *_params:覆寫 default hyperparameters
      multitask_y:optional {horizon: y_series} for auxiliary heads(stored on
        ensemble 不影響 predict_proba)
      min_samples:樣本不足 → raise ValueError;caller 應 fallback 到單 RF

    Returns:
      (ensemble, metrics_dict),metrics_dict 包:
        - ensemble_type / cv_folds
        - n_train / win_rate
        - oof_auc_per_learner: {'lgbm', 'lr', 'rf'}
        - meta_oof_auc / meta_oof_brier(用 OOF 再過一次 meta 的 self-consistency 指標)
        - feature_importances: {'lgbm': dict, 'rf': dict, 'lr': dict}
        - multitask_horizons: list[int](僅在 multitask_y 提供時出現)

    Raises:
      ValueError:len(X) < min_samples 或 y 全同類
    """
    import lightgbm as lgb
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    if feature_names is None:
        feature_names = list(X.columns)
    feature_names = list(feature_names)

    X = X.reset_index(drop=True)[feature_names].copy()
    y = pd.Series(y).reset_index(drop=True).astype(int)

    if len(X) < min_samples:
        raise ValueError(
            f"Stacking 訓練樣本太少({len(X)} < {min_samples})— caller 應 "
            f"fallback 到單 RF。提高樣本量或調 min_samples 才能用 stacking。"
        )
    if y.nunique() < 2:
        raise ValueError("y 全同類,無法訓練 stacking ensemble")

    lgbm_p = dict(DEFAULT_LGBM_PARAMS); lgbm_p.update(lgbm_params or {})
    rf_p = dict(DEFAULT_RF_PARAMS); rf_p.update(rf_params or {})
    lr_p = dict(DEFAULT_LR_PARAMS); lr_p.update(lr_params or {})
    meta_p = dict(DEFAULT_META_PARAMS); meta_p.update(meta_params or {})

    base_learners: dict = {
        "lgbm": lgb.LGBMClassifier(**lgbm_p),
        "lr": _build_lr_pipeline(lr_p),
        "rf": RandomForestClassifier(**rf_p),
    }

    # OOF predictions:給每個 base learner 跑 5-fold cross_val_predict(method='predict_proba')
    # StratifiedKFold(shuffle=True)保證 fold 內 class 比例平衡;對時間序列嚴格
    # OOS 評估走 src/ml_walkforward.py(用 split_by='date')。
    skf = StratifiedKFold(n_splits=cv, shuffle=True, random_state=42)
    oof_preds: dict[str, np.ndarray] = {}
    oof_aucs: dict[str, float] = {}
    for name in sorted(base_learners.keys()):
        learner = base_learners[name]
        # cross_val_predict 內部會 clone learner,不會污染後續 full-data refit
        try:
            oof = cross_val_predict(
                learner, X, y, cv=skf, method="predict_proba", n_jobs=1,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[ENSEMBLE] %s OOF cross_val_predict 失敗:%s — 用 0.5 fallback",
                name, e,
            )
            oof_preds[name] = np.full(len(y), 0.5)
            oof_aucs[name] = float("nan")
            continue
        oof = np.asarray(oof)
        if oof.ndim == 2 and oof.shape[1] == 2:
            oof_preds[name] = oof[:, 1]
        else:
            # degenerate single-class fold output
            oof_preds[name] = np.zeros(len(y))
        oof_aucs[name] = _safe_auc(y, oof_preds[name])

    # Refit base learners on full data — production inference 用
    for name, learner in base_learners.items():
        learner.fit(X, y)

    # Meta-learner:LR on OOF predictions
    oof_df = pd.DataFrame({k: oof_preds[k] for k in sorted(oof_preds.keys())})
    meta_learner = LogisticRegression(**meta_p)
    meta_learner.fit(oof_df, y)

    # Meta self-eval(用 OOF 再餵 meta,non-OOS 但作為 quick sanity)
    meta_proba = meta_learner.predict_proba(oof_df)
    if 1 in list(meta_learner.classes_):
        meta_p1 = np.asarray(meta_proba)[:, list(meta_learner.classes_).index(1)]
    else:
        meta_p1 = np.zeros(len(y))
    meta_oof_auc = _safe_auc(y, meta_p1)
    meta_oof_brier = _safe_brier(y, meta_p1)

    # Feature importances per base learner(供 meta.json 寫出 + UI 顯示)
    feat_imps: dict[str, dict] = {}
    try:
        lgbm = base_learners["lgbm"]
        importances = lgbm.booster_.feature_importance(importance_type="gain")
        feat_imps["lgbm"] = {
            name: float(imp) for name, imp in zip(feature_names, importances)
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[ENSEMBLE] lgbm feature_importances 失敗:%s", e)
    try:
        rf = base_learners["rf"]
        feat_imps["rf"] = {
            name: float(imp)
            for name, imp in zip(feature_names, rf.feature_importances_)
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[ENSEMBLE] rf feature_importances 失敗:%s", e)
    try:
        lr = base_learners["lr"].named_steps["clf"]
        coef = np.asarray(lr.coef_)[0]
        feat_imps["lr"] = {
            name: float(np.abs(c)) for name, c in zip(feature_names, coef)
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[ENSEMBLE] lr coef abs 失敗:%s", e)

    # Auxiliary multi-task heads(共用 same X / 同樣 lgbm_p)
    multitask_heads: dict = {}
    if multitask_y:
        # 5d 對應主 base learner — 不重複訓
        aux_y = {h: yh for h, yh in multitask_y.items() if int(h) != 5}
        multitask_heads = train_multitask_lgbm(X, aux_y, lgbm_params=lgbm_p)

    metrics = {
        "ensemble_type": ENSEMBLE_TYPE,
        "cv_folds": int(cv),
        "n_train": int(len(X)),
        "win_rate": float(y.mean()),
        "oof_auc_per_learner": oof_aucs,
        "meta_oof_auc": meta_oof_auc,
        "meta_oof_brier": meta_oof_brier,
        "feature_importances": feat_imps,
        "feature_names": feature_names,
        "lgbm_params": lgbm_p,
        "rf_params": rf_p,
        "lr_params": lr_p,
        "meta_params": meta_p,
    }
    if multitask_heads:
        metrics["multitask_horizons"] = sorted(multitask_heads.keys())

    ensemble = StackingEnsembleModel(
        base_learners=base_learners,
        meta_learner=meta_learner,
        feature_names=feature_names,
        multitask_heads=multitask_heads,
        train_metrics=metrics,
    )
    return ensemble, metrics


def predict_stacking(ensemble: StackingEnsembleModel, X) -> np.ndarray:
    """便利函式:回 P(y=1) 1D ndarray(對齊 spec 的 API)。"""
    return ensemble.predict_proba(X)[:, 1]


__all__ = [
    "ENSEMBLE_TYPE",
    "MIN_STACKING_SAMPLES",
    "MULTITASK_HORIZONS",
    "DEFAULT_LGBM_PARAMS",
    "DEFAULT_RF_PARAMS",
    "DEFAULT_LR_PARAMS",
    "DEFAULT_META_PARAMS",
    "DEFAULT_CV_FOLDS",
    "StackingEnsembleModel",
    "is_ensemble",
    "train_stacking_ensemble",
    "train_multitask_lgbm",
    "predict_stacking",
]
