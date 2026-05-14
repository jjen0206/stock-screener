"""法人目標價異動推播 + 觸目標價提醒(主公拍板 2026-05-08)。

兩個獨立功能跟新聞推播 reuse 同一個 6 類 eligible_sids helper:

1) **法人異動推播**(調升/調降目標價)
   - 觸發點:scripts/fetch_analyst_targets.py upsert 流程
   - 條件:|Δ%| ≥ 5% AND sid ∈ 6 類聯集 AND 同 sid 同日同方向未推過
   - 訊息:📈 / 📉 + tag + 數字變化 + 距現價

2) **觸目標價提醒**(現價達共識)
   - 觸發點:scripts/daily_market_update.py 跑完後
   - 條件:close ≥ target_consensus × 100% AND sid ∈ 6 類聯集 AND 7 日冷卻
   - 訊息:🎯 + tag + 達標幅度

3) **picks 推播加 Δ 標示** — 在 src/notifier.py:format_pick_block 內,reuse
   src.analyst_targets 的 previous_target_mean 欄(本 module 不負責)。

設計重點:
- 6 類 eligible_sids fetcher 是單一 source of truth(news_fetcher 提供)
- 推播失敗(網路 / token)→ 不寫 sent flag,下次重試
- 推播成功 → 寫 alerts / hit_log 表防重複
"""
from __future__ import annotations

import logging
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import config, database as db

logger = logging.getLogger(__name__)


# === 常數(亮預設值)===
CHANGE_THRESHOLD_PCT = 0.05      # 異動推播門檻 5%
HIT_RATIO = 1.0                  # 觸目標價門檻 100%(達或超)
HIT_COOLDOWN_DAYS = 7            # 觸目標 7 日內不重推


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_iso() -> str:
    return _date.today().isoformat()


# === Item 1: 異動推播 ===

def should_alert_change(
    sid: str,
    old_target: float | None,
    new_target: float | None,
    threshold: float = CHANGE_THRESHOLD_PCT,
) -> tuple[bool, str | None, float | None]:
    """判斷此筆 (old, new) 是否觸發異動推播。

    Returns:
        (eligible, direction, delta_pct):
          - eligible: True / False
          - direction: 'up' / 'down' / None
          - delta_pct: 變動百分比(small float,正數=升、負數=降)/ None
    """
    if not sid or old_target is None or new_target is None:
        return False, None, None
    try:
        old_v = float(old_target)
        new_v = float(new_target)
    except (TypeError, ValueError):
        return False, None, None
    if old_v <= 0:
        return False, None, None
    delta_pct = (new_v - old_v) / old_v
    if abs(delta_pct) < threshold:
        return False, None, delta_pct
    direction = "up" if delta_pct > 0 else "down"
    return True, direction, delta_pct


def was_change_alerted(
    sid: str,
    alert_date: str,
    direction: str,
    db_path: str | Path | None = None,
) -> bool:
    """同 sid 同日同方向過去是否已推播過(防重複)。"""
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM analyst_target_alerts "
            "WHERE sid=? AND alert_date=? AND direction=? "
            "AND (sent_telegram=1 OR sent_discord=1) LIMIT 1",
            (sid, alert_date, direction),
        ).fetchone()
    return row is not None


