"""一次性回補全市場 90 天 daily_price + institutional 歷史。

背景:
  TWSE bulk endpoint 每天只回當日 1 筆 OHLCV → cache 累積要等 1-2 個月。
  短線策略需要 14-60 天歷史(MA60 / KD9 / 5 日均量),沒歷史 → 全部 skip → 0 入選。
  Streamlit Cloud 自己 IP 被 TWSE 擋,不能跑這個 backfill,只能在 GH Actions 跑。

Swing 5y backfill 模式(2026-05-19 Phase 0c-B):
  `python scripts/backfill_history.py --days 1825 --min-existing 1260`
  - 1825 天 ≈ 5 年(含週末/假日 buffer);1260 是純交易日,當 min_existing 閾值
  - 預期跑 ~30-90 min / shard(8-shard 並發共用同 FinMind token,慢於線性 8 倍速)
  - **建議排程**:FinMind quota 6/1 09:00 重置後啟動 — 5y backfill 會吃滿 ~1 hr 配額
  - 詳見 `docs/swing_backfill_runbook.md`

流程:
  1. 列舉全市場 universe(TWSE + TPEx,~2700 檔)
  2. (shard 模式)依 sorted index 切成 N 份,只跑自己的 shard
  3. 對 daily_prices 不足 N 天的個股呼叫 fetch_daily_price(via FinMind)
  4. 對 watchlist + TW_TOP_50 補 institutional
  5. dump 到 data/twse_snapshot/(shard 模式 → daily_prices_shard_K.csv;
     非 shard 模式 → daily_prices.csv 整份)
  6. workflow 自動 git commit + push CSV → Streamlit Cloud git pull → 啟動時讀回

時間預估:
  - FinMind 免費 token 限 600/小時(老 token 1500),per call ~0.5 秒
  - 2700 檔 × 0.6 秒 ≈ 27 分鐘(理想)
  - 受限額時 GH Actions 會 sleep,實測 30-45 分鐘
  - 8 shard 並發共用同一 token → 限額仍 per-token 觸發,效益主要在「跨日累積」+
    long-backoff 等限額窗口 reset,不是線性 8 倍速

Shard 模式(--shard K --total-shards N):
  - universe 排序後均勻切 N 份(`sorted_uni[K::N]`)
  - 只跑自己 shard 的 stock_id
  - 只 dump 自己 shard 的成果(daily_prices_shard_K.csv 等);stocks.csv /
    watchlist.csv 由後續 aggregate_shards.py 統一處理
  - last_backfill_shard_K.txt 含 shard 統計

Exit code:
  0 = 至少 50% 成功
  1 = 失敗 > 50%(可能是 token 過期 / FinMind 大故障)
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
    FinMindAPIError,
    fetch_daily_price,
    fetch_institutional,
)
from src.universe import TW_TOP_50, get_full_universe, load_watchlist  # noqa: E402

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"


def _shard_filter(
    universe: list[str], shard: int, total_shards: int,
) -> list[str]:
    """穩定均勻切片:sorted(universe)[shard::total_shards]。

    - 排序穩定,跨 run 切片一致
    - stride 切法保留 stock_id 在 universe 內的相對分布(避免某 shard 全是熱門股)
    - 不依賴 hash(stock_id),不受 PYTHONHASHSEED 影響
    """
    return sorted(universe)[shard::total_shards]


def _preload_watchlist_csv() -> None:
    """若 SNAPSHOT_DIR/watchlist.csv 存在,先載入 SQLite(防最後 dump 寫空)。"""
    path = SNAPSHOT_DIR / "watchlist.csv"
    if not path.exists():
        return
    df = pd.read_csv(path, dtype={"stock_id": str})
    for _, r in df.iterrows():
        sid = str(r["stock_id"])
        if not sid:
            continue
        note_val = r.get("note")
        note = (
            None if note_val is None or pd.isna(note_val)
            else str(note_val)
        )
        db.add_to_watchlist(sid, note=note)
    print(
        f"[BACKFILL] 預先載入 {len(df)} 筆 watchlist.csv 進 SQLite",
        flush=True,
    )


def _csv_records_nan_to_none(df: pd.DataFrame) -> list[dict]:
    """把 DataFrame 轉 records,並把 NaN 替換成 None(SQLite 不接受 NaN)。"""
    records = df.to_dict("records")
    for r in records:
        for k, v in list(r.items()):
            if pd.isna(v):
                r[k] = None
    return records


def _preload_daily_prices_csv() -> None:
    """讀回 SNAPSHOT_DIR/daily_prices.csv 進 SQLite。

    解決 cross-run checkpoint 失效:GH Actions runner 每次都是新的空 SQLite,
    若不先把上一輪 commit 進 repo 的 CSV 讀回,existing_counts 會永遠 0、
    --min-existing 過濾失效,導致每次都全市場 todo 全跑。

    Shard 模式下也讀整份 daily_prices.csv(包含其他 shard 上次 commit 的成果),
    讓 min_existing 跨 shard 共享 — 自己 shard 的 todo 才不會被別 shard 的成果
    重複跑。
    """
    path = SNAPSHOT_DIR / "daily_prices.csv"
    if not path.exists():
        return
    df = pd.read_csv(path, dtype={"stock_id": str})
    if df.empty:
        return
    records = _csv_records_nan_to_none(df)
    db.upsert_daily_prices(records)
    n_stocks = df["stock_id"].nunique()
    print(
        f"[PRELOAD] 從 daily_prices.csv 讀回 {len(df)} 筆 / {n_stocks} 檔股票",
        flush=True,
    )


def _preload_institutional_csv() -> None:
    """讀回 SNAPSHOT_DIR/institutional.csv 進 SQLite(同 daily_prices 的 cross-run preload)。"""
    path = SNAPSHOT_DIR / "institutional.csv"
    if not path.exists():
        return
    df = pd.read_csv(path, dtype={"stock_id": str})
    if df.empty:
        return
    records = _csv_records_nan_to_none(df)
    db.upsert_institutional(records)
    n_stocks = df["stock_id"].nunique()
    print(
        f"[PRELOAD] 從 institutional.csv 讀回 {len(df)} 筆 / {n_stocks} 檔股票",
        flush=True,
    )


def _dump_watchlist_csv() -> int:
    """從 SQLite dump watchlist 到 SNAPSHOT_DIR/watchlist.csv;回傳行數。"""
    items = db.get_watchlist()
    path = SNAPSHOT_DIR / "watchlist.csv"
    if not items:
        # 空就不寫(避免 clobber 既有 CSV — 雖然 _preload 應該已防止)
        if path.exists():
            print(
                f"[BACKFILL] watchlist 為空,保留既有 {path.name}",
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
    return len(df)


def main() -> int:
    p = argparse.ArgumentParser(
        description="一次性回補全市場 N 天歷史(daily_price + institutional)",
    )
    p.add_argument(
        "--days", type=int, default=90,
        help="回補幾天歷史(預設 90,至少要 60 才能跑 MA60 策略)",
    )
    p.add_argument(
        "--min-existing", type=int, default=30,
        help="既有 daily_prices >= 此數字就跳過該檔(預設 30,短線需求 10 日 + KD9 + buffer)",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="只跑前 N 檔(debug / 試水溫用)",
    )
    p.add_argument(
        "--no-institutional", action="store_true",
        help="跳過 institutional(只補 daily_prices,加速)",
    )
    p.add_argument(
        "--shard", type=int, default=None,
        help="此 process 負責的 shard 編號(0-based,搭配 --total-shards 用)",
    )
    p.add_argument(
        "--total-shards", type=int, default=None,
        help="universe 切成幾片並發跑(搭配 --shard 用)",
    )
    args = p.parse_args()

    # 驗證 shard 參數
    shard_mode = args.shard is not None or args.total_shards is not None
    if shard_mode:
        if args.shard is None or args.total_shards is None:
            print(
                "❌ --shard 跟 --total-shards 必須同時指定",
                flush=True,
            )
            return 2
        if not (0 <= args.shard < args.total_shards):
            print(
                f"❌ --shard {args.shard} 必須在 [0, {args.total_shards}) 範圍內",
                flush=True,
            )
            return 2
        if args.total_shards < 1:
            print("❌ --total-shards 必須 >= 1", flush=True)
            return 2

    # 5y 大 backfill 警示 — 主公手動觸發若不小心填 5000,提早 abort
    # 避免 emoji(Windows cp950 不吃,純文字保險)
    if args.days > 1000:
        print(
            f"[WARN] [BACKFILL] --days={args.days} > 1000(swing 5y 模式)。"
            f"預估每 shard 30-90 min,FinMind quota 可能撞牆 — "
            f"確認在 quota 重置窗口內。詳見 docs/swing_backfill_runbook.md",
            flush=True,
        )

    db.init_db()
    # Preload 既有 CSV → SQLite(GH runner cache.db 是空的,不 preload 的話:
    #   1. watchlist:最後 dump 會 clobber repo 既有 watchlist.csv
    #   2. daily_prices / institutional:existing_counts 永遠 0,--min-existing
    #      checkpoint 失效,每次跑都全市場 todo 全跑、累積 1167 → 永遠跑不滿
    # Shard 模式:照樣 preload 整份 — 跨 shard 共享 checkpoint,自己 shard 的 todo
    # 才會把別 shard 上次跑出的成果視為「已有」,避免重複工。
    _preload_watchlist_csv()
    _preload_daily_prices_csv()
    _preload_institutional_csv()
    universe = get_full_universe()
    if not universe:
        print("[BACKFILL] universe 為空 — 先跑 daily_fetch.py 初始化")
        return 1

    if shard_mode:
        full_n = len(universe)
        universe = _shard_filter(universe, args.shard, args.total_shards)
        print(
            f"[BACKFILL] shard {args.shard}/{args.total_shards}: "
            f"{len(universe)} / {full_n} 檔",
            flush=True,
        )

    # 抓既有歷史天數,過濾出待補清單
    with db.get_conn() as conn:
        existing_counts = {
            r["stock_id"]: r["cnt"]
            for r in conn.execute(
                "SELECT stock_id, COUNT(*) AS cnt "
                "FROM daily_prices GROUP BY stock_id"
            )
        }
    todo = [s for s in universe if existing_counts.get(s, 0) < args.min_existing]
    if args.limit:
        todo = todo[: args.limit]

    today = date.today().isoformat()
    start = (date.today() - timedelta(days=args.days)).isoformat()
    print(
        f"[BACKFILL] universe={len(universe)}, "
        f"已有 >={args.min_existing} 天 = {len(universe) - len(todo)},"
        f"待補 = {len(todo)},範圍 {start}~{today}",
        flush=True,
    )

    # 法人只補 watchlist + TW_TOP_50(不是全市場 — 受 token 限額,白費)
    # Shard 模式:只補「自己 shard 內 ∩ (TW_TOP_50 ∪ watchlist)」,避免 8 個 shard
    # 都對同 50+ 檔抓 institutional 重複工
    inst_target_set = set(s for s, _ in TW_TOP_50)
    for s, _ in load_watchlist():
        inst_target_set.add(s)
    if shard_mode:
        inst_target_set &= set(universe)

    n = len(todo)
    ok_price = ok_inst = fail_price = fail_inst = 0
    t0 = time.time()

    for i, sid in enumerate(todo, start=1):
        try:
            fetch_daily_price(sid, start, today)
            ok_price += 1
        except FinMindAPIError as e:
            fail_price += 1
            if fail_price <= 5:
                print(f"[BACKFILL] {sid} price fail: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            fail_price += 1
            if fail_price <= 5:
                print(
                    f"[BACKFILL] {sid} price fail: {type(e).__name__}: {e}",
                    flush=True,
                )

        if not args.no_institutional and sid in inst_target_set:
            try:
                fetch_institutional(sid, start, today)
                ok_inst += 1
            except Exception:  # noqa: BLE001
                fail_inst += 1

        if i % 50 == 0 or i == n:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (n - i) / rate / 60 if rate > 0 else 0
            print(
                f"[BACKFILL] {i}/{n} (price ok={ok_price} fail={fail_price}, "
                f"inst ok={ok_inst} fail={fail_inst}), "
                f"{rate:.2f}/s, ETA {eta:.1f} min",
                flush=True,
            )

    elapsed = time.time() - t0
    shard_label = (
        f"shard {args.shard}/{args.total_shards} " if shard_mode else ""
    )
    print(
        f"[BACKFILL DONE] {shard_label}共 {n} 檔,"
        f"price ok={ok_price} fail={fail_price},"
        f"inst ok={ok_inst} fail={fail_inst},耗時 {elapsed/60:.1f} 分鐘",
        flush=True,
    )

    # === Dump CSV → snapshot 給 Streamlit Cloud / aggregator 讀 ===
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    if shard_mode:
        _dump_shard_csvs(args.shard, todo)
    else:
        _dump_full_csvs()

    # 寫 backfill 專用 timestamp(shard 模式有自己 suffix)
    import os
    from datetime import datetime, timezone
    last_name = (
        f"last_backfill_shard_{args.shard}.txt"
        if shard_mode else "last_backfill.txt"
    )
    (SNAPSHOT_DIR / last_name).write_text(
        f"backfilled_at={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"git_sha={os.environ.get('GITHUB_SHA', 'local')}\n"
        f"run_id={os.environ.get('GITHUB_RUN_ID', 'local')}\n"
        f"shard={args.shard if shard_mode else 'none'}\n"
        f"total_shards={args.total_shards if shard_mode else 'none'}\n"
        f"days_requested={args.days}\n"
        f"todo={n}\n"
        f"price_ok={ok_price}\n"
        f"price_fail={fail_price}\n"
        f"inst_ok={ok_inst}\n"
        f"inst_fail={fail_inst}\n"
        f"elapsed_min={elapsed/60:.1f}\n",
        encoding="utf-8",
    )
    print(f"[BACKFILL] 寫 {last_name}", flush=True)

    # 退出邏輯三段:
    # - >= 50%:正常完成
    # - 10-50%:部分失敗(可能 FinMind 限額),仍 return 0 讓 workflow commit CSV
    # - < 10%:極可能 token 過期 / API 大故障,return 1 觸發 GH Actions 標紅
    if n == 0:
        return 0
    success_rate = ok_price / n * 100
    if success_rate < 10:
        print(
            f"❌ 成功率 {success_rate:.0f}% < 10%,極可能 token 過期 / "
            f"FinMind 大故障 — exit 1",
            flush=True,
        )
        return 1
    if success_rate < 50:
        print(
            f"⚠️ 成功率 {success_rate:.0f}% 偏低(可能 FinMind 限額),"
            f"但仍有 {ok_price} 檔 backfill 成功 — 照常 commit CSV",
            flush=True,
        )
    return 0


def _dump_full_csvs() -> None:
    """非 shard 模式:dump 整份 daily_prices.csv / institutional.csv /
    stocks.csv / watchlist.csv(原行為,保留 backward compatible)。
    """
    with db.get_conn() as conn:
        df = pd.read_sql(
            "SELECT * FROM daily_prices ORDER BY stock_id, date", conn,
        )
        path = SNAPSHOT_DIR / "daily_prices.csv"
        df.to_csv(path, index=False)
        print(f"[BACKFILL] 寫 {path.name}: {len(df)} 行", flush=True)

        df = pd.read_sql(
            "SELECT * FROM institutional ORDER BY stock_id, date", conn,
        )
        path = SNAPSHOT_DIR / "institutional.csv"
        df.to_csv(path, index=False)
        print(f"[BACKFILL] 寫 {path.name}: {len(df)} 行", flush=True)

        df = pd.read_sql(
            "SELECT stock_id, name, industry FROM stocks "
            "WHERE market='TW' ORDER BY stock_id",
            conn,
        )
        path = SNAPSHOT_DIR / "stocks.csv"
        df.to_csv(path, index=False)
        print(f"[BACKFILL] 寫 {path.name}: {len(df)} 行", flush=True)

    wl_n = _dump_watchlist_csv()
    if wl_n:
        print(f"[BACKFILL] 寫 watchlist.csv: {wl_n} 行", flush=True)


def _dump_shard_csvs(shard: int, todo_ids: list[str]) -> None:
    """Shard 模式:只 dump 此 shard 自己的 daily_prices_shard_K.csv /
    institutional_shard_K.csv。

    篩選邏輯:只 dump 「在 todo_ids 內」的個股 — 即此 shard 嘗試抓的個股。
    別的 shard preload 進來的資料不要 dump 出來(否則 aggregate 階段會看到
    8 份重複的同份 daily_prices.csv 內容)。

    aggregator 之後讀 8 個 shard csv + 既有 daily_prices.csv → merge → 產整份。
    stocks.csv / watchlist.csv 由 aggregator 統一處理(避免 8 個 shard 並發寫衝突)。
    """
    if not todo_ids:
        # 此 shard 沒有 todo(全部 checkpoint 命中)→ 寫空 csv 仍 OK,
        # 讓 aggregate 邏輯一致(永遠有 8 個 shard csv 可讀)
        empty = pd.DataFrame(
            columns=[
                "stock_id", "date", "open", "high", "low", "close",
                "volume", "trading_money", "trading_turnover", "spread",
            ],
        )
        empty.to_csv(SNAPSHOT_DIR / f"daily_prices_shard_{shard}.csv", index=False)
        empty_inst = pd.DataFrame(
            columns=[
                "stock_id", "date", "foreign_buy_sell", "trust_buy_sell",
                "dealer_buy_sell", "total_buy_sell",
            ],
        )
        empty_inst.to_csv(
            SNAPSHOT_DIR / f"institutional_shard_{shard}.csv", index=False,
        )
        print(
            f"[BACKFILL] shard {shard}: 無待補,寫空 shard csv",
            flush=True,
        )
        return

    placeholders = ",".join(["?"] * len(todo_ids))
    with db.get_conn() as conn:
        df = pd.read_sql(
            f"SELECT * FROM daily_prices WHERE stock_id IN ({placeholders}) "
            f"ORDER BY stock_id, date",
            conn, params=todo_ids,
        )
        path = SNAPSHOT_DIR / f"daily_prices_shard_{shard}.csv"
        df.to_csv(path, index=False)
        print(f"[BACKFILL] 寫 {path.name}: {len(df)} 行", flush=True)

        df = pd.read_sql(
            f"SELECT * FROM institutional WHERE stock_id IN ({placeholders}) "
            f"ORDER BY stock_id, date",
            conn, params=todo_ids,
        )
        path = SNAPSHOT_DIR / f"institutional_shard_{shard}.csv"
        df.to_csv(path, index=False)
        print(f"[BACKFILL] 寫 {path.name}: {len(df)} 行", flush=True)


if __name__ == "__main__":
    sys.exit(main())
