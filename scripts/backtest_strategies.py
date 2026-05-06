"""排程入口:跑 N 個交易日歷史回測 → strategy_backtest 表 + dump CSV。

跑時機:nightly workflow 內(daily-notify.yml)放在 precompute_strategies
之後,**只週一跑**(週二~五 skip)避免每天浪費 CI 時間。

雲端 Streamlit Cloud 容器 boot 時透過 snapshot CSV preload 拿到結果,App
端 `_enrich_with_win_rate` 把每 strategy 的 win_rate 灌進 agg DataFrame,
卡片勝率欄自動填上百分比 + 染色。

CLI:
    # 預設(全 11 strategies × pure_stock universe × 126 day lookback)
    python scripts/backtest_strategies.py

    # 跑特定 as-of date
    python scripts/backtest_strategies.py --as-of 2026-05-04

    # 自訂 lookback / target / stop / hold
    python scripts/backtest_strategies.py --lookback 252 --target 0.07 --stop 0.04 --hold 7

    # 只跑單一 strategy
    python scripts/backtest_strategies.py --strategy macd_golden

Exit code:
    0 = 成功(至少一 strategy 寫進表)
    1 = 全空(SQLite 無歷史)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.backtest import backtest_all_strategies  # noqa: E402
from src.strategies import ALL_STRATEGIES  # noqa: E402
from src.universe import pure_stock_universe  # noqa: E402


def dump_strategy_backtest_csv(snapshot_dir: Path | None = None) -> int:
    """把 strategy_backtest 全表 dump 成 CSV(給 nightly workflow git push)。

    雲端容器 boot 時 preload_snapshots 會讀 strategy_backtest.csv 灌進
    SQLite,App 端 load_latest 即可命中。

    回 row count(無資料 → 不寫 CSV,回 0)。
    """
    import pandas as pd

    if snapshot_dir is None:
        snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT strategy, period_end, lookback_days, target_pct, stop_pct, "
            "hold_days, n_fires, n_wins, win_rate, avg_return, computed_at "
            "FROM strategy_backtest "
            "ORDER BY period_end DESC, win_rate DESC"
        ).fetchall()

    if not rows:
        print("[BACKTEST] strategy_backtest 表空,不 dump CSV", flush=True)
        return 0

    df = pd.DataFrame([dict(r) for r in rows])
    csv_path = snapshot_dir / "strategy_backtest.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(
        f"[BACKTEST] dump CSV → {csv_path} ({len(rows)} rows)",
        flush=True,
    )
    return len(rows)


def _print_summary_table(rows: list[dict]) -> None:
    """印 markdown-ish summary table 給 nightly workflow stdout 看。"""
    if not rows:
        print("[BACKTEST] (no rows)", flush=True)
        return
    # sorted by win_rate desc(高的在前)
    rows_sorted = sorted(rows, key=lambda r: -r["win_rate"])
    print("", flush=True)
    print(
        f"{'Strategy':<28s} {'Fires':>7s} {'Wins':>6s} "
        f"{'WinRate':>8s} {'AvgRet':>8s}",
        flush=True,
    )
    print("-" * 64, flush=True)
    for r in rows_sorted:
        print(
            f"{r['strategy']:<28s} {r['n_fires']:>7d} {r['n_wins']:>6d} "
            f"{r['win_rate'] * 100:>7.1f}% {r['avg_return'] * 100:>+7.2f}%",
            flush=True,
        )
    print("", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description="跑歷史回測 → strategy_backtest 表")
    p.add_argument(
        "--as-of",
        help="period_end YYYY-MM-DD;留空 = SQLite daily_prices MAX(date)",
    )
    p.add_argument(
        "--lookback", type=int, default=126,
        help="lookback 交易日數(default 126 ≈ 6 個月)",
    )
    p.add_argument(
        "--target", type=float, default=0.05,
        help="目標報酬 decimal(default 0.05 等於 +5 percent)",
    )
    p.add_argument(
        "--stop", type=float, default=0.03,
        help="停損 decimal(default 0.03 等於 -3 percent)",
    )
    p.add_argument(
        "--hold", type=int, default=5,
        help="持有天數(default 5 個交易日)",
    )
    p.add_argument(
        "--strategy",
        help=f"只跑單一 strategy(default 全跑;可選: {', '.join(ALL_STRATEGIES.keys())})",
    )
    p.add_argument(
        "--no-csv", action="store_true",
        help="跑完不 dump CSV(只寫 SQLite,給 ad-hoc 測試用)",
    )
    p.add_argument(
        "--ml-filter", type=float, default=None,
        help="只回測 ML prob >= 此值的 picks(0.50-0.80;留空 = 不過濾,跟 "
             "Stage 1 之前一致)。給 with-ML vs without-ML 對比驗證用。",
    )
    p.add_argument(
        "--per-strategy-ml", action="store_true",
        help="使用 STRATEGY_ML_THRESHOLDS 的 per-strategy 門檻過濾(Stage 2A)。"
             "跟 --ml-filter 互斥。",
    )
    args = p.parse_args()
    if args.ml_filter is not None and args.per_strategy_ml:
        print(
            "[BACKTEST] --ml-filter 跟 --per-strategy-ml 互斥,只能擇一",
            flush=True,
        )
        return 1

    db.init_db()
    # Fresh container preload(workflow runner 走這條)
    preload = db.preload_snapshots()
    if preload:
        print(f"[BACKTEST] preload snapshots: {preload}", flush=True)

    if args.as_of:
        period_end = args.as_of
    else:
        latest = db.get_latest_trading_date()
        if not latest:
            print("[BACKTEST] daily_prices 表空,無法跑回測", flush=True)
            return 1
        period_end = latest
        print(f"[BACKTEST] period_end={period_end}(latest trading date)", flush=True)

    # Universe — pure_stock(20+ 天歷史的純股票,跟 precompute 共用)
    universe = pure_stock_universe(min_history=20)
    if not universe:
        print(
            "[BACKTEST] pure_stock universe 空 — SQLite 無 20+ 天歷史的股票",
            flush=True,
        )
        return 1
    print(f"[BACKTEST] universe = {len(universe)} 檔", flush=True)

    # 決定跑哪些 strategies
    if args.strategy:
        if args.strategy not in ALL_STRATEGIES:
            print(
                f"[BACKTEST] 未知 strategy: {args.strategy}\n"
                f"可選: {', '.join(ALL_STRATEGIES.keys())}",
                flush=True,
            )
            return 1
        strategies = [args.strategy]
    else:
        strategies = list(ALL_STRATEGIES.keys())

    if args.per_strategy_ml:
        ml_note = "  per-strategy ML thresholds (Stage 2A)"
    elif args.ml_filter is not None:
        ml_note = f"  ML filter ≥ {args.ml_filter:.2f}"
    else:
        ml_note = ""
    print(
        f"[BACKTEST] 跑 {len(strategies)} strategies × {len(universe)} sids "
        f"× {args.lookback} 日 lookback × hold {args.hold} 天 "
        f"(target +{args.target * 100:.0f}% / stop -{args.stop * 100:.0f}%)"
        f"{ml_note}...",
        flush=True,
    )

    rows = backtest_all_strategies(
        universe=universe,
        period_end=period_end,
        lookback_days=args.lookback,
        target_pct=args.target,
        stop_pct=args.stop,
        hold_days=args.hold,
        strategies=strategies,
        ml_filter=args.ml_filter,
        per_strategy_ml=args.per_strategy_ml,
    )

    # ML filter / per-strategy 模式不寫進 strategy_backtest 表(避免覆蓋 baseline)
    is_ad_hoc = args.ml_filter is not None or args.per_strategy_ml
    if not is_ad_hoc:
        n_inserted = db.dump_strategy_backtest(rows)
        print(f"[BACKTEST] 寫入 strategy_backtest {n_inserted} 筆", flush=True)
    else:
        n_inserted = len(rows)
        print(
            "[BACKTEST] ad-hoc mode (ML filter): 不寫 strategy_backtest 表"
            "(只印 summary 給對比)",
            flush=True,
        )

    _print_summary_table(rows)

    # ad-hoc 模式不 dump CSV(也不寫表),純對比
    if not is_ad_hoc and not args.no_csv and n_inserted > 0:
        dump_strategy_backtest_csv()

    return 0 if n_inserted > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
