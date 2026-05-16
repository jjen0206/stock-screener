"""Stage 2B-3:跑 4-mode backtest 對比,印出 ML 各設計貢獻矩陣。

Modes:
    1. baseline               — 沒 ML filter(純 strategy)
    2. global 0.60            — 通用 model + 全 strategies 套 0.60 門檻(Stage 1)
    3. per-strategy thresh    — 通用 model + STRATEGY_ML_THRESHOLDS dict
    4. per-strategy full      — per-strategy models + STRATEGY_ML_THRESHOLDS dict
                                (Stage 2B 完整)

對 11 個 strategies × 4 modes 跑 backtest_all_strategies(),回 4 欄 win_rate
表格。順著 1→4 看每個 design 的貢獻。

CLI:
    python scripts/audit/compare_ml_modes.py
    python scripts/audit/compare_ml_modes.py --lookback 60
    python scripts/audit/compare_ml_modes.py --as-of 2026-04-30

Exit code:
    0 = 跑完(印表)
    1 = SQLite 歷史不足 / universe 空
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.backtest import backtest_all_strategies  # noqa: E402
from src.strategies import ALL_STRATEGIES, STRATEGY_ML_THRESHOLDS  # noqa: E402
from src.universe import pure_stock_universe  # noqa: E402


def _run_mode(
    label: str,
    universe: list[str],
    period_end: str,
    lookback_days: int,
    *,
    ml_filter: float | None = None,
    per_strategy_ml: bool = False,
    disable_per_strategy_models: bool = False,
) -> dict[str, dict]:
    """跑單 mode → 回 {strategy: {n_fires, n_wins, win_rate, avg_return}}。"""
    print(f"\n[MODE] {label} starting...", flush=True)
    t0 = time.perf_counter()
    rows = backtest_all_strategies(
        universe=universe,
        period_end=period_end,
        lookback_days=lookback_days,
        ml_filter=ml_filter,
        per_strategy_ml=per_strategy_ml,
        disable_per_strategy_models=disable_per_strategy_models,
    )
    elapsed = time.perf_counter() - t0
    print(f"[MODE] {label} 完成 ({elapsed:.1f}s)", flush=True)

    out: dict[str, dict] = {}
    for r in rows:
        out[r["strategy"]] = {
            "n_fires": r["n_fires"],
            "n_wins": r["n_wins"],
            "win_rate": r["win_rate"],
            "avg_return": r["avg_return"],
        }
    return out


def _format_cell(stats: dict) -> str:
    """格式 'WR% (fires)' — fires 0 → '— (0)'。"""
    fires = stats.get("n_fires", 0)
    if fires == 0:
        return "—   (0)"
    return f"{stats['win_rate'] * 100:5.1f}% ({fires})"


def _print_comparison_table(
    mode1: dict, mode2: dict, mode3: dict, mode4: dict,
) -> None:
    """4-mode 對比:逐 strategy 一行。每格顯 WR% (n_fires)。"""
    print("\n" + "=" * 100, flush=True)
    print("4-mode comparison (WinRate% / n_fires)", flush=True)
    print("=" * 100, flush=True)
    headers = (
        f"{'Strategy':<22s}"
        f" {'baseline':>15s} {'global 0.60':>15s}"
        f" {'per-strat thr':>15s} {'per-strat full':>15s}"
    )
    print(headers, flush=True)
    print("-" * 100, flush=True)
    for sname in ALL_STRATEGIES.keys():
        s1 = mode1.get(sname, {})
        s2 = mode2.get(sname, {})
        s3 = mode3.get(sname, {})
        s4 = mode4.get(sname, {})
        threshold_marker = (
            f" *{STRATEGY_ML_THRESHOLDS[sname]:.2f}*"
            if sname in STRATEGY_ML_THRESHOLDS else ""
        )
        print(
            f"{sname + threshold_marker:<22s}"
            f" {_format_cell(s1):>15s} {_format_cell(s2):>15s}"
            f" {_format_cell(s3):>15s} {_format_cell(s4):>15s}",
            flush=True,
        )
    print("-" * 100, flush=True)
    # 加總(全 11 strategies 總 fires + weighted WR)
    def _agg(mode: dict) -> tuple[int, int, float]:
        total_fires = sum(s.get("n_fires", 0) for s in mode.values())
        total_wins = sum(s.get("n_wins", 0) for s in mode.values())
        wr = total_wins / total_fires if total_fires else 0.0
        return total_fires, total_wins, wr

    f1, _, wr1 = _agg(mode1)
    f2, _, wr2 = _agg(mode2)
    f3, _, wr3 = _agg(mode3)
    f4, _, wr4 = _agg(mode4)
    def cell_summary(f: int, wr: float) -> str:
        return f"{wr * 100:5.1f}% ({f})"
    print(
        f"{'TOTAL (weighted)':<22s}"
        f" {cell_summary(f1, wr1):>15s} {cell_summary(f2, wr2):>15s}"
        f" {cell_summary(f3, wr3):>15s} {cell_summary(f4, wr4):>15s}",
        flush=True,
    )
    print("\n* 標記表示該 strategy 在 STRATEGY_ML_THRESHOLDS 內(threshold 值)\n", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Stage 2B 4-mode ML backtest 對比")
    p.add_argument(
        "--lookback", type=int, default=126,
        help="回測 lookback 交易日(default 126 ≈ 6 個月)",
    )
    p.add_argument(
        "--as-of",
        help="period_end YYYY-MM-DD;留空 = SQLite daily_prices MAX(date)",
    )
    args = p.parse_args()

    db.init_db()
    counts = db.preload_snapshots()
    if counts:
        print(f"[COMPARE] preload snapshots: {counts}", flush=True)

    if args.as_of:
        period_end = args.as_of
    else:
        period_end = db.get_latest_trading_date()
        if not period_end:
            print("[COMPARE] daily_prices 表空", flush=True)
            return 1

    universe = pure_stock_universe(min_history=20)
    if not universe:
        print("[COMPARE] universe 空", flush=True)
        return 1
    print(
        f"[COMPARE] period_end={period_end} | universe={len(universe)} | "
        f"lookback {args.lookback}d",
        flush=True,
    )

    # Mode 1: baseline(no ML)
    mode1 = _run_mode(
        "1. baseline (no ML)",
        universe, period_end, args.lookback,
    )

    # Mode 2: global 0.60(通用 model + 全 strategies 一致 0.60)
    mode2 = _run_mode(
        "2. global 0.60 (Stage 1)",
        universe, period_end, args.lookback,
        ml_filter=0.60,
    )

    # Mode 3: per-strategy threshold + 通用 model(Stage 2A semantics)
    mode3 = _run_mode(
        "3. per-strategy thresh + general model (Stage 2A)",
        universe, period_end, args.lookback,
        per_strategy_ml=True,
        disable_per_strategy_models=True,
    )

    # Mode 4: per-strategy threshold + per-strategy models(完整 Stage 2B)
    mode4 = _run_mode(
        "4. per-strategy full (Stage 2B)",
        universe, period_end, args.lookback,
        per_strategy_ml=True,
    )

    _print_comparison_table(mode1, mode2, mode3, mode4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
