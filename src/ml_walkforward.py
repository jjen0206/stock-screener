"""ML walk-forward validation framework(M2 後續)。

random 80/20 split 對時間序列會 leak「未來資訊」回 train(同一 sid 後一天的
feature 跟前一天的 y 高度相關)。walk-forward = expanding window:
  split 1: train [0, N)        → test [N, N+T)
  split 2: train [0, N+T)      → test [N+T, N+2T)
  split 3: train [0, N+2T)     → test [N+2T, N+3T)
  ...

對每 split 算 ROC AUC / PR AUC / log loss(train + test),供時序 OOS 評估。
聚合 mean / std / min / max,看模型 OOS 穩定度。

不動 src/ml_predictor.py 既有 train_short_pick_model — 那條路徑還是 production
train,本 module 純 read-only evaluator,寫到新表 ml_walkforward_results。

Hyperparams 對齊 ml_predictor.train_short_pick_model:
  RandomForestClassifier(
      n_estimators=100, max_depth=5, min_samples_leaf=5,
      class_weight="balanced", random_state=42, n_jobs=-1,
  )
"""
from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

# sklearn lazy import:跟 src/ml_predictor.py 同樣的 cold-start 考量(streamlit /
# notifier import 鏈不會被拖累),且 train_one_split 才真的要用。

DEFAULT_N_SPLITS = 5
DEFAULT_TEST_SIZE = 20
DEFAULT_MIN_TRAIN_SIZE = 100
# sentinel:n_splits=None → 用 max_possible(全部可分 splits)
# 修法依據:docs/ml-overfit-root-cause.md(W1 報告)— 原 hard cap=5 對大樣本
# (taiex_alpha 2248 / bias_convergence 2685)只測前 200 rows、浪費 90%+ 資料

# RandomForest hyperparams 對齊 production train_short_pick_model
_RF_KWARGS = dict(
    n_estimators=100,
    max_depth=5,
    min_samples_leaf=5,
    class_weight="balanced",
    random_state=42,
    n_jobs=-1,
)


