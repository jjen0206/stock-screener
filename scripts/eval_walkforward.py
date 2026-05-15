"""跑既有 v3 模型 walk-forward 評估,寫表 ml_walkforward_results。

對 short_pick(通用)+ per_strategy 8 個已訓 model 做 expanding-window CV
(對齊 src/ml_walkforward.py),報每 split 的 train/test ROC AUC + PR AUC +
log loss,聚合 mean/std,跟 random 80/20 split 結果(M2 報告 short_pick
ROC AUC 0.7180)對比 — 看時序 OOS 是否仍 ≥ 0.65。

短解釋:random split 把同一 sid 後一天的 feature 跟前一天的 y 都算進去
shuffle,「未來資訊」leak 回 train,ROC AUC 通常虛高;walk-forward 嚴格
時序切,結果更貼真實 production 表現。

用法:
    python scripts/eval_walkforward.py
    python scripts/eval_walkforward.py --models short_pick
    python scripts/eval_walkforward.py --models short_pick,gap_up
    python scripts/eval_walkforward.py --n-splits 5 --test-size 20

Exit:
    0 = 至少一個 model 跑出來
    1 = 全部失敗 / 樣本不足
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from src import database as db, ml_predictor, ml_walkforward as wf  # noqa: E402
from src.universe import TW_TOP_50  # noqa: E402

# 從 train_per_strategy_ml import gather_training_set(reuse 既有 dataset 構造)
import importlib.util  # noqa: E402

_TPS_PATH = _ROOT / "scripts" / "train_per_strategy_ml.py"
_spec = importlib.util.spec_from_file_location("train_per_strategy_ml", _TPS_PATH)
_tps = importlib.util.module_from_spec(_spec)
sys.modules["train_per_strategy_ml"] = _tps
_spec.loader.exec_module(_tps)

# 預設 8 個 per_strategy(已 trained,跟 commit 0b90444 對齊)
DEFAULT_PER_STRATEGY = [
    "ma_alignment",
    "bias_convergence",
    "macd_golden",
    "bb_lower_rebound",
    "volume_breakout",
    "taiex_alpha",
    "big_holder_inflow",
    "gap_up",
]

# 預設改 None / 50 / 300:依 docs/ml-overfit-root-cause.md
# 原 5/20/100 對大樣本浪費 90%+ 資料 + test_size=20 統計雜訊極大
DEFAULT_N_SPLITS: int | None = None
DEFAULT_TEST_SIZE = 50
DEFAULT_MIN_TRAIN = 300
PER_STRATEGY_LOOKBACK = 200


def build_short_pick_dataset_with_dates(
    stock_ids: list[str] | None = None,
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    """跟 ml_predictor.build_training_dataset 同邏輯,但連 date 一起回傳。

    回 DataFrame:FEATURE_NAMES + ['y', 'stock_id', 'date'],按 date asc 排序。
    """
    if stock_ids is None:
        stock_ids = [s for s, _ in TW_TOP_50]

    rows: list[dict] = []
    for sid in stock_ids:
        with db.get_conn(db_path) as conn:
            dates = [
                r["date"] for r in conn.execute(
                    "SELECT date FROM daily_prices WHERE stock_id=? "
                    "ORDER BY date ASC",
                    (sid,),
                ).fetchall()
            ]
        if len(dates) < ml_predictor.MIN_HISTORY_DAYS + ml_predictor.LABEL_LOOKAHEAD_DAYS:
            continue
        for d in dates[
            ml_predictor.MIN_HISTORY_DAYS - 1:
            -ml_predictor.LABEL_LOOKAHEAD_DAYS
        ]:
            f = ml_predictor.extract_features(sid, d, db_path=db_path)
            if f is None:
                continue
            y = ml_predictor.compute_label(sid, d, db_path=db_path)
            if y is None:
                continue
            row = dict(f)
            row["y"] = int(y)
            row["stock_id"] = sid
            row["date"] = d
            rows.append(row)

    if not rows:
        return pd.DataFrame(
            columns=ml_predictor.FEATURE_NAMES + ["y", "stock_id", "date"]
        )
    df = pd.DataFrame(rows)
    return df.sort_values("date", kind="stable").reset_index(drop=True)


def _build_per_strategy_dataset_with_dates(
    strategy_name: str,
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    """reuse train_per_strategy_ml.gather_training_set,rename label→y,排序。"""
    df = _tps.gather_training_set(
        strategy_name,
        lookback_days=PER_STRATEGY_LOOKBACK,
        db_path=db_path,
    )
    if df.empty:
        return df
    df = df.rename(columns={"label": "y"})
    return df.sort_values("date", kind="stable").reset_index(drop=True)


def _write_results_to_db(
    model_name: str,
    results: list[dict],
    evaluated_at: str,
    db_path: str | Path | None = None,
) -> None:
    """把 walkforward_train_test 結果寫進 ml_walkforward_results。"""
    if not results:
        return
    with db.get_conn(db_path) as conn:
        for r in results:
            conn.execute(
                """
                INSERT OR REPLACE INTO ml_walkforward_results (
                    model_name, split_idx, train_start, train_end,
                    test_start, test_end, train_n, test_n,
                    roc_auc, pr_auc, log_loss, train_roc_auc,
                    evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_name,
                    r["split_idx"],
                    r["train_start"],
                    r["train_end"],
                    r["test_start"],
                    r["test_end"],
                    r["train"]["n"],
                    r["test"]["n"],
                    r["test"]["roc_auc"],
                    r["test"]["pr_auc"],
                    r["test"]["log_loss"],
                    r["train"]["roc_auc"],
                    evaluated_at,
                ),
            )
        conn.commit()


