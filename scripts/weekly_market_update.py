"""排程入口:每週日抓 TWSE 全市場資料,dump 成 CSV commit 進 repo。

背景:Streamlit Cloud 的 IP 會被 TWSE OpenAPI 擋(回空 body 觸發 JSONDecodeError),
但 GitHub Actions runner (Azure / Linux) 不被擋。

Workaround 流程:
  1. 每週六 23:00 UTC (週日 07:00 台北) workflow 跑此腳本
  2. 此腳本呼叫 update_long_term_data_free(TW_TOP_50) 抓 TWSE
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
from src.universe import TW_TOP_50  # noqa: E402

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"

# TAIEX 抓多少天(週 K MA20 = 20 週 = 100 trading days,200 天 calendar 留緩衝)
_TAIEX_BACKFILL_DAYS = 200


def main() -> int:
    sids = [s for s, _ in TW_TOP_50]
    db.init_db()

    # 確保 universe 在 stocks 表
    db.upsert_stocks([
        {"stock_id": sid, "name": name, "market": "TW"}
        for sid, name in TW_TOP_50
    ])

    print(
        f"[WEEKLY] 跑 update_long_term_data_free, {len(sids)} 檔 TWSE 大型股...",
        flush=True,
    )
    result = update_long_term_data_free(sids)
    print(
        f"[WEEKLY] daily_metrics: {len(result['success_metrics'])}/{len(sids)}, "
        f"EPS: {len(result['success_eps'])}/{len(sids)}, "
        f"failed: {len(result['failed'])}",
        flush=True,
    )

    if not result["success_metrics"]:
        err = result.get("error")
        print(
            f"[WEEKLY] 全部 fail,不寫 CSV。"
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
        print(f"[WEEKLY] 寫 {path.name}: {len(df)} 行", flush=True)

        # 2. financials.quarterly(EPS / ROE,長線選股用)
        df = pd.read_sql(
            "SELECT * FROM financials WHERE period_type='quarterly' "
            "ORDER BY stock_id, period",
            conn,
        )
        path = SNAPSHOT_DIR / "financials_quarterly.csv"
        df.to_csv(path, index=False)
        print(f"[WEEKLY] 寫 {path.name}: {len(df)} 行", flush=True)

        # 3. stocks(包含 industry,長線清單顯示用)
        df = pd.read_sql(
            "SELECT stock_id, name, industry FROM stocks WHERE market='TW' "
            "ORDER BY stock_id",
            conn,
        )
        path = SNAPSHOT_DIR / "stocks.csv"
        df.to_csv(path, index=False)
        print(f"[WEEKLY] 寫 {path.name}: {len(df)} 行", flush=True)

    # 4. TAIEX 加權指數 200 天歷史(大盤頁的 K 線 + 多週期 + 技術總覽都需要)
    # 走 fetch_daily_price 走 SQLite cache,差的範圍才打 FinMind。
    from datetime import date as _date, timedelta as _td
    print(f"[WEEKLY] TAIEX {_TAIEX_BACKFILL_DAYS} 天 backfill...", flush=True)
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
            print(f"[WEEKLY] 寫 {path.name}: {taiex_rows} 行", flush=True)
        else:
            print("[WEEKLY] TAIEX 抓不到資料,跳過 dump", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[WEEKLY] TAIEX 抓取 fail (繼續):{type(e).__name__}: {e}", flush=True)

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
        f"taiex_rows={taiex_rows}\n",
        encoding="utf-8",
    )
    print("[WEEKLY] 寫 last_update.txt", flush=True)

    print("[WEEKLY] 完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
