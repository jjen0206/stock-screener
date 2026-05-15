"""每日 09:00 TW cron:檢查 5 個關鍵表新鮮度,stale → push Telegram + Discord。

設計:
  - 每天早上掃 daily_prices / institutional / shareholder_concentration /
    pick_outcomes / daily_picks 最新日期。
  - 各表閾值:
      daily_prices             > 1 交易日 stale → ⚠️
      institutional            > 1 交易日 stale → ⚠️
      shareholder_concentration> 8 calendar 天 stale → ⚠️
      pick_outcomes            > 1 交易日 stale → ⚠️
      daily_picks              > 1 交易日 stale → ⚠️
  - 任一表 stale → 整合成「⚠️ 資料新鮮度警告」一則訊息推 Telegram + Discord。
  - 全部 fresh → silent(不推「全好」訊息,避免每日噪音)。

CLI:
    python scripts/data_health_alert.py
    python scripts/data_health_alert.py --dry-run
    python scripts/data_health_alert.py --no-telegram --no-discord

Exit:
  0 = 成功(無論 stale 與否)
  1 = 嚴重錯誤(DB 開不了等)
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date as _date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.discord_notifier import send_discord_message  # noqa: E402
from src.notifier import send_telegram_message  # noqa: E402

logger = logging.getLogger(__name__)


# 各表閾值定義 — (table_label, latest_query, threshold, kind)
# kind = 'trading' 用 weekday 計算,'calendar' 用日曆天
CHECKS: list[dict] = [
    {
        "table": "daily_prices",
        "label": "daily_prices",
        "sql": "SELECT MAX(date) AS m FROM daily_prices WHERE stock_id != 'TAIEX'",
        "threshold": 1,
        "kind": "trading",
    },
    {
        "table": "institutional",
        "label": "institutional",
        "sql": "SELECT MAX(date) AS m FROM institutional",
        "threshold": 1,
        "kind": "trading",
    },
    {
        "table": "shareholder_concentration",
        "label": "shareholder_concentration",
        "sql": "SELECT MAX(week_end) AS m FROM shareholder_concentration",
        "threshold": 8,
        "kind": "calendar",
    },
    {
        "table": "pick_outcomes",
        "label": "pick_outcomes",
        "sql": "SELECT MAX(pick_date) AS m FROM pick_outcomes",
        "threshold": 1,
        "kind": "trading",
    },
    {
        "table": "daily_picks",
        "label": "daily_picks",
        "sql": "SELECT MAX(trade_date) AS m FROM daily_picks",
        "threshold": 1,
        "kind": "trading",
    },
]


def _today() -> _date:
    return _date.today()


def _parse_iso(d: str | None) -> _date | None:
    if not d:
        return None
    try:
        return _date.fromisoformat(d[:10])
    except (ValueError, IndexError):
        return None


def _trading_days_between(later: _date, earlier: _date) -> int:
    """「today 之前已過的交易日,扣 earlier 後還剩幾個」— 不算 later 那天自己。

    rationale:cron 跑在 09:00 TW,當日 TWSE 資料尚未公布 → 不該因為「latest =
    昨天」就警告。Walk earlier+1 ~ later-1 計 weekday 數。

    same day → 0;later < earlier 防守回 0;不扣台股國定假日(會輕微高估 stale,
    寧可多推也別漏)。
    """
    if later <= earlier:
        return 0
    n = 0
    cur = earlier + timedelta(days=1)
    # 排除 later 本日(下載當日的資料還沒公布)
    while cur < later:
        if cur.weekday() < 5:  # 0=Mon ... 4=Fri
            n += 1
        cur += timedelta(days=1)
    return n


def _calendar_days_between(later: _date, earlier: _date) -> int:
    if later <= earlier:
        return 0
    return (later - earlier).days


def _max_date(conn: sqlite3.Connection, sql: str) -> str | None:
    try:
        row = conn.execute(sql).fetchone()
        return row["m"] if row and row["m"] else None
    except sqlite3.OperationalError:
        return None


def audit(conn: sqlite3.Connection, today: _date | None = None) -> list[dict]:
    """回 stale 列表(僅 over threshold 的);每筆含 {table, latest, stale_days,
    kind, threshold, message}。

    table 完全沒資料 → 視為嚴重 stale(stale_days=None,給訊息特殊處理)。
    """
    today = today or _today()
    stale: list[dict] = []
    for chk in CHECKS:
        latest_iso = _max_date(conn, chk["sql"])
        if not latest_iso:
            stale.append({
                "table": chk["table"],
                "label": chk["label"],
                "latest": None,
                "stale_days": None,
                "kind": chk["kind"],
                "threshold": chk["threshold"],
                "message": f"{chk['label']} 無資料",
            })
            continue
        latest = _parse_iso(latest_iso)
        if not latest:
            stale.append({
                "table": chk["table"],
                "label": chk["label"],
                "latest": latest_iso,
                "stale_days": None,
                "kind": chk["kind"],
                "threshold": chk["threshold"],
                "message": f"{chk['label']} 最新日期解析失敗 ({latest_iso})",
            })
            continue
        diff = (
            _trading_days_between(today, latest)
            if chk["kind"] == "trading"
            else _calendar_days_between(today, latest)
        )
        if diff > chk["threshold"]:
            unit = "交易日" if chk["kind"] == "trading" else "天"
            stale.append({
                "table": chk["table"],
                "label": chk["label"],
                "latest": latest_iso,
                "stale_days": diff,
                "kind": chk["kind"],
                "threshold": chk["threshold"],
                "message": (
                    f"{chk['label']} 落後 {diff} {unit}"
                    f"(最新 {latest_iso},閾值 {chk['threshold']} {unit})"
                ),
            })
    return stale


def format_message(stale: list[dict], today: _date | None = None) -> str:
    """組多行整合訊息。stale 必須非空(caller 該先 check)。"""
    today = today or _today()
    lines = [f"⚠️ 資料新鮮度警告({today.isoformat()})"]
    for s in stale:
        lines.append(f"• {s['message']}")
    lines.append("")
    lines.append("(請檢查對應 cron / fetch script)")
    return "\n".join(lines)


def run(
    dry_run: bool = False,
    send_telegram: bool = True,
    send_discord: bool = True,
    db_path: str | Path | None = None,
    today: _date | None = None,
) -> dict:
    """主 entry point。回 {stale: [...], pushed: bool, channels: {...}}。"""
    today = today or _today()
    with db.get_conn(db_path) as conn:
        stale = audit(conn, today=today)

    result: dict = {
        "stale_count": len(stale),
        "stale": stale,
        "pushed": False,
        "channels": {"telegram": None, "discord": None},
        "message": None,
    }

    if not stale:
        print("[DATA-HEALTH] 全部表 fresh,silent", flush=True)
        return result

    msg = format_message(stale, today=today)
    result["message"] = msg
    print(f"[DATA-HEALTH] stale {len(stale)} table(s):\n{msg}", flush=True)

    if dry_run:
        return result

    if send_telegram:
        result["channels"]["telegram"] = send_telegram_message(msg, parse_mode="")
    if send_discord:
        result["channels"]["discord"] = send_discord_message(msg)
    result["pushed"] = any(v for v in result["channels"].values() if v)
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="每日資料新鮮度檢查 + 告警")
    p.add_argument("--dry-run", action="store_true", help="不真送,只 print")
    p.add_argument("--no-telegram", action="store_true", help="跳過 Telegram")
    p.add_argument("--no-discord", action="store_true", help="跳過 Discord")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

    try:
        preload_counts = db.preload_snapshots()
        if preload_counts:
            print(f"[DATA-HEALTH] preload snapshots: {preload_counts}", flush=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("[DATA-HEALTH] preload_snapshots 失敗(忽略): %s", e)

    try:
        run(
            dry_run=args.dry_run,
            send_telegram=not args.no_telegram,
            send_discord=not args.no_discord,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[DATA-HEALTH] 嚴重錯誤: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
