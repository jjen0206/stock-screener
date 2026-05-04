"""Plan G - Part 1:每策略 R:R grid search,maximize 期望值(EV = avg_return)。

從追 win rate 改追 EV。固定 target=5%/stop=3%/hold=5 對所有策略不一定最優 —
有的策略適合「短期小目標」(macd_golden 0.05/0.02/3),有的適合「長期大目標」
(gap_up 0.10/0.04/7)。本 script 對每策略獨立 grid search 找最佳組合。

設計:
- 每策略**只跑一次** screener + ML threshold 過濾(套 STRATEGY_ML_THRESHOLDS),
  collect 每張 pick 的 future OHLC(max(hold_days)=10 天)
- 對 64 個合法 (target, stop, hold) 組合在記憶體 sweep simulate_outcome
- 比 naive 64x backtest 快 ~64 倍

Grid(64 個合法組合 / 策略;stop ≥ target 不合法 R:R<1 略過):
    target_pct: [0.03, 0.05, 0.07, 0.10, 0.15]
    stop_pct:   [0.02, 0.03, 0.04, 0.05]
    hold_days:  [3, 5, 7, 10]

Winner 條件:fires ≥ MIN_FIRES (default 10) 中 EV 最高者。沒過 → 該策略走
DEFAULT_RR_PARAMS (0.05, 0.03, 5)。

CLI:
    python scripts/optimize_strategy_rr.py
    python scripts/optimize_strategy_rr.py --lookback 126
    python scripts/optimize_strategy_rr.py --strategy ma_alignment
    python scripts/optimize_strategy_rr.py --min-fires 30
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src._bulk_load import bulk_load_prices  # noqa: E402
from src.backtest import _list_trading_dates, simulate_outcome  # noqa: E402
from src.ml_predictor import (  # noqa: E402
    load_model, load_strategy_model, predict_for_strategy,
)
from src.strategies import ALL_STRATEGIES, STRATEGY_ML_THRESHOLDS  # noqa: E402
from src.universe import pure_stock_universe  # noqa: E402


GRID = {
    "target_pct": [0.03, 0.05, 0.07, 0.10, 0.15],
    "stop_pct":   [0.02, 0.03, 0.04, 0.05],
    "hold_days":  [3, 5, 7, 10],
}
MAX_HOLD = max(GRID["hold_days"])  # 10:預先 collect 這麼多天 future OHLC
DEFAULT_MIN_FIRES = 10
DEFAULT_LOOKBACK = 60


def collect_picks_with_futures(
    strategy_name: str,
    universe: list[str],
    period_end: str,
    lookback_days: int,
    ml_threshold: float | None,
    ml_model,
    strategy_model,
) -> list[dict]:
    """跑 strategy 在 lookback 內每天的 picks,套 ML 過濾,collect (sid, D, entry,
    ml_prob, future_df=10 days) 給 grid search 在記憶體 sweep。

    ml_threshold=None → 不過濾(該策略不在 STRATEGY_ML_THRESHOLDS 內)。
    """
    screen_fn = ALL_STRATEGIES[strategy_name]
    all_dates = _list_trading_dates(period_end, lookback_days)
    if len(all_dates) < MAX_HOLD + 1:
        return []
    pickable_dates = all_dates[: -MAX_HOLD]

    end_for_bulk = all_dates[-1]
    bulk_lookback = lookback_days + MAX_HOLD + 5
    with db.get_conn() as conn:
        prices_by_sid = bulk_load_prices(
            conn, universe, end_for_bulk, lookback_days=bulk_lookback,
        )

    picks: list[dict] = []
    for D in pickable_dates:
        try:
            df = screen_fn(D, params=None, stock_ids=universe)
        except Exception:  # noqa: BLE001
            continue
        if df is None or df.empty:
            continue

        sids_today = [str(r["stock_id"]) for _, r in df.iterrows()]
        # 預測 ML probs(若 strategy 有 threshold,否則無需 predict)
        if ml_threshold is not None and (strategy_model is not None or ml_model is not None):
            ml_probs = predict_for_strategy(
                strategy_name=strategy_name,
                stock_ids=sids_today,
                target_date=D,
                fallback_model=ml_model,
                strategy_model=strategy_model,
            )
        else:
            ml_probs = {sid: None for sid in sids_today}

        for _, pick_row in df.iterrows():
            sid = str(pick_row["stock_id"])
            entry = float(pick_row.get("close", 0) or 0)
            if entry <= 0:
                continue

            # 套 ML threshold 過濾
            if ml_threshold is not None:
                prob = ml_probs.get(sid)
                if prob is None or prob < ml_threshold:
                    continue

            sid_df = prices_by_sid.get(sid)
            if sid_df is None or sid_df.empty:
                continue
            future = sid_df[sid_df["date"] > D].head(MAX_HOLD)
            if len(future) < MAX_HOLD:
                continue  # 邊界,沒足夠 max_hold 天 future

            picks.append({
                "sid": sid,
                "D": D,
                "entry": entry,
                "ml_prob": ml_probs.get(sid),
                "future_df": future,
            })
    return picks


def sweep_rr(
    picks: list[dict], target_pct: float, stop_pct: float, hold_days: int,
) -> dict:
    """套 (target, stop, hold) 在記憶體 simulate_outcome,回 fires/wins/WR/EV。"""
    n_fires = 0
    n_wins = 0
    total_ret = 0.0
    for p in picks:
        future = p["future_df"].head(hold_days)
        if len(future) < hold_days:
            continue
        outcome, ret = simulate_outcome(
            future, p["entry"], target_pct=target_pct, stop_pct=stop_pct,
        )
        n_fires += 1
        total_ret += ret
        if outcome == "win":
            n_wins += 1
    if n_fires == 0:
        return {
            "fires": 0, "wins": 0,
            "win_rate": 0.0, "avg_return": 0.0,
        }
    return {
        "fires": n_fires,
        "wins": n_wins,
        "win_rate": n_wins / n_fires,
        "avg_return": total_ret / n_fires,
    }


def find_best_rr(
    picks: list[dict], min_fires: int = DEFAULT_MIN_FIRES,
) -> tuple[dict | None, list[dict]]:
    """Grid search 64 combos,回 (winner, all_candidates)。

    Winner = fires ≥ min_fires 且 avg_return 最高;tie 取 fires 多的(更穩)。
    沒過 → winner=None。
    """
    candidates: list[dict] = []
    for tgt in GRID["target_pct"]:
        for stop in GRID["stop_pct"]:
            if stop >= tgt:
                continue  # R:R < 1 無意義
            for hold in GRID["hold_days"]:
                stats = sweep_rr(picks, tgt, stop, hold)
                stats["target"] = tgt
                stats["stop"] = stop
                stats["hold"] = hold
                candidates.append(stats)

    eligible = [c for c in candidates if c["fires"] >= min_fires]
    if not eligible:
        return None, candidates
    # max avg_return,tie → max fires
    eligible.sort(key=lambda c: (-c["avg_return"], -c["fires"]))
    return eligible[0], candidates


def _print_summary(results: dict[str, dict], strategies: list[str]) -> None:
    print("\n" + "=" * 90, flush=True)
    print("Per-strategy R:R 最佳組合 (max EV with fires ≥ min_fires)", flush=True)
    print("=" * 90, flush=True)
    print(
        f"{'Strategy':<22} {'Target':>7} {'Stop':>6} {'Hold':>5} "
        f"{'EV':>9} {'WR':>8} {'Fires':>7} {'Picks':>7}",
        flush=True,
    )
    print("-" * 80, flush=True)
    for sname in strategies:
        r = results.get(sname, {})
        best = r.get("best")
        n_picks = r.get("n_picks", 0)
        if best is None:
            print(
                f"{sname:<22} {'(no winner — default 5%/3%/5d)':<48} {n_picks:>7d}",
                flush=True,
            )
        else:
            print(
                f"{sname:<22} {best['target'] * 100:>6.0f}% "
                f"{best['stop'] * 100:>5.0f}% {best['hold']:>5d} "
                f"{best['avg_return'] * 100:>+8.2f}% "
                f"{best['win_rate'] * 100:>7.1f}% "
                f"{best['fires']:>7d} {n_picks:>7d}",
                flush=True,
            )

    print("\nRecommended STRATEGY_RR_PARAMS:", flush=True)
    print("STRATEGY_RR_PARAMS = {", flush=True)
    for sname in strategies:
        r = results.get(sname, {})
        best = r.get("best")
        if best is not None:
            print(
                f'    "{sname}": ({best["target"]:.2f}, '
                f'{best["stop"]:.2f}, {best["hold"]}),',
                flush=True,
            )
    no_winner = [s for s in strategies if results.get(s, {}).get("best") is None]
    if no_winner:
        print(
            f"    # 沒過 min_fires:{', '.join(no_winner)} → 走 DEFAULT_RR_PARAMS",
            flush=True,
        )
    print("}", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Per-strategy R:R grid search → max EV")
    p.add_argument(
        "--lookback", type=int, default=DEFAULT_LOOKBACK,
        help=f"backtest 交易日(default {DEFAULT_LOOKBACK})",
    )
    p.add_argument(
        "--strategy",
        help=f"單一 strategy(default 全跑;可選 {', '.join(ALL_STRATEGIES.keys())})",
    )
    p.add_argument(
        "--min-fires", type=int, default=DEFAULT_MIN_FIRES,
        help=f"winner 至少 fires(default {DEFAULT_MIN_FIRES})",
    )
    p.add_argument(
        "--as-of", help="period_end YYYY-MM-DD;留空 = SQLite latest",
    )
    args = p.parse_args()

    db.init_db()
    counts = db.preload_snapshots()
    if counts:
        print(f"[OPT-RR] preload: {counts}", flush=True)

    if args.as_of:
        period_end = args.as_of
    else:
        period_end = db.get_latest_trading_date()
        if not period_end:
            print("[OPT-RR] daily_prices 表空", flush=True)
            return 1

    universe = pure_stock_universe(min_history=20)
    if not universe:
        print("[OPT-RR] universe 空", flush=True)
        return 1

    # 通用 model 當 fallback
    general_model_path = config.PROJECT_ROOT / "models" / "short_pick.pkl"
    general_model = (
        load_model(general_model_path) if general_model_path.exists() else None
    )

    if args.strategy:
        if args.strategy not in ALL_STRATEGIES:
            print(
                f"[OPT-RR] 未知 strategy: {args.strategy}\n"
                f"可選: {', '.join(ALL_STRATEGIES.keys())}",
                flush=True,
            )
            return 1
        strategies = [args.strategy]
    else:
        strategies = list(ALL_STRATEGIES.keys())

    n_combos = sum(
        1
        for tgt in GRID["target_pct"]
        for stop in GRID["stop_pct"]
        if stop < tgt
        for _ in GRID["hold_days"]
    )
    print(
        f"[OPT-RR] period_end={period_end} | universe={len(universe)} | "
        f"lookback {args.lookback}d | min_fires={args.min_fires} | "
        f"{n_combos} combos × {len(strategies)} strategies",
        flush=True,
    )

    results: dict[str, dict] = {}
    for sname in strategies:
        threshold = STRATEGY_ML_THRESHOLDS.get(sname)
        sm = load_strategy_model(sname)
        sm_tag = "trained" if sm is not None else "fallback→general"
        thr_tag = f"≥{threshold}" if threshold is not None else "no ML filter"

        t0 = time.time()
        picks = collect_picks_with_futures(
            sname, universe, period_end, args.lookback,
            ml_threshold=threshold,
            ml_model=general_model, strategy_model=sm,
        )
        elapsed = time.time() - t0
        print(
            f"[OPT-RR] {sname}: {len(picks)} picks "
            f"({sm_tag}, {thr_tag}, {elapsed:.1f}s)",
            flush=True,
        )

        best, candidates = find_best_rr(picks, min_fires=args.min_fires)
        results[sname] = {
            "best": best,
            "candidates": candidates,
            "n_picks": len(picks),
        }

        if best is None:
            print(
                f"  → no winner (all combos < {args.min_fires} fires)",
                flush=True,
            )
        else:
            print(
                f"  → winner: target={best['target'] * 100:.0f}% "
                f"stop={best['stop'] * 100:.0f}% hold={best['hold']}d "
                f"EV={best['avg_return'] * 100:+.2f}% "
                f"WR={best['win_rate'] * 100:.1f}% fires={best['fires']}",
                flush=True,
            )

    _print_summary(results, strategies)
    return 0


if __name__ == "__main__":
    sys.exit(main())
