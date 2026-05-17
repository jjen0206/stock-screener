"""盤中觸發告警(30 分鐘 cron):active paper_trades + watchlist 三條件即時推播。

設計:
  - 撈 active paper_trades(status='active')→ 拿 entry_price / current_stop /
    target_price 當「進場區間下緣 / 停損 / 突破壓力」。
  - watchlist 加入 sid 集合(只是擴大 intraday 抓取範圍;沒對應 active trade
    就沒門檻可比，靜默 skip)。
  - 透過 src.intraday.get_intraday_quote 拿即時價;失敗 fallback 用
    daily_prices.close 最新一筆。
  - 對每筆 active trade 比對三條件:
      stop_loss:current_price < current_stop(fallback stop_price)
      entry_zone:current_price ≤ entry_price
      breakout:current_price > target_price
  - 命中 → 查 alert_dedup(sid, alert_type, today)→ 若已存在 skip;否則寫
    dedup row + push Telegram + Discord(各 channel 失敗只 log,不影響 dedup)。

CLI:
    python scripts/intraday_alerts.py              # 正式
    python scripts/intraday_alerts.py --dry-run    # 不推不寫 dedup,只 print
    python scripts/intraday_alerts.py --no-telegram --no-discord

Exit:
  0 = 跑完(無命中也 OK)
  1 = 嚴重錯誤(DB 開不了等)
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Iterable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src import price_alerts as pa  # noqa: E402
from src.discord_notifier import send_discord_message  # noqa: E402
from src.intraday import get_intraday_quote  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402
from src.notifier import send_telegram_message  # noqa: E402

logger = logging.getLogger(__name__)

ALERT_STOP_LOSS = "stop_loss"
ALERT_ENTRY_ZONE = "entry_zone"
ALERT_BREAKOUT = "breakout"
# G 個股價格警報(price_alerts 表)觸發 → 統一打到這個 alert_type
# 寫 alert_dedup,當日不重推。
ALERT_PRICE_ALERT = "price_alert"
ALERT_INTRADAY_DROP = "intraday_drop"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_iso() -> str:
    return _date.today().isoformat()


def load_active_trades(conn: sqlite3.Connection) -> list[dict]:
    """撈 active paper_trades,組成可比對的 dict list。

    回 [{sid, name, entry_price, stop_price, current_stop, target_price}, ...]
    current_stop 為 NULL → fallback 用 stop_price。
    """
    rows = conn.execute(
        """
        SELECT sid, name, entry_price, stop_price, current_stop, target_price
        FROM paper_trades
        WHERE status = 'active'
        """,
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        entry = float(r["entry_price"])
        stop_price = float(r["stop_price"])
        cur_stop_raw = r["current_stop"]
        cur_stop = float(cur_stop_raw) if cur_stop_raw is not None else stop_price
        out.append({
            "sid": str(r["sid"]),
            "name": r["name"] or "",
            "entry_price": entry,
            "stop_price": stop_price,
            "current_stop": cur_stop,
            "target_price": float(r["target_price"]),
        })
    return out


def load_watchlist_sids(conn: sqlite3.Connection) -> set[str]:
    """讀 watchlist.stock_id 集合,擴大 intraday 報價範圍用。"""
    try:
        rows = conn.execute("SELECT stock_id FROM watchlist").fetchall()
        return {str(r["stock_id"]) for r in rows if r["stock_id"]}
    except sqlite3.OperationalError:
        return set()


def _latest_close_map(conn: sqlite3.Connection, sids: Iterable[str]) -> dict[str, float]:
    """daily_prices 每個 sid 最新一筆 close,當 intraday 抓不到時的 fallback。

    回 {sid: close}。
    """
    out: dict[str, float] = {}
    for sid in sids:
        row = conn.execute(
            "SELECT close FROM daily_prices WHERE stock_id=? "
            "ORDER BY date DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if row and row["close"] is not None:
            out[sid] = float(row["close"])
    return out


def fetch_current_prices(
    conn: sqlite3.Connection,
    sids: list[str],
    use_intraday: bool = True,
) -> dict[str, dict]:
    """回 {sid: {current, prev_close, change_pct, source}} — 缺資料就不放。

    source = 'intraday'(yfinance)/ 'daily_close'(SQLite fallback)。
    use_intraday=False 時純走 daily_close 來源(unit test 用)。
    """
    out: dict[str, dict] = {}
    intraday: dict[str, dict | None] = {}
    if use_intraday and sids:
        try:
            intraday = get_intraday_quote(sids)
        except Exception as e:  # noqa: BLE001
            logger.warning("[INTRADAY-ALERTS] intraday 抓失敗,fallback daily: %s", e)
            intraday = {}

    fallback_map = _latest_close_map(conn, sids)
    for sid in sids:
        q = intraday.get(sid)
        if q and q.get("current") is not None:
            out[sid] = {
                "current": float(q["current"]),
                "prev_close": q.get("prev_close"),
                "change_pct": q.get("change_pct"),
                "source": "intraday",
            }
        elif sid in fallback_map:
            close = fallback_map[sid]
            out[sid] = {
                "current": close,
                "prev_close": close,
                "change_pct": 0.0,
                "source": "daily_close",
            }
    return out


def is_already_alerted(
    conn: sqlite3.Connection, sid: str, alert_type: str, alert_date: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM alert_dedup "
        "WHERE sid=? AND alert_type=? AND alert_date=? LIMIT 1",
        (sid, alert_type, alert_date),
    ).fetchone()
    return row is not None


def record_alert(
    conn: sqlite3.Connection,
    sid: str,
    alert_type: str,
    alert_date: str,
    ref_price: float,
    threshold: float,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO alert_dedup "
        "(sid, alert_type, alert_date, sent_at, ref_price, threshold) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, alert_type, alert_date, _now_iso(), ref_price, threshold),
    )


def evaluate_alerts(
    trades: list[dict], prices: dict[str, dict],
) -> list[dict]:
    """對每筆 active trade 比對 3 條件,回 [{sid, name, alert_type, threshold,
    current, change_pct, message}, ...](已命中 + 未去重前的清單)。

    同一 sid 三條件可能同時命中(極端情況),全部回,caller 各自查 dedup。
    """
    out: list[dict] = []
    for t in trades:
        sid = t["sid"]
        q = prices.get(sid)
        if not q or q.get("current") is None:
            continue
        current = float(q["current"])
        change_pct = q.get("change_pct")
        name = t["name"]
        stop_hit = current < t["current_stop"]
        breakout_hit = current > t["target_price"]
        # 跌破停損優先(下行)/ 突破壓力優先(上行),
        # entry_zone 只在 stop < current ≤ entry 時觸發 — 避免「跌破停損」+
        # 「進場時機」同時推造成語意衝突。
        entry_zone_hit = (
            not stop_hit
            and not breakout_hit
            and current <= t["entry_price"]
        )

        if stop_hit:
            out.append({
                "sid": sid,
                "name": name,
                "alert_type": ALERT_STOP_LOSS,
                "threshold": t["current_stop"],
                "current": current,
                "change_pct": change_pct,
                "message": format_alert_message(
                    sid, name, ALERT_STOP_LOSS,
                    t["current_stop"], current, change_pct,
                ),
            })
        if entry_zone_hit:
            out.append({
                "sid": sid,
                "name": name,
                "alert_type": ALERT_ENTRY_ZONE,
                "threshold": t["entry_price"],
                "current": current,
                "change_pct": change_pct,
                "message": format_alert_message(
                    sid, name, ALERT_ENTRY_ZONE,
                    t["entry_price"], current, change_pct,
                ),
            })
        if breakout_hit:
            out.append({
                "sid": sid,
                "name": name,
                "alert_type": ALERT_BREAKOUT,
                "threshold": t["target_price"],
                "current": current,
                "change_pct": change_pct,
                "message": format_alert_message(
                    sid, name, ALERT_BREAKOUT,
                    t["target_price"], current, change_pct,
                ),
            })
    return out


def format_alert_message(
    sid: str, name: str, alert_type: str,
    threshold: float, current: float, change_pct: float | None,
) -> str:
    """組單行告警訊息(Telegram / Discord 共用)。

    範例:
      ⛔ 2330 台積電 跌破停損 1198.00(目前 1195.00,-0.25%)
      💰 2330 台積電 進場時機 832.00(目前 830.00)
      🚀 2330 台積電 突破壓力 900.00(目前 902.00,+0.22%)
    entry_zone 不顯 change_pct(進場時機重點是價位,不是漲跌)。
    """
    sid_label = f"{sid} {name}".strip() if name else sid
    if alert_type == ALERT_STOP_LOSS:
        emoji, action = "⛔", "跌破停損"
    elif alert_type == ALERT_ENTRY_ZONE:
        emoji, action = "💰", "進場時機"
    elif alert_type == ALERT_BREAKOUT:
        emoji, action = "🚀", "突破壓力"
    else:
        emoji, action = "ℹ️", alert_type

    head = f"{emoji} {sid_label} {action} {threshold:.2f}"
    if alert_type == ALERT_ENTRY_ZONE or change_pct is None:
        return f"{head}(目前 {current:.2f})"
    sign = "+" if change_pct >= 0 else ""
    return f"{head}(目前 {current:.2f},{sign}{change_pct:.2f}%)"


def _push_price_alert_candidates(
    conn: sqlite3.Connection,
    candidates: list[dict],
    alert_type_label: str,
    today_iso: str,
    *,
    dry_run: bool,
    send_telegram: bool,
    send_discord: bool,
    stats: dict[str, int],
    mark_triggered_on_push: bool,
) -> None:
    """統一處理 price_alerts engine 算出的 triggered candidates。

    - alert_dedup 同日去重
    - 推 Telegram + Discord(失敗只 log)
    - mark_triggered_on_push=True 表示對 price_alerts row 標 triggered_at + is_active=0
      (用於 manual price_above/below/pct_change/ex_dividend);intraday_drop 系統算的
      則 False(無對應 row)。
    """
    for c in candidates:
        sid = c["stock_id"]
        if is_already_alerted(conn, sid, alert_type_label, today_iso):
            stats["n_skipped_dedup"] += 1
            print(
                f"[INTRADAY-ALERTS] SKIP dedup {sid} {alert_type_label}",
                flush=True,
            )
            continue

        msg = c["message"]
        print(f"[INTRADAY-ALERTS] {msg}", flush=True)

        if dry_run:
            stats["n_pushed"] += 1
            continue

        tg_ok = True
        dc_ok = True
        if send_telegram:
            tg_ok = send_telegram_message(msg, parse_mode="")
        if send_discord:
            dc_ok = send_discord_message(msg)

        ref_price = c.get("current_price")
        threshold = c.get("target_value")
        record_alert(
            conn, sid, alert_type_label, today_iso,
            ref_price=float(ref_price) if ref_price is not None else 0.0,
            threshold=float(threshold) if threshold is not None else 0.0,
        )
        if mark_triggered_on_push and c.get("alert_id"):
            # 走既有 conn(避開 outer BEGIN holding writer lock 時另開連線
            # 撞 database is locked)
            try:
                from datetime import datetime, timezone
                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE price_alerts SET triggered_at=?, is_active=0 "
                    "WHERE id=? AND is_active=1",
                    (ts, int(c["alert_id"])),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[INTRADAY-ALERTS] mark_triggered 失敗 id=%s: %s",
                    c["alert_id"], e,
                )

        if tg_ok or dc_ok:
            stats["n_pushed"] += 1
        else:
            stats["n_failed"] += 1


def run(
    dry_run: bool = False,
    send_telegram: bool = True,
    send_discord: bool = True,
    use_intraday: bool = True,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """主要 entry point — 給 CLI / test 共用。

    回 {n_active_trades, n_candidates, n_pushed, n_skipped_dedup, n_failed,
        n_price_alerts, n_intraday_drops}。
    """
    stats = {
        "n_active_trades": 0,
        "n_candidates": 0,
        "n_pushed": 0,
        "n_skipped_dedup": 0,
        "n_failed": 0,
        "n_price_alerts": 0,
        "n_intraday_drops": 0,
    }
    today_iso = _today_iso()

    with db.get_conn(db_path) as conn:
        trades = load_active_trades(conn)
        stats["n_active_trades"] = len(trades)

        if trades:
            watchlist_sids = load_watchlist_sids(conn)
            trade_sids = {t["sid"] for t in trades}
            # 報價抓 union(active + watchlist),但只對 active trade 比門檻
            all_sids = sorted(trade_sids | watchlist_sids)
            prices = fetch_current_prices(conn, all_sids, use_intraday=use_intraday)

            candidates = evaluate_alerts(trades, prices)
            stats["n_candidates"] = len(candidates)

            for c in candidates:
                if is_already_alerted(conn, c["sid"], c["alert_type"], today_iso):
                    stats["n_skipped_dedup"] += 1
                    print(
                        f"[INTRADAY-ALERTS] SKIP dedup {c['sid']} {c['alert_type']}",
                        flush=True,
                    )
                    continue

                msg = c["message"]
                print(f"[INTRADAY-ALERTS] {msg}", flush=True)

                if dry_run:
                    stats["n_pushed"] += 1
                    continue

                tg_ok = True
                dc_ok = True
                if send_telegram:
                    tg_ok = send_telegram_message(msg, parse_mode="")
                if send_discord:
                    dc_ok = send_discord_message(msg)
                # dedup 寫入不依賴 push 成功(channel 暫斷不該下次又狂推)
                record_alert(
                    conn, c["sid"], c["alert_type"], today_iso,
                    ref_price=c["current"], threshold=c["threshold"],
                )
                if tg_ok or dc_ok:
                    stats["n_pushed"] += 1
                else:
                    stats["n_failed"] += 1
        else:
            print("[INTRADAY-ALERTS] 無 active paper_trades,跳過 trade 條件檢查", flush=True)

        # === G:price_alerts(主公手動設)+ intraday_drop(持倉急殺)===
        if pa.is_enabled():
            try:
                pa_candidates = pa.check_price_alerts(conn)
            except Exception as e:  # noqa: BLE001
                logger.warning("[INTRADAY-ALERTS] check_price_alerts 失敗: %s", e)
                pa_candidates = []
            stats["n_price_alerts"] = len(pa_candidates)
            _push_price_alert_candidates(
                conn, pa_candidates, ALERT_PRICE_ALERT, today_iso,
                dry_run=dry_run,
                send_telegram=send_telegram,
                send_discord=send_discord,
                stats=stats,
                mark_triggered_on_push=True,
            )

            try:
                drop_candidates = pa.check_intraday_drop(conn)
            except Exception as e:  # noqa: BLE001
                logger.warning("[INTRADAY-ALERTS] check_intraday_drop 失敗: %s", e)
                drop_candidates = []
            stats["n_intraday_drops"] = len(drop_candidates)
            _push_price_alert_candidates(
                conn, drop_candidates, ALERT_INTRADAY_DROP, today_iso,
                dry_run=dry_run,
                send_telegram=send_telegram,
                send_discord=send_discord,
                stats=stats,
                mark_triggered_on_push=False,
            )
        else:
            print("[INTRADAY-ALERTS] PRICE_ALERT_ENABLED=false,跳過 price alerts", flush=True)

        conn.commit()

    print(f"[INTRADAY-ALERTS] done: {stats}", flush=True)
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description="盤中觸發告警(active paper_trades)")
    p.add_argument("--dry-run", action="store_true", help="不真送,不寫 dedup")
    p.add_argument("--no-telegram", action="store_true", help="跳過 Telegram")
    p.add_argument("--no-discord", action="store_true", help="跳過 Discord")
    p.add_argument(
        "--no-intraday", action="store_true",
        help="不打 yfinance,純用 daily_prices 最新一筆當現價(debug 用)",
    )
    args = p.parse_args()

    setup_file_logging("intraday_alerts", level=logging.WARNING, mirror_print=True)

    try:
        preload_counts = db.preload_snapshots()
        if preload_counts:
            print(f"[INTRADAY-ALERTS] preload snapshots: {preload_counts}", flush=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("[INTRADAY-ALERTS] preload_snapshots 失敗(忽略): %s", e)

    try:
        run(
            dry_run=args.dry_run,
            send_telegram=not args.no_telegram,
            send_discord=not args.no_discord,
            use_intraday=not args.no_intraday,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[INTRADAY-ALERTS] 嚴重錯誤: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
