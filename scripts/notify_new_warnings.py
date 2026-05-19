"""每日警示 diff 推播(2026-05-19 主公拍板加,精英化方案 B):

比對「昨日生效中警示集合」vs「今日生效中警示集合」,只推「今日新增」的 (sid, warning_type)。
沒新增 → silent(不推空訊息)。

跑點:stock-warnings.yml 在 fetch_stock_warnings.py 之後,17:13 一天一次。

去重:走 alert_dedup 表 key=(sid, "new_warning:warning_type", today),同日重啟也不重推。

Kill-switch:env NEW_WARNINGS_NOTIFY_ENABLED=false → exit 0,不推。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.discord_notifier import send_discord_message  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402
from src.notifier import send_telegram_message  # noqa: E402


logger = logging.getLogger(__name__)
TAIPEI_TZ = timezone(timedelta(hours=8))


def _today_taipei_iso() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")


def _yesterday_taipei_iso() -> str:
    return (datetime.now(TAIPEI_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


def _is_enabled() -> bool:
    val = os.environ.get("NEW_WARNINGS_NOTIFY_ENABLED", "true").strip().lower()
    return val not in ("false", "0", "no", "off", "")


def _active_warning_set(conn, as_of: str) -> set[tuple[str, str]]:
    """撈在 as_of 仍生效的 (sid, warning_type) tuple 集合。

    生效 = announced_date <= as_of AND (effective_to IS NULL OR effective_to >= as_of)。
    表不存在 / 失敗 → 空集合。
    """
    try:
        from src.warnings_filter import ALL_WARNING_TYPES
        wt_ph = ",".join("?" * len(ALL_WARNING_TYPES))
        rows = conn.execute(
            f"SELECT DISTINCT stock_id, warning_type FROM stock_warnings "
            f"WHERE warning_type IN ({wt_ph}) "
            f"AND announced_date <= ? "
            f"AND (effective_to IS NULL OR effective_to >= ?)",
            list(ALL_WARNING_TYPES) + [as_of, as_of],
        ).fetchall()
        return {(str(r["stock_id"]), str(r["warning_type"])) for r in rows}
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[NEW-WARNINGS] _active_warning_set 失敗: %s: %s",
            type(ex).__name__, ex,
        )
        return set()


def compute_new_warnings(
    today_set: set[tuple[str, str]],
    yesterday_set: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    """回 sorted (sid, warning_type) 今天新增的清單。

    新增 = 今天在,昨天不在(set difference)。空 → []。
    """
    return sorted(today_set - yesterday_set)


def _stock_name(conn, sid: str) -> str:
    try:
        row = conn.execute(
            "SELECT name FROM stocks WHERE stock_id=? LIMIT 1", (sid,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return ""
    return (row["name"] or "") if row else ""


def _dedup_check(conn, sid: str, warning_type: str, today: str) -> bool:
    """alert_dedup 命中 → True(已推過)。表不存在 → False(視同未推)。"""
    key_type = f"new_warning:{warning_type}"
    try:
        row = conn.execute(
            "SELECT 1 FROM alert_dedup "
            "WHERE sid=? AND alert_type=? AND alert_date=? LIMIT 1",
            (sid, key_type, today),
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def _dedup_record(conn, sid: str, warning_type: str, today: str) -> None:
    key_type = f"new_warning:{warning_type}"
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO alert_dedup "
            "(sid, alert_type, alert_date, sent_at, ref_price, threshold) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, key_type, today, now_iso, 0.0, 0.0),
        )
    except Exception as ex:  # noqa: BLE001
        logger.warning("[NEW-WARNINGS] dedup record 失敗: %s", ex)


def format_new_warnings_message(
    diff_pairs: list[tuple[str, str, str]],
    today_iso: str,
    channel: str = "telegram",
) -> str:
    """組推播訊息。空 → "",caller skip。

    diff_pairs: [(sid, warning_type, name), ...]
    """
    if not diff_pairs:
        return ""
    try:
        from src.warnings_filter import WARNING_TYPE_LABELS
    except Exception:  # noqa: BLE001
        WARNING_TYPE_LABELS = {}  # type: ignore[assignment]
    is_html = (channel == "telegram")

    def _h(s: str) -> str:
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    if is_html:
        header = f"⚠️ <b>今日新增警示 ({today_iso})</b>"
    else:
        header = f"⚠️ **今日新增警示 ({today_iso})**"
    lines = [header]
    for sid, wt, name in diff_pairs[:15]:
        label = WARNING_TYPE_LABELS.get(wt, wt)
        if is_html:
            lines.append(
                f"• {_h(sid)} {_h(name)} — 新增「{_h(label)}」"
            )
        else:
            lines.append(f"• {sid} {name} — 新增「{label}」")
    if len(diff_pairs) > 15:
        lines.append(f"… 其他 {len(diff_pairs) - 15} 檔不顯")
    lines.append("")
    lines.append("📌 持倉如命中,建議立即檢視 / 不進場")
    return "\n".join(lines)


def run(
    dry_run: bool = False,
    send_telegram: bool = True,
    send_discord: bool = True,
    db_path: str | Path | None = None,
) -> dict:
    """主流程。回 {n_new, n_pushed, n_dedup_skip, tg_msg}。"""
    if not _is_enabled():
        print("[NEW-WARNINGS] NEW_WARNINGS_NOTIFY_ENABLED=false,跳過", flush=True)
        return {"n_new": 0, "n_pushed": 0, "n_dedup_skip": 0, "tg_msg": ""}

    today = _today_taipei_iso()
    yesterday = _yesterday_taipei_iso()

    with db.get_conn(db_path) as conn:
        today_set = _active_warning_set(conn, today)
        yesterday_set = _active_warning_set(conn, yesterday)
        new_pairs = compute_new_warnings(today_set, yesterday_set)

        # alert_dedup filter
        filtered: list[tuple[str, str, str]] = []
        n_dedup = 0
        for sid, wt in new_pairs:
            if _dedup_check(conn, sid, wt, today):
                n_dedup += 1
                continue
            name = _stock_name(conn, sid)
            filtered.append((sid, wt, name))

        result = {
            "n_new": len(new_pairs),
            "n_pushed": 0,
            "n_dedup_skip": n_dedup,
            "tg_msg": "",
            "dc_msg": "",
        }
        if not filtered:
            print(
                f"[NEW-WARNINGS] today={today} yesterday={yesterday} "
                f"new={len(new_pairs)} all_deduped({n_dedup}) → silent",
                flush=True,
            )
            return result

        tg_msg = format_new_warnings_message(filtered, today, channel="telegram")
        dc_msg = format_new_warnings_message(filtered, today, channel="discord")
        result["tg_msg"] = tg_msg
        result["dc_msg"] = dc_msg

        if dry_run:
            print("\n=== Telegram (HTML) ===\n", flush=True)
            print(tg_msg, flush=True)
            print("\n=== Discord ===\n", flush=True)
            print(dc_msg, flush=True)
            result["n_pushed"] = len(filtered)
            return result

        tg_ok = True
        dc_ok = True
        if send_telegram and config.TELEGRAM_BOT_TOKEN:
            tg_ok = send_telegram_message(tg_msg, parse_mode="HTML")
        if send_discord and config.DISCORD_WEBHOOK_URL:
            dc_ok = send_discord_message(dc_msg)

        # dedup 寫入(不依賴 push 成功 — 主公規則:channel 暫斷也不該下次又狂推)
        for sid, wt, _name in filtered:
            _dedup_record(conn, sid, wt, today)
        conn.commit()

        if tg_ok or dc_ok:
            result["n_pushed"] = len(filtered)
        print(
            f"[NEW-WARNINGS] today={today} new={len(new_pairs)} "
            f"deduped={n_dedup} pushed={result['n_pushed']} "
            f"telegram={'✅' if tg_ok else '❌'} discord={'✅' if dc_ok else '❌'}",
            flush=True,
        )
        return result


def main() -> int:
    p = argparse.ArgumentParser(
        description="每日警示新增 diff 推播(只推新增,silent on no-change)",
    )
    p.add_argument("--dry-run", action="store_true", help="不真送,只 print")
    p.add_argument("--no-telegram", action="store_true")
    p.add_argument("--no-discord", action="store_true")
    args = p.parse_args()
    setup_file_logging("notify_new_warnings", mirror_print=True)
    try:
        run(
            dry_run=args.dry_run,
            send_telegram=not args.no_telegram,
            send_discord=not args.no_discord,
        )
    except Exception as ex:  # noqa: BLE001
        print(
            f"[NEW-WARNINGS] FATAL: {type(ex).__name__}: {ex}",
            file=sys.stderr, flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
