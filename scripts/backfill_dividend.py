"""排程入口:分 shard 抓全市場 dividend 近 5 年資料(週跑一次)。

dividend 是年級資料(每年除權息一次),不需要每天 fetch。週日 22:13 台北 一輪
(`.github/workflows/backfill-dividend.yml`)就夠覆蓋全市場。

架構(類似 backfill_history 的 8-way matrix):
  - 每個 shard 跑 universe 內 ~250 檔(2060/8),每檔 fetch_dividend 抓近 5 年
  - dump 自己 shard 的 dividend_shard_K.csv 到 data/twse_snapshot/
  - aggregator job 合併 8 shard CSV → dividend.csv,清掉 shard 暫存

CLI:
    python scripts/backfill_dividend.py --shard 0 --total-shards 8

    # 限定前 N 檔(debug)
    python scripts/backfill_dividend.py --shard 0 --total-shards 8 --limit 10

    # 修改 lookback 年數(default 5,長線連續配息檢查需要 5 年)
    python scripts/backfill_dividend.py --shard 0 --total-shards 8 --years 5

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
    FinMindAPIError, FinMindQuotaError, fetch_dividend,
)
from src.universe import pure_stock_universe  # noqa: E402

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"


def _shard_filter(
    universe: list[str], shard: int, total_shards: int,
) -> list[str]:
    """穩定均勻切片:sorted(universe)[shard::total_shards]。

    跟 backfill_history.py 同邏輯 — stride 切法保留 stock_id 分布,跨 run 一致。
    """
    return sorted(universe)[shard::total_shards]


def _preload_dividend_csv() -> None:
    """若 SNAPSHOT_DIR/dividend.csv 存在,先載入 SQLite。

    讓 sync_log 反映既有資料 → 避免每次 backfill 都重打 FinMind。
    """
    path = SNAPSHOT_DIR / "dividend.csv"
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
        year_v = r.get("year")
        try:
            year_int = int(year_v) if pd.notna(year_v) else None
        except (TypeError, ValueError):
            year_int = None
        if year_int is None:
            continue
        rows.append({
            "stock_id": str(r["stock_id"]),
            "year": year_int,
            "cash_dividend": (
                float(r["cash_dividend"])
                if pd.notna(r.get("cash_dividend")) else 0
            ),
            "stock_dividend": (
                float(r["stock_dividend"])
                if pd.notna(r.get("stock_dividend")) else 0
            ),
            "ex_dividend_date": (
                str(r["ex_dividend_date"])
                if pd.notna(r.get("ex_dividend_date")) else None
            ),
        })
    if rows:
        db.upsert_dividend(rows)
        print(
            f"[BACKFILL-DIV] 預先載入 {len(rows)} 筆 dividend.csv 進 SQLite",
            flush=True,
        )


def _dump_shard_csv(shard: int, todo_ids: list[str]) -> None:
    """Dump 此 shard 對 todo_ids 範圍內的 dividend rows 為 CSV。

    跟 backfill_history._dump_shard_csvs 同模式;沒 todo 寫空 csv 確保 aggregate
    永遠看到 8 個檔。
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SNAPSHOT_DIR / f"dividend_shard_{shard}.csv"
    if not todo_ids:
        empty = pd.DataFrame(
            columns=[
                "stock_id", "year", "cash_dividend",
                "stock_dividend", "ex_dividend_date",
            ],
        )
        empty.to_csv(out_path, index=False)
        print(
            f"[BACKFILL-DIV] shard {shard}: 無待補,寫空 shard csv",
            flush=True,
        )
        return
    placeholders = ",".join(["?"] * len(todo_ids))
    with db.get_conn() as conn:
        df = pd.read_sql(
            f"SELECT * FROM dividend WHERE stock_id IN ({placeholders}) "
            f"ORDER BY stock_id, year",
            conn, params=todo_ids,
        )
    df.to_csv(out_path, index=False)
    print(f"[BACKFILL-DIV] 寫 {out_path.name}: {len(df)} 行", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(
        description="分 shard 抓全市場 dividend 近 5 年資料",
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
        help="抓近幾年配息(default 5,長線連續配息條件需要)",
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
    _preload_dividend_csv()

    universe = pure_stock_universe(min_history=20)
    if not universe:
        print(
            "[BACKFILL-DIV] universe 為空 — 先跑 daily_fetch.py 初始化 stocks",
            flush=True,
        )
        return 1

    full_n = len(universe)
    universe = _shard_filter(universe, args.shard, args.total_shards)
    print(
        f"[BACKFILL-DIV] shard {args.shard}/{args.total_shards}: "
        f"{len(universe)} / {full_n} 檔",
        flush=True,
    )

    if args.limit:
        universe = universe[: args.limit]

    today = date.today().isoformat()
    start = (date.today() - timedelta(days=args.years * 365 + 30)).isoformat()
    print(
        f"[BACKFILL-DIV] universe={len(universe)},範圍 {start}~{today}",
        flush=True,
    )

    # 記錄 pre-call dividend 表行數,結束時算 delta(真實寫入 row 數)
    # 比單純看 ok/fail 更能反映「真的有抓到資料」(strict=True 後 ok 等於
    # 有 API call 成功的檔數;但成功 call 也可能 0 rows)。
    with db.get_conn() as conn:
        pre_rows = conn.execute("SELECT COUNT(*) FROM dividend").fetchone()[0]

    n = len(universe)
    ok = fail = quota_fail = empty = 0
    t0 = time.time()
    for i, sid in enumerate(universe, start=1):
        try:
            df = fetch_dividend(sid, start, today, strict=True)
        except FinMindQuotaError as e:
            quota_fail += 1
            fail += 1
            if quota_fail <= 3:
                print(
                    f"[BACKFILL-DIV] {sid} quota 爆: {e}",
                    flush=True,
                )
            # 連續 quota 失敗 → fail-fast 早結束(再跑也只是消耗 runner 時鐘)
            if quota_fail >= 5:
                print(
                    f"[BACKFILL-DIV] 連續 {quota_fail} 次 quota 爆 — "
                    f"提前結束此 shard,避免占用 runner 配額",
                    flush=True,
                )
                break
        except FinMindAPIError as e:
            fail += 1
            if fail <= 5:
                print(f"[BACKFILL-DIV] {sid} fail: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            fail += 1
            if fail <= 5:
                print(
                    f"[BACKFILL-DIV] {sid} fail: {type(e).__name__}: {e}",
                    flush=True,
                )
        else:
            ok += 1
            # 沒 raise 但回空 df = API 沒這檔資料(可能正常,如 ETF / 新 IPO)
            if df.empty:
                empty += 1

        if i % 50 == 0 or i == n:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (n - i) / rate / 60 if rate > 0 else 0
            print(
                f"[BACKFILL-DIV] {i}/{n} (ok={ok} empty={empty} "
                f"fail={fail} quota={quota_fail}), "
                f"{rate:.2f}/s, ETA {eta:.1f} min",
                flush=True,
            )

    with db.get_conn() as conn:
        post_rows = conn.execute("SELECT COUNT(*) FROM dividend").fetchone()[0]
    delta_rows = post_rows - pre_rows

    elapsed = time.time() - t0
    print(
        f"[BACKFILL-DIV DONE] shard {args.shard}/{args.total_shards} "
        f"共 {n} 檔,ok={ok} empty={empty} fail={fail} quota={quota_fail},"
        f"SQLite dividend 表 +{delta_rows} rows(pre={pre_rows} → post={post_rows})"
        f",耗時 {elapsed/60:.1f} 分鐘",
        flush=True,
    )

    # Dump shard CSV(包含 preload 進來的既有資料 + 本輪新抓的 → aggregate 去重)
    _dump_shard_csv(args.shard, universe)

    # 寫 shard timestamp 給 aggregator 統計用
    import os
    from datetime import datetime, timezone
    (SNAPSHOT_DIR / f"last_dividend_shard_{args.shard}.txt").write_text(
        f"backfilled_at={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"git_sha={os.environ.get('GITHUB_SHA', 'local')}\n"
        f"run_id={os.environ.get('GITHUB_RUN_ID', 'local')}\n"
        f"shard={args.shard}\n"
        f"total_shards={args.total_shards}\n"
        f"years={args.years}\n"
        f"todo={n}\n"
        f"ok={ok}\n"
        f"empty={empty}\n"
        f"fail={fail}\n"
        f"quota_fail={quota_fail}\n"
        f"delta_rows={delta_rows}\n"
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
    # 額外守護:若 ok 看似正常但 SQLite 完全沒新 row 入(全部 empty df)→ 也算 silent fail
    # 2026-05-18 bug:ok=2060 / delta_rows=0 全市場滲漏。dividends 普及程度高,
    # delta_rows=0 + ok 很大幾乎等於系統壞。例外:全 universe 之前都已 cache → 此時
    # 也合法,所以 gate 加 pre_rows<100 條件避免誤殺週週重跑。
    if delta_rows == 0 and ok > 100 and pre_rows < 100:
        print(
            f"❌ ok={ok} 但 SQLite dividend 一筆都沒寫進去(pre={pre_rows} → "
            f"post={post_rows}),疑似 quota / dataset access 問題 — exit 1",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
