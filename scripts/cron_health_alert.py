"""每日 18:00 TW cron:掃 sync_log_heartbeat 找 stale / recently-failed tasks,
推 Telegram + Discord 警告主公。

設計:
  - preload_snapshots() 把 sync_log_heartbeat.csv 灌進 SQLite
  - find_stale_tasks:last_success > expected_interval * stale_multiplier
  - find_recent_failures:24h 內 failure(但已恢復也顯示,告訴主公哪裡 flaky)
  - 全好 → silent;每週日 weekly_checkpoint=true 強制推「過去 7 天健康度」

CLI:
    python scripts/cron_health_alert.py
    python scripts/cron_health_alert.py --dry-run
    python scripts/cron_health_alert.py --weekly-checkpoint    # 不管有沒有 stale 都推
    python scripts/cron_health_alert.py --no-telegram --no-discord
    python scripts/cron_health_alert.py --multiplier 1.5

Exit:
  0 = 成功(無論是否有 stale)
  1 = preload / DB 嚴重錯誤
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.discord_notifier import send_discord_message  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402
from src.notifier import send_telegram_message  # noqa: E402
from src.system_monitoring import heartbeat  # noqa: E402

logger = logging.getLogger(__name__)

TPE = timezone(timedelta(hours=8))


def _fmt_taipei(iso_utc: str | None) -> str:
    """ISO UTC → 台北時間「2026-05-18 08:30」格式;None → '從未'。"""
    if not iso_utc:
        return "從未"
    try:
        s = iso_utc.rstrip("Z") + "+00:00" if iso_utc.endswith("Z") else iso_utc
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TPE).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_utc


def _fmt_hours(h: float | None) -> str:
    if h is None:
        return "?"
    if h < 24:
        return f"{h:.1f} hr"
    return f"{h/24:.1f} d"


def format_message(
    stale: list[dict],
    recent_failures: list[dict],
    weekly_checkpoint: bool,
    now_tpe: datetime,
) -> str:
    """組多行訊息。

    stale 非空 / recent_failures 非空 → 警告訊息
    都空 + weekly_checkpoint → 「全綠」checkpoint
    都空 + 平日 → caller 不該 call(會 silent)
    """
    ts = now_tpe.strftime("%Y-%m-%d %H:%M")

    if not stale and not recent_failures:
        # weekly checkpoint 全綠
        return (
            f"✅ 系統健康度週報 [{ts} 台北]\n\n"
            "過去 7 天所有 cron task 都至少成功一次,沒有 stale 也沒有最近 failure。"
        )

    lines = [f"⚠️ 系統健康度警告 [{ts} 台北]", ""]

    if stale:
        # 區分「從未成功」vs「曾成功但 stale」
        never = [s for s in stale if s["hours_since_success"] is None]
        elapsed = [s for s in stale if s["hours_since_success"] is not None]

        lines.append(f"🔴 {len(stale)} 個任務超時未成功:")
        for s in elapsed:
            lines.append(
                f"- {s['task_name']}:上次成功 {_fmt_taipei(s['last_success_at'])}"
                f"({_fmt_hours(s['hours_since_success'])} 前,"
                f"正常 {_fmt_hours(s['expected_interval_hours'])})"
            )
        for s in never:
            lines.append(
                f"- {s['task_name']}:從未成功(預期間隔 "
                f"{_fmt_hours(s['expected_interval_hours'])})"
            )
        lines.append("")

    if recent_failures:
        # 過濾掉「已經在 stale 列表的」避免重複(stale 訊息已涵蓋)
        stale_names = {s["task_name"] for s in stale}
        unique = [f for f in recent_failures if f["task_name"] not in stale_names]
        if unique:
            lines.append(f"🟡 {len(unique)} 個任務最近 24h 內失敗(可能已恢復):")
            for f in unique:
                reason = (f.get("last_failure_reason") or "(無原因)")[:80]
                lines.append(
                    f"- {f['task_name']}:{_fmt_taipei(f['last_failure_at'])} 失敗,"
                    f"原因「{reason}」"
                )
            lines.append("")

    lines.append("查看 GitHub Actions runs:")
    lines.append("https://github.com/jjen0206/stock-screener/actions")
    return "\n".join(lines)


def run(
    dry_run: bool = False,
    send_telegram: bool = True,
    send_discord: bool = True,
    weekly_checkpoint: bool = False,
    stale_multiplier: float = 2.0,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> dict:
    """主 entry。回 {stale, recent_failures, pushed, channels, message}。"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    stale = heartbeat.find_stale_tasks(
        db_path=db_path, now=now, stale_multiplier=stale_multiplier,
    )
    recent_failures = heartbeat.find_recent_failures(
        db_path=db_path, now=now, window_hours=24.0,
    )

    result: dict = {
        "stale_count": len(stale),
        "stale": stale,
        "recent_failures": recent_failures,
        "pushed": False,
        "channels": {"telegram": None, "discord": None},
        "message": None,
    }

    if not stale and not recent_failures and not weekly_checkpoint:
        print("[CRON-HEALTH] 全部 task fresh,silent", flush=True)
        return result

    msg = format_message(stale, recent_failures, weekly_checkpoint, now.astimezone(TPE))
    result["message"] = msg
    print(f"[CRON-HEALTH]\n{msg}", flush=True)

    if dry_run:
        return result

    if send_telegram:
        result["channels"]["telegram"] = send_telegram_message(msg, parse_mode="")
    if send_discord:
        result["channels"]["discord"] = send_discord_message(msg)
    result["pushed"] = any(v for v in result["channels"].values() if v)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="cron task heartbeat 健康度告警")
    p.add_argument("--dry-run", action="store_true", help="不真送,只 print")
    p.add_argument("--no-telegram", action="store_true")
    p.add_argument("--no-discord", action="store_true")
    p.add_argument(
        "--weekly-checkpoint", action="store_true",
        help="即使無 stale 也推「全綠」checkpoint(週日用)",
    )
    p.add_argument(
        "--multiplier", type=float, default=2.0,
        help="stale 容忍倍數(預設 2.0)",
    )
    args = p.parse_args()

    setup_file_logging("cron_health_alert", level=logging.WARNING, mirror_print=True)

    try:
        preload_counts = db.preload_snapshots()
        if preload_counts:
            print(f"[CRON-HEALTH] preload snapshots: {preload_counts}", flush=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("[CRON-HEALTH] preload_snapshots 失敗(忽略): %s", e)

    try:
        run(
            dry_run=args.dry_run,
            send_telegram=not args.no_telegram,
            send_discord=not args.no_discord,
            weekly_checkpoint=args.weekly_checkpoint,
            stale_multiplier=args.multiplier,
        )
        return 0
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