def _train_one_split(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    rf_kwargs: dict | None = None,
    calibrate: bool = True,
) -> dict:
    """一個 split 內訓 + 評(train + test 各算 ROC / PR / log loss + Brier)。

    rf_kwargs:覆寫 default _RF_KWARGS(供 per-strategy 客製 hyperparams,
    e.g. gap_up 用 max_depth=5 / min_samples_leaf=10 抑制 overfit)。

    calibrate=True 時額外算 calibrated 的 brier(用 X_train 後 20% holdout
    fit calibrator),讓 WF 評估能對比 raw vs calibrated 校準狀況。
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import (
        average_precision_score, log_loss, roc_auc_score,
    )

    kwargs = dict(_RF_KWARGS)
    if rf_kwargs:
        kwargs.update(rf_kwargs)
    model = RandomForestClassifier(**kwargs)
    model.fit(X_train, y_train)

    # 訓 calibrator(用 X_train 後 20% holdout — 不再 leak X_test 進去)
    calibrator = None
    if calibrate:
        try:
            from src import ml_calibration

            n = len(X_train)
            cal_holdout = max(10, int(round(n * 0.2)))
            cal_train_n = n - cal_holdout
            if cal_train_n >= 30 and cal_holdout >= 10:
                X_cal = X_train.iloc[cal_train_n:]
                y_cal = y_train.iloc[cal_train_n:]
                if len(set(y_cal.tolist())) >= 2:
                    calibrator = ml_calibration.fit_calibrator(
                        model, X_cal, y_cal, method="isotonic",
                    )
        except Exception:  # noqa: BLE001
            calibrator = None

    def _eval(X, y):
        # predict_proba 對 class=1 的欄(class_weight=balanced 仍會輸出 [P0, P1])
        proba = model.predict_proba(X)
        if 1 in model.classes_:
            idx = list(model.classes_).index(1)
            p1 = proba[:, idx]
        else:
            # 訓練資料全 0(極端 case)→ 給 0.0,後續 metric 全是 NaN 不能算
            p1 = np.zeros(len(X))
        # raw brier(永遠算,單類也算得出來)
        y_arr = np.asarray(y, dtype=float)
        raw_brier = float(np.mean((y_arr - p1) ** 2)) if len(y_arr) else float("nan")
        # calibrated brier:有 calibrator 才算
        cal_brier = float("nan")
        if calibrator is not None and len(p1):
            try:
                p1_cal = calibrator.transform(p1)
                cal_brier = float(np.mean((y_arr - p1_cal) ** 2))
            except Exception:  # noqa: BLE001
                cal_brier = float("nan")
        # 單類 → roc_auc / pr_auc 算不出來(sklearn raise),回 NaN
        unique_y = set(np.unique(y).tolist())
        if len(unique_y) < 2:
            return {
                "roc_auc": float("nan"),
                "pr_auc": float("nan"),
                "log_loss": float("nan"),
                "brier": raw_brier,
                "calibrated_brier": cal_brier,
                "n": int(len(y)),
                "pos_rate": float(np.mean(y)),
            }
        return {
            "roc_auc": float(roc_auc_score(y, p1)),
            "pr_auc": float(average_precision_score(y, p1)),
            "log_loss": float(log_loss(y, np.clip(p1, 1e-7, 1 - 1e-7))),
            "brier": raw_brier,
            "calibrated_brier": cal_brier,
            "n": int(len(y)),
            "pos_rate": float(np.mean(y)),
        }

    return {
        "train": _eval(X_train, y_train),
        "test": _eval(X_test, y_test),
    }


def walkforward_train_test(
    features_df: pd.DataFrame,
    *,
    n_splits: int | None = DEFAULT_N_SPLITS,
    test_size: int = DEFAULT_TEST_SIZE,
    min_train_size: int = DEFAULT_MIN_TRAIN_SIZE,
    date_col: str = "date",
    target_col: str = "y",
    feature_cols: Sequence[str] | None = None,
    rf_kwargs: dict | None = None,
    split_by: str = "row",
) -> list[dict]:
    """expanding-window walk-forward CV。

    Args:
      features_df:必含 date_col + target_col + feature columns。多筆同 date OK
        (多 sid 同日各自一筆),會一起進對應 fold。
      n_splits:最多 split 數。實際可用 split 受 (len-min_train_size) // test_size 限制。
        傳 None → 直接用 max_possible(全部可分 splits 都跑),避免大樣本只測前段。
      test_size:每個 test fold 大小。split_by="row" → rows;"date" → 日數。
      min_train_size:第一個 train fold 至少要的大小(rows 或 days,同上)。
      feature_cols:None → 自動取所有非 date/target 欄。
      split_by:"row"(default,行索引切)或 "date"(對 unique dates 切,
        同日所有 sids 保證在同一 fold,消除 cross-sectional 虛高 — 主公拍板
        2026-05-15)。

    Returns:
      list of dict,每個 split 一個:
        {
          "split_idx": int,
          "train_start": "YYYY-MM-DD", "train_end": ...,
          "test_start": ..., "test_end": ...,
          "train": {roc_auc, pr_auc, log_loss, n, pos_rate},
          "test":  {roc_auc, pr_auc, log_loss, n, pos_rate},
        }

    Raises:
      ValueError:date_col / target_col 缺;min_train_size 太大導致 0 split;
        split_by 不是 'row' / 'date'。
    """
    if split_by not in ("row", "date"):
        raise ValueError(
            f"split_by 必須是 'row' 或 'date',got {split_by!r}"
        )
    if date_col not in features_df.columns:
        raise ValueError(
            f"features_df 缺 date column '{date_col}' — walk-forward 必須有時間軸"
        )
    if target_col not in features_df.columns:
        raise ValueError(
            f"features_df 缺 target column '{target_col}'"
        )

    if feature_cols is None:
        feature_cols = [
            c for c in features_df.columns
            if c not in (date_col, target_col)
        ]
    if not feature_cols:
        raise ValueError("features_df 沒有任何 feature column")

    # drop 任何 feature / target 為 NaN 的 row(graceful 處理)
    df = features_df.dropna(subset=list(feature_cols) + [target_col]).copy()
    if df.empty:
        # 完全 NaN — 不 raise,回空(scripts 端可決定是否視為失敗)
        return []
    # 按時間排序(穩定:同 date 內 sid 的相對順序保留即可,不影響 split 結果)
    df = df.sort_values(date_col, kind="stable").reset_index(drop=True)

    if split_by == "date":
        return _walkforward_by_date(
            df,
            n_splits=n_splits,
            test_size=test_size,
            min_train_size=min_train_size,
            date_col=date_col,
            target_col=target_col,
            feature_cols=list(feature_cols),
            rf_kwargs=rf_kwargs,
        )

    n = len(df)
    if min_train_size >= n:
        raise ValueError(
            f"min_train_size={min_train_size} >= 樣本數 {n},無法 split"
        )

    # 計算可用 splits:從 min_train_size 開始,每次往前推 test_size
    # split i:train = [0, min_train_size + i*test_size)
    #         test  = [min_train_size + i*test_size, min_train_size + (i+1)*test_size)
    max_possible = (n - min_train_size) // max(1, test_size)
    if max_possible <= 0:
        # test_size 太大 / 樣本不夠 → 沒 fold;graceful 回空(別 raise,讓 caller
        # 印 warning 跳下一個 model 即可)
        return []

    # n_splits=None → 全部可分 splits(避免 cap=5 對大樣本只測前 200 rows)
    actual_splits = max_possible if n_splits is None else min(n_splits, max_possible)

    results: list[dict] = []
    for i in range(actual_splits):
        train_end = min_train_size + i * test_size
        test_end = train_end + test_size
        if test_end > n:
            break

        train_slice = df.iloc[:train_end]
        test_slice = df.iloc[train_end:test_end]

        X_train = train_slice[list(feature_cols)]
        y_train = train_slice[target_col].astype(int)
        X_test = test_slice[list(feature_cols)]
        y_test = test_slice[target_col].astype(int)

        # 訓練資料全同類 → sklearn fit 會 OK(class_weight 會 degenerate),但
        # predict_proba 只有 1 column → eval 階段會 detect 並回 NaN
        if len(set(y_train.unique())) < 2:
            # train 全同類 — 這個 split 沒意義,跳過(不算進 results)
            continue

        metrics = _train_one_split(
            X_train, y_train, X_test, y_test, rf_kwargs=rf_kwargs,
        )

        results.append({
            "split_idx": i,
            "train_start": str(train_slice[date_col].iloc[0]),
            "train_end": str(train_slice[date_col].iloc[-1]),
            "test_start": str(test_slice[date_col].iloc[0]),
            "test_end": str(test_slice[date_col].iloc[-1]),
            **metrics,
        })

    return results


def _walkforward_by_date(
    df: pd.DataFrame,
    *,
    n_splits: int | None,
    test_size: int,
    min_train_size: int,
    date_col: str,
    target_col: str,
    feature_cols: list[str],
    rf_kwargs: dict | None,
) -> list[dict]:
    """split_by='date' 實作:對 unique dates 切,同日 sids 保留同 fold。

    test_size / min_train_size 在此模式下單位為「日數」。每 split:
      train = dates[0 : min_train_size + i*test_size]   的所有 rows
      test  = dates[min_train_size + i*test_size : +test_size] 的所有 rows
    """
    unique_dates = sorted(df[date_col].unique().tolist())
    n_dates = len(unique_dates)

    if min_train_size >= n_dates:
        raise ValueError(
            f"min_train_size={min_train_size} >= unique dates {n_dates}(split_by=date),"
            f"無法 split"
        )

    max_possible = (n_dates - min_train_size) // max(1, test_size)
    if max_possible <= 0:
        return []

    actual_splits = max_possible if n_splits is None else min(n_splits, max_possible)

    # 預先做 date → rows 的 group,避免每 split 都 isin 線性掃
    # 注意:df 已經按 date 排序,所以 cum index 切片就夠
    date_to_idx: dict = {}
    for idx, d in enumerate(df[date_col].tolist()):
        date_to_idx.setdefault(d, []).append(idx)

    results: list[dict] = []
    for i in range(actual_splits):
        train_end_d = min_train_size + i * test_size
        test_end_d = train_end_d + test_size
        if test_end_d > n_dates:
            break

        train_dates = unique_dates[:train_end_d]
        test_dates = unique_dates[train_end_d:test_end_d]

        train_idxs: list[int] = []
        for d in train_dates:
            train_idxs.extend(date_to_idx[d])
        test_idxs: list[int] = []
        for d in test_dates:
            test_idxs.extend(date_to_idx[d])

        train_slice = df.iloc[train_idxs]
        test_slice = df.iloc[test_idxs]

        # 同日 sids 保留同 fold(invariant guard,單測會驗證)
        # 不可能有 date 同時出現 train 跟 test — train/test_dates 不交集

        X_train = train_slice[feature_cols]
        y_train = train_slice[target_col].astype(int)
        X_test = test_slice[feature_cols]
        y_test = test_slice[target_col].astype(int)

        if len(set(y_train.unique())) < 2:
            continue
        if len(test_slice) == 0:
            continue

        metrics = _train_one_split(
            X_train, y_train, X_test, y_test, rf_kwargs=rf_kwargs,
        )

        results.append({
            "split_idx": i,
            "train_start": str(train_dates[0]),
            "train_end": str(train_dates[-1]),
            "test_start": str(test_dates[0]),
            "test_end": str(test_dates[-1]),
            **metrics,
        })

    return results


def walkforward_summary(results: list[dict]) -> dict:
    """聚合 splits → mean / std / min / max(只算 test 端)。

    Returns:
      {
        "n_splits": int,
        "test_roc_auc": {"mean", "std", "min", "max"},
        "test_pr_auc":  {...},
        "test_log_loss": {...},
        "train_roc_auc_mean": float,  # 給 overfit gap 參考
      }
      results 為空 → 全 NaN(caller 自己決定如何處理)。
    """
    if not results:
        empty_stat = {k: float("nan") for k in ("mean", "std", "min", "max")}
        return {
            "n_splits": 0,
            "test_roc_auc": dict(empty_stat),
            "test_pr_auc": dict(empty_stat),
            "test_log_loss": dict(empty_stat),
            "test_brier_raw": dict(empty_stat),
            "test_brier_calibrated": dict(empty_stat),
            "train_roc_auc_mean": float("nan"),
        }

    def _agg(vals: list[float]) -> dict:
        arr = np.array([v for v in vals if not np.isnan(v)], dtype=float)
        if len(arr) == 0:
            return {k: float("nan") for k in ("mean", "std", "min", "max")}
        return {
            "mean": float(arr.mean()),
            "std": float(arr.std(ddof=0)),  # population std,單一 split 時 std=0 不 NaN
            "min": float(arr.min()),
            "max": float(arr.max()),
        }

    test_roc = [r["test"]["roc_auc"] for r in results]
    test_pr = [r["test"]["pr_auc"] for r in results]
    test_ll = [r["test"]["log_loss"] for r in results]
    train_roc = [r["train"]["roc_auc"] for r in results]
    # brier:_train_one_split 加進來,舊 split 沒這欄 → graceful NaN
    test_brier_raw = [r["test"].get("brier", float("nan")) for r in results]
    test_brier_cal = [
        r["test"].get("calibrated_brier", float("nan")) for r in results
    ]

    train_roc_arr = np.array(
        [v for v in train_roc if not np.isnan(v)], dtype=float
    )
    train_roc_mean = float(train_roc_arr.mean()) if len(train_roc_arr) else float("nan")

    return {
        "n_splits": len(results),
        "test_roc_auc": _agg(test_roc),
        "test_pr_auc": _agg(test_pr),
        "test_log_loss": _agg(test_ll),
        "test_brier_raw": _agg(test_brier_raw),
        "test_brier_calibrated": _agg(test_brier_cal),
        "train_roc_auc_mean": train_roc_mean,
    }


__all__ = [
    "DEFAULT_N_SPLITS",
    "DEFAULT_TEST_SIZE",
    "DEFAULT_MIN_TRAIN_SIZE",
    "walkforward_train_test",
    "walkforward_summary",
]
