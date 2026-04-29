"""一次性回補全市場 90 天 daily_price + institutional 歷史。

背景:
  TWSE bulk endpoint 每天只回當日 1 筆 OHLCV → cache 累積要等 1-2 個月。
  短線策略需要 14-60 天歷史(MA60 / KD9 / 5 日均量),沒歷史 → 全部 skip → 0 入選。
  Streamlit Cloud 自己 IP 被 TWSE 擋,不能跑這個 backfill,只能在 GH Actions 跑。

流程:
  1. 列舉全市場 universe(TWSE + TPEx,~2700 檔)
  2. 對 daily_prices 不足 N 天的個股呼叫 fetch_daily_price(via FinMind)
  3. 對 watchlist + TW_TOP_50 補 institutional
  4. dump daily_prices + institutional + stocks 到 data/twse_snapshot/*.csv
  5. workflow 自動 git commit + push CSV → Streamlit Cloud git pull → 啟動時讀回

時間預估:
  - FinMind 免費 token 限 600/小時(老 token 1500),per call ~0.5 秒
  - 2700 檔 × 0.6 秒 ≈ 27 分鐘(理想)
  - 受限額時 GH Actions 會 sleep,實測 30-45 分鐘

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


def main() -> int:
    p = argparse.ArgumentParser(
        description="一次性回補全市場 N 天歷史(daily_price + institutional)",
    )
    p.add_argument(
        "--days", type=int, default=90,
        help="回補幾天歷史(預設 90,至少要 60 才能跑 MA60 策略)",
    )
    p.add_argument(
        "--min-existing", type=int, default=60,
        help="既有 daily_prices >= 此數字就跳過該檔(預設 60)",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="只跑前 N 檔(debug / 試水溫用)",
    )
    p.add_argument(
        "--no-institutional", action="store_true",
        help="跳過 institutional(只補 daily_prices,加速)",
    )
    args = p.parse_args()

    db.init_db()
    universe = get_full_universe()
    if not universe:
        print("[BACKFILL] universe 為空 — 先跑 daily_fetch.py 初始化")
        return 1

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
    inst_target_set = set(s for s, _ in TW_TOP_50)
    for s, _ in load_watchlist():
        inst_target_set.add(s)

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
    print(
        f"[BACKFILL DONE] 共 {n} 檔,price ok={ok_price} fail={fail_price},"
        f"inst ok={ok_inst} fail={fail_inst},耗時 {elapsed/60:.1f} 分鐘",
        flush=True,
    )

    # === Dump CSV → snapshot 給 Streamlit Cloud 讀 ===
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with db.get_conn() as conn:
        # daily_prices
        df = pd.read_sql(
            "SELECT * FROM daily_prices ORDER BY stock_id, date", conn,
        )
        path = SNAPSHOT_DIR / "daily_prices.csv"
        df.to_csv(path, index=False)
        print(f"[BACKFILL] 寫 {path.name}: {len(df)} 行", flush=True)

        # institutional
        df = pd.read_sql(
            "SELECT * FROM institutional ORDER BY stock_id, date", conn,
        )
        path = SNAPSHOT_DIR / "institutional.csv"
        df.to_csv(path, index=False)
        print(f"[BACKFILL] 寫 {path.name}: {len(df)} 行", flush=True)

        # stocks(name + industry)— 順手更新讓雲端有完整名稱
        df = pd.read_sql(
            "SELECT stock_id, name, industry FROM stocks "
            "WHERE market='TW' ORDER BY stock_id",
            conn,
        )
        path = SNAPSHOT_DIR / "stocks.csv"
        df.to_csv(path, index=False)
        print(f"[BACKFILL] 寫 {path.name}: {len(df)} 行", flush=True)

    # 寫 backfill 專用 timestamp(跟 weekly_market_update 的 last_update.txt 分開)
    import os
    from datetime import datetime, timezone
    (SNAPSHOT_DIR / "last_backfill.txt").write_text(
        f"backfilled_at={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"git_sha={os.environ.get('GITHUB_SHA', 'local')}\n"
        f"run_id={os.environ.get('GITHUB_RUN_ID', 'local')}\n"
        f"days_requested={args.days}\n"
        f"todo={n}\n"
        f"price_ok={ok_price}\n"
        f"price_fail={fail_price}\n"
        f"inst_ok={ok_inst}\n"
        f"inst_fail={fail_inst}\n"
        f"elapsed_min={elapsed/60:.1f}\n",
        encoding="utf-8",
    )
    print("[BACKFILL] 寫 last_backfill.txt", flush=True)

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


if __name__ == "__main__":
    sys.exit(main())
