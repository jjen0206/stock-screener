"""把 8 個 backfill_revenue shard 的 CSV 合併成單一 monthly_revenue.csv。

跑法:
    python scripts/aggregate_revenue_shards.py --total-shards 8

流程:
  1. 讀整份既有 data/twse_snapshot/monthly_revenue.csv(若有)當底
  2. 讀 8 個 revenue_shard_*.csv concat 上去
  3. drop_duplicates(stock_id, period) keep='last' → 寫回 monthly_revenue.csv
  4. 統計 8 shard 加總成 last_revenue.txt
  5. 刪掉 shard csv / last_revenue_shard_*.txt(repo 不留 transient 檔)

key_cols=(stock_id, period)。

Exit code:
  0 = aggregate 成功
  1 = shard csv 一片都沒讀到(非預期 — 可能 8 個 shard 都 fail)
  2 = 參數錯誤
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"
_COLS = ["stock_id", "period", "revenue", "revenue_yoy"]


def _read_csv_safe(path: Path) -> pd.DataFrame:
    """讀 CSV,檔不存在或為空回空 DataFrame。"""
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype={"stock_id": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def aggregate_revenue(total_shards: int) -> tuple[int, int]:
    """合併 revenue_shard_*.csv → monthly_revenue.csv。

    回 (shard 行數總和, 合併後總行數)。
    """
    base = _read_csv_safe(SNAPSHOT_DIR / "monthly_revenue.csv")
    base_n = len(base)
    shard_dfs: list[pd.DataFrame] = []
    shard_total = 0
    for k in range(total_shards):
        path = SNAPSHOT_DIR / f"revenue_shard_{k}.csv"
        df = _read_csv_safe(path)
        shard_total += len(df)
        if not df.empty:
            shard_dfs.append(df)

    parts = [df for df in [base] + shard_dfs if not df.empty]
    if not parts:
        merged = pd.DataFrame(columns=_COLS)
    else:
        merged = pd.concat(parts, ignore_index=True)
        for c in _COLS:
            if c not in merged.columns:
                merged[c] = None
        merged = merged[_COLS]
        merged = merged.drop_duplicates(
            subset=["stock_id", "period"], keep="last",
        )
        merged = merged.sort_values(["stock_id", "period"], kind="stable")

    out = SNAPSHOT_DIR / "monthly_revenue.csv"
    merged.to_csv(out, index=False)
    print(
        f"[AGG-REV] monthly_revenue.csv: 既有 {base_n} 行 + shard {shard_total} 行 "
        f"→ 去重後 {len(merged)} 行",
        flush=True,
    )
    return shard_total, len(merged)


def aggregate_last_revenue(total_shards: int) -> dict[str, int]:
    """合 8 個 last_revenue_shard_*.txt 加總。"""
    totals = {
        "todo": 0, "ok": 0, "fail": 0,
        "quota_fail": 0, "delta_rows": 0,
    }
    elapsed_max = 0.0
    shards_seen = 0
    backfilled_at = ""
    git_sha = ""
    run_id = ""
    years = ""
    for k in range(total_shards):
        path = SNAPSHOT_DIR / f"last_revenue_shard_{k}.txt"
        if not path.exists():
            continue
        shards_seen += 1
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key in totals:
                try:
                    totals[key] += int(val)
                except ValueError:
                    pass
            elif key == "elapsed_min":
                try:
                    elapsed_max = max(elapsed_max, float(val))
                except ValueError:
                    pass
            elif key == "backfilled_at" and val > backfilled_at:
                backfilled_at = val
            elif key == "git_sha" and not git_sha:
                git_sha = val
            elif key == "run_id" and not run_id:
                run_id = val
            elif key == "years" and not years:
                years = val

    success_rate = (
        totals["ok"] / totals["todo"] * 100 if totals["todo"] > 0 else 0.0
    )
    out = SNAPSHOT_DIR / "last_revenue.txt"
    out.write_text(
        f"backfilled_at={backfilled_at}\n"
        f"git_sha={git_sha}\n"
        f"run_id={run_id}\n"
        f"shards_completed={shards_seen}/{total_shards}\n"
        f"years={years}\n"
        f"todo={totals['todo']}\n"
        f"ok={totals['ok']}\n"
        f"fail={totals['fail']}\n"
        f"quota_fail={totals['quota_fail']}\n"
        f"delta_rows={totals['delta_rows']}\n"
        f"success_rate_pct={success_rate:.1f}\n"
        f"elapsed_min_max={elapsed_max:.1f}\n",
        encoding="utf-8",
    )
    print(
        f"[AGG-REV] last_revenue.txt: shards {shards_seen}/{total_shards}, "
        f"ok={totals['ok']} fail={totals['fail']} quota={totals['quota_fail']} "
        f"delta_rows={totals['delta_rows']} ({success_rate:.1f}%)",
        flush=True,
    )
    return totals


def cleanup_shard_files(total_shards: int) -> int:
    """刪 shard 暫存 csv / txt。回刪掉的檔數。"""
    deleted = 0
    for k in range(total_shards):
        for name in (
            f"revenue_shard_{k}.csv",
            f"last_revenue_shard_{k}.txt",
        ):
            path = SNAPSHOT_DIR / name
            if path.exists():
                path.unlink()
                deleted += 1
    if deleted:
        print(f"[AGG-REV] 清掉 {deleted} 個 shard 暫存檔", flush=True)
    return deleted


def main() -> int:
    p = argparse.ArgumentParser(
        description="合併 backfill_revenue shard 的 CSV 成單一 monthly_revenue.csv",
    )
    p.add_argument(
        "--total-shards", type=int, default=8,
        help="幾個 shard(default 8,跟 backfill-revenue workflow matrix 對齊)",
    )
    p.add_argument(
        "--no-cleanup", action="store_true",
        help="不刪 shard 暫存檔(debug 用)",
    )
    args = p.parse_args()

    if args.total_shards < 1:
        print("❌ --total-shards 必須 >= 1", flush=True)
        return 2

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    any_shard_csv = any(
        (SNAPSHOT_DIR / f"revenue_shard_{k}.csv").exists()
        for k in range(args.total_shards)
    )
    if not any_shard_csv:
        print(
            f"❌ 找不到任何 revenue_shard_*.csv (0 ~ {args.total_shards-1})"
            f" — 8 shard 都 fail?",
            flush=True,
        )
        return 1

    aggregate_revenue(args.total_shards)
    aggregate_last_revenue(args.total_shards)

    if not args.no_cleanup:
        cleanup_shard_files(args.total_shards)

    print("[AGG-REV] 完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
