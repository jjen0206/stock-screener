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

from src.notifier import notify_multi_strategy  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(
        description="每日短線選股 → Telegram + Discord 並行推播",
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
        "--no-telegram", action="store_true",
        help="跳過 Telegram 推播",
    )
    p.add_argument(
        "--no-discord", action="store_true",
        help="跳過 Discord 推播",
    )
    args = p.parse_args()

    params = json.loads(args.params_json) if args.params_json else None

    results = notify_multi_strategy(
        date=args.date, params=params,
        send_telegram=not args.no_telegram,
        send_discord=not args.no_discord,
    )

    # Summary 列印每個通道的結果
    if not results:
        print(
            "兩個通道都跳過(沒設 secrets 或 --no-* 旗標關閉) — "
            "exit 0,什麼都沒推。"
        )
        return 0

    parts = []
    for ch in ("telegram", "discord"):
        if ch in results:
            parts.append(f"{ch.title()}: {'✅' if results[ch] else '❌'}")
    print(f"推播結果 — {' | '.join(parts)}")

    # 任一個成功就視為整體 OK(GitHub Actions 不要因為某個通道掛掉就紅)
    return 0 if any(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
