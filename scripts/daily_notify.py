"""排程入口:跑當日短線選股並推播 Telegram。

使用範例:
    # 跑當日(預設台北今天)
    python scripts/daily_notify.py

    # 跑特定日
    python scripts/daily_notify.py --date 2026-04-25

    # 自訂短線參數
    python scripts/daily_notify.py --params-json '{"volume_multiplier": 1.8}'

排程方式(GitHub Actions / 主機 cron / Windows 工作排程器):
    詳見 README「Telegram 推播」章節的 GitHub Actions yaml 範例。

Exit code:
    0 = 成功推播(訊息送出)
    1 = 失敗(缺 token、網路錯、API 4xx/5xx 等)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# 讓本檔從任何 cwd 執行都能 import src.*
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src import paper_trading as pt  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402
from src.notifier import (  # noqa: E402
    compute_top_picks,
    notify_elite_top_picks,
    notify_top_picks,
)


def main() -> int:
    setup_file_logging("daily_notify", mirror_print=True)
    p = argparse.ArgumentParser(
        description="每日短線精選(高信心+共識≥2)→ Telegram + Discord 並行推播",
    )
    p.add_argument(
        "--date", default=None,
        help="YYYY-MM-DD;預設今日(系統時區)",
    )
    p.add_argument(
        "--params-json", default=None,
        help='短線參數 JSON,例 \'{"volume_multiplier": 1.8}\'',
    )
    p.add_argument(
        "--top-n", type=int, default=5,
        help="推播幾張 picks(default 5,by ml_prob desc)",
    )
    p.add_argument(
        "--confluence-n", type=int, default=2,
        help="confluence 最少命中策略數(default 2)",
    )
    p.add_argument(
        "--no-telegram", action="store_true",
        help="跳過 Telegram 推播",
    )
    p.add_argument(
        "--no-discord", action="store_true",
        help="跳過 Discord 推播",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="不真的送,只 print 訊息到 stdout(看排版用)",
    )
    p.add_argument(
        "--legacy", action="store_true",
        help="走 legacy 4-section 完整版(預設關 — 走 elite Top 5 精英版)",
    )
    p.add_argument(
        "--top-n-long", type=int, default=3,
        help="長線觀察 Top N(elite 模式專用,default 3)",
    )
    p.add_argument(
        "--no-news", action="store_true",
        help="elite 模式關閉 daily news caption",
    )
    args = p.parse_args()

    # GitHub Actions runner fresh container,SQLite 空 → preload snapshot CSV
    # 確保短線篩選看到 daily_prices 90 天歷史 + institutional / TAIEX 資料。
    # daily_fetch step 已 preload 過,但這裡再 preload 一次:
    # (a) 萬一 daily_fetch step 失敗或被 skip,daily_notify 仍能正常跑
    # (b) idempotent,upsert ON CONFLICT 不重複資料
    preload_counts = db.preload_snapshots()
    if preload_counts:
        print(f"[NOTIFY] preload snapshots: {preload_counts}", flush=True)

    # 篩選日期:user 指定 > 最後交易日 > today fallback
    # 週末 / 假日跑 today() 找不到當日 close → 推「今日無入選」誤判,改用
    # SQLite daily_prices MAX(date) 當 target。
    if args.date:
        target_date = args.date
    else:
        latest = db.get_latest_trading_date()
        from datetime import date as _date
        target_date = latest or _date.today().isoformat()
        if latest:
            print(
                f"[NOTIFY] 使用最後交易日 {latest} 當篩選日期"
                f"(系統時區今日 = {_date.today().isoformat()})",
                flush=True,
            )

    params = json.loads(args.params_json) if args.params_json else None

    # elite 模式為 default(2026-05-19 主公拍板 — 方案 B 精英化)
    # legacy 路徑保留:--legacy flag(主公手動切回 4-section 完整版的逃生口)
    if args.legacy:
        results = notify_top_picks(
            date=target_date,
            params=params,
            top_n=args.top_n,
            confluence_n=args.confluence_n,
            send_telegram=not args.no_telegram,
            send_discord=not args.no_discord,
            dry_run=args.dry_run,
        )
    else:
        # elite 模式:Top 5 短線 + Top 3 長線觀察 + daily news caption
        # (砍掉 legacy 「昨日複盤」「高信心交集」「大戶進場」3 個 section)
        # 觸發前先 refetch news,確保 caption 是最新的(原 news-notify hourly cron
        # 已砍 — main 推播只在 daily-notify 觸發時抓一次)
        if not args.no_news:
            try:
                from src.news_fetcher import fetch_and_store_news
                _r, _ins, _sk = fetch_and_store_news()
                print(
                    f"[NOTIFY] elite-mode news refetch: inserted={_ins}",
                    flush=True,
                )
            except Exception as e:  # noqa: BLE001
                print(
                    f"[NOTIFY] elite-mode news refetch 失敗(non-blocking): "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )
        results = notify_elite_top_picks(
            date=target_date,
            params=params,
            top_n_short=args.top_n,
            top_n_long=args.top_n_long,
            confluence_n=args.confluence_n,
            send_telegram=not args.no_telegram,
            send_discord=not args.no_discord,
            dry_run=args.dry_run,
            include_news=not args.no_news,
        )

    # Summary 列印每個通道的結果
    if not results:
        print(
            "兩個通道都跳過(沒設 secrets 或 --no-* 旗標關閉) — "
            "推播略過,但仍會執行 paper_trades auto-seed。"
        )
    else:
        parts = []
        for ch in ("telegram", "discord"):
            if ch in results:
                parts.append(f"{ch.title()}: {'✅' if results[ch] else '❌'}")
        print(f"推播結果 — {' | '.join(parts)}")

    # === Auto-seed paper_trades ===
    # 推播完(或推播被關掉)都要同步把今日 picks 寫進 paper_trades,主公不用再
    # 到 Streamlit 頁手點「一鍵加入」。reuse paper_trading.bulk_add_paper_trades
    # (也是 page 5343 「一鍵加入」用的同一隻 helper)經 (sid, entry_date) UNIQUE
    # 約束去重。
    # dry-run 不寫(避免本機測試污染 paper_trades);任一例外都不擋整支腳本 —
    # exit code 仍以推播結果為準。
    # 關鍵不變量:--no-telegram --no-discord 時 results={} 也必須跑 auto-seed,
    # 不能因為「沒任何通道推」就 early return 跳過(GH Actions inline backtest
    # step 倚賴 paper_trades 有今日 row 才有東西可算)。
    if args.dry_run:
        print("[NOTIFY] dry-run: 跳過 paper_trades auto-seed", flush=True)
    else:
        try:
            today_picks = compute_top_picks(
                date=target_date,
                top_n=args.top_n,
                confluence_n=args.confluence_n,
                params=params,
            )
            seed = pt.auto_seed_from_picks(today_picks, entry_date=target_date)
            print(
                f"[NOTIFY] paper_trades auto-seed: "
                f"added={seed['added']} skipped={seed['skipped']} "
                f"errors={seed['errors']} (entry_date={target_date})",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            print(
                f"[NOTIFY] auto-seed failed (non-blocking): "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

    # 沒推任何通道 → exit 0(被使用者明確關掉,不是失敗)
    if not results:
        return 0
    # 任一個成功就視為整體 OK(GitHub Actions 不要因為某個通道掛掉就紅)
    return 0 if any(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
