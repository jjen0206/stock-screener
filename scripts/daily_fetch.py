"""排程入口:全市場 bulk 抓價量 + 對 TOP_50 + watchlist 抓 institutional。

設計:
- daily_prices 用 TWSE/TPEx bulk endpoint(免 token + 一次拿全市場 ~2360 檔 < 30 秒)
- institutional **lazy load**:只對 TW_TOP_50 + watchlist (~70 檔) 抓
  避免 2360 檔 × FinMind = 燒爆 1500/小時 token 限額
- universe 順手 init(第一次跑會把全市場 stock_id 寫入 stocks 表)

GitHub Actions runner (Azure IP) 不被 TWSE 擋,所以 bulk endpoint 可用。

Exit code:0 = 跑完(部分失敗也算)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.data_fetcher import (  # noqa: E402
    fetch_all_daily_prices_bulk,
    fetch_daily_price,
    fetch_institutional,
    validate_daily_price_sanity,
)
from src.universe import TW_TOP_50, get_full_universe, load_watchlist  # noqa: E402

# 健康警戒線:bulk daily_prices 抓到少於這數量視為異常 → exit 1 讓 GH Actions 標紅
_MIN_BULK_ROWS_HEALTHY = 2000

# Bulk fetch sanity retry(2026-05-04 事件後加)— 若 TWSE OpenData 在跑的時候
# 還沒 publish 今日資料,sleep 後 retry。最多 3 次,間隔 5 分鐘。
_BULK_RETRY_MAX = 3
_BULK_RETRY_WAIT_SECS = 300


def _bulk_fetch_with_freshness_retry() -> "object":
    """Bulk fetch + sanity check:回傳 max(date) 必須比 SQLite 既有更新,否則 retry。

    給 GH Actions schedule 在 TWSE OpenData publication 緩衝期內跑時用 —
    2026-05-04 事件:fetch 5/5 00:11 台北跑時 TWSE OpenData 還在服務 4/30 舊
    資料,寫入了 2360 筆但全是既有 4/30 資料的 UPSERT 重寫,5/4 資料 0 筆。
    schedule 已從 22:13 改 02:30 台北給 publication 緩衝;sanity retry 是
    第二層保險。

    Cache TTL 60s < retry wait 300s,sleep 後再呼叫會 cache miss 真重抓。
    """
    import pandas as pd

    # SQLite 既有 max(個股 only,TAIEX 走別的 endpoint 跟個股不同步,排除)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM daily_prices WHERE stock_id != 'TAIEX'"
        ).fetchone()
    existing_max = row["d"] if row and row["d"] else None

    last_df: "pd.DataFrame" = pd.DataFrame()
    last_max: str | None = None
    for attempt in range(1, _BULK_RETRY_MAX + 1):
        df = fetch_all_daily_prices_bulk()
        last_df = df
        if df.empty:
            # 空 = TWSE OpenData 完全沒回應(連線斷 / endpoint 掛)— retry 也
            # 救不回來。直接回空,讓上層 caller 走 bulk_df.empty 既有 path。
            print(
                f"[FETCH] bulk attempt {attempt}/{_BULK_RETRY_MAX} 完全空,"
                f"不 retry(空 = endpoint 異常,sleep 也救不回)",
                flush=True,
            )
            return df

        last_max = str(df["date"].max())
        if existing_max is None or last_max > existing_max:
            print(
                f"[FETCH] bulk attempt {attempt}/{_BULK_RETRY_MAX}: "
                f"max={last_max} > existing={existing_max} ✅ 拿到新資料",
                flush=True,
            )
            return df

        # 有資料但 max 沒新 → 唯一該 sleep retry 的情境(2026-05-04 systemic bug)
        print(
            f"[FETCH] bulk attempt {attempt}/{_BULK_RETRY_MAX}: "
            f"max={last_max} <= existing={existing_max}(TWSE OpenData "
            f"可能還沒 publish 今日)",
            flush=True,
        )
        if attempt < _BULK_RETRY_MAX:
            print(
                f"[FETCH] sleep {_BULK_RETRY_WAIT_SECS}s 後 retry...",
                flush=True,
            )
            time.sleep(_BULK_RETRY_WAIT_SECS)

    # 全部 retry 完仍沒新資料 — 回最後一次 df 讓流程繼續(週末 / 假日 / TWSE
    # 異常 都會走這裡,不該 raise 阻斷 nightly)
    print(
        f"[FETCH] WARN: bulk {_BULK_RETRY_MAX} 次嘗試 max={last_max} 仍 <= "
        f"existing={existing_max} — 寫入既有 UPSERT(週末/假日/TWSE 異常)",
        flush=True,
    )
    return last_df


def run(institutional_days: int = 7) -> dict:
    """執行 daily 流程:全市場 bulk 價量 + 小批 institutional + watchlist 90 day。

    回完整 summary dict(含異常清單、DB size、各表 row 數)。
    """
    t_start = time.time()
    db.init_db()
    # GitHub Actions runner 是 fresh container,SQLite 空 → 先 preload snapshot
    # CSV(daily_prices ~130K 行歷史)避免短線篩選看到 cache 空 = 0 picks。
    # streamlit cloud boot 走 _load_snapshot_if_needed 也 reuse 同 helper。
    preload_counts = db.preload_snapshots()
    if preload_counts:
        print(f"[FETCH] preload snapshots: {preload_counts}", flush=True)
    db_path_before = config.PROJECT_ROOT / config.DATABASE_PATH
    size_before = (
        os.path.getsize(db_path_before) if db_path_before.exists() else 0
    )

    # 1. Init / refresh universe(全市場 ~2360 檔寫入 stocks 表)
    universe_sids = get_full_universe()
    print(f"[FETCH] universe = {len(universe_sids)} 檔(twse + tpex)", flush=True)

    # 2. Bulk 抓全市場 OHLCV(< 30 秒)+ freshness retry
    print("[FETCH] bulk 抓全市場 daily_prices...", flush=True)
    bulk_df = _bulk_fetch_with_freshness_retry()
    sanity_issues: list[tuple[str, str]] = []
    if bulk_df.empty:
        print("[FETCH] WARN: bulk 完全失敗,跳過 daily_prices 寫入", flush=True)
        bulk_rows = 0
    else:
        # 異常偵測:不阻擋寫入,但記錄到 summary
        sanity_issues = validate_daily_price_sanity(bulk_df)
        if sanity_issues:
            print(
                f"[FETCH] WARN: {len(sanity_issues)} 檔資料異常 — "
                f"前 5: {sanity_issues[:5]}",
                flush=True,
            )
        rows = bulk_df.to_dict("records")
        bulk_rows = db.upsert_daily_prices(rows)
        print(f"[FETCH] 寫入 {bulk_rows} 筆 daily_prices", flush=True)

    # 3. 對 TW_TOP_50 + watchlist 抓 institutional(lazy load 邏輯)
    inst_sids = set(s for s, _ in TW_TOP_50)
    for s, _ in load_watchlist():
        inst_sids.add(s)
    inst_sids_list = sorted(inst_sids)
    today = date.today().isoformat()
    start = (date.today() - timedelta(days=institutional_days)).isoformat()

    print(
        f"[FETCH] 對 {len(inst_sids_list)} 檔(TOP_50 + watchlist)抓 institutional "
        f"近 {institutional_days} 天...",
        flush=True,
    )
    inst_ok = 0
    inst_fail = 0
    n_inst = len(inst_sids_list)
    for i, sid in enumerate(inst_sids_list, start=1):
        try:
            fetch_institutional(sid, start, today)
            inst_ok += 1
        except Exception as e:  # noqa: BLE001
            inst_fail += 1
            if i <= 5:  # 只印前 5 個 fail 細節
                print(
                    f"[FETCH]   {sid} institutional fail: {type(e).__name__}",
                    flush=True,
                )
        if i % 10 == 0:
            print(f"[FETCH]   institutional {i}/{n_inst}...", flush=True)

    # 4. 對 watchlist 個股抓 90 天 daily_price 歷史(補 ATR/漲跌% 等需要歷史的指標)
    #    這個與 bulk 的差別:bulk 只給「當日 1 筆」,90 天才足以算 ATR(14)
    wl_sids = [s for s, _ in load_watchlist()]
    hist_ok = 0
    hist_fail = 0
    if wl_sids:
        hist_start = (date.today() - timedelta(days=90)).isoformat()
        print(
            f"[FETCH] 對 {len(wl_sids)} 檔 watchlist 抓 90 天 daily_price 歷史...",
            flush=True,
        )
        for i, sid in enumerate(wl_sids, start=1):
            try:
                fetch_daily_price(sid, hist_start, today)
                hist_ok += 1
            except Exception as e:  # noqa: BLE001
                hist_fail += 1
                if i <= 5:
                    print(
                        f"[FETCH]   {sid} 90 day fail: {type(e).__name__}",
                        flush=True,
                    )
            if i % 10 == 0:
                print(
                    f"[FETCH]   watchlist 90 day {i}/{len(wl_sids)}...",
                    flush=True,
                )
        print(
            f"[FETCH]   watchlist 90 day ok={hist_ok}/{len(wl_sids)}, "
            f"fail={hist_fail}",
            flush=True,
        )

    # === 詳細 SUMMARY ===
    elapsed = time.time() - t_start
    size_after = (
        os.path.getsize(db_path_before) if db_path_before.exists() else 0
    )
    delta_mb = (size_after - size_before) / 1e6

    table_counts: dict[str, int] = {}
    with db.get_conn() as conn:
        for table in ["daily_prices", "institutional", "stocks", "watchlist"]:
            try:
                cnt = conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table}"
                ).fetchone()["c"]
                table_counts[table] = cnt
            except Exception:  # noqa: BLE001
                table_counts[table] = -1

    print("", flush=True)
    print("=" * 60, flush=True)
    print("[FETCH SUMMARY]", flush=True)
    print(f"  Universe:     {len(universe_sids)} 檔 (twse + tpex)", flush=True)
    print(
        f"  daily_prices: {bulk_rows} bulk + watchlist 90day {hist_ok}/{len(wl_sids)}",
        flush=True,
    )
    print(
        f"  institutional: {inst_ok}/{n_inst} success "
        f"(TW_TOP_50 + watchlist), fail={inst_fail}",
        flush=True,
    )
    if sanity_issues:
        print(
            f"  ⚠️ Sanity issues: {len(sanity_issues)} 檔(前 5: "
            f"{[s for s, _ in sanity_issues[:5]]})",
            flush=True,
        )
    print(f"  Time:         {elapsed:.1f}s", flush=True)
    print(
        f"  Disk:         cache.db = {size_after / 1e6:.1f} MB "
        f"(delta {delta_mb:+.2f} MB)",
        flush=True,
    )
    print("  Table rows:", flush=True)
    for table, cnt in table_counts.items():
        print(f"    {table:<20s} {cnt:>8d} rows", flush=True)
    if size_after / 1e6 > 200:
        print(
            "  ⚠️ DB > 200 MB,考慮 archive 舊資料",
            flush=True,
        )
    print("=" * 60, flush=True)

    return {
        "bulk_rows": bulk_rows,
        "institutional_ok": inst_ok,
        "institutional_fail": inst_fail,
        "watchlist_history_ok": hist_ok,
        "universe_size": len(universe_sids),
        "sanity_issues": sanity_issues,
        "elapsed_secs": elapsed,
        "db_size_bytes": size_after,
        "table_counts": table_counts,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="全市場 bulk 抓價量 + 小批 institutional",
    )
    p.add_argument(
        "--institutional-days", type=int, default=7,
        help="對 TOP_50 + watchlist 抓近 N 天的法人(預設 7)",
    )
    args = p.parse_args()
    summary = run(institutional_days=args.institutional_days)

    # 健康警戒:bulk 抓到太少視為異常 → exit 1 讓 GH Actions 標紅
    if summary["bulk_rows"] < _MIN_BULK_ROWS_HEALTHY:
        print(
            f"❌ daily_prices bulk={summary['bulk_rows']} < "
            f"{_MIN_BULK_ROWS_HEALTHY} 警戒線 — exit 1",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
