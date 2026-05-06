"""ad-hoc:對每個策略獨立 grid search,找最佳 ML 過濾門檻。

**Originally one-shot — 2026-05-04 Stage 2A 校準觸發。Stage 2B 加
`--use-per-strategy-models` flag 後變 reusable**(每次 retrain 後可重跑)。

跑法:
    # Stage 2A:用通用 short_pick.pkl 校準
    python scripts/audit/calibrate_ml_thresholds.py

    # Stage 2B:用新 per-strategy models 校準
    python scripts/audit/calibrate_ml_thresholds.py --use-per-strategy-models

    # 自訂 lookback
    python scripts/audit/calibrate_ml_thresholds.py --lookback 60

對 11 個策略 × 8 個門檻(None / 0.50-0.80 step 0.05)做 grid search,印
winner threshold + 建議的 STRATEGY_ML_THRESHOLDS dict 內容。

Winner 條件:
- win_rate >= 0.55 (高過拋硬幣 + 緩衝)
- n_fires >= 30 (避免樣本太小不穩)

優化:每策略 one-pass(跑一次 screener + predict + simulate),所有門檻在
記憶體 sweep。比 naive 8x backtest 快約 8 倍。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import argparse  # noqa: E402

from src import config, database as db  # noqa: E402
from src._bulk_load import bulk_load_prices  # noqa: E402
from src.backtest import _list_trading_dates, simulate_outcome  # noqa: E402
from src.ml_predictor import (  # noqa: E402
    load_model, load_strategy_model, predict_batch, predict_for_strategy,
)
from src.strategies import ALL_STRATEGIES  # noqa: E402
from src.universe import pure_stock_universe  # noqa: E402


THRESHOLDS = [None, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
WINNER_MIN_WR = 0.55
WINNER_MIN_FIRES = 30

# 輕量版 — 樣本 ≥ 50 的 7 個策略(volume_kd / ma_squeeze / inst_consensus /
# inst_silent_accum 4 個 sample 太小不跑,過濾結果不穩)
LIGHTWEIGHT_STRATEGIES = [
    "ma_alignment",
    "bias_convergence",
    "macd_golden",
    "bb_lower_rebound",
    "rsi_recovery",
    "volume_breakout",
    "gap_up",
]


def _collect_strategy_picks(
    strategy_name: str,
    universe: list[str],
    period_end: str,
    lookback_days: int,
    target_pct: float,
    stop_pct: float,
    hold_days: int,
    ml_model,
    strategy_model=None,
) -> list[dict]:
    """跑一次 strategy backtest,collect 每張 pick 的 (sid, D, ml_prob, outcome, ret)。

    後續 sweep threshold 都在記憶體 filter,不重跑。回 list[dict]。

    strategy_model:給 Stage 2B 用 — 預載的 per-strategy model。傳就走
    predict_for_strategy 路由(strategy_model → fallback ml_model);沒就
    退舊路徑 predict_batch(ml_model, ...)。
    """
    screen_fn = ALL_STRATEGIES[strategy_name]
    all_dates = _list_trading_dates(period_end, lookback_days)
    if len(all_dates) < hold_days + 1:
        return []
    pickable_dates = all_dates[: -hold_days]

    end_for_bulk = all_dates[-1]
    bulk_lookback = lookback_days + hold_days + 5
    with db.get_conn() as conn:
        prices_by_sid = bulk_load_prices(
            conn, universe, end_for_bulk, lookback_days=bulk_lookback,
        )

    records: list[dict] = []
    for D in pickable_dates:
        try:
            df = screen_fn(D, params=None, stock_ids=universe)
        except Exception:  # noqa: BLE001
            continue
        if df is None or df.empty:
            continue

        # 一次 batch ML predict 整個 D 的 picks
        sids_today = [str(r["stock_id"]) for _, r in df.iterrows()]
        if strategy_model is not None or ml_model is not None:
            if strategy_model is not None:
                # Stage 2B 路徑:per-strategy model 優先,沒 → fallback ml_model
                ml_probs = predict_for_strategy(
                    strategy_name=strategy_name,
                    stock_ids=sids_today,
                    target_date=D,
                    fallback_model=ml_model,
                    strategy_model=strategy_model,
                )
            else:
                # Stage 2A:通用 model
                ml_probs = predict_batch(ml_model, sids_today, D)
        else:
            ml_probs = {}

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
            outcome, ret = simulate_outcome(
                future, entry_price,
                target_pct=target_pct, stop_pct=stop_pct,
            )
            records.append({
                "D": D,
                "sid": sid,
                "ml_prob": ml_probs.get(sid),
                "outcome": outcome,
                "ret": ret,
            })
    return records


def _sweep_threshold(records: list[dict], threshold: float | None) -> dict:
    """從 records 套門檻,算 fires / wins / win_rate / avg_return。

    threshold=None → 全保留(不過濾,baseline)。
    """
    if threshold is None:
        kept = records
    else:
        kept = [
            r for r in records
            if r["ml_prob"] is not None and r["ml_prob"] >= threshold
        ]
    n_fires = len(kept)
    if n_fires == 0:
        return {"fires": 0, "wins": 0, "win_rate": 0.0, "avg_return": 0.0}
    n_wins = sum(1 for r in kept if r["outcome"] == "win")
    total_ret = sum(r["ret"] for r in kept)
    return {
        "fires": n_fires,
        "wins": n_wins,
        "win_rate": n_wins / n_fires,
        "avg_return": total_ret / n_fires,
    }


def _print_strategy_table(
    strategy_name: str,
    sweeps: dict[str | None, dict],
) -> None:
    print(f"\n=== {strategy_name} ===", flush=True)
    print(
        f"{'Threshold':<10} {'Fires':>7} {'Wins':>6} {'WinRate':>9} {'AvgRet':>9}",
        flush=True,
    )
    print("-" * 50, flush=True)
    for t, stats in sweeps.items():
        t_str = "none" if t is None else f"{t:.2f}"
        print(
            f"{t_str:<10} {stats['fires']:>7d} {stats['wins']:>6d} "
            f"{stats['win_rate'] * 100:>8.1f}% "
            f"{stats['avg_return'] * 100:>+8.2f}%",
            flush=True,
        )


def _pick_winner(sweeps: dict[str | None, dict]) -> tuple[str | None, dict]:
    """從 sweeps 選最佳 threshold:WR>=55% AND fires>=30,win_rate 最高者。
    若都不過關 → 回 (None, baseline)。
    """
    candidates = [
        (t, s) for t, s in sweeps.items()
        if s["fires"] >= WINNER_MIN_FIRES and s["win_rate"] >= WINNER_MIN_WR
    ]
    if not candidates:
        return None, sweeps[None]
    # 取 win_rate 最高;tie 取 fires 多的(樣本越多越穩)
    candidates.sort(key=lambda x: (-x[1]["win_rate"], -x[1]["fires"]))
    return candidates[0][0], candidates[0][1]


def _run_calibration(
    universe: list[str],
    period_end: str,
    lookback_days: int,
    ml_model,
    strategies: list[str] | None = None,
    use_per_strategy_models: bool = False,
) -> dict[str, dict]:
    """對(全 11 或指定子集)strategies 跑 grid search,
    回 {strategy: {sweeps, winner_t, winner_stats}}。

    use_per_strategy_models=True → 每 strategy 預載自己的 .pkl;沒 .pkl 走
    通用 model fallback。
    """
    out: dict[str, dict] = {}
    keys = strategies if strategies else list(ALL_STRATEGIES.keys())

    # Stage 2B:預載 per-strategy models 一次
    per_strategy_models: dict[str, object] = {}
    if use_per_strategy_models:
        for sname in keys:
            sm = load_strategy_model(sname)
            per_strategy_models[sname] = sm
            tag = "trained" if sm is not None else "fallback→general"
            print(f"  [PRELOAD] {sname}: {tag}", flush=True)

    for sname in keys:
        t0 = time.perf_counter()
        sm = per_strategy_models.get(sname) if use_per_strategy_models else None
        records = _collect_strategy_picks(
            sname, universe, period_end, lookback_days,
            target_pct=0.05, stop_pct=0.03, hold_days=5,
            ml_model=ml_model, strategy_model=sm,
        )
        elapsed = time.perf_counter() - t0
        sweeps = {t: _sweep_threshold(records, t) for t in THRESHOLDS}
        winner_t, winner_stats = _pick_winner(sweeps)
        _print_strategy_table(sname, sweeps)
        winner_str = "none" if winner_t is None else f"{winner_t:.2f}"
        print(
            f"  → winner: {winner_str} "
            f"(WR={winner_stats['win_rate'] * 100:.1f}%, "
            f"fires={winner_stats['fires']}) | "
            f"elapsed={elapsed:.1f}s",
            flush=True,
        )
        out[sname] = {
            "sweeps": sweeps,
            "winner_t": winner_t,
            "winner_stats": winner_stats,
            "elapsed": elapsed,
        }
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="ML threshold calibration grid search")
    p.add_argument(
        "--use-per-strategy-models", action="store_true",
        help="Stage 2B:每 strategy 用其 .pkl 替通用 model(找不到 → fallback 通用)。",
    )
    p.add_argument(
        "--lookback", type=int, default=30,
        help="lookback 交易日(default 30,輕量版)",
    )
    p.add_argument(
        "--strategies",
        help="逗號分隔 strategy keys(default 7 個 sample ≥ 50 的)",
    )
    args = p.parse_args()

    db.init_db()
    preload = db.preload_snapshots()
    if preload:
        print(f"[CALIBRATE] preload: {preload}", flush=True)

    period_end = db.get_latest_trading_date()
    if not period_end:
        print("[CALIBRATE] daily_prices 表空,無法跑", flush=True)
        return 1

    universe = pure_stock_universe(min_history=20)
    if not universe:
        print("[CALIBRATE] universe 空", flush=True)
        return 1

    model_path = config.PROJECT_ROOT / "models" / "short_pick.pkl"
    if not model_path.exists():
        print(f"[CALIBRATE] model 不存在:{model_path}", flush=True)
        return 1
    ml_model = load_model(model_path)
    if ml_model is None:
        print("[CALIBRATE] model load fail", flush=True)
        return 1

    if args.strategies:
        strategies_to_run = [s.strip() for s in args.strategies.split(",")]
    else:
        strategies_to_run = LIGHTWEIGHT_STRATEGIES

    mode_tag = "Stage 2B (per-strategy models)" if args.use_per_strategy_models \
        else "Stage 2A (general model)"
    print(
        f"[CALIBRATE] {mode_tag} | period_end={period_end}, "
        f"universe={len(universe)} 檔, target +5% / stop -3% / hold 5 天 / "
        f"lookback {args.lookback} 天 / {len(strategies_to_run)} strategies",
        flush=True,
    )

    print("\n" + "=" * 70, flush=True)
    print(f"{args.lookback}-day lookback grid search ({mode_tag})", flush=True)
    print("=" * 70, flush=True)
    results_30 = _run_calibration(
        universe, period_end, args.lookback, ml_model,
        strategies=strategies_to_run,
        use_per_strategy_models=args.use_per_strategy_models,
    )

    # Recommend STRATEGY_ML_THRESHOLDS(只用 30-day winner;穩定性 check
    # 留給 user 自己跑 60-day 確認)
    print("\n" + "=" * 70, flush=True)
    print("Per-strategy winner summary", flush=True)
    print("=" * 70, flush=True)
    print(
        f"\n{'Strategy':<22} {'winner_t':<10} {'WR':<8} {'fires':<8}",
        flush=True,
    )
    print("-" * 60, flush=True)

    final_thresholds: dict[str, float | None] = {}
    for sname in strategies_to_run:
        r = results_30.get(sname, {})
        t = r.get("winner_t")
        wr = r.get("winner_stats", {}).get("win_rate", 0.0)
        fires = r.get("winner_stats", {}).get("fires", 0)
        final_thresholds[sname] = t
        t_str = "none" if t is None else f"{t:.2f}"
        print(
            f"{sname:<22} {t_str:<10} {wr * 100:>5.1f}% {fires:>6d}",
            flush=True,
        )

    print("\nRecommended STRATEGY_ML_THRESHOLDS:", flush=True)
    print("STRATEGY_ML_THRESHOLDS = {", flush=True)
    for sname, t in final_thresholds.items():
        if t is not None:
            print(f'    "{sname}": {t:.2f},', flush=True)
    # 沒過 winner 條件的策略不在 dict 內(等同 None,unfiltered)
    skipped = [s for s, t in final_thresholds.items() if t is None]
    if skipped:
        print(
            f"    # 沒過 winner 條件:{', '.join(skipped)}",
            flush=True,
        )
    # 沒在 strategies_to_run 內的(常 sample 太小)
    not_run = [s for s in ALL_STRATEGIES.keys() if s not in strategies_to_run]
    if not_run:
        print(f"    # 沒跑(sample 太小):{', '.join(not_run)}", flush=True)
    print("}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
