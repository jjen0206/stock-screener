"""排程入口:預跑 default params 全策略 × 多 universe,結果寫 daily_picks 表。

跑時機:nightly workflow 內(daily-notify.yml)放在 weekly_market_update 之後、
daily_notify 之前。雲端 Streamlit Cloud 容器 boot 時透過 snapshot CSV preload
拿到結果,App 端 _run_all_strategies_cached 命中即 0ms 回。

Universe(default params 共 3 種):
- pure_stock:純股票(過濾 ETF / 債券 / 槓反)+ 歷史 ≥20 天 — dashboard / 短線
- with_etf:全股(含 ETF / 債券)+ 歷史 ≥20 天 — 短線「📊 含 ETF」option
- top_50:TW_TOP_50 hardcoded list — 短線「快速:50 檔大型股」option

User 改 sliders → params_hash 不是 default_v1 → App 端 fallback 走 runtime
(此腳本只覆蓋 default 路徑,但是熱路徑 99% 用得到)。

Exit code:
    0 = 成功(全 universe 都跑完 + dump_daily_picks 至少一筆)
    1 = 全失敗(罕見;通常代表 SQLite 沒歷史資料)

CLI:
    python scripts/precompute_strategies.py                # 跑當日(latest trading date)
    python scripts/precompute_strategies.py --date 2026-05-04
    python scripts/precompute_strategies.py --backfill 30  # 倒推 30 天(Part 4 加)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.strategies import run_all_strategies  # noqa: E402
from src.universe import (  # noqa: E402
    TW_TOP_50, is_pure_stock, pure_stock_universe,
)


# Default params 對應「user 沒改任何 slider」的場景。
# 留 None 等 run_all_strategies 走各策略 DEFAULT_*_PARAMS。
DEFAULT_PARAMS_HASH = "default_v1"


def _with_etf_universe(min_history: int = 20) -> list[str]:
    """20+ 天歷史所有股(含 ETF / 債券)。對應 App 短線頁「📊 含 ETF」選項。"""
    sids_with_history = set(db.stocks_with_min_history(min_history))
    if not sids_with_history:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id FROM stocks WHERE market='TW' "
            "AND name IS NOT NULL AND name != '' "
            "ORDER BY stock_id"
        ).fetchall()
    return [r["stock_id"] for r in rows if r["stock_id"] in sids_with_history]


def _build_universes() -> dict[str, list[str]]:
    """組 3 個 universe(以當下 SQLite 內容為準)。"""
    return {
        "pure_stock": pure_stock_universe(min_history=20),
        "with_etf": _with_etf_universe(min_history=20),
        "top_50": [s for s, _ in TW_TOP_50],
    }


def precompute_for_date(trade_date: str) -> dict[str, int]:
    """跑 trade_date 的 default-params strategies × 3 universe,寫進 daily_picks。

    回 {universe_key: row_count_inserted}。
    """
    db.init_db()

    print(
        f"[PRECOMPUTE] target_date={trade_date} params_hash={DEFAULT_PARAMS_HASH}",
        flush=True,
    )

    # 重跑前先清掉這天的舊資料 — 避免 universe size 變了之後留遺漏
    cleared = db.clear_daily_picks_for_date(trade_date)
    if cleared:
        print(f"[PRECOMPUTE] 清掉舊 {cleared} 筆 daily_picks", flush=True)

    universes = _build_universes()
    results: dict[str, int] = {}

    for u_key, sids in universes.items():
        if not sids:
            print(f"[PRECOMPUTE] {u_key}: universe 空,跳過", flush=True)
            results[u_key] = 0
            continue

        t0 = time.perf_counter()
        agg = run_all_strategies(trade_date, stock_ids=sids)  # default params
        elapsed = time.perf_counter() - t0
        n_picks = len(agg)
        n_signal_rows = sum(
            len((info or {}).get("details", {})) for info in agg.values()
        )

        inserted = db.dump_daily_picks(
            trade_date, u_key, agg, params_hash=DEFAULT_PARAMS_HASH,
        )
        results[u_key] = inserted

        # 每策略命中數摘要
        per_strategy: dict[str, int] = {}
        for info in agg.values():
            for k in (info or {}).get("details", {}):
                per_strategy[k] = per_strategy.get(k, 0) + 1

        print(
            f"[PRECOMPUTE] {u_key:<12s} "
            f"sids={len(sids):<4d} picks={n_picks:<3d} "
            f"signals={n_signal_rows:<4d} elapsed={elapsed:5.1f}s | "
            + ", ".join(f"{k}={v}" for k, v in sorted(per_strategy.items())),
            flush=True,
        )

    return results


def main() -> int:
    p = argparse.ArgumentParser(description="預跑 daily_picks(default params)")
    p.add_argument(
        "--date",
        help="目標日期 YYYY-MM-DD;留空 = SQLite 內 daily_prices MAX(date)",
    )
    args = p.parse_args()

    db.init_db()
    # GitHub Actions runner fresh container,要 preload snapshot CSV 確保有歷史
    preload = db.preload_snapshots()
    if preload:
        print(f"[PRECOMPUTE] preload snapshots: {preload}", flush=True)

    if args.date:
        target = args.date
    else:
        target = db.get_latest_trading_date()
        if not target:
            from datetime import date as _date
            target = _date.today().isoformat()
            print(
                f"[PRECOMPUTE] 警告:SQLite 無歷史,用 today={target}",
                flush=True,
            )

    results = precompute_for_date(target)
    total = sum(results.values())
    print(f"[PRECOMPUTE] DONE total_rows={total}", flush=True)
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
