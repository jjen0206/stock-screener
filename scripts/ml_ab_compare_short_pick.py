"""M2 Phase 2:short_pick v1 → v3 A/B compare + retrain + safety swap。

流程(apples-to-apples,排除 v1 disk pkl 的 data leakage 偏差):
  1. Build v3 training dataset(16 features)
  2. 同一個 random_state=42 split 出 X_train / X_test
  3. **Baseline**:在 X_train[v2 11 cols] 從零訓一個 v2-equiv model → 評估 X_test
     → baseline_v1.json。比直接用 .v1.bak 公平 — .v1.bak 訓練資料可能與 X_test
     重疊(同一 universe 同 sliding window),造成 ROC AUC 虛高。
  4. 訓練 v3 model on X_train(全 16 features)→ 評估同 X_test → new_v3.json
  5. **Safety gate**:v3 ROC AUC ≥ v1 - 0.02 AND v3 PR AUC ≥ v1 - 0.02 → accept;
     不然 keep .v1.bak,exit code 2 提示主控 rollback

Exit codes:
  0 = accepted(新 model 寫到 models/short_pick.pkl)
  1 = error(資料/IO)
  2 = rejected(v3 比 v1 退步 > 0.02 ROC AUC 或 PR AUC)

Output:
  models/short_pick.pkl.v3.candidate(候選,通過 gate 才 promote 到 .pkl)
  models/baseline_v1.json + models/new_v3.json
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import database as db, ml_predictor  # noqa: E402
from src.universe import TW_TOP_50  # noqa: E402

DEFAULT_OUTPUT = _ROOT / "models" / "short_pick.pkl"
BACKUP_PATH = _ROOT / "models" / "short_pick.pkl.v1.bak"
CANDIDATE_PATH = _ROOT / "models" / "short_pick.pkl.v3.candidate"
BASELINE_JSON = _ROOT / "models" / "baseline_v1.json"
NEW_JSON = _ROOT / "models" / "new_v3.json"

# Safety gate:v3 不能比 v1 退超過此值
ROC_TOLERANCE = 0.02
PR_TOLERANCE = 0.02

V2_FEATURE_COUNT = 11  # 舊 model n_features_in_,新 FEATURE_NAMES 前 11 個


def _compute_metrics(y_true, y_prob, label: str) -> dict:
    """ROC AUC + PR AUC + base rate(可選 metric 補充)。"""
    from sklearn.metrics import roc_auc_score, average_precision_score

    roc = float(roc_auc_score(y_true, y_prob))
    pr = float(average_precision_score(y_true, y_prob))
    base = float(np.mean(y_true))
    print(
        f"[AB/{label}] ROC AUC = {roc:.4f}, PR AUC = {pr:.4f}, "
        f"base rate = {base:.1%}",
        flush=True,
    )
    return {"roc_auc": roc, "pr_auc": pr, "base_rate": base, "n": len(y_true)}


def main() -> int:
    db.init_db()
    counts = db.preload_snapshots()
    if counts:
        print(f"[AB] preload snapshots: {counts}", flush=True)

    if not BACKUP_PATH.exists():
        print(
            f"[AB] FAIL backup 不存在:{BACKUP_PATH} - 先跑 backup 再來。",
            flush=True,
        )
        return 1

    # === Build dataset(用 TW_TOP_50 跟原 train_ml_model.py 一致) ===
    sids = [s for s, _ in TW_TOP_50]
    print(
        f"[AB] universe = {len(sids)} 檔,構造 v3 training dataset...",
        flush=True,
    )
    t0 = time.time()
    X, y = ml_predictor.build_training_dataset(stock_ids=sids)
    elapsed = time.time() - t0
    print(
        f"[AB] dataset: {len(X)} samples × {len(X.columns)} features / "
        f"win rate {y.mean():.1%} / 耗時 {elapsed:.1f}s",
        flush=True,
    )

    if len(X) < 50:
        print(
            f"[AB] FAIL 樣本太少({len(X)} < 50),retrain abort。",
            flush=True,
        )
        return 1
    if len(set(y)) < 2:
        print(
            "[AB] FAIL label 只有單一 class,無法評估 ROC AUC。",
            flush=True,
        )
        return 1

    # === Same 8:2 split across v1 / v3 評估 ===
    from sklearn.model_selection import train_test_split

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y,
    )
    print(
        f"[AB] split: train={len(X_train)} test={len(X_test)}",
        flush=True,
    )

    # === Baseline:重訓 v2-equiv model on X_train[:11 cols],同 split 評估 ===
    # 不直接用 .v1.bak 是因為它的訓練資料可能與 X_test 重疊(同 sliding window),
    # 會虛高 ROC。重訓 v2-equiv 跟 v3 共享同一 train/test split,A/B 公平。
    v2_cols = ml_predictor.FEATURE_NAMES[:V2_FEATURE_COUNT]
    X_train_v2 = X_train[v2_cols]
    X_test_v2 = X_test[v2_cols]

    from sklearn.ensemble import RandomForestClassifier

    v1_model = RandomForestClassifier(
        n_estimators=100,
        max_depth=5,
        min_samples_leaf=5,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )
    v1_model.fit(X_train_v2, y_train)
    v1_proba = v1_model.predict_proba(X_test_v2)
    v1_classes = list(v1_model.classes_)
    if 1 in v1_classes:
        v1_idx = v1_classes.index(1)
        v1_prob = v1_proba[:, v1_idx]
    else:
        v1_prob = np.zeros(len(X_test_v2))

    v1_metrics = _compute_metrics(y_test.to_numpy(), v1_prob, "v1")
    BASELINE_JSON.write_text(
        json.dumps({
            "version": "v1",
            "feature_count": V2_FEATURE_COUNT,
            "feature_names": v2_cols,
            **v1_metrics,
        }, indent=2),
        encoding="utf-8",
    )
    print(f"[AB] baseline_v1.json 寫到 {BASELINE_JSON}", flush=True)

    # === Retrain v3 on X_train(全 16 features) ===
    print("[AB] 訓練 v3 RandomForestClassifier(16 features)...", flush=True)
    t0 = time.time()
    v3_model = RandomForestClassifier(
        n_estimators=100,
        max_depth=5,
        min_samples_leaf=5,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )
    v3_model.fit(X_train, y_train)
    train_elapsed = time.time() - t0
    print(f"[AB] v3 訓練完成,耗時 {train_elapsed:.1f}s", flush=True)

    # === Evaluate v3 ===
    v3_proba = v3_model.predict_proba(X_test)
    v3_classes = list(v3_model.classes_)
    if 1 in v3_classes:
        v3_idx = v3_classes.index(1)
        v3_prob = v3_proba[:, v3_idx]
    else:
        v3_prob = np.zeros(len(X_test))
    v3_metrics = _compute_metrics(y_test.to_numpy(), v3_prob, "v3")

    # 也算 accuracy / precision / recall / f1 給 meta.json
    from sklearn.metrics import (
        accuracy_score, precision_score, recall_score, f1_score,
    )

    v3_pred = v3_model.predict(X_test)
    full_metrics = {
        **v3_metrics,
        "accuracy": float(accuracy_score(y_test, v3_pred)),
        "precision": float(precision_score(y_test, v3_pred, zero_division=0)),
        "recall": float(recall_score(y_test, v3_pred, zero_division=0)),
        "f1": float(f1_score(y_test, v3_pred, zero_division=0)),
        "n_train": len(X_train),
        "n_test": len(X_test),
        "train_elapsed_secs": train_elapsed,
        "feature_count": len(ml_predictor.FEATURE_NAMES),
        "feature_names": list(ml_predictor.FEATURE_NAMES),
    }

    NEW_JSON.write_text(
        json.dumps({"version": "v3", **full_metrics}, indent=2),
        encoding="utf-8",
    )
    print(f"[AB] new_v3.json 寫到 {NEW_JSON}", flush=True)

    # Feature importance
    importances = sorted(
        zip(ml_predictor.FEATURE_NAMES, v3_model.feature_importances_),
        key=lambda kv: -kv[1],
    )
    print("[AB] === v3 Feature importance(全部 16) ===", flush=True)
    for name, imp in importances:
        print(f"  {name:<28s} {imp:.4f}", flush=True)

    # === Safety gate ===
    roc_diff = v3_metrics["roc_auc"] - v1_metrics["roc_auc"]
    pr_diff = v3_metrics["pr_auc"] - v1_metrics["pr_auc"]
    print(
        f"[AB] === Compare ===\n"
        f"  ROC AUC: v1={v1_metrics['roc_auc']:.4f} → "
        f"v3={v3_metrics['roc_auc']:.4f} (Δ={roc_diff:+.4f})\n"
        f"  PR  AUC: v1={v1_metrics['pr_auc']:.4f} → "
        f"v3={v3_metrics['pr_auc']:.4f} (Δ={pr_diff:+.4f})",
        flush=True,
    )

    accept = (roc_diff >= -ROC_TOLERANCE) and (pr_diff >= -PR_TOLERANCE)
    if not accept:
        print(
            f"[AB] REJECT - v3 退步超過容忍 "
            f"(ROC >= -{ROC_TOLERANCE} AND PR >= -{PR_TOLERANCE})。",
            flush=True,
        )
        # 不寫 .pkl,候選留在 .v3.candidate 路徑供 debug
        ml_predictor.save_model(v3_model, CANDIDATE_PATH)
        print(
            f"[AB] candidate model 已存 {CANDIDATE_PATH} 供分析;"
            f"models/short_pick.pkl 保持 v1。",
            flush=True,
        )
        return 2

    # === ACCEPT:promote candidate → models/short_pick.pkl ===
    ml_predictor.save_model(v3_model, DEFAULT_OUTPUT)
    print(f"[AB] ACCEPT - v3 model 寫到 {DEFAULT_OUTPUT}", flush=True)

    # Dump v3 meta sidecar(含完整 16 feature 順序)
    meta_path = ml_predictor.dump_model_meta(
        DEFAULT_OUTPUT,
        metrics={
            "n_train": full_metrics["n_train"],
            "n_test": full_metrics["n_test"],
            "win_rate_overall": float(y.mean()),
            "accuracy": full_metrics["accuracy"],
            "precision": full_metrics["precision"],
            "recall": full_metrics["recall"],
            "f1": full_metrics["f1"],
        },
        feature_names=list(ml_predictor.FEATURE_NAMES),
    )
    print(f"[AB] Metadata 已存 {meta_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
