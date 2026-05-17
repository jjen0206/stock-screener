"""排程入口:補 financials 表(quarterly EPS / ROE)缺漏的全市場資料。

背景
----
2026-05-16 健診:`financials` 只覆蓋 1073 / 2127 純股(~50%),長線基本面策略
(EPS 加速 / ROE 篩選 / 殖利率)在沒財報的 1054 檔上完全跑不了。

歷史:`scripts/backfill_financials.py` 在 2026-05-06 commit `967905e` 被刪
(當時判斷 daily_market_update 會 cover)。實際上 daily_market_update 走
`update_long_term_data_free` 對全市場 ~2060 檔逐一打 FinMind,常因 quota 或 stock
不存在於 FinMind quarterly dataset 而 skip。2026-05-16 主公拍板重建 backfill script
做一次性補滿。

設計
----
跟 backfill_institutional.py 同模式:per-stock loop,走 SQLite sync_log cache,
有資料的 stock 跳過。FinMind quarterly EPS/ROE 需要 token(`FINMIND_TOKEN` env),
無 token 模式 fetch_quarterly_financials 會 raise 跳過該 sid。

跟 backfill_revenue.py / backfill_dividend.py 的 8-shard 模式不同:financials
量級小(quarterly 一檔 ~20 row × 5 年),非 backfill 巔峰時段單 process 已夠跑。

CLI
---
::

    # default: 跑全 pure_stock universe,只補缺漏(已有資料的 sid 跳過)
    python scripts/backfill_financials.py

    # 強制重抓所有 sid(忽略 sync_log)
    python scripts/backfill_financials.py --force

    # 限定前 N 檔(debug)
    python scripts/backfill_financials.py --limit 20

    # 修改 lookback 年數(default 5)
    python scripts/backfill_financials.py --years 5

    # 跑完 dump 進 financials_quarterly.csv(GH workflow 用,讓雲端 reload)
    python scripts/backfill_financials.py --dump-csv

    # 分批跑(避過 FinMind quota 撞牆)— stocks 表 row 0~200
    python scripts/backfill_financials.py --batch-start 0 --batch-end 200

    # 單次最多跑 200 檔(default 200,避免一個 workflow run 內把 quota 燒乾)
    python scripts/backfill_financials.py --max-stocks 200

Exit code
---------
- 0  成功(全部 OK 或失敗 < 25%)
- 1  超過 25% 檔失敗(FinMind quota 或大故障)
- 2  CLI 參數錯誤
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"


def stocks_with_financials(db_path: str | Path | None = None) -> set[str]:
    """回 financials 表中 period_type='quarterly' 有至少 1 筆的 stock_id set。

    給 backfill 跳過已有資料的 sid 用(避免重打 FinMind)。
    """
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT stock_id FROM financials "
            "WHERE period_type='quarterly'"
        ).fetchall()
    return {r["stock_id"] for r in rows}


def backfill_one(
    sid: str,
    start: str,
    end: str,
    fetch_fn,
) -> tuple[bool, str]:
    """補一檔 quarterly financials。

    fetch_fn(sid, start, end) 應該是 fetch_quarterly_financials 或同簽名 mock。
    依賴 sync_log + upsert_financials 走快取 — 重跑 idempotent。

    Returns (ok, status):
      ok=True, status='ok'        → 抓到並寫入(或本來就有 cache)
      ok=False, status='empty'    → 回空 DataFrame(FinMind 該股無資料,可能 ETF/受益憑證)
      ok=False, status='error'    → 例外(token 不足 / 網路 / FinMindAPIError)

    Raises:
      FinMindQuotaError: status=402 quota 爆 — caller(main)該 fail-fast 整批中斷,
        不再 retry / sleep。
    """
    # 在 lazy import 區拿 quota class(避免頂層 import 拖慢 --help)
    from src.data_fetcher import FinMindQuotaError
    try:
        df = fetch_fn(sid, start, end)
    except FinMindQuotaError:
        # 不 swallow — 主迴圈 catch 後 log + 提早中斷
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("[BACKFILL-FIN] %s fail: %s: %s", sid, type(e).__name__, e)
        return False, "error"
    if df is None or (hasattr(df, "empty") and df.empty):
        return False, "empty"
    return True, "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="補 financials 表(quarterly EPS / ROE)缺漏的全市場資料",
    )
    parser.add_argument(
        "--years", type=int, default=5,
        help="抓近幾年季報(default 5,EPS 加速 / ROE 篩選需要 5+ 季)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="強制重抓(忽略 financials 表已有的 sid)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="只跑前 N 檔(debug,0 = 全跑)",
    )
    parser.add_argument(
        "--sleep", type=float, default=0.5,
        help="每檔間 sleep 秒數(default 0.5,FinMind throttle 友善)",
    )
    parser.add_argument(
        "--progress-every", type=int, default=50,
        help="每 N 檔印一次進度(default 50)",
    )
    parser.add_argument(
        "--batch-start", type=int, default=0,
        help=(
            "從 universe 的第 N 檔開始(0-based,inclusive)。"
            "搭配 --batch-end 做分批 dispatch — 例 0/200 跑前 200 檔,200/400 跑下一批,"
            "避過 FinMind quota 撞牆"
        ),
    )
    parser.add_argument(
        "--batch-end", type=int, default=0,
        help=(
            "跑到 universe 的第 N 檔結束(0-based,exclusive)。0 = 跑到 universe 末尾。"
            "搭配 --batch-start"
        ),
    )
    parser.add_argument(
        "--max-stocks", type=int, default=200,
        help=(
            "單次 run 上限檔數(default 200)。即使 batch 範圍更大,也只跑這麼多 — "
            "防 FinMind quota(600/hr 免費 token,2000 檔一次跑會撞)"
        ),
    )
    parser.add_argument(
        "--dump-csv", action="store_true",
        help="跑完後 dump 進 data/twse_snapshot/financials_quarterly.csv",
    )
    args = parser.parse_args(argv)

    setup_file_logging("backfill_financials")
    db.init_db()

    # Lazy import:--help / 參數錯誤時不用 import heavy modules
    from src.data_fetcher import fetch_quarterly_financials
    from src.universe import pure_stock_universe

    universe = pure_stock_universe(min_history=20)
    if not universe:
        print(
            "[BACKFILL-FIN] pure_stock universe 為空 — 先跑 daily_fetch.py 初始化 stocks",
            flush=True,
        )
        return 1

    # 1. batch range:對 universe(已排序 stable)切 [batch_start, batch_end) 視窗
    #    讓主公手動分批 dispatch(0/200 → 200/400 → ...),避過 FinMind quota
    batch_lo = max(0, args.batch_start)
    batch_hi = args.batch_end if args.batch_end > 0 else len(universe)
    if batch_lo >= len(universe):
        print(
            f"[BACKFILL-FIN] batch-start={batch_lo} >= universe size={len(universe)} "
            "→ nothing to do",
            flush=True,
        )
        return 0
    if batch_lo >= batch_hi:
        print(
            f"[BACKFILL-FIN] batch-start={batch_lo} >= batch-end={batch_hi} → 區間空,exit",
            file=sys.stderr, flush=True,
        )
        return 2
    universe_slice = universe[batch_lo:batch_hi]

    # 2. skip 已有 financials.quarterly 的 sid(除非 --force)
    if args.force:
        todo = list(universe_slice)
    else:
        have = stocks_with_financials()
        todo = [s for s in universe_slice if s not in have]

    if args.limit > 0:
        todo = todo[: args.limit]
    # 3. 單次 run 上限(預設 200,防 quota 撞牆)
    if args.max_stocks > 0 and len(todo) > args.max_stocks:
        print(
            f"[BACKFILL-FIN] todo={len(todo)} > max-stocks={args.max_stocks},"
            f"截到前 {args.max_stocks} 檔",
            flush=True,
        )
        todo = todo[: args.max_stocks]

    today = date.today().isoformat()
    start = (date.today() - timedelta(days=args.years * 365 + 30)).isoformat()

    print(
        f"[BACKFILL-FIN] universe={len(universe)} "
        f"batch=[{batch_lo}:{batch_hi}] slice={len(universe_slice)} "
        f"待補={len(todo)} max-stocks={args.max_stocks} 範圍={start}~{today}",
        flush=True,
    )
    logger.info(
        "[BACKFILL-FIN] universe=%d batch=[%d:%d] slice=%d todo=%d range=%s~%s",
        len(universe), batch_lo, batch_hi, len(universe_slice), len(todo),
        start, today,
    )

    if not todo:
        print("[BACKFILL-FIN] nothing to do", flush=True)
        if args.dump_csv:
            _dump_csv()
        return 0

    # lazy import:--help 不要 import data_fetcher
    from src.data_fetcher import FinMindQuotaError

    n = len(todo)
    ok = fail = empty = 0
    quota_hit = False
    t0 = time.time()

    for i, sid in enumerate(todo, start=1):
        try:
            success, status = backfill_one(
                sid, start, today, fetch_quarterly_financials,
            )
        except FinMindQuotaError as ex:
            # quota 爆 — 沒救,不 retry / 不繼續燒 sleep,馬上中斷整批
            logger.warning(
                "[BACKFILL-FIN] FinMind quota 爆 (%s),中斷整批 — "
                "改日再跑或加 token: %s",
                sid, ex,
            )
            print(
                f"[BACKFILL-FIN] ⚠ FinMind quota 爆({sid}),中斷整批 — "
                f"改日再跑或加 token: {ex}",
                file=sys.stderr, flush=True,
            )
            quota_hit = True
            break

        if success:
            ok += 1
        elif status == "empty":
            empty += 1
        else:
            fail += 1
            # 失敗超過 25% 提前中斷(避免一直撞 FinMind)
            if fail > max(20, n // 4):
                print(
                    f"[BACKFILL-FIN] 失敗 > 25%({fail}/{i}),中斷",
                    flush=True,
                )
                break

        if i % args.progress_every == 0 or i == n:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (n - i) / rate / 60 if rate > 0 else 0
            line = (
                f"[BACKFILL-FIN] {i}/{n} ok={ok} empty={empty} fail={fail} "
                f"{rate:.2f}/s ETA={eta:.1f}m"
            )
            print(line, flush=True)
            logger.info(line)

        if args.sleep > 0:
            time.sleep(args.sleep)

    elapsed = time.time() - t0
    print(
        f"[BACKFILL-FIN DONE] 耗時 {elapsed/60:.1f} 分鐘,"
        f"ok={ok} empty={empty} fail={fail} / {n} 檔",
        flush=True,
    )
    logger.info(
        "[BACKFILL-FIN DONE] elapsed=%.1fm ok=%d empty=%d fail=%d todo=%d",
        elapsed / 60, ok, empty, fail, n,
    )

    if args.dump_csv:
        _dump_csv()

    # exit code:quota 爆 / 失敗 > 25% 視為整體失敗(workflow 該看到紅燈)
    if quota_hit:
        return 1
    if fail > 0 and fail > n // 4:
        return 1
    return 0


def _dump_csv() -> int:
    """Dump financials.quarterly 進 financials_quarterly.csv。

    跟 daily_market_update.main 內 step 2 同邏輯(stock_id, period_type='quarterly')。
    回 row count。
    """
    import pandas as pd

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with db.get_conn() as conn:
        df = pd.read_sql(
            "SELECT stock_id, period_type, period, revenue, revenue_yoy, eps, roe "
            "FROM financials WHERE period_type='quarterly' "
            "ORDER BY stock_id, period",
            conn,
        )
    if df.empty:
        print(
            "[BACKFILL-FIN] financials.quarterly 表空,不 dump CSV",
            flush=True,
        )
        return 0
    out = SNAPSHOT_DIR / "financials_quarterly.csv"
    df.to_csv(out, index=False)
    print(f"[BACKFILL-FIN] 寫 {out.name}: {len(df)} 行", flush=True)
    return len(df)


if __name__ == "__main__":
    sys.exit(main())
