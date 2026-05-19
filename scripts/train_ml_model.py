"""訓練短線勝率預測模型 → 存 models/short_pick.pkl。

Phase 2 #P2-5(2026-05-18)新增 --backend 參數:可選 'rf'(default, 舊單一
RandomForest)或 'ensemble'(LightGBM + LR + RF stacking ensemble)。

**預設仍 rf 的原因**:2026-05-18 walk-forward eval(TW_TOP_50,1290 samples,
5d ATR label)顯示 ensemble AUC 比 RF baseline 低 2.6pp(0.550 vs 0.577)。
詳見 `docs/ml-ensemble-walkforward-2026-05-18.md`。在 short_pick 全市場 fallback
model 這個特定 dataset 上,stacking 並未通過 spec 的「+2pp gate」,所以
default 維持 rf 不變,避免拖累 production。

何時開 --backend ensemble:
  - per-strategy datasets(2000+ samples + cost-aware labels)— 仍可能 win
  - LightGBM hyperparameters 重 tune 後(e.g. num_leaves 從 31 → 7-15 抑制
    overfit on 500-sample folds)再重跑 walk-forward
  - 加更多 features / 切換到 cost-aware label 後

用法:
    python scripts/train_ml_model.py                        # default: backend=rf(safe)
    python scripts/train_ml_model.py --backend ensemble     # 切到 stacking(自負風險)
    python scripts/train_ml_model.py --universe top50       # 預設,~60 檔
    python scripts/train_ml_model.py --universe full        # 全市場
    python scripts/train_ml_model.py --no-multitask         # 關 auxiliary 1d/3d/10d heads

輸出:
- 模型 pkl 進 models/short_pick.pkl
  - backend=ensemble:StackingEnsembleModel(joblib pickle)
  - backend=rf:RandomForestClassifier(舊格式)
- Calibrator pkl 進 models/calibrators/short_pick.pkl
- console 印 metrics + feature importance
- exit 0 = 訓練成功;exit 1 = 資料太少
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src import ml_predictor  # noqa: E402
from src.universe import TW_TOP_50  # noqa: E402

DEFAULT_OUTPUT = _ROOT / "models" / "short_pick.pkl"
# Multi-task auxiliary horizons(1d/3d/10d;5d 是主 label,自動加進去)
DEFAULT_MULTITASK_HORIZONS = (1, 3, 5, 10)


def _train_ensemble(X, y, multitask_y):
    """走 stacking ensemble 路徑:LightGBM + LR + RF base + LR meta。

    回 (model, metrics)。metrics 對齊 dump_model_meta 期望的鍵
    (n_train, n_test, accuracy, precision, recall, f1, win_rate_overall)。
    """
    from src import ml_ensemble
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import (
        accuracy_score, precision_score, recall_score, f1_score,
    )

    # 8:2 split 給 accuracy/precision/recall/f1 評估(對齊舊 train_short_pick_model
    # 的 metrics 介面,讓 dump_model_meta 不用改)
    if len(X) >= 50:
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42,
            stratify=y if len(set(y)) > 1 else None,
        )
        mt_train = None
        if multitask_y:
            mt_train = {}
            train_idx = X_train.index
            for h, y_h in multitask_y.items():
                mt_train[h] = y_h.loc[train_idx]
    else:
        X_train, X_test, y_train, y_test = X, X, y, y
        mt_train = multitask_y

    ensemble, ens_metrics = ml_ensemble.train_stacking_ensemble(
        X_train, y_train,
        feature_names=ml_predictor.FEATURE_NAMES,
        multitask_y=mt_train,
    )

    y_pred = ensemble.predict(X_test)
    metrics = {
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "win_rate_overall": float(y.mean()),
        "accuracy": float(accuracy_score(y_test, y_pred)),
        "precision": float(precision_score(y_test, y_pred, zero_division=0)),
        "recall": float(recall_score(y_test, y_pred, zero_division=0)),
        "f1": float(f1_score(y_test, y_pred, zero_division=0)),
        "ensemble": ens_metrics,
    }
    return ensemble, metrics


def main() -> int:
    p = argparse.ArgumentParser(description="訓練短線勝率預測模型")
    p.add_argument(
        "--universe", choices=["top50", "full", "watchlist"],
        default="top50",
        help="訓練 universe(預設 top50,~60 檔約 1-2 分鐘)",
    )
    p.add_argument(
        "--output", default=str(DEFAULT_OUTPUT),
        help=f"輸出 pkl 路徑(預設 {DEFAULT_OUTPUT})",
    )
    p.add_argument(
        "--backend", choices=["ensemble", "rf"], default="rf",
        help=(
            "預設 rf(walk-forward eval 在 TW_TOP_50 上 ensemble 比 RF 低 2.6pp,"
            "詳見 docs/ml-ensemble-walkforward-2026-05-18.md)。"
            "傳 ensemble 切到 LightGBM + LR + RF stacking(Phase 2 #P2-5)。"
        ),
    )
    p.add_argument(
        "--no-multitask", action="store_true",
        help="關閉 auxiliary 1d/3d/10d multitask heads(只訓主 5d label)",
    )
    args = p.parse_args()

    db.init_db()
    counts = db.preload_snapshots()
    if counts:
        print(f"[TRAIN] preload snapshots: {counts}", flush=True)

    if args.universe == "top50":
        sids = [s for s, _ in TW_TOP_50]
    elif args.universe == "watchlist":
        items = db.get_watchlist()
        sids = [it["stock_id"] for it in items] or [s for s, _ in TW_TOP_50]
    else:  # full
        with db.get_conn() as conn:
            sids = [
                r["stock_id"] for r in conn.execute(
                    "SELECT DISTINCT stock_id FROM daily_prices "
                    "WHERE stock_id != 'TAIEX' "
                    "ORDER BY stock_id"
                ).fetchall()
            ]

    print(f"[TRAIN] universe = {len(sids)} 檔 / backend = {args.backend}", flush=True)
    print("[TRAIN] 構造訓練資料(sliding window features + label)...", flush=True)
    t0 = time.time()

    use_multitask = (args.backend == "ensemble") and (not args.no_multitask)
    if use_multitask:
        X, y, multitask_y = ml_predictor.build_training_dataset(
            stock_ids=sids,
            multitask_horizons=DEFAULT_MULTITASK_HORIZONS,
        )
    else:
        X, y = ml_predictor.build_training_dataset(stock_ids=sids)
        multitask_y = None

    elapsed = time.time() - t0
    print(
        f"[TRAIN] dataset 構造完成:{len(X)} samples × {len(X.columns)} features"
        f" / win rate {y.mean():.1%} / 耗時 {elapsed:.1f}s",
        flush=True,
    )

    if len(X) < 50:
        print(
            f"❌ 訓練資料太少({len(X)} samples),最少需 50 筆。"
            "可能原因:SQLite 內 daily_prices 歷史不足(需 45+ 天)。",
            flush=True,
        )
        return 1

    metrics: dict
    if args.backend == "ensemble":
        from src import ml_ensemble

        if len(X) < ml_ensemble.MIN_STACKING_SAMPLES:
            print(
                f"⚠ 樣本 {len(X)} < MIN_STACKING_SAMPLES "
                f"{ml_ensemble.MIN_STACKING_SAMPLES} → fallback 到單 RF",
                flush=True,
            )
            print("[TRAIN] 訓練 RandomForestClassifier(fallback)...", flush=True)
            t0 = time.time()
            model, metrics = ml_predictor.train_short_pick_model(X, y)
            print(f"[TRAIN] 訓練完成,耗時 {time.time() - t0:.1f}s", flush=True)
        else:
            print(
                "[TRAIN] 訓練 stacking ensemble"
                "(LightGBM + LR + RF + LR meta-learner)...",
                flush=True,
            )
            t0 = time.time()
            model, metrics = _train_ensemble(X, y, multitask_y)
            print(f"[TRAIN] 訓練完成,耗時 {time.time() - t0:.1f}s", flush=True)
    else:
        # backend == 'rf' — 舊路徑保留
        print("[TRAIN] 訓練 RandomForestClassifier(--backend rf)...", flush=True)
        t0 = time.time()
        model, metrics = ml_predictor.train_short_pick_model(X, y)
        print(f"[TRAIN] 訓練完成,耗時 {time.time() - t0:.1f}s", flush=True)

    print("[TRAIN] === 評估指標(8:2 train/test split) ===", flush=True)
    print(f"  整體 win rate: {metrics['win_rate_overall']:.1%}", flush=True)
    print(f"  訓練樣本:    {metrics['n_train']}", flush=True)
    print(f"  測試樣本:    {metrics['n_test']}", flush=True)
    print(f"  Accuracy:     {metrics['accuracy']:.3f}", flush=True)
    print(f"  Precision:    {metrics['precision']:.3f}", flush=True)
    print(f"  Recall:       {metrics['recall']:.3f}", flush=True)
    print(f"  F1:           {metrics['f1']:.3f}", flush=True)

    ens_metrics = metrics.get("ensemble")
    if ens_metrics:
        print("[TRAIN] === Stacking ensemble 指標(OOF) ===", flush=True)
        for name, auc in ens_metrics.get("oof_auc_per_learner", {}).items():
            print(f"  base {name:<6s} OOF AUC: {auc:.3f}", flush=True)
        print(
            f"  meta OOF AUC:   {ens_metrics.get('meta_oof_auc', float('nan')):.3f}",
            flush=True,
        )
        print(
            f"  meta OOF Brier: {ens_metrics.get('meta_oof_brier', float('nan')):.4f}",
            flush=True,
        )
        mt_horizons = ens_metrics.get("multitask_horizons", [])
        if mt_horizons:
            print(
                f"  Multitask heads trained: {mt_horizons}d (auxiliary)",
                flush=True,
            )

    # Feature importance:RF 走 model.feature_importances_,ensemble 走 lgbm gain
    print("[TRAIN] === Feature importance(top 5) ===", flush=True)
    if hasattr(model, "feature_importances_"):
        importances = sorted(
            zip(ml_predictor.FEATURE_NAMES, model.feature_importances_),
            key=lambda kv: -kv[1],
        )
        for name, imp in importances[:5]:
            print(f"  {name:<28s} {imp:.4f}", flush=True)
    elif ens_metrics:
        lgbm_imps = ens_metrics.get("feature_importances", {}).get("lgbm", {})
        top5 = sorted(lgbm_imps.items(), key=lambda kv: -kv[1])[:5]
        for name, imp in top5:
            print(f"  lgbm {name:<28s} {imp:.2f}", flush=True)

    out_path = Path(args.output)
    ml_predictor.save_model(model, out_path)
    print(f"[TRAIN] 模型已存 {out_path}", flush=True)

    # 訓練 calibrator(time-based 最後 20% holdout) — 對 ensemble + RF 都適用
    # (Calibrator 走 base_model.predict_proba,duck-type 一致)
    try:
        from src import ml_calibration

        # train_with_calibration 內部 hardcode RandomForest — 我們改成手動跑 ensemble
        # / RF 的 calibrator,共用 ml_calibration.fit_calibrator
        n = len(X)
        holdout_n = max(1, int(round(n * 0.2)))
        train_n = n - holdout_n
        # 時序假設:build_training_dataset rows 在 per-sid 內 asc,跨 sid 不嚴格,
        # 但「後 20%」對「整體分布」夠用(對齊舊 train_with_calibration 邏輯)。
        X_holdout = X.iloc[train_n:]
        y_holdout = y.iloc[train_n:]
        if len(set(y_holdout.tolist())) >= 2:
            raw_holdout = ml_calibration._extract_positive_proba(model, X_holdout)
            raw_metrics = ml_calibration.compute_calibration_metrics(
                y_holdout.to_numpy(), raw_holdout,
            )
            calibrator = ml_calibration.fit_calibrator(
                model, X_holdout, y_holdout, method="isotonic",
            )
            cal_path = ml_calibration.save_calibrator(calibrator, "short_pick")
            calibrated_holdout = calibrator.transform(raw_holdout)
            cal_metrics = ml_calibration.compute_calibration_metrics(
                y_holdout.to_numpy(), calibrated_holdout,
            )
            print(
                f"[TRAIN] Calibrator({calibrator.method})已存 {cal_path} — "
                f"holdout n={holdout_n}",
                flush=True,
            )
            print(
                f"[TRAIN] Brier raw={raw_metrics['brier_score']:.4f} → "
                f"calibrated={cal_metrics['brier_score']:.4f} "
                f"(Δ={cal_metrics['brier_score'] - raw_metrics['brier_score']:+.4f},越低越好)",
                flush=True,
            )
            metrics["calibration"] = {
                "method": calibrator.method,
                "n_holdout": int(holdout_n),
                "raw_brier": float(raw_metrics["brier_score"]),
                "calibrated_brier": float(cal_metrics["brier_score"]),
            }
        else:
            print(
                "[TRAIN] holdout 全同類,跳過 calibrator",
                flush=True,
            )
    except Exception as e:  # noqa: BLE001
        print(f"[TRAIN] Calibrator 訓練失敗(non-fatal):{type(e).__name__}: {e}", flush=True)

    # dump sidecar metadata 給「⚙️ 系統」頁顯示
    model_type = (
        "StackingEnsembleModel" if metrics.get("ensemble") else "RandomForestClassifier"
    )
    meta_path = ml_predictor.dump_model_meta(
        out_path,
        metrics=metrics,
        feature_names=ml_predictor.FEATURE_NAMES,
        model_type=model_type,
    )
    print(f"[TRAIN] Metadata 已存 {meta_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
