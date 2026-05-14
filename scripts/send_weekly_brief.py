"""週報 Telegram + Discord 推播：軍師系統結論。

每週日台灣時間 10:00（UTC 02:00）由 .github/workflows/weekly-brief.yml 跑:
  1. 讀 SQLite (preload from CSV snapshot)
  2. build_system_brief(conn) 拿軍師結論
  3. format_brief_for_telegram(brief) 包成 markdown
  4. send_telegram_message + send_discord_message 並送（任一個成功即 ok）

Usage:
    python scripts/send_weekly_brief.py
    python scripts/send_weekly_brief.py --dry-run     # 印訊息不推
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 確保 src/ 可 import（scripts/ 走 python scripts/xxx.py 直跑時 sys.path 沒帶 root）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.discord_notifier import send_discord_message  # noqa: E402
from src.notifier import send_telegram_message  # noqa: E402
from src.system_brief import (  # noqa: E402
    build_system_brief,
    format_brief_for_telegram,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="週報 Telegram brief 推播")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只印訊息不真的送 Telegram / Discord",
    )
    args = parser.parse_args()

    db.init_db()
    # 雲端 CI 環境 SQLite 是空的 — 從 data/twse_snapshot/*.csv preload
    try:
        db.preload_snapshots()
    except Exception as e:  # noqa: BLE001
        print(f"[WEEKLY-BRIEF] preload_snapshots failed: {e}", file=sys.stderr)

    with db.get_conn() as conn:
        brief = build_system_brief(conn)

    text = format_brief_for_telegram(brief)
    print("=" * 60)
    print("WEEKLY BRIEF TEXT (len={}):".format(len(text)))
    print("=" * 60)
    print(text)
    print("=" * 60)

    if args.dry_run:
        print("[WEEKLY-BRIEF] --dry-run, 跳過推播")
        return 0

    # 並送 Telegram + Discord，任一成功就算 OK（同 daily_notify pattern）
    tg_ok = send_telegram_message(text)
    dc_ok = send_discord_message(text)
    print(f"[WEEKLY-BRIEF] Telegram={'OK' if tg_ok else 'FAIL'} / "
          f"Discord={'OK' if dc_ok else 'FAIL'}")

    if not (tg_ok or dc_ok):
        print("[WEEKLY-BRIEF] 推播全失敗 — 確認 secrets 是否設定", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