def evaluate_model(
    model_name: str,
    df: pd.DataFrame,
    n_splits: int,
    test_size: int,
    min_train_size: int,
) -> tuple[list[dict], dict]:
    """跑 walk-forward,回 (per-split results, summary)。"""
    if df.empty:
        return [], wf.walkforward_summary([])
    feature_cols = [
        c for c in df.columns
        if c not in ("y", "stock_id", "date")
    ]
    results = wf.walkforward_train_test(
        df,
        n_splits=n_splits,
        test_size=test_size,
        min_train_size=min_train_size,
        feature_cols=feature_cols,
    )
    return results, wf.walkforward_summary(results)


def _print_summary_table(rows: list[dict]) -> None:
    """印對照表 — model / n_splits / mean ROC / std / min / max。"""
    if not rows:
        print("[WF-EVAL] (no results)", flush=True)
        return
    print("", flush=True)
    print(
        f"{'Model':<24s} {'n':>4s} {'WF ROC mean':>12s} {'std':>7s} "
        f"{'min':>7s} {'max':>7s} {'Train ROC':>10s} {'Random ROC':>12s}",
        flush=True,
    )
    print("-" * 96, flush=True)
    for r in rows:
        s = r["summary"]
        rand = r.get("random_split_roc")
        rand_str = f"{rand:>11.4f}" if rand is not None else "         n/a"
        if s["n_splits"] == 0:
            print(
                f"{r['model']:<24s} {s['n_splits']:>4d} "
                f"{'n/a':>12s} {'n/a':>7s} {'n/a':>7s} {'n/a':>7s} "
                f"{'n/a':>10s} {rand_str:>12s}",
                flush=True,
            )
            continue
        print(
            f"{r['model']:<24s} {s['n_splits']:>4d} "
            f"{s['test_roc_auc']['mean']:>12.4f} "
            f"{s['test_roc_auc']['std']:>7.4f} "
            f"{s['test_roc_auc']['min']:>7.4f} "
            f"{s['test_roc_auc']['max']:>7.4f} "
            f"{s['train_roc_auc_mean']:>10.4f} "
            f"{rand_str:>12s}",
            flush=True,
        )
    print("", flush=True)


