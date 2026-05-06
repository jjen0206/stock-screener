"""排程入口:分 shard 抓全市場 monthly_revenue 近 5 年資料(週跑一次)。

monthly_revenue = 月營收(每月 10 號前公布),週跑一次足夠覆蓋全市場。
週一 14:13 UTC = 22:13 台北 一輪(`.github/workflows/backfill-revenue.yml`)。

架構(完全 mirror backfill_dividend.py / backfill_financials.py 的 8-way matrix):
  - 每 shard 跑 universe 內 ~250 檔(2060/8),每檔 fetch_monthly_revenue 抓近 5 年
  - dump 自己 shard 的 revenue_shard_K.csv 到 data/twse_snapshot/
  - aggregator job 合併 8 shard CSV → monthly_revenue.csv,清掉 shard 暫存

CLI:
    python scripts/backfill_revenue.py --shard 0 --total-shards 8

    # 限定前 N 檔(debug)
    python scripts/backfill_revenue.py --shard 0 --total-shards 8 --limit 10

    # 修改 lookback 年數(default 5)
    python scripts/backfill_revenue.py --shard 0 --total-shards 8 --years 5

Exit code:
  0 = 成功(包含部分失敗,但有 commit shard CSV)
  1 = 全部失敗(可能 FinMind quota 或網路掛)
  2 = 參數錯誤
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.data_fetcher import (  # noqa: E402
    FinMindAPIError, fetch_monthly_revenue,
)
from src.universe import pure_stock_universe  # noqa: E402

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"


def _shard_filter(
    universe: list[str], shard: int, total_shards: int,
) -> list[str]:
    """穩定均勻切片:sorted(universe)[shard::total_shards]。"""
    return sorted(universe)[shard::total_shards]


def _preload_revenue_csv() -> None:
    """若 SNAPSHOT_DIR/monthly_revenue.csv 存在,先載入 SQLite。

    讓 sync_log 反映既有資料 → 避免每次 backfill 都重打 FinMind。
    """
    path = SNAPSHOT_DIR / "monthly_revenue.csv"
    if not path.exists():
        return
    try:
        df = pd.read_csv(path, dtype={"stock_id": str})
    except pd.errors.EmptyDataError:
        return
    if df.empty:
        return
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "stock_id": str(r["stock_id"]),
            "period_type": "monthly_revenue",
            "period": str(r["period"]),
            "revenue": float(r["revenue"]) if pd.notna(r.get("revenue")) else None,
            "revenue_yoy": (
                float(r["revenue_yoy"]) if pd.notna(r.get("revenue_yoy")) else None
            ),
            "eps": None,
            "roe": None,
        })
    if rows:
        db.upsert_financials(rows)
        print(
            f"[BACKFILL-REV] 預先載入 {len(rows)} 筆 monthly_revenue.csv 進 SQLite",
            flush=True,
        )


def _dump_shard_csv(shard: int, todo_ids: list[str]) -> None:
    """Dump 此 shard 對 todo_ids 範圍內 monthly_revenue rows 為 CSV。"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SNAPSHOT_DIR / f"revenue_shard_{shard}.csv"
    cols = ["stock_id", "period", "revenue", "revenue_yoy"]
    if not todo_ids:
        empty = pd.DataFrame(columns=cols)
        empty.to_csv(out_path, index=False)
        print(
            f"[BACKFILL-REV] shard {shard}: 無待補,寫空 shard csv",
            flush=True,
        )
        return
    placeholders = ",".join(["?"] * len(todo_ids))
    with db.get_conn() as conn:
        df = pd.read_sql(
            f"SELECT stock_id, period, revenue, revenue_yoy "
            f"FROM financials "
            f"WHERE stock_id IN ({placeholders}) AND period_type='monthly_revenue' "
            f"ORDER BY stock_id, period",
            conn, params=todo_ids,
        )
    df.to_csv(out_path, index=False)
    print(f"[BACKFILL-REV] 寫 {out_path.name}: {len(df)} 行", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(
        description="分 shard 抓全市場 monthly_revenue 近 5 年資料",
    )
    p.add_argument(
        "--shard", type=int, required=True,
        help="此 process 負責的 shard 編號(0-based)",
    )
    p.add_argument(
        "--total-shards", type=int, default=8,
        help="universe 切成幾片並發跑(default 8)",
    )
    p.add_argument(
        "--years", type=int, default=5,
        help="抓近幾年月營收(default 5)",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="只跑前 N 檔(debug 用)",
    )
    args = p.parse_args()

    if args.total_shards < 1:
        print("❌ --total-shards 必須 >= 1", flush=True)
        return 2
    if not (0 <= args.shard < args.total_shards):
        print(
            f"❌ --shard {args.shard} 必須在 [0, {args.total_shards}) 範圍內",
            flush=True,
        )
        return 2

    db.init_db()
    _preload_revenue_csv()

    universe = pure_stock_universe(min_history=20)
    if not universe:
        print(
            "[BACKFILL-REV] universe 為空 — 先跑 daily_fetch.py 初始化 stocks",
            flush=True,
        )
        return 1

    full_n = len(universe)
    universe = _shard_filter(universe, args.shard, args.total_shards)
    print(
        f"[BACKFILL-REV] shard {args.shard}/{args.total_shards}: "
        f"{len(universe)} / {full_n} 檔",
        flush=True,
    )

    if args.limit:
        universe = universe[: args.limit]

    today = date.today().isoformat()
    start = (date.today() - timedelta(days=args.years * 365 + 30)).isoformat()
    print(
        f"[BACKFILL-REV] universe={len(universe)},範圍 {start}~{today}",
        flush=True,
    )

    n = len(universe)
    ok = fail = 0
    t0 = time.time()
    for i, sid in enumerate(universe, start=1):
        try:
            fetch_monthly_revenue(sid, start, today)
            ok += 1
        except FinMindAPIError as e:
            fail += 1
            if fail <= 5:
                print(f"[BACKFILL-REV] {sid} fail: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            fail += 1
            if fail <= 5:
                print(
                    f"[BACKFILL-REV] {sid} fail: {type(e).__name__}: {e}",
                    flush=True,
                )

        if i % 50 == 0 or i == n:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (n - i) / rate / 60 if rate > 0 else 0
            print(
                f"[BACKFILL-REV] {i}/{n} (ok={ok} fail={fail}), "
                f"{rate:.2f}/s, ETA {eta:.1f} min",
                flush=True,
            )

    elapsed = time.time() - t0
    print(
        f"[BACKFILL-REV DONE] shard {args.shard}/{args.total_shards} "
        f"共 {n} 檔,ok={ok} fail={fail},耗時 {elapsed/60:.1f} 分鐘",
        flush=True,
    )

    _dump_shard_csv(args.shard, universe)

    import os
    from datetime import datetime, timezone
    (SNAPSHOT_DIR / f"last_revenue_shard_{args.shard}.txt").write_text(
        f"backfilled_at={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"git_sha={os.environ.get('GITHUB_SHA', 'local')}\n"
        f"run_id={os.environ.get('GITHUB_RUN_ID', 'local')}\n"
        f"shard={args.shard}\n"
        f"total_shards={args.total_shards}\n"
        f"years={args.years}\n"
        f"todo={n}\n"
        f"ok={ok}\n"
        f"fail={fail}\n"
        f"elapsed_min={elapsed/60:.1f}\n",
        encoding="utf-8",
    )

    if n == 0:
        return 0
    success_rate = ok / n * 100
    if success_rate < 10:
        print(
            f"❌ 成功率 {success_rate:.0f}% < 10%(可能 FinMind 大故障 / quota)"
            f" — exit 1",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
