"""CLI:給 GitHub Actions workflow 收尾呼叫,寫 heartbeat 到
data/twse_snapshot/sync_log_heartbeat.csv。

Usage:
    python scripts/heartbeat_record.py success morning_brief --interval 24
    python scripts/heartbeat_record.py failure news_notify --reason "TWSE 503" --interval 1

Exit:
  0 = 成功(寫入或無需寫入)
  1 = 參數錯誤
  2 = 寫入失敗
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.system_monitoring import heartbeat  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(
        description="Record cron task heartbeat (success / failure)"
    )
    p.add_argument("status", choices=["success", "failure"])
    p.add_argument("task_name", help="e.g. morning_brief, daily_notify")
    p.add_argument(
        "--interval", type=float, default=None,
        help="expected_interval_hours (success 必填,failure 首次出現必填)"
    )
    p.add_argument(
        "--reason", type=str, default="",
        help="failure 原因(success 忽略)",
    )
    args = p.parse_args()

    try:
        if args.status == "success":
            if args.interval is None:
                print("[HEARTBEAT] --interval 必填 for success", file=sys.stderr)
                return 1
            row = heartbeat.record_success(args.task_name, args.interval)
        else:
            row = heartbeat.record_failure(
                args.task_name,
                reason=args.reason or "(no reason given)",
                expected_interval_hours=args.interval,
            )
        print(f"[HEARTBEAT] {args.status} -> {row}", flush=True)
        return 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
