"""排程入口:每天抓 TWSE 全市場財報資料,dump 成 CSV commit 進 repo。

(原 weekly_market_update.py — 名稱誤導,實際被 daily-notify.yml 每天呼叫。
2026-05-05 主公拍板更名為 daily_market_update + universe 從 TW_TOP_50 50 檔
擴到 pure_stock_universe ~2060 檔長線涵蓋全市場。)

背景:Streamlit Cloud 的 IP 會被 TWSE OpenAPI 擋(回空 body 觸發 JSONDecodeError),
但 GitHub Actions runner (Azure / Linux) 不被擋。

Workaround 流程:
  1. 每工作日 14:13 UTC (22:13 台北) daily-notify.yml workflow 跑此腳本
  2. 此腳本呼叫 update_long_term_data_free(pure_stock_universe) 抓 TWSE
     全市場(~2060 檔,從 TW_TOP_50 50 檔擴大),~30 分鐘
  3. 把 daily_metrics / financials.quarterly / stocks 三張表 dump 成 CSV
  4. 寫到 data/twse_snapshot/ 路徑(.gitignore 不排除 CSV)
  5. workflow 自動 git commit + push 這些 CSV
  6. Streamlit Cloud app 啟動時讀 CSV 灌進 SQLite (見 app.py _load_snapshot_if_needed)

watchlist.csv 不在這支腳本的負責範圍 — 雲端 add/remove ☆ 會走 GitHub Contents API
推到獨立的 watchlist-sync 分支(見 src/github_sync.py),main 上的 watchlist.csv
僅作為初次部署 / fallback seed,由人手或 scripts/commit_watchlist.py 維護。

Exit code:
  0 = 成功(只要 daily_metrics 有寫到任何資料)
  1 = 全部失敗(TWSE 完全不通,連 GitHub Actions runner 都抓不到 — 罕見)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.data_fetcher import fetch_daily_price  # noqa: E402
from src.financial_fetcher_free import update_long_term_data_free  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402
from src.universe import TW_TOP_50, pure_stock_universe  # noqa: E402

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"

# TAIEX 抓多少天(週 K MA20 = 20 週 = 100 trading days,200 天 calendar 留緩衝)
_TAIEX_BACKFILL_DAYS = 200

# monthly_revenue + dividend 不在 daily 抓 — 2026-05-06 主公發現整合進來會撞
# 120 min workflow timeout(全市場 ~2060 檔 × FinMind throttle)。改回:
#   - backfill-revenue.yml  週一 22:13 cron 8-shard 全市場
#   - backfill-dividend.yml 週日 22:13 cron 8-shard 全市場
# 兩者本質上都是月 / 年級資料,週跑 1 次足夠。daily-notify 拿掉這兩個 fetch
# 後跑時長從 2h+ → ~15 min。


def main() -> int:
    setup_file_logging("daily_market_update", mirror_print=True)
    db.init_db()

    # universe 從 TW_TOP_50(50 檔)擴到 pure_stock_universe(~2060 檔)
    # — 主公拍板長線涵蓋全市場。fallback TW_TOP_50 給首次部署 / SQLite 空時用。
    sids = pure_stock_universe(min_history=20)
    if len(sids) < 50:
        print(
            f"[DAILY] pure_stock universe 只 {len(sids)} 檔(可能 fresh "
            f"container),fallback TW_TOP_50",
            flush=True,
        )
        sids = [s for s, _ in TW_TOP_50]

    # 確保 TW_TOP_50 永遠在 stocks 表(若 universe 從 SQLite 撈,TOP_50 通常已在)
    db.upsert_stocks([
        {"stock_id": sid, "name": name, "market": "TW"}
        for sid, name in TW_TOP_50
    ])

    print(
        f"[DAILY] 跑 update_long_term_data_free, {len(sids)} 檔(全市場)...",
        flush=True,
    )
    result = update_long_term_data_free(sids)
    print(
        f"[DAILY] daily_metrics: {len(result['success_metrics'])}/{len(sids)}, "
        f"EPS: {len(result['success_eps'])}/{len(sids)}, "
        f"failed: {len(result['failed'])}",
        flush=True,
    )

    if not result["success_metrics"]:
        err = result.get("error")
        print(
            f"[DAILY] 全部 fail,不寫 CSV。"
            f"error={type(err).__name__ if err else 'unknown'}: {str(err)[:200]}",
            flush=True,
        )
        return 1

    # Dump 三張表到 CSV
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with db.get_conn() as conn:
        # 1. daily_metrics(PE / PB / 殖利率)
        df = pd.read_sql("SELECT * FROM daily_metrics ORDER BY stock_id", conn)
        path = SNAPSHOT_DIR / "daily_metrics.csv"
        df.to_csv(path, index=False)
        print(f"[DAILY] 寫 {path.name}: {len(df)} 行", flush=True)

        # 2. financials.quarterly(EPS / ROE,長線選股用)
        df = pd.read_sql(
            "SELECT * FROM financials WHERE period_type='quarterly' "
            "ORDER BY stock_id, period",
            conn,
        )
        path = SNAPSHOT_DIR / "financials_quarterly.csv"
        df.to_csv(path, index=False)
        print(f"[DAILY] 寫 {path.name}: {len(df)} 行", flush=True)

        # 3. stocks(包含 industry,長線清單顯示用)
        df = pd.read_sql(
            "SELECT stock_id, name, industry FROM stocks WHERE market='TW' "
            "ORDER BY stock_id",
            conn,
        )
        path = SNAPSHOT_DIR / "stocks.csv"
        df.to_csv(path, index=False)
        print(f"[DAILY] 寫 {path.name}: {len(df)} 行", flush=True)

    # 4. daily_prices 全市場(個股 OHLCV)— **Bug 修:原版只 dump TAIEX,
    # 沒 dump 個股 daily_prices,導致 daily_fetch.py 寫進 runner SQLite 的
    # 個股當日資料隨 runner 銷毀就消失,Streamlit Cloud 永遠看不到 5/4 之後的
    # 個股 close。2026-05-06 主公發現「股價 39.55 vs 📡 40.85」差 3 元就是這 bug。
    # backfill-history.yml 只 workflow_dispatch 手動觸發,scheduled workflow
    # 永不 commit daily_prices.csv → 此修補。
    with db.get_conn() as conn:
        df = pd.read_sql(
            "SELECT * FROM daily_prices WHERE stock_id != 'TAIEX' "
            "ORDER BY stock_id, date",
            conn,
        )
    path = SNAPSHOT_DIR / "daily_prices.csv"
    df.to_csv(path, index=False)
    daily_prices_rows = len(df)
    print(f"[DAILY] 寫 {path.name}: {daily_prices_rows} 行", flush=True)

    # 4b. institutional 全市場(daily_fetch.py 已抓 TOP_50 + watchlist 寫進 SQLite,
    # 但跟 daily_prices 一樣只 backfill-history.yml 才會 dump CSV → 雲端 reboot 後
    # snapshot 卡舊日期。同 daily_prices 修法 pattern,把 dump 整合進 daily。
    with db.get_conn() as conn:
        df = pd.read_sql(
            "SELECT * FROM institutional ORDER BY date, stock_id",
            conn,
        )
    path = SNAPSHOT_DIR / "institutional.csv"
    df.to_csv(path, index=False)
    institutional_rows = len(df)
    print(f"[DAILY] 寫 {path.name}: {institutional_rows} 行", flush=True)

    # 5. TAIEX 加權指數 200 天歷史(大盤頁的 K 線 + 多週期 + 技術總覽都需要)
    # 走 fetch_daily_price 走 SQLite cache,差的範圍才打 FinMind。
    from datetime import date as _date, timedelta as _td
    print(f"[DAILY] TAIEX {_TAIEX_BACKFILL_DAYS} 天 backfill...", flush=True)
    taiex_rows = 0
    try:
        today_iso = _date.today().isoformat()
        start_iso = (_date.today() - _td(days=_TAIEX_BACKFILL_DAYS)).isoformat()
        fetch_daily_price("TAIEX", start_iso, today_iso)
        with db.get_conn() as conn:
            df = pd.read_sql(
                "SELECT * FROM daily_prices WHERE stock_id='TAIEX' "
                "ORDER BY date",
                conn,
            )
        if not df.empty:
            path = SNAPSHOT_DIR / "taiex.csv"
            df.to_csv(path, index=False)
            taiex_rows = len(df)
            print(f"[DAILY] 寫 {path.name}: {taiex_rows} 行", flush=True)
        else:
            print("[DAILY] TAIEX 抓不到資料,跳過 dump", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[DAILY] TAIEX 抓取 fail (繼續):{type(e).__name__}: {e}", flush=True)

    # monthly_revenue / dividend 不在這 daily script 抓 — 2026-05-06 主公拍板
    # 砍掉,改回 backfill-revenue.yml(週一)+ backfill-dividend.yml(週日)
    # 各自 8-shard 全市場跑。原因:整合進 daily 全市場 ~2060 檔 × FinMind quota
    # throttle 把 daily-notify 拉到 2h+ 撞 120 min workflow timeout。

    # watchlist.csv 不再由 weekly 維護;改由雲端 app 推到 watchlist-sync 分支
    # (見 src/github_sync.py)。main 上的 watchlist.csv 保留為 seed。

    # 寫 timestamp + git/run id 方便事後追溯
    import os
    from datetime import datetime, timezone
    (SNAPSHOT_DIR / "last_update.txt").write_text(
        f"updated_at={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"git_sha={os.environ.get('GITHUB_SHA', 'local')}\n"
        f"run_id={os.environ.get('GITHUB_RUN_ID', 'local')}\n"
        f"daily_metrics_rows={result['success_metrics'].__len__()}\n"
        f"eps_rows={result['success_eps'].__len__()}\n"
        f"daily_prices_rows={daily_prices_rows}\n"
        f"institutional_rows={institutional_rows}\n"
        f"taiex_rows={taiex_rows}\n",
        encoding="utf-8",
    )
    print("[DAILY] 寫 last_update.txt", flush=True)

    # === 觸目標價提醒(主公拍板 2026-05-08)===
    # close 已更新到最新交易日,跑 notify_target_hits 比對法人共識目標。
    # 內部 filter sid ∈ 6 類聯集 + 7 日冷卻 + close ≥ target × 100% → push。
    try:
        from src.analyst_target_alerts import notify_target_hits
        hit_result = notify_target_hits()
        print(
            f"[DAILY] 觸目標推播 — 候選 {hit_result['n_candidates']} 筆 → "
            f"通過 filter {hit_result['n_eligible']} 筆 → "
            f"TG={hit_result['n_pushed_telegram']} / "
            f"Discord={hit_result['n_pushed_discord']}",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[DAILY] 觸目標推播 step 失敗(忽略):{e}", flush=True)

    print("[DAILY] 完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
