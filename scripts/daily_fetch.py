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
import sys
from datetime import date, timedelta
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.data_fetcher import (  # noqa: E402
    fetch_all_daily_prices_bulk,
    fetch_daily_price,
    fetch_institutional,
)
from src.universe import TW_TOP_50, get_full_universe, load_watchlist  # noqa: E402


def run(institutional_days: int = 7) -> dict:
    """執行 daily 流程:全市場 bulk 價量 + 小批 institutional。

    回 {bulk_rows, institutional_ok, institutional_fail, universe_size}
    """
    db.init_db()

    # 1. Init / refresh universe(全市場 ~2360 檔寫入 stocks 表)
    universe_sids = get_full_universe()
    print(f"[FETCH] universe = {len(universe_sids)} 檔(twse + tpex)", flush=True)

    # 2. Bulk 抓全市場 OHLCV(< 30 秒)
    print("[FETCH] bulk 抓全市場 daily_prices...", flush=True)
    bulk_df = fetch_all_daily_prices_bulk()
    if bulk_df.empty:
        print("[FETCH] WARN: bulk 完全失敗,跳過 daily_prices 寫入", flush=True)
        bulk_rows = 0
    else:
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

    print(
        f"\n[FETCH] done. "
        f"daily_prices={bulk_rows}, "
        f"institutional ok={inst_ok}/{n_inst}, fail={inst_fail}, "
        f"watchlist 90day ok={hist_ok}/{len(wl_sids)}",
        flush=True,
    )
    return {
        "bulk_rows": bulk_rows,
        "institutional_ok": inst_ok,
        "institutional_fail": inst_fail,
        "watchlist_history_ok": hist_ok,
        "universe_size": len(universe_sids),
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
    run(institutional_days=args.institutional_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