def record_change_alert(
    sid: str,
    alert_date: str,
    direction: str,
    old_target: float | None,
    new_target: float | None,
    sent_telegram: bool,
    sent_discord: bool,
    db_path: str | Path | None = None,
) -> None:
    """寫進 analyst_target_alerts 表(INSERT OR REPLACE 防重複)。"""
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO analyst_target_alerts (
                sid, alert_date, direction, sent_telegram, sent_discord,
                old_target, new_target, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sid, alert_date, direction) DO UPDATE SET
                sent_telegram = analyst_target_alerts.sent_telegram | excluded.sent_telegram,
                sent_discord  = analyst_target_alerts.sent_discord  | excluded.sent_discord
            """,
            (
                sid, alert_date, direction,
                1 if sent_telegram else 0,
                1 if sent_discord else 0,
                old_target, new_target,
                _now_iso(),
            ),
        )


def _name_for(sid: str, db_path: str | Path | None = None) -> str:
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM stocks WHERE stock_id=?", (sid,),
        ).fetchone()
    return row["name"] if row and row["name"] else ""


def _latest_close(sid: str, db_path: str | Path | None = None) -> float | None:
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT close FROM daily_prices WHERE stock_id=? "
            "ORDER BY date DESC LIMIT 1",
            (sid,),
        ).fetchone()
    if row and row["close"] is not None:
        try:
            return float(row["close"])
        except (TypeError, ValueError):
            return None
    return None


def format_target_change_block(
    change: dict[str, Any],
    channel: str = "telegram",
) -> str:
    """組單則異動推播訊息。

    change dict 必含:sid, name, old, new, direction, delta_pct, num_analysts,
                    tags(list[str]), close(可 None)
    格式:
        📈 *台積電 (2330)* [⭐ 關注] 法人調升目標
        共識 1180 → 1240 (+5.1%)  券商 28 家
        現價 1150  距目標 +7.8%
    """
    bold_l = "**" if channel == "discord" else "*"

    sid = str(change.get("sid") or "")
    name = str(change.get("name") or "")
    old = change.get("old")
    new = change.get("new")
    direction = change.get("direction") or "up"
    delta_pct = change.get("delta_pct") or 0.0
    n_analysts = change.get("num_analysts")
    tags = change.get("tags") or []
    close = change.get("close")

    emoji = "📈" if direction == "up" else "📉"
    label = "法人調升目標" if direction == "up" else "法人調降目標"

    # Header: emoji *公司名 (sid)* [tags] label
    header = f"{emoji} {bold_l}{name} ({sid}){bold_l}"
    if tags:
        header += f" [{' · '.join(tags)}]"
    header += f" {label}"
    lines = [header]

    # 共識 X → Y (+5.1%)  券商 N 家
    sign = "+" if delta_pct >= 0 else "-"
    delta_str = f"{sign}{abs(delta_pct) * 100:.1f}%"
    line2 = f"共識 {old:.0f} → {new:.0f} ({delta_str})"
    if n_analysts:
        line2 += f"  券商 {int(n_analysts)} 家"
    lines.append(line2)

    # 現價 + 距目標
    if close and new and close > 0:
        upside = (new - close) / close * 100
        sign_u = "+" if upside >= 0 else "-"
        lines.append(
            f"現價 {close:.2f}  距目標 {sign_u}{abs(upside):.1f}%"
        )
    return "\n".join(lines)


def notify_target_changes(
    changes: list[dict[str, Any]],
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """主流程:filter eligible → 防重複 → format → push TG+Discord → 記錄。

    changes: list of {sid, source, old_target_mean, new_target_mean,
                       num_analysts, fetched_at} from upsert_analyst_target
                       (caller batch 收集)。
    Returns: {n_eligible, n_alerted, n_pushed_telegram, n_pushed_discord}
    """
    if not changes:
        return {
            "n_eligible": 0, "n_alerted": 0,
            "n_pushed_telegram": 0, "n_pushed_discord": 0,
        }

    # 6 類聯集 + tag map
    from src.news_fetcher import compute_news_tags, get_eligible_news_sids
    eligible_groups = get_eligible_news_sids(db_path=db_path)
    eligible_all = eligible_groups.get("all") or set()

    today = _today_iso()
    qualified: list[dict[str, Any]] = []
    for ch in changes:
        sid = str(ch.get("sid") or "")
        if not sid or sid not in eligible_all:
            continue
        eligible, direction, delta_pct = should_alert_change(
            sid, ch.get("old_target_mean"), ch.get("new_target_mean"),
        )
        if not eligible:
            continue
        if was_change_alerted(sid, today, direction, db_path=db_path):
            continue
        qualified.append({
            "sid": sid,
            "name": _name_for(sid, db_path=db_path),
            "old": ch.get("old_target_mean"),
            "new": ch.get("new_target_mean"),
            "direction": direction,
            "delta_pct": delta_pct,
            "num_analysts": ch.get("num_analysts"),
            "close": _latest_close(sid, db_path=db_path),
            "tags": compute_news_tags(sid, eligible_groups),
        })

    if not qualified:
        return {
            "n_eligible": 0, "n_alerted": 0,
            "n_pushed_telegram": 0, "n_pushed_discord": 0,
        }

    # 組訊息(每筆一個 block,用空行隔)
    sep = "\n\n"
    tg_msg = sep.join(
        format_target_change_block(c, "telegram") for c in qualified
    )
    dc_msg = sep.join(
        format_target_change_block(c, "discord") for c in qualified
    )

    # 推播
    sent_tg = False
    sent_dc = False
    if config.TELEGRAM_BOT_TOKEN:
        from src.notifier import send_telegram_message
        # 2026-05-15 主公拍板 Step 1 quick fallback:parse_mode="" 純文字,
        # bypass Markdown entity 解析錯誤(2026-05-14 觸目標推播 byte 2259 全 fail)。
        # Step 2 會回 HTML / MarkdownV2 + 完整 escape 動態字串(公司名 / 備註)。
        sent_tg = bool(send_telegram_message(tg_msg, parse_mode=""))
    if config.DISCORD_WEBHOOK_URL:
        from src.discord_notifier import send_discord_message
        sent_dc = bool(send_discord_message(dc_msg))

    # 記錄(只在實際 sent 才 mark,失敗下次重試)
    if sent_tg or sent_dc:
        for c in qualified:
            record_change_alert(
                sid=c["sid"], alert_date=today, direction=c["direction"],
                old_target=c["old"], new_target=c["new"],
                sent_telegram=sent_tg, sent_discord=sent_dc,
                db_path=db_path,
            )

    return {
        "n_eligible": len(qualified),
        "n_alerted": len(qualified) if (sent_tg or sent_dc) else 0,
        "n_pushed_telegram": len(qualified) if sent_tg else 0,
        "n_pushed_discord": len(qualified) if sent_dc else 0,
    }


# === Item 2: 觸目標價推播 ===

def was_hit_within_cooldown(
    sid: str,
    cooldown_days: int = HIT_COOLDOWN_DAYS,
    db_path: str | Path | None = None,
) -> bool:
    """同 sid 在最近 N 日內是否已推過觸目標(防 7 日內重推)。"""
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT hit_date FROM target_hit_log WHERE sid=? "
            "AND (sent_telegram=1 OR sent_discord=1) "
            "ORDER BY hit_date DESC LIMIT 1",
            (sid,),
        ).fetchone()
    if not rows:
        return False
    try:
        last_hit = _date.fromisoformat(rows["hit_date"])
        days_since = (_date.today() - last_hit).days
        return days_since < cooldown_days
    except (TypeError, ValueError):
        return False


def record_hit(
    sid: str, hit_date: str, close: float, target_consensus: float,
    sent_telegram: bool, sent_discord: bool,
    db_path: str | Path | None = None,
) -> None:
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO target_hit_log (
                sid, hit_date, close, target_consensus,
                sent_telegram, sent_discord, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sid, hit_date) DO UPDATE SET
                close = excluded.close,
                target_consensus = excluded.target_consensus,
                sent_telegram = target_hit_log.sent_telegram | excluded.sent_telegram,
                sent_discord  = target_hit_log.sent_discord  | excluded.sent_discord
            """,
            (
                sid, hit_date, close, target_consensus,
                1 if sent_telegram else 0,
                1 if sent_discord else 0,
                _now_iso(),
            ),
        )


