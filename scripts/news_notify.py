"""排程入口:抓 TWSE 重大訊息 → 白名單過濾 → 推 Telegram + Discord。

每小時 cron 跑(`.github/workflows/news-notify.yml`),處理:
  1. fetch TWSE OpenAPI t187ap04_L 全市場當日重訊
  2. dedup by url_hash 寫進 SQLite news 表(INSERT OR IGNORE)
  3. 撈 sent_telegram=0 / sent_discord=0 + article_no 在白名單內的 unsent rows
  4. format_news_message 合併推播,每 channel 一次最多 5 則(訊息長度限制)
  5. mark sent_telegram=1 / sent_discord=1

CLI:
    # 正式跑(GH Actions)
    python scripts/news_notify.py

    # dry-run:fetch + 列印,不真送
    python scripts/news_notify.py --dry-run

    # 限制單批最多 N 則(default 5)
    python scripts/news_notify.py --batch-size 5

    # 跳過 channel
    python scripts/news_notify.py --no-telegram
    python scripts/news_notify.py --no-discord

Exit code:
  0 = 跑完(無 unsent 也 OK)
  1 = fetch 失敗 / 嚴重錯誤
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402
from src.news_fetcher import (  # noqa: E402
    IMPORTANT_ARTICLES, fetch_and_store_news, list_unsent_important_news,
    mark_news_sent,
)
from src.notifier import (  # noqa: E402
    format_news_message, send_telegram_message,
)


def _push_channel(
    channel: str, batch_size: int, dry_run: bool,
) -> tuple[int, int]:
    """處理單一 channel:撈 unsent → format → send → mark。

    回 (n_pushed, n_remaining)。
    """
    unsent = list_unsent_important_news(channel=channel, limit=batch_size)
    if not unsent:
        print(
            f"[NEWS-{channel.upper()}] 無 unsent 重要新聞,跳過",
            flush=True,
        )
        return 0, 0

    msg = format_news_message(unsent, channel=channel)
    if dry_run:
        print(f"\n=== {channel.upper()} (dry-run) ===\n", flush=True)
        print(msg, flush=True)
        print(f"\n=== END {channel.upper()} ({len(unsent)} 則) ===\n", flush=True)
        return len(unsent), 0

    # 真送 — Telegram news 走 HTML 避開 Markdown entity 解析炸 400
    if channel == "telegram":
        ok = send_telegram_message(msg, parse_mode="HTML")
    else:
        from src.discord_notifier import send_discord_message
        ok = send_discord_message(msg)

    if not ok:
        print(
            f"[NEWS-{channel.upper()}] 送失敗(網路 / token / rate limit),不 mark sent — 下次再試",
            flush=True,
        )
        return 0, len(unsent)

    # mark sent
    ids = [r["id"] for r in unsent]
    n_marked = mark_news_sent(ids, channel=channel)
    print(
        f"[NEWS-{channel.upper()}] 推送 {len(unsent)} 則,標 sent={n_marked} 筆",
        flush=True,
    )
    return n_marked, 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="抓 TWSE 重大訊息 → 白名單過濾 → 推 Telegram + Discord",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只 print 訊息到 stdout,不真送 channel",
    )
    p.add_argument(
        "--batch-size", type=int, default=5,
        help="單批最多推 N 則(default 5,訊息長度顧 4096 / 2000 字限制)",
    )
    p.add_argument(
        "--no-telegram", action="store_true",
        help="跳過 Telegram 推播",
    )
    p.add_argument(
        "--no-discord", action="store_true",
        help="跳過 Discord 推播",
    )
    args = p.parse_args()

    setup_file_logging("news_notify", mirror_print=True)

    db.init_db()
    # GH Actions runner 是 fresh container,SQLite 空 → preload snapshot CSV
    counts = db.preload_snapshots()
    if counts:
        print(f"[NEWS] preload snapshots: {counts}", flush=True)

    # 1. fetch + store
    try:
        rows, inserted, skipped = fetch_and_store_news()
    except Exception as e:  # noqa: BLE001
        print(
            f"[NEWS] ❌ fetch_twse_news 失敗 ({type(e).__name__}: {e})"
            f" — 跳過本輪 cron,下次再試",
            flush=True,
        )
        return 1

    print(
        f"[NEWS] fetch TWSE: 共 {len(rows)} 則,新加 {inserted} / 跳過 {skipped}"
        f"(已存在),白名單 = {len(IMPORTANT_ARTICLES)} 條款",
        flush=True,
    )

    # 統計這輪 fetch 後的 unsent 量(各 channel 獨立)
    if args.dry_run:
        # dry-run 也順便印整體分布給主公看
        from collections import Counter
        articles = Counter(r.get("article_no") or "" for r in rows)
        print("\n[NEWS] 本輪條款分布(top 15):", flush=True)
        for art, cnt in articles.most_common(15):
            mark = "✅" if art in IMPORTANT_ARTICLES else "❌"
            print(f"  {mark} {art:<10s}: {cnt}", flush=True)

    pushed_total = 0
    remaining_total = 0

    if not args.no_telegram:
        if config.TELEGRAM_BOT_TOKEN or args.dry_run:
            tg_pushed, tg_remaining = _push_channel(
                "telegram", args.batch_size, args.dry_run,
            )
            pushed_total += tg_pushed
            remaining_total += tg_remaining
        else:
            print("[NEWS-TELEGRAM] TELEGRAM_BOT_TOKEN 未設,跳過", flush=True)

    if not args.no_discord:
        if config.DISCORD_WEBHOOK_URL or args.dry_run:
            dc_pushed, dc_remaining = _push_channel(
                "discord", args.batch_size, args.dry_run,
            )
            pushed_total += dc_pushed
            remaining_total += dc_remaining
            # rate limit 緩衝(Telegram + Discord 之間 sleep 1 秒)
            time.sleep(1)
        else:
            print("[NEWS-DISCORD] DISCORD_WEBHOOK_URL 未設,跳過", flush=True)

    print(
        f"[NEWS] 完成 — 推送 {pushed_total} 則,剩 {remaining_total} 則 unsent"
        f"(下輪 cron 接手)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
