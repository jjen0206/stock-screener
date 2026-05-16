"""Stage 2B-1:per-strategy ML 模型訓練(每 strategy 一個 .pkl)。

設計:對每個 strategy,跑過去 LOOKBACK_DAYS 個交易日,把該 strategy 在每天
fire 的 picks 抽 features + simulate_outcome 算 label(+5%/-3%/5 day hold,
跟 backtest 一致),累積成 per-strategy 訓練集 → train RandomForest 存
`models/per_strategy/<strategy>.pkl` + `<strategy>.meta.json`。

跟 `scripts/train_ml_model.py`(通用 short_pick.pkl)的差異:
- 通用版用 sliding window 對所有 sids 抽 features × ATR-based label
- per-strategy 版只看「該 strategy 真的 fire 那些 picks」+ %-based label
  (跟 strategy_backtest 表的 win_rate 同口徑)

樣本量低於 MIN_TRAIN_SAMPLES(預設 100)的 strategy → 標記 fallback,不存
pkl(只存 meta.json with status="fallback");inference 時 fallback 到通用
模型。

CLI:
    # 全 11 strategies × 200 day lookback
    python scripts/train_per_strategy_ml.py

    # 單一 strategy
    python scripts/train_per_strategy_ml.py --strategy ma_alignment

    # 自訂 lookback
    python scripts/train_per_strategy_ml.py --lookback 252

Exit code:
    0 = 至少一個 strategy 訓練成功
    1 = 全部 strategies 樣本不足(SQLite 歷史太短或 universe 太小)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from src import database as db, ml_predictor  # noqa: E402
from src._bulk_load import bulk_load_prices  # noqa: E402
from src.backtest import _list_trading_dates, simulate_outcome  # noqa: E402
from src.strategies import ALL_STRATEGIES  # noqa: E402
from src.universe import pure_stock_universe  # noqa: E402


# === 常數(跟 backtest 同口徑) ===
MIN_TRAIN_SAMPLES = 100
LOOKBACK_DAYS = 200
TARGET_PCT = 0.05
STOP_PCT = 0.03
HOLD_DAYS = 5
MODEL_DIR = _ROOT / "models" / "per_strategy"

# RandomForest 預設 hyperparams(其餘 strategy 用)
DEFAULT_RF_PARAMS = {
    "n_estimators": 200,
    "max_depth": 10,
    "min_samples_leaf": 5,
}

# Per-strategy 覆寫(只對 overfit 風險高的 strategy 加強 regularization)
# 2026-05-15:gap_up override 移除。Diagnose 顯示 ML 從現有 features 學不到
# gap_up follow-through(WR edge 在 vol_ratio sweet spot,不在 features 內),
# 已改走路 B(rule-based 過濾,從 STRATEGY_ML_THRESHOLDS 拿掉 gap_up)。
# 詳見 docs/gap-up-decision-2026-05-15.md。
STRATEGY_RF_PARAMS: dict[str, dict] = {}


def _rf_params_for(strategy_name: str) -> dict:
    """回某 strategy 的 RandomForest hyperparams(missing → DEFAULT)。"""
    return STRATEGY_RF_PARAMS.get(strategy_name, DEFAULT_RF_PARAMS)


def gather_training_set(
    strategy_name: str,
    lookback_days: int = LOOKBACK_DAYS,
    period_end: str | None = None,
    universe: list[str] | None = None,
    target_pct: float = TARGET_PCT,
    stop_pct: float = STOP_PCT,
    hold_days: int = HOLD_DAYS,
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    """掃 strategy 在 lookback 內每天的 picks,抽 features + 算 label。

    回 DataFrame: 11 個 feature columns + ['label', 'stock_id', 'date']。
    label 1 = win(target_pct 觸到 before stop_pct)、0 = lose。

    period_end=None → 用 daily_prices MAX(date)。
    universe=None → 用 pure_stock_universe(min_history=20)。
    """
    if strategy_name not in ALL_STRATEGIES:
        raise ValueError(
            f"未知 strategy: {strategy_name}. 可選: {list(ALL_STRATEGIES.keys())}"
        )
    screen_fn = ALL_STRATEGIES[strategy_name]

    if period_end is None:
        latest = db.get_latest_trading_date()
        if not latest:
            return pd.DataFrame(columns=ml_predictor.FEATURE_NAMES + [
                "label", "stock_id", "date",
            ])
        period_end = latest

    if universe is None:
        universe = pure_stock_universe(min_history=20)
    if not universe:
        return pd.DataFrame(columns=ml_predictor.FEATURE_NAMES + [
            "label", "stock_id", "date",
        ])

    all_dates = _list_trading_dates(period_end, lookback_days)
    if len(all_dates) < hold_days + 1:
        return pd.DataFrame(columns=ml_predictor.FEATURE_NAMES + [
            "label", "stock_id", "date",
        ])
    pickable_dates = all_dates[: -hold_days]

    # bulk load OHLC 一次,避免 N×M 次 SQL
    end_for_bulk = all_dates[-1]
    bulk_lookback = lookback_days + hold_days + 5
    with db.get_conn(db_path) as conn:
        prices_by_sid = bulk_load_prices(
            conn, universe, end_for_bulk, lookback_days=bulk_lookback,
        )

    rows: list[dict] = []
    for D in pickable_dates:
        try:
            df = screen_fn(D, params=None, stock_ids=universe)
        except Exception:  # noqa: BLE001
            continue
        if df is None or df.empty:
            continue

        for _, pick_row in df.iterrows():
            sid = str(pick_row["stock_id"])
            entry_price = float(pick_row.get("close", 0) or 0)
            if entry_price <= 0:
                continue

            sid_df = prices_by_sid.get(sid)
            if sid_df is None or sid_df.empty:
                continue
            future = sid_df[sid_df["date"] > D].head(hold_days)
            if len(future) < hold_days:
                continue

            outcome, _ = simulate_outcome(
                future, entry_price,
                target_pct=target_pct, stop_pct=stop_pct,
            )
            label = 1 if outcome == "win" else 0

            feats = ml_predictor.extract_features(sid, D, db_path=db_path)
            if feats is None:
                continue

            row = dict(feats)
            row["label"] = label
            row["stock_id"] = sid
            row["date"] = D
            rows.append(row)

    if not rows:
        return pd.DataFrame(columns=ml_predictor.FEATURE_NAMES + [
            "label", "stock_id", "date",
        ])
    return pd.DataFrame(rows)


def train_one(
    strategy_name: str,
    train_df: pd.DataFrame,
    output_dir: Path = MODEL_DIR,
    min_samples: int = MIN_TRAIN_SAMPLES,
) -> dict:
    """訓 RandomForest + dump pkl/meta。回 status dict。

    status dict 鍵:
      strategy / samples / wins / win_rate / status / oob_score / pkl_path
    status 取值:'trained'(成功) / 'fallback'(樣本不足,只 dump meta)。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pkl_path = output_dir / f"{strategy_name}.pkl"
    meta_path = output_dir / f"{strategy_name}.meta.json"

    n_samples = len(train_df)
    n_wins = int(train_df["label"].sum()) if n_samples else 0
    win_rate = (n_wins / n_samples) if n_samples else 0.0

    if n_samples < min_samples or len(set(train_df.get("label", []))) < 2:
        meta = {
            "strategy": strategy_name,
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "samples": n_samples,
            "wins": n_wins,
            "win_rate": win_rate,
            "status": "fallback",
            "reason": (
                f"samples {n_samples} < {min_samples}"
                if n_samples < min_samples
                else "label only has one class"
            ),
            "min_train_samples": min_samples,
            "lookback_days": LOOKBACK_DAYS,
            "target_pct": TARGET_PCT,
            "stop_pct": STOP_PCT,
            "hold_days": HOLD_DAYS,
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return {
            "strategy": strategy_name,
            "samples": n_samples,
            "wins": n_wins,
            "win_rate": win_rate,
            "status": "fallback",
            "oob_score": None,
            "pkl_path": None,
            "calibration": None,
        }

    # 真的訓:RandomForest with OOB
    from sklearn.ensemble import RandomForestClassifier
    from src import ml_calibration

    # gather_training_set 內 rows 按 (date asc, sid) 序 append,所以 row order
    # 就是時序;最後 20% holdout 走 train_with_calibration 邏輯(time-based)
    train_df = train_df.sort_values(["date"], kind="stable").reset_index(drop=True)
    X = train_df[ml_predictor.FEATURE_NAMES].copy()
    y = train_df["label"].astype(int)

    rf_params = _rf_params_for(strategy_name)
    base_rf_kwargs = {
        "n_estimators": rf_params["n_estimators"],
        "max_depth": rf_params["max_depth"],
        "min_samples_leaf": rf_params["min_samples_leaf"],
        "class_weight": "balanced",
        "oob_score": True,
        "bootstrap": True,
        "random_state": 42,
        "n_jobs": -1,
    }
    model = RandomForestClassifier(**base_rf_kwargs)
    model.fit(X, y)
    oob_score = float(getattr(model, "oob_score_", 0.0))

    importances = sorted(
        zip(ml_predictor.FEATURE_NAMES, model.feature_importances_),
        key=lambda kv: -kv[1],
    )

    ml_predictor.save_model(model, pkl_path)

    # 訓練 calibrator(time-based 後 20% holdout 重訓一個 base + 算 raw/cal brier)
    # 注意:save 的 base model 仍是「全資料訓的 model」(production 用)— calibrator
    # 是用 holdout 評估 + fit,跟 production model 共用同份 X 拍 raw probs 算 brier。
    calibration_info: dict | None = None
    try:
        # 用全資料 model 對全資料的 holdout 段算 raw probs,fit calibrator
        n = len(X)
        holdout_n = max(1, int(round(n * 0.2)))
        train_n = n - holdout_n
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
            cal_path = ml_calibration.save_calibrator(calibrator, strategy_name)
            calibrated_holdout = calibrator.transform(raw_holdout)
            cal_metrics = ml_calibration.compute_calibration_metrics(
                y_holdout.to_numpy(), calibrated_holdout,
            )
            calibration_info = {
                "method": calibrator.method,
                "n_holdout": int(holdout_n),
                "raw_brier": float(raw_metrics["brier_score"]),
                "calibrated_brier": float(cal_metrics["brier_score"]),
                "pkl_path": str(cal_path),
            }
            print(
                f"[TRAIN-PS] {strategy_name}: calibrator({calibrator.method}) "
                f"Brier {raw_metrics['brier_score']:.4f} → "
                f"{cal_metrics['brier_score']:.4f}",
                flush=True,
            )
        else:
            print(
                f"[TRAIN-PS] {strategy_name}: holdout 全同類,跳過 calibrator",
                flush=True,
            )
    except Exception as e:  # noqa: BLE001
        print(
            f"[TRAIN-PS] {strategy_name}: calibrator 訓練失敗(non-fatal):"
            f"{type(e).__name__}: {e}",
            flush=True,
        )

    meta = {
        "strategy": strategy_name,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "samples": n_samples,
        "wins": n_wins,
        "win_rate": win_rate,
        "status": "trained",
        "oob_score": oob_score,
        "feature_importances": [
            {"name": n, "importance": float(imp)} for n, imp in importances
        ],
        "feature_names": ml_predictor.FEATURE_NAMES,
        "min_train_samples": min_samples,
        "lookback_days": LOOKBACK_DAYS,
        "target_pct": TARGET_PCT,
        "stop_pct": STOP_PCT,
        "hold_days": HOLD_DAYS,
        "model_type": "RandomForestClassifier",
        "n_estimators": rf_params["n_estimators"],
        "max_depth": rf_params["max_depth"],
        "min_samples_leaf": rf_params["min_samples_leaf"],
    }
    if calibration_info:
        meta["calibration"] = calibration_info
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    return {
        "strategy": strategy_name,
        "samples": n_samples,
        "wins": n_wins,
        "win_rate": win_rate,
        "status": "trained",
        "oob_score": oob_score,
        "pkl_path": str(pkl_path),
        "calibration": calibration_info,
    }


def _print_summary(results: list[dict]) -> None:
    """印 markdown-ish summary table 給 nightly workflow stdout 看。"""
    if not results:
        print("[TRAIN-PS] (no results)", flush=True)
        return
    print("", flush=True)
    print(
        f"{'Strategy':<24s} {'Samples':>8s} {'Wins':>6s} "
        f"{'WinRate':>8s} {'OOB':>7s} {'Brier raw→cal':>15s} {'Status':>10s}",
        flush=True,
    )
    print("-" * 90, flush=True)
    for r in results:
        oob = f"{r['oob_score'] * 100:>6.1f}%" if r["oob_score"] is not None else "    n/a"
        cal = r.get("calibration")
        if cal:
            brier_str = (
                f"{cal['raw_brier']:.3f}→{cal['calibrated_brier']:.3f}"
            )
        else:
            brier_str = "n/a"
        print(
            f"{r['strategy']:<24s} {r['samples']:>8d} {r['wins']:>6d} "
            f"{r['win_rate'] * 100:>7.1f}% {oob} {brier_str:>15s} {r['status']:>10s}",
            flush=True,
        )
    print("", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Stage 2B-1: per-strategy ML 訓練")
    p.add_argument(
        "--lookback", type=int, default=LOOKBACK_DAYS,
        help=f"訓練 lookback 交易日(default {LOOKBACK_DAYS})",
    )
    p.add_argument(
        "--strategy",
        help=f"只訓練單一 strategy(default 全跑;可選: {', '.join(ALL_STRATEGIES.keys())})",
    )
    p.add_argument(
        "--min-samples", type=int, default=MIN_TRAIN_SAMPLES,
        help=f"最少訓練樣本(default {MIN_TRAIN_SAMPLES},不足 → fallback)",
    )
    p.add_argument(
        "--as-of",
        help="period_end YYYY-MM-DD;留空 = SQLite daily_prices MAX(date)",
    )
    args = p.parse_args()

    db.init_db()
    counts = db.preload_snapshots()
    if counts:
        print(f"[TRAIN-PS] preload snapshots: {counts}", flush=True)

    if args.as_of:
        period_end = args.as_of
    else:
        latest = db.get_latest_trading_date()
        if not latest:
            print("[TRAIN-PS] daily_prices 表空,無法訓練", flush=True)
            return 1
        period_end = latest
        print(f"[TRAIN-PS] period_end={period_end}", flush=True)

    universe = pure_stock_universe(min_history=20)
    if not universe:
        print("[TRAIN-PS] pure_stock universe 空", flush=True)
        return 1
    print(f"[TRAIN-PS] universe = {len(universe)} 檔", flush=True)

    if args.strategy:
        if args.strategy not in ALL_STRATEGIES:
            print(
                f"[TRAIN-PS] 未知 strategy: {args.strategy}\n"
                f"可選: {', '.join(ALL_STRATEGIES.keys())}",
                flush=True,
            )
            return 1
        strategies = [args.strategy]
    else:
        strategies = list(ALL_STRATEGIES.keys())

    print(
        f"[TRAIN-PS] 訓練 {len(strategies)} strategies × {args.lookback} 日 lookback "
        f"(target +{TARGET_PCT * 100:.0f}% / stop -{STOP_PCT * 100:.0f}% / "
        f"hold {HOLD_DAYS} 天)...",
        flush=True,
    )

    results: list[dict] = []
    for name in strategies:
        t0 = time.time()
        train_df = gather_training_set(
            name,
            lookback_days=args.lookback,
            period_end=period_end,
            universe=universe,
        )
        gather_secs = time.time() - t0
        print(
            f"[TRAIN-PS] {name}: gathered {len(train_df)} samples "
            f"({gather_secs:.1f}s)",
            flush=True,
        )

        t0 = time.time()
        result = train_one(name, train_df, min_samples=args.min_samples)
        train_secs = time.time() - t0
        if result["status"] == "trained":
            print(
                f"[TRAIN-PS] {name}: trained → OOB={result['oob_score']:.3f} "
                f"({train_secs:.1f}s)",
                flush=True,
            )
        else:
            print(
                f"[TRAIN-PS] {name}: fallback "
                f"(samples {result['samples']} < {args.min_samples})",
                flush=True,
            )
        results.append(result)

    _print_summary(results)

    n_trained = sum(1 for r in results if r["status"] == "trained")
    print(
        f"[TRAIN-PS] 完成 — {n_trained}/{len(results)} 訓練成功,"
        f"{len(results) - n_trained} fallback",
        flush=True,
    )
    return 0 if n_trained > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