def _load_random_split_roc(model_name: str) -> float | None:
    """從 .meta.json 撈舊的 random split ROC AUC(short_pick) / OOB(per_strategy)
    給對照。
    """
    import json
    if model_name == "short_pick":
        # short_pick 沒存 ROC AUC 在 meta(只 accuracy);改撈 new_v3.json
        new_v3 = _ROOT / "models" / "new_v3.json"
        if new_v3.exists():
            try:
                d = json.loads(new_v3.read_text(encoding="utf-8"))
                return float(d.get("roc_auc"))
            except Exception:
                return None
        return None
    # per_strategy:meta 有 oob_score(不完全等同 ROC,但同數量級給對照)
    p = _ROOT / "models" / "per_strategy" / f"{model_name}.meta.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return float(d.get("oob_score")) if d.get("oob_score") else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="ML 模型 walk-forward 評估")
    ap.add_argument(
        "--models", default="all",
        help="comma-separated model names,或 'all' / 'short_pick' / 'per_strategy'",
    )
    ap.add_argument(
        "--n-splits", type=int, default=DEFAULT_N_SPLITS,
        help="None / 未指定 → 跑全部可分 splits(避免大樣本只測前段)",
    )
    ap.add_argument("--test-size", type=int, default=DEFAULT_TEST_SIZE)
    ap.add_argument("--min-train-size", type=int, default=DEFAULT_MIN_TRAIN)
    args = ap.parse_args()

    db.init_db()
    counts = db.preload_snapshots()
    if counts:
        print(f"[WF-EVAL] preload snapshots: {counts}", flush=True)

    # decode model list
    if args.models == "all":
        model_names = ["short_pick"] + DEFAULT_PER_STRATEGY
    elif args.models == "short_pick":
        model_names = ["short_pick"]
    elif args.models == "per_strategy":
        model_names = list(DEFAULT_PER_STRATEGY)
    else:
        model_names = [m.strip() for m in args.models.split(",") if m.strip()]

    evaluated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary_rows: list[dict] = []
    success = 0

    for name in model_names:
        t0 = time.time()
        print(f"[WF-EVAL] === {name} === build dataset...", flush=True)
        try:
            if name == "short_pick":
                df = build_short_pick_dataset_with_dates()
            else:
                df = _build_per_strategy_dataset_with_dates(name)
        except Exception as e:  # noqa: BLE001
            print(f"[WF-EVAL] {name} build dataset 失敗:{e}", flush=True)
            continue

        n = len(df)
        print(
            f"[WF-EVAL] {name} dataset: n={n} "
            f"({time.time() - t0:.1f}s)",
            flush=True,
        )
        if n == 0:
            summary_rows.append({
                "model": name,
                "summary": wf.walkforward_summary([]),
                "random_split_roc": _load_random_split_roc(name),
            })
            continue

        try:
            results, summary = evaluate_model(
                name, df,
                n_splits=args.n_splits,
                test_size=args.test_size,
                min_train_size=args.min_train_size,
            )
        except ValueError as e:
            print(f"[WF-EVAL] {name} walk-forward 失敗:{e}", flush=True)
            summary_rows.append({
                "model": name,
                "summary": wf.walkforward_summary([]),
                "random_split_roc": _load_random_split_roc(name),
            })
            continue

        if results:
            success += 1
            _write_results_to_db(name, results, evaluated_at)

        summary_rows.append({
            "model": name,
            "summary": summary,
            "random_split_roc": _load_random_split_roc(name),
        })
        print(
            f"[WF-EVAL] {name} done: {summary['n_splits']} splits "
            f"test_roc_mean={summary['test_roc_auc']['mean']:.4f} "
            f"({time.time() - t0:.1f}s)",
            flush=True,
        )

    _print_summary_table(summary_rows)
    return 0 if success > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
