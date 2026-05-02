"""把 8 個 backfill shard 的 CSV 合併成單一 daily_prices.csv / institutional.csv。

跑法:
    python scripts/aggregate_shards.py --total-shards 8

流程:
  1. 讀整份既有 data/twse_snapshot/daily_prices.csv(若有)當底
  2. 讀 8 個 daily_prices_shard_*.csv concat 上去
  3. drop_duplicates(stock_id, date) keep='last' → 寫回 daily_prices.csv
     (last 因為 shard CSV 是「本次抓的最新值」,優先於既有底)
  4. institutional 同邏輯
  5. 從 SQLite dump stocks.csv / watchlist.csv(在 aggregator job 跑 daily_fetch
     之後,SQLite 才有完整 stocks 表)
  6. 寫 last_backfill.txt(統計 8 shard 加總)
  7. 刪掉 shard csv / last_backfill_shard_*.txt(repo 不留 transient 檔)

設計取捨:
  - 用純 pandas 操作不過 SQLite(daily_prices / institutional 部分),避免依賴
    aggregator 的 SQLite 狀態 — shard 的成果在 CSV 裡,純文字 merge 最直觀
  - stocks.csv / watchlist.csv 仍走 SQLite(因為要 dump 排序 + name/industry,
    SQLite 是 source of truth — aggregator 跑 daily_fetch 之後 stocks 表會齊)

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

from src import database as db  # noqa: E402

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"


def _read_csv_safe(path: Path) -> pd.DataFrame:
    """讀 CSV,檔不存在或為空回空 DataFrame(不拋例外)。"""
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, dtype={"stock_id": str})
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    return df


def _merge_dedup(
    base_df: pd.DataFrame,
    shard_dfs: list[pd.DataFrame],
    key_cols: list[str],
) -> pd.DataFrame:
    """concat base + shards,以 key_cols 去重(keep='last' = shard 蓋過 base)。"""
    parts = [df for df in [base_df] + shard_dfs if not df.empty]
    if not parts:
        return pd.DataFrame()
    merged = pd.concat(parts, ignore_index=True)
    merged = merged.drop_duplicates(subset=key_cols, keep="last")
    return merged


def aggregate_daily_prices(total_shards: int) -> tuple[int, int]:
    """合併 daily_prices_shard_*.csv → daily_prices.csv。

    回 (shard 行數總和, 合併後總行數)。
    """
    base = _read_csv_safe(SNAPSHOT_DIR / "daily_prices.csv")
    base_n = len(base)
    shard_dfs: list[pd.DataFrame] = []
    shard_total = 0
    for k in range(total_shards):
        path = SNAPSHOT_DIR / f"daily_prices_shard_{k}.csv"
        df = _read_csv_safe(path)
        shard_total += len(df)
        if not df.empty:
            shard_dfs.append(df)

    merged = _merge_dedup(base, shard_dfs, key_cols=["stock_id", "date"])
    if not merged.empty:
        merged = merged.sort_values(["stock_id", "date"], kind="stable")
    out = SNAPSHOT_DIR / "daily_prices.csv"
    merged.to_csv(out, index=False)
    print(
        f"[AGGREGATE] daily_prices.csv: 既有 {base_n} 行 + shard {shard_total} 行 "
        f"→ 去重後 {len(merged)} 行",
        flush=True,
    )
    return shard_total, len(merged)


def aggregate_institutional(total_shards: int) -> tuple[int, int]:
    """合併 institutional_shard_*.csv → institutional.csv。"""
    base = _read_csv_safe(SNAPSHOT_DIR / "institutional.csv")
    base_n = len(base)
    shard_dfs: list[pd.DataFrame] = []
    shard_total = 0
    for k in range(total_shards):
        path = SNAPSHOT_DIR / f"institutional_shard_{k}.csv"
        df = _read_csv_safe(path)
        shard_total += len(df)
        if not df.empty:
            shard_dfs.append(df)

    merged = _merge_dedup(base, shard_dfs, key_cols=["stock_id", "date"])
    if not merged.empty:
        merged = merged.sort_values(["stock_id", "date"], kind="stable")
    out = SNAPSHOT_DIR / "institutional.csv"
    merged.to_csv(out, index=False)
    print(
        f"[AGGREGATE] institutional.csv: 既有 {base_n} 行 + shard {shard_total} 行 "
        f"→ 去重後 {len(merged)} 行",
        flush=True,
    )
    return shard_total, len(merged)


def dump_stocks_csv() -> int:
    """從 SQLite dump stocks.csv(name + industry)。aggregator 跑 daily_fetch
    後 stocks 表會齊,從 SQLite 拉最準。
    """
    with db.get_conn() as conn:
        df = pd.read_sql(
            "SELECT stock_id, name, industry FROM stocks "
            "WHERE market='TW' ORDER BY stock_id",
            conn,
        )
    path = SNAPSHOT_DIR / "stocks.csv"
    df.to_csv(path, index=False)
    print(f"[AGGREGATE] stocks.csv: {len(df)} 行", flush=True)
    return len(df)


def dump_watchlist_csv() -> int:
    """從 SQLite dump watchlist.csv;空就保留既有(別 clobber)。"""
    items = db.get_watchlist()
    path = SNAPSHOT_DIR / "watchlist.csv"
    if not items:
        if path.exists():
            print(
                f"[AGGREGATE] watchlist 為空,保留既有 {path.name}",
                flush=True,
            )
        return 0
    df = pd.DataFrame([
        {
            "stock_id": it["stock_id"],
            "added_at": it["added_at"],
            "note": it.get("note"),
        }
        for it in items
    ])
    df.to_csv(path, index=False)
    print(f"[AGGREGATE] watchlist.csv: {len(df)} 行", flush=True)
    return len(df)


def aggregate_last_backfill(total_shards: int) -> dict[str, int]:
    """把 8 個 last_backfill_shard_*.txt 加總成單一 last_backfill.txt。"""
    totals = {
        "todo": 0, "price_ok": 0, "price_fail": 0,
        "inst_ok": 0, "inst_fail": 0,
    }
    elapsed_max = 0.0
    shards_seen = 0
    backfilled_at = ""
    git_sha = ""
    run_id = ""
    days_requested = ""
    for k in range(total_shards):
        path = SNAPSHOT_DIR / f"last_backfill_shard_{k}.txt"
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
            elif key == "days_requested" and not days_requested:
                days_requested = val

    success_rate = (
        totals["price_ok"] / totals["todo"] * 100
        if totals["todo"] > 0 else 0.0
    )
    out = SNAPSHOT_DIR / "last_backfill.txt"
    out.write_text(
        f"backfilled_at={backfilled_at}\n"
        f"git_sha={git_sha}\n"
        f"run_id={run_id}\n"
        f"shards_completed={shards_seen}/{total_shards}\n"
        f"days_requested={days_requested}\n"
        f"todo={totals['todo']}\n"
        f"price_ok={totals['price_ok']}\n"
        f"price_fail={totals['price_fail']}\n"
        f"price_success_rate_pct={success_rate:.1f}\n"
        f"inst_ok={totals['inst_ok']}\n"
        f"inst_fail={totals['inst_fail']}\n"
        f"elapsed_min_max={elapsed_max:.1f}\n",
        encoding="utf-8",
    )
    print(
        f"[AGGREGATE] last_backfill.txt: shards {shards_seen}/{total_shards}, "
        f"price ok={totals['price_ok']} fail={totals['price_fail']} "
        f"({success_rate:.1f}%)",
        flush=True,
    )
    return totals


def cleanup_shard_files(total_shards: int) -> int:
    """刪掉 shard 暫存 csv / txt(repo 不留 transient 檔)。回刪掉的檔數。"""
    deleted = 0
    for k in range(total_shards):
        for name in (
            f"daily_prices_shard_{k}.csv",
            f"institutional_shard_{k}.csv",
            f"last_backfill_shard_{k}.txt",
        ):
            path = SNAPSHOT_DIR / name
            if path.exists():
                path.unlink()
                deleted += 1
    if deleted:
        print(f"[AGGREGATE] 清掉 {deleted} 個 shard 暫存檔", flush=True)
    return deleted


def main() -> int:
    p = argparse.ArgumentParser(
        description="合併 backfill shard 的 CSV 成單一 daily_prices.csv / institutional.csv",
    )
    p.add_argument(
        "--total-shards", type=int, default=8,
        help="幾個 shard(預設 8,跟 backfill workflow matrix 對齊)",
    )
    p.add_argument(
        "--no-cleanup", action="store_true",
        help="不刪 shard 暫存檔(debug 用)",
    )
    p.add_argument(
        "--no-stocks-watchlist", action="store_true",
        help="跳過 stocks.csv / watchlist.csv dump(SQLite 沒 init 時用)",
    )
    args = p.parse_args()

    if args.total_shards < 1:
        print("❌ --total-shards 必須 >= 1", flush=True)
        return 2

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # 確認至少有一個 shard csv 存在(否則 aggregator 沒事可做)
    any_shard_csv = any(
        (SNAPSHOT_DIR / f"daily_prices_shard_{k}.csv").exists()
        for k in range(args.total_shards)
    )
    if not any_shard_csv:
        print(
            f"❌ 找不到任何 daily_prices_shard_*.csv (0 ~ {args.total_shards-1})"
            f" — 8 個 shard 是不是都 fail?",
            flush=True,
        )
        return 1

    aggregate_daily_prices(args.total_shards)
    aggregate_institutional(args.total_shards)

    if not args.no_stocks_watchlist:
        db.init_db()
        dump_stocks_csv()
        dump_watchlist_csv()

    aggregate_last_backfill(args.total_shards)

    if not args.no_cleanup:
        cleanup_shard_files(args.total_shards)

    print("[AGGREGATE] 完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
