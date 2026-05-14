"""vectorbt 策略級 grid search 入口(階段 3 baseline:volume_breakout)。

設計重點:
- 全市場 universe(pure_stock_universe — 排掉 ETF / TAIEX),lookback 過去
  6 個月交易日
- 對指定策略跑 params_grid 內所有組合,寫進 vbt_grid_results 表(UPSERT)
- 印 Top 5 by sharpe + 軍師判讀
- 不自動推進為 production default,只是「建議」

CLI:
    # 預設(volume_breakout)
    python scripts/vbt_grid_search.py

    # 指定其他策略 + 自訂 lookback 月份
    python scripts/vbt_grid_search.py --strategy volume_breakout --months 6

Exit code:
    0 = 成功(至少 1 row 寫進表)
    1 = 全空(資料 / 命中不足)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.universe import pure_stock_universe  # noqa: E402
from src.vbt_backtest import (  # noqa: E402
    backtest_strategy_with_params, persist_grid_results,
)


# === per-strategy grid 定義 ===

# volume_breakout 兩 params:vbo_vol_ratio_min × highest_lookback
# 2026-05-14 第二次縮:3 × 3 = 9 組合(原 7 × 6 = 42 在全市場 vbt 上跑不完;
# NaN drop fix 後 close matrix 變大,vbt allocation 在 27+ 組合會 slow-down 到 hang)
VOLUME_BREAKOUT_GRID: dict[str, list] = {
    "vbo_vol_ratio_min": [1.0, 1.5, 2.0],
    "highest_lookback": [3, 5, 10],
}

# bias_convergence:3 × 3 × 1 = 9 組合,vol_ratio_min 固定在 default 1.2
# (NaN fix 後 27 組合 × 全市場跑不完,先縮成 2D sweep 在 default vol_ratio 上;
#  若需 vol_ratio 維度可下次再單獨跑)
BIAS_CONVERGENCE_GRID: dict[str, list] = {
    "bias_low": [-8.0, -5.0, -3.0],
    "bias_high": [0.0, 1.0, 2.5],
    "vol_ratio_min": [1.2],
}

# macd_golden:單 param,跨 6 個 level,涵蓋 default 1.0
MACD_GOLDEN_GRID: dict[str, list] = {
    "vol_ratio_min": [0.8, 1.0, 1.2, 1.5, 1.8, 2.0],
}

# ma_alignment:策略本身只吃 lookback_days(資料窗,非門檻);沒真正可 tune
# 的閾值。仍跑 1 組 default 當 baseline,讓 UI 看到該策略也有 grid 條目。
MA_ALIGNMENT_GRID: dict[str, list] = {
    "lookback_days": [80],
}

# 未來其他策略 grid 補進這 dict;CLI --strategy 才能用
STRATEGY_GRIDS: dict[str, dict[str, list]] = {
    "volume_breakout": VOLUME_BREAKOUT_GRID,
    "bias_convergence": BIAS_CONVERGENCE_GRID,
    "macd_golden": MACD_GOLDEN_GRID,
    "ma_alignment": MA_ALIGNMENT_GRID,
}


def _grid_size(grid: dict[str, list]) -> int:
    """cartesian product 組合數。"""
    n = 1
    for v in grid.values():
        n *= len(v)
    return n


def _latest_trading_date() -> str:
    """從 daily_prices 撈最新交易日。"""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(date) FROM daily_prices WHERE stock_id != 'TAIEX'"
        ).fetchone()
    return row[0] if row and row[0] else datetime.now().strftime("%Y-%m-%d")


def _months_ago(end_date: str, months: int) -> str:
    """end_date 往前推 months 個月(粗估 30 天 / 月,不抓真實月曆)。"""
    end = datetime.strptime(end_date, "%Y-%m-%d")
    start = end - timedelta(days=months * 30)
    return start.strftime("%Y-%m-%d")


def _print_top_n_and_verdict(df, top_n: int = 5) -> None:
    """印 Top N + 「軍師判讀」。"""
    if df is None or df.empty:
        print("[VBT] 無結果 — 全 0 trades 或 universe 太小")
        return

    print(f"\n=== Top {top_n} by Sharpe ===")
    cols = ["params_json", "n_trades", "total_return", "sharpe",
            "max_drawdown", "win_rate"]
    top = df.head(top_n)[cols].copy()
    print(top.to_string(index=False))

    best = df.iloc[0]
    print("\n=== 軍師判讀 ===")
    print(f"最佳組合:{best['params_json']}")
    print(f"  Sharpe         {float(best['sharpe']):.4f}")
    print(f"  Total return   {float(best['total_return']):.2f}%")
    print(f"  Max drawdown   {float(best['max_drawdown']):.2f}%")
    print(f"  Win rate       {float(best['win_rate']):.2f}%")
    print(f"  N trades       {int(best['n_trades'])}")

    if int(best["n_trades"]) < 10:
        print("  [!]樣本過小(<10 trades)— 雜訊大,建議用更長 lookback 再驗")
    if float(best["sharpe"]) < 0:
        print("  [!]最佳組合 Sharpe < 0 — 該策略在此區間整體不賺(慎用 default)")
    if float(best["max_drawdown"]) > 20:
        print("  [!]Max drawdown > 20% — 風控門檻需配合(停損 / 部位 sizing)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="vectorbt 策略級 grid search(階段 3:volume_breakout baseline)"
    )
    parser.add_argument(
        "--strategy", default="volume_breakout",
        choices=list(STRATEGY_GRIDS.keys()),
        help="跑哪個策略(目前只接 volume_breakout;後續策略補進 STRATEGY_GRIDS)",
    )
    parser.add_argument("--months", type=int, default=6, help="lookback 月數(預設 6)")
    parser.add_argument("--top-n", type=int, default=5, help="印 Top N(預設 5)")
    parser.add_argument(
        "--universe-size", type=int, default=None,
        help="限制 universe 大小(預設整個 pure_stock_universe);測試時可給 50",
    )
    args = parser.parse_args()

    end_date = _latest_trading_date()
    start_date = _months_ago(end_date, args.months)
    grid = STRATEGY_GRIDS[args.strategy]

    universe = pure_stock_universe()
    if args.universe_size:
        universe = universe[: args.universe_size]
    print(f"[VBT] strategy={args.strategy}  universe={len(universe)}  "
          f"range=[{start_date}, {end_date}]")
    print(f"[VBT] grid: {grid} → {_grid_size(grid)} 組合")

    df = backtest_strategy_with_params(
        args.strategy,
        params_grid=grid,
        start_date=start_date,
        end_date=end_date,
        universe=universe,
    )
    if df is None or df.empty:
        print("[VBT] 結果為空 — abort")
        return 1

    n_written = persist_grid_results(
        df, period_start=start_date, period_end=end_date,
    )
    print(f"[VBT] {n_written} row 寫進 vbt_grid_results 表")
    _print_top_n_and_verdict(df, top_n=args.top_n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
