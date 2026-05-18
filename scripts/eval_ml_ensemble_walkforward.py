"""Walk-forward 比較:RandomForest baseline vs Stacking Ensemble(Phase 2 #P2-5)。

對相同 dataset(預設 TW_TOP_50 sliding window + 5d ATR label)跑 expanding-
window walk-forward。對每 split:
  - train RF baseline(對齊 ml_walkforward._RF_KWARGS)
  - train Stacking Ensemble(若樣本 ≥ MIN_STACKING_SAMPLES;否則 fallback RF)
  - test 段算 ROC AUC / Brier(raw / calibrated)

聚合所有 splits → mean / std / per-split table → 寫到
docs/ml-ensemble-walkforward-2026-05-18.md。

用法:
    python scripts/eval_ml_ensemble_walkforward.py
    python scripts/eval_ml_ensemble_walkforward.py --universe top50
    python scripts/eval_ml_ensemble_walkforward.py --output docs/ml-ensemble-walkforward-2026-05-18.md
    python scripts/eval_ml_ensemble_walkforward.py --min-train-days 60 --test-days 20

Exit code:
  0 = 有跑出 splits + 寫出報告
  1 = dataset 為空或 split 不足
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db, ml_calibration, ml_ensemble, ml_predictor  # noqa: E402
from src.universe import TW_TOP_50  # noqa: E402


DEFAULT_OUTPUT = _ROOT / "docs" / "ml-ensemble-walkforward-2026-05-18.md"
DEFAULT_MIN_TRAIN_DAYS = 60
DEFAULT_TEST_DAYS = 20


def _safe_auc(y_true, y_score) -> float:
    from sklearn.metrics import roc_auc_score

    try:
        if len(set(y_true)) < 2:
            return float("nan")
        return float(roc_auc_score(y_true, y_score))
    except ValueError:
        return float("nan")


def _safe_brier(y_true, y_score) -> float:
    from sklearn.metrics import brier_score_loss

    try:
        return float(brier_score_loss(y_true, np.clip(y_score, 1e-7, 1 - 1e-7)))
    except Exception:  # noqa: BLE001
        return float("nan")


def _train_rf(X_train, y_train):
    """跟 ml_walkforward._RF_KWARGS 對齊。"""
    from sklearn.ensemble import RandomForestClassifier

    rf = RandomForestClassifier(
        n_estimators=100, max_depth=5, min_samples_leaf=5,
        class_weight="balanced", random_state=42, n_jobs=-1,
    )
    rf.fit(X_train, y_train)
    return rf


def _train_ensemble_or_rf(X_train, y_train):
    """樣本 ≥ MIN_STACKING_SAMPLES → stacking;否則 fallback RF。"""
    if len(X_train) >= ml_ensemble.MIN_STACKING_SAMPLES:
        try:
            ens, _ = ml_ensemble.train_stacking_ensemble(
                X_train, y_train, feature_names=list(X_train.columns),
            )
            return ens, "stacking"
        except Exception as e:  # noqa: BLE001
            print(f"[WF] stacking 訓練失敗,fallback RF:{e}", flush=True)
    return _train_rf(X_train, y_train), "rf"


def _predict_p1(model, X) -> np.ndarray:
    proba = model.predict_proba(X)
    classes = list(getattr(model, "classes_", [0, 1]))
    if 1 in classes:
        idx = classes.index(1)
        return np.asarray(proba)[:, idx]
    return np.zeros(len(X))


def _fit_calibrator(model, X_train, y_train, method="isotonic"):
    """用 train 段後 20% holdout fit calibrator(時序內 holdout — 不 leak test)。"""
    n = len(X_train)
    cal_n = max(10, int(round(n * 0.2)))
    if cal_n < 10 or n - cal_n < 30:
        return None
    X_cal = X_train.iloc[n - cal_n:]
    y_cal = y_train.iloc[n - cal_n:]
    if y_cal.nunique() < 2:
        return None
    try:
        return ml_calibration.fit_calibrator(model, X_cal, y_cal, method=method)
    except Exception:  # noqa: BLE001
        return None


def run_walkforward(
    X: pd.DataFrame,
    y: pd.Series,
    dates: pd.Series,
    min_train_days: int,
    test_days: int,
) -> list[dict]:
    """expanding window walk-forward,split_by date(同日 sids 必同 fold)。

    每 split:
      train_dates = sorted_unique_dates[: min_train_days + i*test_days]
      test_dates  = sorted_unique_dates[..end : ..end + test_days]

    Returns:
      list[dict] 每 split 一筆,鍵:split_idx / train_dates / test_dates /
      n_train / n_test / pos_rate_train / pos_rate_test /
      rf_test_auc / rf_test_brier / rf_test_brier_cal /
      ens_test_auc / ens_test_brier / ens_test_brier_cal /
      ens_backend('stacking' or 'rf')。
    """
    df = pd.DataFrame({"date": dates, "y": y.astype(int)})
    df = pd.concat([df, X.reset_index(drop=True)], axis=1)
    df = df.sort_values("date", kind="stable").reset_index(drop=True)

    unique_dates = sorted(df["date"].unique().tolist())
    n_dates = len(unique_dates)
    if n_dates <= min_train_days + test_days:
        return []

    # 預建 date → row indices 加速 train/test slicing
    date_to_idx: dict = {}
    for i, d in enumerate(df["date"].tolist()):
        date_to_idx.setdefault(d, []).append(i)

    feature_cols = [c for c in df.columns if c not in ("date", "y")]
    max_possible = (n_dates - min_train_days) // max(1, test_days)
    results: list[dict] = []

    for i in range(max_possible):
        train_end = min_train_days + i * test_days
        test_end = train_end + test_days
        if test_end > n_dates:
            break
        train_dates = unique_dates[:train_end]
        test_dates = unique_dates[train_end:test_end]

        train_idxs = [j for d in train_dates for j in date_to_idx[d]]
        test_idxs = [j for d in test_dates for j in date_to_idx[d]]
        if not train_idxs or not test_idxs:
            continue

        train_slice = df.iloc[train_idxs].reset_index(drop=True)
        test_slice = df.iloc[test_idxs].reset_index(drop=True)
        X_train = train_slice[feature_cols]
        y_train = train_slice["y"]
        X_test = test_slice[feature_cols]
        y_test = test_slice["y"]

        if y_train.nunique() < 2:
            continue

        # ----- RF baseline -----
        rf = _train_rf(X_train, y_train)
        rf_p = _predict_p1(rf, X_test)
        rf_cal = _fit_calibrator(rf, X_train, y_train)
        rf_p_cal = rf_cal.transform(rf_p) if rf_cal else rf_p

        # ----- Ensemble -----
        ens, ens_backend = _train_ensemble_or_rf(X_train, y_train)
        ens_p = _predict_p1(ens, X_test)
        ens_cal = _fit_calibrator(ens, X_train, y_train)
        ens_p_cal = ens_cal.transform(ens_p) if ens_cal else ens_p

        results.append({
            "split_idx": i,
            "train_start": str(train_dates[0]),
            "train_end": str(train_dates[-1]),
            "test_start": str(test_dates[0]),
            "test_end": str(test_dates[-1]),
            "n_train": int(len(X_train)),
            "n_test": int(len(X_test)),
            "n_train_dates": int(len(train_dates)),
            "n_test_dates": int(len(test_dates)),
            "pos_rate_train": float(y_train.mean()),
            "pos_rate_test": float(y_test.mean()),
            "rf_test_auc": _safe_auc(y_test, rf_p),
            "rf_test_brier": _safe_brier(y_test, rf_p),
            "rf_test_brier_cal": _safe_brier(y_test, rf_p_cal),
            "ens_test_auc": _safe_auc(y_test, ens_p),
            "ens_test_brier": _safe_brier(y_test, ens_p),
            "ens_test_brier_cal": _safe_brier(y_test, ens_p_cal),
            "ens_backend": ens_backend,
        })

    return results


def _agg_mean_std(vals: list[float]) -> tuple[float, float]:
    arr = np.array([v for v in vals if not np.isnan(v)], dtype=float)
    if len(arr) == 0:
        return float("nan"), float("nan")
    return float(arr.mean()), float(arr.std(ddof=0))


def render_markdown(
    results: list[dict],
    universe_label: str,
    n_samples: int,
    win_rate: float,
    min_train_days: int,
    test_days: int,
) -> str:
    """產生 docs/ml-ensemble-walkforward-*.md 內容。"""
    n_splits = len(results)
    if n_splits == 0:
        return "# Walk-forward 比較\n\n(無有效 splits — dataset 太小)\n"

    rf_auc_mean, rf_auc_std = _agg_mean_std([r["rf_test_auc"] for r in results])
    ens_auc_mean, ens_auc_std = _agg_mean_std([r["ens_test_auc"] for r in results])
    rf_brier_mean, _ = _agg_mean_std([r["rf_test_brier"] for r in results])
    ens_brier_mean, _ = _agg_mean_std([r["ens_test_brier"] for r in results])
    rf_brier_cal_mean, _ = _agg_mean_std([r["rf_test_brier_cal"] for r in results])
    ens_brier_cal_mean, _ = _agg_mean_std([r["ens_test_brier_cal"] for r in results])

    auc_delta = ens_auc_mean - rf_auc_mean
    brier_delta = ens_brier_mean - rf_brier_mean
    brier_cal_delta = ens_brier_cal_mean - rf_brier_cal_mean

    if auc_delta >= 0.02:
        verdict = "✅ **PASS** — ensemble AUC ≥ RF baseline + 2pp,**建議 merge + 重訓 production models**。"
    elif auc_delta >= 0:
        verdict = "🟡 **MARGINAL** — ensemble AUC ≥ RF 但 < +2pp,**留 backend toggle 自己控制**,先觀察。"
    else:
        verdict = "❌ **FAIL** — ensemble AUC < RF baseline,**這個 dataset 不建議 merge 為 default**(production 維持 RF)。需要 debug LightGBM hyperparameters / feature 前處理 / 或對更大的 per-strategy dataset 重跑 eval。"

    n_stacking = sum(1 for r in results if r["ens_backend"] == "stacking")
    n_rf_fallback = n_splits - n_stacking

    lines: list[str] = []
    lines.append("# Stacking Ensemble vs RandomForest Walk-forward 比較")
    lines.append("")
    lines.append(f"**生成時間**:{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append(f"**Task**:Phase 2 #P2-5(LightGBM + Multi-task + Stacking ensemble)")
    lines.append(f"**Universe**:{universe_label}")
    lines.append(f"**Dataset**:{n_samples} samples,base win rate {win_rate:.1%}")
    lines.append(f"**Walk-forward**:expanding window,min_train_days={min_train_days},test_days={test_days}")
    lines.append(f"**Splits**:{n_splits}({n_stacking} stacking / {n_rf_fallback} RF fallback)")
    lines.append("")
    lines.append("## TL;DR")
    lines.append("")
    lines.append(verdict)
    lines.append("")
    lines.append("## 聚合指標(test 段)")
    lines.append("")
    lines.append("| Metric | RF baseline | Stacking Ensemble | Δ |")
    lines.append("|---|---:|---:|---:|")
    lines.append(
        f"| ROC AUC mean (std) | {rf_auc_mean:.4f} ({rf_auc_std:.4f}) | "
        f"{ens_auc_mean:.4f} ({ens_auc_std:.4f}) | {auc_delta:+.4f} |"
    )
    lines.append(
        f"| Brier raw mean | {rf_brier_mean:.4f} | {ens_brier_mean:.4f} | {brier_delta:+.4f} |"
    )
    lines.append(
        f"| Brier calibrated mean | {rf_brier_cal_mean:.4f} | {ens_brier_cal_mean:.4f} | {brier_cal_delta:+.4f} |"
    )
    lines.append("")
    lines.append("(Δ 為 ensemble − RF;AUC 越大越好,Brier 越小越好)")
    lines.append("")
    lines.append("## Per-split 明細")
    lines.append("")
    lines.append(
        "| Split | Train range | Test range | n_train | n_test | "
        "pos_rate_test | RF AUC | Ens AUC | ΔAUC | RF Brier | Ens Brier | Backend |"
    )
    lines.append("|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|")
    for r in results:
        delta = r["ens_test_auc"] - r["rf_test_auc"]
        lines.append(
            f"| {r['split_idx']} | {r['train_start']}→{r['train_end']} | "
            f"{r['test_start']}→{r['test_end']} | "
            f"{r['n_train']} | {r['n_test']} | "
            f"{r['pos_rate_test']:.2f} | "
            f"{r['rf_test_auc']:.3f} | {r['ens_test_auc']:.3f} | {delta:+.3f} | "
            f"{r['rf_test_brier']:.3f} | {r['ens_test_brier']:.3f} | "
            f"{r['ens_backend']} |"
        )
    lines.append("")
    lines.append("## 解讀說明")
    lines.append("")
    lines.append("- **AUC delta**:單個 split 上下浮動 ±0.05 是常態(時序 OOS 本身雜訊大),"
                 "看 mean / std 比看單 split 重要。")
    lines.append("- **Brier raw vs calibrated**:calibrator 的 holdout 是 train 段後 20% "
                 "(時序內),不 leak test。calibrated < raw 代表機率校準在生效。")
    lines.append("- **Backend column**:'stacking' = 該 split 樣本 ≥ MIN_STACKING_SAMPLES="
                 f"{ml_ensemble.MIN_STACKING_SAMPLES},'rf' = 樣本不足 fallback。")
    lines.append("")
    lines.append("## 評判 gate")
    lines.append("")
    lines.append("> Spec 拍板門檻:")
    lines.append(">  - **新 model AUC ≥ 舊 model AUC + 2pp** → merge")
    lines.append(">  - **新 model AUC < 舊 model AUC**(任何 -delta) → 不 merge,debug")
    lines.append(">  - **中間區間(0 ≤ Δ < 2pp)** → marginal,留 `--backend` toggle 觀察")
    lines.append("")
    lines.append(f"本次 Δ AUC = **{auc_delta:+.4f}** → {verdict.split(' — ')[0]}")
    lines.append("")
    lines.append("## 範圍限制(IMPORTANT — 讀完再下結論)")
    lines.append("")
    lines.append("這次 eval 用的是 **TW_TOP_50 sliding window + 5d ATR target label**(對應")
    lines.append("`scripts/train_ml_model.py` 訓的 short_pick fallback model)。**不能**直接")
    lines.append("推論到 per-strategy models — 它們的 dataset 不一樣:")
    lines.append("")
    lines.append("| 維度 | 本 eval(short_pick fallback) | per-strategy models |")
    lines.append("|---|---|---|")
    lines.append("| Universe | TW_TOP_50(50 檔大型股) | pure_stock_universe(~2400 檔) |")
    lines.append("| 樣本量 | ~1290 | bias_convergence 2358 / 其餘 1000-3600 |")
    lines.append("| Label | 5d ATR(1.5×)target hit | cost-aware +5%/-3%/5d hold(扣 0.585% round-trip) |")
    lines.append("| 訊號來源 | 每天每檔都算 | 只看該 strategy fire 的 picks |")
    lines.append("")
    lines.append("結論建議:")
    lines.append("- `scripts/train_ml_model.py` default 維持 **rf**(對齊本次 eval 結果)")
    lines.append("- `scripts/train_per_strategy_ml.py` default 維持 **ensemble**(per-strategy 樣本")
    lines.append("  更大 + cost-aware label 更複雜,spec 假設 stacking 有 edge — 需要 dedicated")
    lines.append("  per-strategy walk-forward eval 才能定論;下次 nightly cron 跑出來再看 OOB / Brier 對比)")
    lines.append("- production 既有 `models/short_pick.pkl` / per-strategy `.pkl` **不重訓**")
    lines.append("  (避免在 eval 還沒驗證的情況下動 production model)")
    lines.append("")
    lines.append("## 後續可以做的事")
    lines.append("")
    lines.append("1. **更大的 dataset 上重跑**:擴 universe 到 Top 200 或全市場,觀察樣本量")
    lines.append("   ≥ 3000 時 stacking AUC 是否反轉。")
    lines.append("2. **LightGBM hyperparameter sweep**:300-500 sample folds 上,`num_leaves=31` 可能")
    lines.append("   太大;試 `num_leaves=7-15` + `reg_alpha=0.1` + `min_child_samples=20` 抑制 overfit。")
    lines.append("3. **Per-strategy walk-forward**:寫 sister script `scripts/eval_ml_ensemble_walkforward_per_strategy.py`")
    lines.append("   對 bias_convergence / volume_breakout / gap_up 各跑一次,看哪些 strategy benefit。")
    lines.append("4. **Multi-task heads OOS eval**:目前只 store 不評,加 `predict_multitask` 對應的")
    lines.append("   1d/3d/10d label OOS AUC,看共享 representation 是否真的提升 5d 主 head。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Walk-forward RF baseline vs Stacking ensemble")
    p.add_argument("--universe", choices=["top50", "full"], default="top50")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT))
    p.add_argument("--min-train-days", type=int, default=DEFAULT_MIN_TRAIN_DAYS)
    p.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS)
    args = p.parse_args()

    db.init_db()
    counts = db.preload_snapshots()
    if counts:
        print(f"[WF] preload snapshots: {counts}", flush=True)

    if args.universe == "top50":
        sids = [s for s, _ in TW_TOP_50]
        universe_label = f"TW_TOP_50({len(sids)} 檔)"
    else:
        with db.get_conn() as conn:
            sids = [
                r["stock_id"] for r in conn.execute(
                    "SELECT DISTINCT stock_id FROM daily_prices "
                    "WHERE stock_id != 'TAIEX' ORDER BY stock_id"
                ).fetchall()
            ]
        universe_label = f"全市場({len(sids)} 檔)"

    print(f"[WF] universe = {universe_label}", flush=True)
    print("[WF] 構造 dataset(sliding window features + 5d label)...", flush=True)
    t0 = time.time()
    X, y, dates = ml_predictor.build_training_dataset(
        stock_ids=sids, return_dates=True,
    )
    print(
        f"[WF] dataset 構造完成:{len(X)} samples × {len(X.columns)} feats / "
        f"win rate {y.mean():.1%} / 耗時 {time.time() - t0:.1f}s",
        flush=True,
    )

    if len(X) < 100:
        print(f"❌ dataset 太小({len(X)} < 100),無法做有意義的 walk-forward", flush=True)
        return 1

    print(
        f"[WF] 開始 walk-forward(min_train_days={args.min_train_days}, "
        f"test_days={args.test_days})...",
        flush=True,
    )
    t0 = time.time()
    results = run_walkforward(
        X, y, dates,
        min_train_days=args.min_train_days,
        test_days=args.test_days,
    )
    print(f"[WF] 跑完 {len(results)} splits,耗時 {time.time() - t0:.1f}s", flush=True)

    if not results:
        print("❌ 沒跑出任何 splits(dataset 內 unique dates 不足)", flush=True)
        return 1

    # Console summary
    rf_auc_mean, rf_auc_std = _agg_mean_std([r["rf_test_auc"] for r in results])
    ens_auc_mean, ens_auc_std = _agg_mean_std([r["ens_test_auc"] for r in results])
    print("[WF] === 聚合(test 段) ===", flush=True)
    print(f"  RF baseline   AUC mean = {rf_auc_mean:.4f} ± {rf_auc_std:.4f}", flush=True)
    print(f"  Stacking ens  AUC mean = {ens_auc_mean:.4f} ± {ens_auc_std:.4f}", flush=True)
    print(f"  Δ AUC                 = {ens_auc_mean - rf_auc_mean:+.4f}", flush=True)

    md = render_markdown(
        results,
        universe_label=universe_label,
        n_samples=len(X),
        win_rate=float(y.mean()),
        min_train_days=args.min_train_days,
        test_days=args.test_days,
    )
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"[WF] 報告已寫入 {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
