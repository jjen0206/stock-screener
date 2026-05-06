"""throwaway audit:60-day confluence 過濾對勝率影響對比。

對 lookback 內每個交易日跑 run_all_strategies,對每張 pick 算「命中策略數」
n_hit = len(details)。然後做 4-mode 對比 simulate_outcome(target +5% / stop
-3% / hold 5d):
  Mode 1: baseline(no confluence,no ML)
  Mode 2: confluence ≥ 2(共識,no ML)
  Mode 3: confluence ≥ 3
  Mode 4: confluence ≥ 2 + per-strategy ML(雙層,user 關鍵情境)

執行:
    python scripts/audit/backtest_confluence.py --lookback 60
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src._bulk_load import bulk_load_prices  # noqa: E402
from src.backtest import _list_trading_dates, simulate_outcome  # noqa: E402
from src.ml_predictor import (  # noqa: E402
    load_model, load_strategy_model, predict_for_strategy,
)
from src.strategies import (  # noqa: E402
    ALL_STRATEGIES, STRATEGY_ML_THRESHOLDS, run_all_strategies,
)
from src.universe import pure_stock_universe  # noqa: E402


TARGET_PCT = 0.05
STOP_PCT = 0.03
HOLD_DAYS = 5


def _strictest_threshold(matched: list[str]) -> float | None:
    ths = [
        STRATEGY_ML_THRESHOLDS[s]
        for s in matched if STRATEGY_ML_THRESHOLDS.get(s) is not None
    ]
    return max(ths) if ths else None


def _routing_strategy(matched: list[str]) -> str | None:
    cands = [
        (s, STRATEGY_ML_THRESHOLDS[s])
        for s in matched if STRATEGY_ML_THRESHOLDS.get(s) is not None
    ]
    if not cands:
        return None
    return max(cands, key=lambda kv: kv[1])[0]


def collect_picks(
    universe: list[str], period_end: str, lookback_days: int,
) -> list[dict]:
    """跑每個交易日 run_all_strategies,collect 每張 pick + 命中策略數 + outcome。

    回 list[{D, sid, n_hit, matched, outcome, ret, ml_prob_for_routing}]。
    """
    all_dates = _list_trading_dates(period_end, lookback_days)
    if len(all_dates) < HOLD_DAYS + 1:
        return []
    pickable_dates = all_dates[: -HOLD_DAYS]
    end_for_bulk = all_dates[-1]
    bulk_lookback = lookback_days + HOLD_DAYS + 5
    with db.get_conn() as conn:
        prices_by_sid = bulk_load_prices(
            conn, universe, end_for_bulk, lookback_days=bulk_lookback,
        )

    # 預載 per-strategy models 避免每次 load
    strategy_models: dict[str, object] = {}
    for s in ALL_STRATEGIES.keys():
        strategy_models[s] = load_strategy_model(s)
    general_path = config.PROJECT_ROOT / "models" / "short_pick.pkl"
    general_model = load_model(general_path) if general_path.exists() else None

    out: list[dict] = []
    for D in pickable_dates:
        try:
            agg = run_all_strategies(D, stock_ids=universe)
        except Exception:
            continue
        if not agg:
            continue

        # 對該 D 所有 picks 一次 ML predict per (chosen_strategy)
        # 不同 sid 可能不同 routing,group 一下
        sid_to_chosen: dict[str, str | None] = {}
        for sid, info in agg.items():
            matched = list((info.get("details") or {}).keys())
            sid_to_chosen[sid] = _routing_strategy(matched)
        groups: dict[str | None, list[str]] = defaultdict(list)
        for sid, chosen in sid_to_chosen.items():
            groups[chosen].append(sid)

        ml_probs: dict[str, float | None] = {}
        for chosen, sids in groups.items():
            sm = strategy_models.get(chosen) if chosen else None
            try:
                probs = predict_for_strategy(
                    strategy_name=chosen, stock_ids=sids, target_date=D,
                    fallback_model=general_model, strategy_model=sm,
                )
                ml_probs.update(probs)
            except Exception:
                ml_probs.update({s: None for s in sids})

        # 對每張 pick 算 outcome
        for sid, info in agg.items():
            matched = list((info.get("details") or {}).keys())
            n_hit = len(matched)
            details = info.get("details") or {}
            # entry price:取任一 strategy 的 close(都是同一檔同日 close)
            entry = None
            for s, row in details.items():
                if isinstance(row, dict) and row.get("close"):
                    entry = float(row["close"])
                    break
            if entry is None or entry <= 0:
                continue
            sid_df = prices_by_sid.get(sid)
            if sid_df is None or sid_df.empty:
                continue
            future = sid_df[sid_df["date"] > D].head(HOLD_DAYS)
            if len(future) < HOLD_DAYS:
                continue
            outcome, ret = simulate_outcome(
                future, entry,
                target_pct=TARGET_PCT, stop_pct=STOP_PCT,
            )
            out.append({
                "D": D, "sid": sid,
                "n_hit": n_hit, "matched": matched,
                "outcome": outcome, "ret": ret,
                "ml_prob": ml_probs.get(sid),
            })
    return out


def _evaluate(
    picks: list[dict],
    *,
    confluence_n: int = 1,
    apply_ml: bool = False,
) -> dict:
    """套不同 mode filter 後算 fires / wins / WR / EV。"""
    n_fires = 0
    n_wins = 0
    total_ret = 0.0
    for p in picks:
        if p["n_hit"] < confluence_n:
            continue
        if apply_ml:
            thr = _strictest_threshold(p["matched"])
            if thr is not None:
                prob = p["ml_prob"]
                if prob is None or prob < thr:
                    continue
        n_fires += 1
        if p["outcome"] == "win":
            n_wins += 1
        total_ret += p["ret"]
    return {
        "fires": n_fires,
        "wins": n_wins,
        "win_rate": n_wins / n_fires if n_fires else 0.0,
        "ev": total_ret / n_fires if n_fires else 0.0,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--lookback", type=int, default=60)
    args = p.parse_args()

    db.init_db()
    counts = db.preload_snapshots()
    if counts:
        print(f"[CONF] preload: {counts}", flush=True)
    period_end = db.get_latest_trading_date()
    if not period_end:
        print("[CONF] daily_prices 表空", flush=True)
        return 1
    universe = pure_stock_universe(min_history=20)
    print(
        f"[CONF] period_end={period_end} | universe={len(universe)} | "
        f"lookback {args.lookback}d",
        flush=True,
    )

    t0 = time.perf_counter()
    picks = collect_picks(universe, period_end, args.lookback)
    elapsed = time.perf_counter() - t0
    print(
        f"[CONF] collected {len(picks)} pick rows ({elapsed:.1f}s)", flush=True,
    )

    # 命中策略數分布
    dist: dict[int, int] = defaultdict(int)
    for x in picks:
        dist[x["n_hit"]] += 1
    print("\n命中策略數分布(picks 內):", flush=True)
    for n in sorted(dist.keys()):
        print(f"  {n} 策略: {dist[n]} 張", flush=True)

    # 4 modes
    modes = [
        ("Mode 1: baseline (no confluence, no ML)",
         {"confluence_n": 1, "apply_ml": False}),
        ("Mode 2: confluence ≥ 2 (共識,no ML)",
         {"confluence_n": 2, "apply_ml": False}),
        ("Mode 3: confluence ≥ 3",
         {"confluence_n": 3, "apply_ml": False}),
        ("Mode 4: confluence ≥ 2 + per-strategy ML(雙層)",
         {"confluence_n": 2, "apply_ml": True}),
    ]

    print("\n" + "=" * 80, flush=True)
    print("Confluence + ML 過濾對勝率影響(60-day)", flush=True)
    print("=" * 80, flush=True)
    print(
        f"{'Mode':<55} {'Fires':>7} {'Wins':>6} {'WR':>8} {'EV':>9}",
        flush=True,
    )
    print("-" * 88, flush=True)
    for label, kwargs in modes:
        stats = _evaluate(picks, **kwargs)
        print(
            f"{label:<55} {stats['fires']:>7d} {stats['wins']:>6d} "
            f"{stats['win_rate'] * 100:>7.1f}% {stats['ev'] * 100:>+8.2f}%",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
