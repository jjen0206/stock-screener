"""Threshold sweep for bias_convergence — find ML gate threshold that maximizes
cost-adjusted EV (= fires × avg_return).

Usage:
    python scripts/audit/sweep_bias_convergence_threshold.py --as-of 2026-05-11

Sweeps thresholds: 0.50, 0.55, 0.60, 0.65, 0.70, 0.75
Prints table and ranks by total_return = fires × avg_return.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.backtest import backtest_strategy  # noqa: E402
from src.ml_predictor import load_model, load_strategy_model  # noqa: E402
from src.universe import pure_stock_universe  # noqa: E402


THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="bias_convergence")
    p.add_argument("--as-of", help="period_end YYYY-MM-DD; default=latest")
    p.add_argument("--lookback", type=int, default=126)
    p.add_argument("--hold", type=int, default=5)
    args = p.parse_args()

    db.init_db()
    db.preload_snapshots()

    period_end = args.as_of or db.get_latest_trading_date()
    if not period_end:
        print("no trading dates available", flush=True)
        return 1

    universe = pure_stock_universe(min_history=20)
    print(f"[SWEEP] {args.strategy} as-of={period_end} lookback={args.lookback}d "
          f"hold={args.hold}d universe={len(universe)}")

    # Load both general and strategy-specific models
    general_path = _ROOT / "models" / "short_pick.pkl"
    general = load_model(general_path) if general_path.exists() else None
    strat_model = load_strategy_model(args.strategy)

    # Baseline (no ML filter)
    baseline = backtest_strategy(
        args.strategy, universe, period_end,
        lookback_days=args.lookback, hold_days=args.hold,
        ml_filter=None, ml_model=None, strategy_model=None,
    )
    print()
    print(f"{'Threshold':<10s} {'Fires':>6s} {'Wins':>6s} {'WR':>7s} "
          f"{'AvgRet':>8s} {'TotalRet':>10s} {'CapEff':>10s}")
    print("-" * 65)
    print(f"{'baseline':<10s} {baseline['n_fires']:>6d} {baseline['n_wins']:>6d} "
          f"{baseline['win_rate']*100:>6.1f}% {baseline['avg_return']*100:>+7.2f}% "
          f"{baseline['n_fires']*baseline['avg_return']*100:>+9.2f}% "
          f"{'n/a':>10s}")

    rows = []
    for thr in THRESHOLDS:
        r = backtest_strategy(
            args.strategy, universe, period_end,
            lookback_days=args.lookback, hold_days=args.hold,
            ml_filter=thr, ml_model=general, strategy_model=strat_model,
        )
        # CapEff = avgRet / (fires per day) — penalize zero-fire (no opportunity)
        cap_eff = r["avg_return"] if r["n_fires"] >= 5 else float("nan")
        total_ret = r["n_fires"] * r["avg_return"]
        rows.append((thr, r, total_ret, cap_eff))
        print(f"{thr:<10.2f} {r['n_fires']:>6d} {r['n_wins']:>6d} "
              f"{r['win_rate']*100:>6.1f}% {r['avg_return']*100:>+7.2f}% "
              f"{total_ret*100:>+9.2f}% {cap_eff*100:>+9.2f}%")

    # Rank candidates
    print()
    print("[RANK] by total_return (fires × avg_return, capital-weighted):")
    for thr, r, total_ret, _ in sorted(rows, key=lambda kv: -kv[2])[:3]:
        print(f"  thr={thr:.2f}: fires={r['n_fires']} WR={r['win_rate']*100:.1f}% "
              f"avgRet={r['avg_return']*100:+.2f}% total={total_ret*100:+.2f}%")

    print()
    print("[RANK] by avg_return (per-trade quality, fires >= 10 only):")
    quality = [(thr, r, total_ret) for thr, r, total_ret, _ in rows if r["n_fires"] >= 10]
    for thr, r, total_ret in sorted(quality, key=lambda kv: -kv[1]["avg_return"])[:3]:
        print(f"  thr={thr:.2f}: fires={r['n_fires']} WR={r['win_rate']*100:.1f}% "
              f"avgRet={r['avg_return']*100:+.2f}% total={total_ret*100:+.2f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