def find_hit_candidates(
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """SELECT 全市場 close ≥ target_mean 的個股(JOIN daily_prices latest)。

    Returns list of dict {sid, name, close, target_consensus, num_analysts,
                          fetched_at}
    """
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        latest = conn.execute(
            "SELECT MAX(date) AS d FROM daily_prices"
        ).fetchone()
        trade_date = latest["d"] if latest else None
        if not trade_date:
            return []
        rows = conn.execute(
            """
            SELECT p.stock_id AS sid,
                   COALESCE(s.name, '') AS name,
                   p.close AS close,
                   a.target_mean AS target_consensus,
                   a.num_analysts AS num_analysts,
                   a.fetched_at AS fetched_at
            FROM daily_prices p
            JOIN analyst_targets a
              ON a.stock_id = p.stock_id
              AND a.source = 'yfinance'   -- 優先 yfinance 較準
            LEFT JOIN stocks s ON s.stock_id = p.stock_id
            WHERE p.date = ?
              AND a.target_mean IS NOT NULL
              AND a.target_mean > 0
              AND p.close >= a.target_mean * ?
            """,
            (trade_date, HIT_RATIO),
        ).fetchall()
    return [dict(r) for r in rows]


def format_target_hit_block(
    hit: dict[str, Any],
    channel: str = "telegram",
) -> str:
    """組單則觸目標推播訊息。

    格式:
        🎯 *台積電 (2330)* [⭐ 關注] 觸法人共識目標
        現價 1245  共識目標 1240  (達 +0.4%)
        券商 28 家  最後更新 2026-05-07
    """
    bold_l = "**" if channel == "discord" else "*"
    sid = str(hit.get("sid") or "")
    name = str(hit.get("name") or "")
    close = float(hit.get("close") or 0)
    target = float(hit.get("target_consensus") or 0)
    n = hit.get("num_analysts")
    fetched = str(hit.get("fetched_at") or "")
    tags = hit.get("tags") or []

    header = f"🎯 {bold_l}{name} ({sid}){bold_l}"
    if tags:
        header += f" [{' · '.join(tags)}]"
    header += " 觸法人共識目標"
    lines = [header]

    if target > 0:
        excess = (close - target) / target * 100
        sign = "+" if excess >= 0 else "-"
        lines.append(
            f"現價 {close:.2f}  共識目標 {target:.2f}  "
            f"(達 {sign}{abs(excess):.1f}%)"
        )
    else:
        lines.append(f"現價 {close:.2f}")

    line3 = ""
    if n:
        line3 += f"券商 {int(n)} 家"
    if fetched:
        # fetched_at iso → date 部分
        date_part = fetched[:10] if len(fetched) >= 10 else fetched
        line3 += f"  最後更新 {date_part}" if line3 else f"最後更新 {date_part}"
    if line3:
        lines.append(line3)
    return "\n".join(lines)


def notify_target_hits(
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """主流程:撈 hit candidates → filter eligible + 7 日冷卻 →
    format → push TG+Discord → 記錄。

    Returns: {n_candidates, n_eligible, n_pushed_telegram, n_pushed_discord}
    """
    candidates = find_hit_candidates(db_path=db_path)
    if not candidates:
        return {
            "n_candidates": 0, "n_eligible": 0,
            "n_pushed_telegram": 0, "n_pushed_discord": 0,
        }

    from src.news_fetcher import compute_news_tags, get_eligible_news_sids
    eligible_groups = get_eligible_news_sids(db_path=db_path)
    eligible_all = eligible_groups.get("all") or set()

    today = _today_iso()
    qualified: list[dict[str, Any]] = []
    for c in candidates:
        sid = str(c.get("sid") or "")
        if not sid or sid not in eligible_all:
            continue
        if was_hit_within_cooldown(sid, db_path=db_path):
            continue
        c["tags"] = compute_news_tags(sid, eligible_groups)
        qualified.append(c)

    if not qualified:
        return {
            "n_candidates": len(candidates), "n_eligible": 0,
            "n_pushed_telegram": 0, "n_pushed_discord": 0,
        }

    sep = "\n\n"
    tg_msg = sep.join(
        format_target_hit_block(c, "telegram") for c in qualified
    )
    dc_msg = sep.join(
        format_target_hit_block(c, "discord") for c in qualified
    )

    sent_tg = False
    sent_dc = False
    if config.TELEGRAM_BOT_TOKEN:
        from src.notifier import send_telegram_message
        # 2026-05-15 Step 1 quick fallback,同上(觸目標 hit 路徑)
        sent_tg = bool(send_telegram_message(tg_msg, parse_mode=""))
    if config.DISCORD_WEBHOOK_URL:
        from src.discord_notifier import send_discord_message
        sent_dc = bool(send_discord_message(dc_msg))

    if sent_tg or sent_dc:
        for c in qualified:
            record_hit(
                sid=c["sid"], hit_date=today,
                close=float(c["close"]),
                target_consensus=float(c["target_consensus"]),
                sent_telegram=sent_tg, sent_discord=sent_dc,
                db_path=db_path,
            )

    return {
        "n_candidates": len(candidates),
        "n_eligible": len(qualified),
        "n_pushed_telegram": len(qualified) if sent_tg else 0,
        "n_pushed_discord": len(qualified) if sent_dc else 0,
    }


__all__ = [
    "CHANGE_THRESHOLD_PCT",
    "HIT_RATIO",
    "HIT_COOLDOWN_DAYS",
    "should_alert_change",
    "was_change_alerted",
    "record_change_alert",
    "format_target_change_block",
    "notify_target_changes",
    "find_hit_candidates",
    "was_hit_within_cooldown",
    "format_target_hit_block",
    "notify_target_hits",
]
