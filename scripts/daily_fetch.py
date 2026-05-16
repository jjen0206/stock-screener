"""排程入口:全市場 bulk 抓價量 + 對拓寬 universe 抓 institutional。

設計:
- daily_prices 用 TWSE/TPEx bulk endpoint(免 token + 一次拿全市場 ~2360 檔 < 30 秒)
- institutional **拓寬 universe**:成交量 Top 300 + theme(~144 檔)+ watchlist
  + TW_TOP_50 union(主公 2026-05-15 拍板,從 49 檔擴到 300-500 檔)
  per-call sleep 0.4s 控速避開 FinMind 免費 600/hr token 上限
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
from src.logging_setup import setup_file_logging  # noqa: E402
from src.data_fetcher import (  # noqa: E402
    fetch_all_daily_prices_bulk,
    fetch_daily_price,
    fetch_institutional,
    validate_daily_price_sanity,
)
from src.universe import (  # noqa: E402
    TW_TOP_50,
    build_institutional_universe,
    get_full_universe,
    load_watchlist,
)

# 健康警戒線:bulk daily_prices 抓到少於這數量視為異常 → exit 1 讓 GH Actions 標紅
_MIN_BULK_ROWS_HEALTHY = 2000

# Bulk fetch sanity retry(2026-05-04 事件後加)— 若 TWSE OpenData 在跑的時候
# 還沒 publish 今日資料,sleep 後 retry。最多 3 次,間隔 5 分鐘。
_BULK_RETRY_MAX = 3
_BULK_RETRY_WAIT_SECS = 300

# institutional 拓寬 universe 配置(主公 2026-05-15 拍板)
# Top 300 volume + theme universe (~144) + watchlist + TW_TOP_50 union ≈ 350-500 檔
_INST_TOP_VOLUME_N = 300
_INST_VOLUME_LOOKBACK = 5
# Per-call sleep:FinMind 免費 token 600/hr ≈ 10 calls/min → 6s/call 安全
# 但 _api_call 內部已有 retry,500 檔 × 0.4s = 200s,搭配 retry buffer 仍在 1hr 內。
_INST_PER_CALL_SLEEP_SECS = 0.4


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
    # 嘗試過 FinMind 主源 / FinMind topup 都因雲端 GH Actions IP ban + 免費 quota
    # 不夠燒 30 分 timeout(2026-05-05 事件,run id 25357135081 / 25358200043)。
    # 回 TWSE bare bulk(配 freshness retry)— 5 分鐘跑完。TWSE publication lag
    # 期間個股可能拿不到當日資料,接受 nightly 推「個股 max 日」訊號(get_latest_
    # trading_date 排除 TAIEX 確保 screener 用個股 max 不撞牆)。
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

    # 3. 對拓寬 universe 抓 institutional(主公 2026-05-15 拍板拓寬)
    # = Top 300 by volume + theme universe (~144) + watchlist + TW_TOP_50
    # 主公觀察:每日 49 檔太窄 → 「大戶進場」訊號幾乎抓不到 → 拓寬到 ~300-500
    inst_sids_list = build_institutional_universe(
        top_volume_n=_INST_TOP_VOLUME_N,
        lookback_days=_INST_VOLUME_LOOKBACK,
    )
    # build_ 失敗 / SQLite 還沒填價量(GH Actions fresh container)→ 退回舊
    # TW_TOP_50 + watchlist union 保底
    if not inst_sids_list:
        fallback = set(s for s, _ in TW_TOP_50)
        for s, _ in load_watchlist():
            fallback.add(s)
        inst_sids_list = sorted(fallback)
        print(
            f"[FETCH] WARN: build_institutional_universe 回空 → fallback "
            f"TOP_50 + watchlist = {len(inst_sids_list)} 檔",
            flush=True,
        )
    today = date.today().isoformat()
    start = (date.today() - timedelta(days=institutional_days)).isoformat()

    print(
        f"[FETCH] 對 {len(inst_sids_list)} 檔(Top {_INST_TOP_VOLUME_N} 量 + "
        f"主題 + watchlist + TOP_50)抓 institutional 近 {institutional_days} 天...",
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
        # Rate-limit:FinMind 免費 600/hr,sleep 0.4s 確保整批 < 60 min。
        # 連續 fail(多次 RuntimeError)時可能想退避更久,但留給 _api_call
        # 內部 retry / FinMindAPIError 觸發。
        if _INST_PER_CALL_SLEEP_SECS > 0 and i < n_inst:
            time.sleep(_INST_PER_CALL_SLEEP_SECS)
        if i % 50 == 0:
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
        f"(Top {_INST_TOP_VOLUME_N} 量 + 主題 + watchlist + TOP_50), "
        f"fail={inst_fail}",
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
    setup_file_logging("daily_fetch", mirror_print=True)
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
