"""Quota reset reminder — 推 Telegram + Discord 告知主公可手動 trigger backfill。

背景(2026-05-19):
  - financials backfill (PR #20) schedule */30 跑到第二批就撞 FinMind 月配額,
    剩餘 cron fire 全 noop 浪費 GHA runner 月 2000 min 額度。
  - company_profiles backfill (PR #27) schedule hourly 同樣撞 Gemini free tier
    daily 1500 req limit,三次全 fail。
  - 兩個 workflow 的 schedule 已移除,改 alert-based manual trigger:
      - company_profiles: 每天 00:30 台北 (Gemini 配額 00:00 PT 重置後)
      - financials: 每月 1 號 09:00 台北 (FinMind 月配額重置)

CLI:
    python scripts/alert_quota_reset.py company_profiles
    python scripts/alert_quota_reset.py financials
    python scripts/alert_quota_reset.py company_profiles --dry-run
    python scripts/alert_quota_reset.py financials --no-telegram

Exit:
  0 = 成功(推送成功或 dry-run)
  1 = 嚴重錯誤(unknown kind 等)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.discord_notifier import send_discord_message  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402
from src.notifier import send_telegram_message  # noqa: E402

logger = logging.getLogger(__name__)


# 兩種 alert 的訊息模板 + workflow link
ALERTS: dict[str, dict] = {
    "company_profiles": {
        "title": "⏰ Gemini quota 已重置 (00:00 台北)",
        "workflow_url": (
            "https://github.com/jjen0206/stock-screener/actions/workflows/"
            "backfill-company-profiles-llm-once.yml"
        ),
        "body": (
            "可手動 trigger company_profiles backfill:\n"
            "{url}\n\n"
            "選擇 batch_start/end 參數:\n"
            "- 第一批: 0:500\n"
            "- 後續: 500:1000 / 1000:1500 / 1500:2000 / 2000:end\n\n"
            "每天 1500 req limit, 5s sleep 設定下單批 ~42min 跑完。"
        ),
    },
    "financials": {
        "title": "📅 FinMind 月配額已重置 (1 號)",
        "workflow_url": (
            "https://github.com/jjen0206/stock-screener/actions/workflows/"
            "backfill-financials-once.yml"
        ),
        "body": (
            "可手動 trigger financials backfill:\n"
            "{url}\n\n"
            "選擇參數: years=5 force=false (skip 已有 sid), batch=200 (預設)\n"
            "全部 ~2060 檔, 跑 ~10 次 dispatch 完成 (每次間隔 30 min)。"
        ),
    },
}


def format_message(kind: str) -> str:
    spec = ALERTS[kind]
    return f"{spec['title']}\n{spec['body'].format(url=spec['workflow_url'])}"


def run(
    kind: str,
    dry_run: bool = False,
    send_telegram: bool = True,
    send_discord: bool = True,
) -> dict:
    if kind not in ALERTS:
        raise ValueError(
            f"unknown kind {kind!r}; expected one of {sorted(ALERTS)}"
        )

    msg = format_message(kind)
    result: dict = {
        "kind": kind,
        "message": msg,
        "channels": {"telegram": None, "discord": None},
        "pushed": False,
    }

    print(f"[QUOTA-ALERT] {kind}:\n{msg}", flush=True)

    if dry_run:
        return result

    if send_telegram:
        result["channels"]["telegram"] = send_telegram_message(msg, parse_mode="")
    if send_discord:
        result["channels"]["discord"] = send_discord_message(msg)
    result["pushed"] = any(v for v in result["channels"].values() if v)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="Quota reset reminder")
    p.add_argument(
        "kind",
        choices=sorted(ALERTS),
        help="哪個 backfill 的 quota 已重置",
    )
    p.add_argument("--dry-run", action="store_true", help="不真送,只 print")
    p.add_argument("--no-telegram", action="store_true", help="跳過 Telegram")
    p.add_argument("--no-discord", action="store_true", help="跳過 Discord")
    args = p.parse_args()

    setup_file_logging("alert_quota_reset", level=logging.WARNING, mirror_print=True)

    try:
        run(
            kind=args.kind,
            dry_run=args.dry_run,
            send_telegram=not args.no_telegram,
            send_discord=not args.no_discord,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[QUOTA-ALERT] 嚴重錯誤: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
