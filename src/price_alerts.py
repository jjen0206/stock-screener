"""G 個股價格警報引擎(2026-05-17 加)。

對 `price_alerts` 表的 active 警報依類型比對當前股價,回觸發清單給 caller
(intraday_alerts / morning_brief / daily_notify)寫 dedup + 推播。

Kill-switch:env `PRICE_ALERT_ENABLED=true`(預設 on)。off 時所有 check_*
直接回 []。

主要 API:
- `is_enabled() -> bool`
- `check_price_alerts(conn)` — price_above / price_below / pct_change / ex_dividend
- `check_intraday_drop(conn, threshold_pct=-3.0)` — 持倉股當日急殺
- `check_ex_dividend_alerts(conn, days_ahead=3)` — 持倉 / watchlist N 日內除權息
- `format_alert_message(...)` — 推播訊息格式(Telegram / Discord 共用)

每個 check_* 都回 list[dict] 結構:
    {
      "alert_id": int | None,    # price_alerts.id;intraday_drop / ex_dividend
                                  # 系統算的可能無對應 row → None
      "stock_id": str,
      "alert_type": str,
      "target_value": float | None,
      "current_price": float | None,
      "message": str,
    }
intraday_drop / ex_dividend(系統算的)不寫 price_alerts row,只交給 caller
推播 + alert_dedup 去重。caller 自行決定要不要 mark_triggered(對手動設的)。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# 預設「當日急殺」門檻(%)。負數表跌幅。
DEFAULT_INTRADAY_DROP_PCT = -3.0
# 持倉「重大急跌」門檻 — 2026-05-19 主公拍板加(精英化方案 B):
# 跌 -5%+ 立即推「⚠️ XXXX 急跌 -5.2%,建議檢視持倉」。
DEFAULT_HOLDING_SEVERE_DROP_PCT = -5.0
# 預設「除權息倒數」天數。
DEFAULT_EX_DIVIDEND_DAYS_AHEAD = 3


def is_enabled() -> bool:
    """讀 env PRICE_ALERT_ENABLED(預設 true)。"""
    raw = os.getenv("PRICE_ALERT_ENABLED", "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def _today_iso() -> str:
    return _date.today().isoformat()


def _latest_close(conn: sqlite3.Connection, sid: str) -> float | None:
    """daily_prices 最新一筆 close;沒資料 / NULL 回 None。"""
    row = conn.execute(
        "SELECT close FROM daily_prices WHERE stock_id=? "
        "ORDER BY date DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if not row or row["close"] is None:
        return None
    try:
        return float(row["close"])
    except (TypeError, ValueError):
        return None


def _stock_name(conn: sqlite3.Connection, sid: str) -> str:
    """從 stocks 拿名稱,沒資料回空字串。"""
    try:
        row = conn.execute(
            "SELECT name FROM stocks WHERE stock_id=? LIMIT 1", (sid,),
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    return (row["name"] or "") if row else ""


def _parse_baseline_from_notes(notes: str | None) -> float | None:
    """從 notes 字串撈 base=XXX 當 pct_change baseline。

    例:notes='base=600' → 600.0;沒有 / 解析失敗 → None。
    """
    if not notes:
        return None
    for token in str(notes).replace(",", " ").split():
        token = token.strip().lower()
        if token.startswith("base="):
            try:
                return float(token[len("base="):])
            except (TypeError, ValueError):
                return None
    return None


def format_alert_message(
    sid: str,
    name: str,
    alert_type: str,
    target_value: float | None,
    current_price: float | None,
    *,
    triggered_at: str | None = None,
    extra: str | None = None,
) -> str:
    """組單行警報訊息(Telegram / Discord 共用)。

    範例:
        🚨 警報觸發
        2330 台積電 當前 $612 達到設定價位 ≥ $610
        [2026-05-17 13:42]
        建議: 確認進場/出場決策
    """
    sid_label = f"{sid} {name}".strip() if name else sid
    cur_str = f"${current_price:.2f}" if current_price is not None else "—"
    tv_str = f"${target_value:.2f}" if target_value is not None else "—"
    if alert_type == "price_above":
        line = f"{sid_label} 當前 {cur_str} 達到設定價位 ≥ {tv_str}"
    elif alert_type == "price_below":
        line = f"{sid_label} 當前 {cur_str} 跌破設定價位 ≤ {tv_str}"
    elif alert_type == "pct_change":
        pct = target_value if target_value is not None else 0.0
        line = (
            f"{sid_label} 當前 {cur_str} 漲跌幅達 ±{pct:.2f}% 門檻"
        )
    elif alert_type == "ex_dividend":
        days = int(target_value) if target_value is not None else 0
        line = f"{sid_label} {days} 日內除權息"
    elif alert_type == "intraday_drop":
        pct = target_value if target_value is not None else 0.0
        line = (
            f"{sid_label} 當前 {cur_str} 當日跌幅 ≤ {pct:.2f}%(持倉急殺)"
        )
    else:
        line = f"{sid_label} 當前 {cur_str}(警報類型 {alert_type})"

    ts = triggered_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = ["🚨 警報觸發", line, f"[{ts}]"]
    if extra:
        parts.append(extra)
    else:
        parts.append("建議: 確認進場/出場決策")
    return "\n".join(parts)


def _eval_single_alert(
    conn: sqlite3.Connection, alert: dict,
) -> dict | None:
    """對單筆 price_alerts row 算是否觸發。回 triggered dict 或 None。"""
    sid = str(alert.get("stock_id") or "").strip()
    if not sid:
        return None
    alert_type = alert.get("alert_type")
    target = alert.get("target_value")
    notes = alert.get("notes")
    cur = _latest_close(conn, sid)
    name = _stock_name(conn, sid)

    triggered = False
    if alert_type == "price_above":
        if cur is not None and target is not None and cur >= float(target):
            triggered = True
    elif alert_type == "price_below":
        if cur is not None and target is not None and cur <= float(target):
            triggered = True
    elif alert_type == "pct_change":
        if cur is not None and target is not None:
            baseline = _parse_baseline_from_notes(notes)
            if baseline is None or baseline == 0:
                # 沒填 baseline → 視為無法評估,不觸發(避免誤觸)
                return None
            change_pct = abs(cur - baseline) / abs(baseline) * 100.0
            if change_pct >= float(target):
                triggered = True
    elif alert_type == "ex_dividend":
        days_ahead = int(target) if target is not None else DEFAULT_EX_DIVIDEND_DAYS_AHEAD
        if _is_ex_dividend_within(conn, sid, days_ahead):
            triggered = True
    elif alert_type == "intraday_drop":
        # 手動建的 intraday_drop:target 是門檻 %(負數),拿當日漲跌 vs 門檻
        if cur is not None and target is not None:
            change_pct = _current_change_pct(conn, sid, cur)
            if change_pct is not None and change_pct <= float(target):
                triggered = True
    else:
        return None

    if not triggered:
        return None
    return {
        "alert_id": alert.get("id"),
        "stock_id": sid,
        "name": name,
        "alert_type": alert_type,
        "target_value": (float(target) if target is not None else None),
        "current_price": cur,
        "message": format_alert_message(
            sid, name, alert_type,
            (float(target) if target is not None else None),
            cur,
        ),
    }


def check_price_alerts(
    conn: sqlite3.Connection,
) -> list[dict]:
    """對所有 active price_alerts 比對當前價,回觸發清單。

    包含手動建的 price_above / price_below / pct_change / ex_dividend /
    intraday_drop(手動門檻)— 系統算的 intraday_drop / ex_dividend 用各自
    專用 check_* 函式。
    """
    if not is_enabled():
        return []
    rows = conn.execute(
        "SELECT * FROM price_alerts WHERE is_active=1 ORDER BY created_at"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            res = _eval_single_alert(conn, dict(r))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[PRICE-ALERTS] eval 失敗 id=%s sid=%s: %s",
                r["id"], r["stock_id"], e,
            )
            continue
        if res:
            out.append(res)
    return out


def _current_change_pct(
    conn: sqlite3.Connection, sid: str, current_price: float,
) -> float | None:
    """算當日 vs 前一交易日的漲跌幅 %。

    取 daily_prices 最近兩根 close,(latest - prev) / prev * 100。若 current
    跟 latest 差異夠大(intraday 即時報價),用 current vs latest 算「相對
    最後收盤」的當日漲跌。
    """
    rows = conn.execute(
        "SELECT close FROM daily_prices WHERE stock_id=? "
        "ORDER BY date DESC LIMIT 2",
        (sid,),
    ).fetchall()
    if not rows:
        return None
    try:
        latest = float(rows[0]["close"]) if rows[0]["close"] is not None else None
    except (TypeError, ValueError):
        latest = None
    if latest is None or latest <= 0:
        return None
    # current 跟 latest 近(差 < 0.5%)→ 視為「latest 就是今日 close」,
    # 拿 prev 當基準;否則 current 跟 latest 不同(intraday tick)→ 拿 latest
    # 當前收盤 baseline。
    if abs(current_price - latest) / latest < 0.005 and len(rows) >= 2:
        try:
            prev = (
                float(rows[1]["close"]) if rows[1]["close"] is not None else None
            )
        except (TypeError, ValueError):
            prev = None
        if prev and prev > 0:
            return (current_price - prev) / prev * 100.0
        return None
    return (current_price - latest) / latest * 100.0


def check_holding_severe_drop(
    conn: sqlite3.Connection,
    threshold_pct: float = DEFAULT_HOLDING_SEVERE_DROP_PCT,
) -> list[dict]:
    """持倉 + watchlist 急跌警報(2026-05-19 主公拍板加,精英化方案 B):
    跌 ≤ -5% 立即推「⚠️ XXXX 急跌 -5.2%,建議檢視持倉(停損 XX.XX)」。

    來源:user_positions(is_open=1) ∪ watchlist。系統算的(無對應 price_alerts
    row),alert_type='holding_severe_drop'。caller 走 alert_dedup 防同日重推。

    跟既有 check_intraday_drop(-3% 一般警報、只持倉)分開:
    - 門檻較嚴(-5% vs -3%)
    - 來源較廣(holdings + watchlist vs 只 holdings)
    - 訊息更急(「急跌」字眼)
    """
    if not is_enabled():
        return []
    sids: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT stock_id FROM user_positions WHERE is_open=1"
        ).fetchall()
        for r in rows:
            sid = str(r["stock_id"] or "").strip()
            if sid:
                sids.add(sid)
    except sqlite3.OperationalError:
        pass
    try:
        rows = conn.execute("SELECT stock_id FROM watchlist").fetchall()
        for r in rows:
            sid = str(r["stock_id"] or "").strip()
            if sid:
                sids.add(sid)
    except sqlite3.OperationalError:
        pass
    if not sids:
        return []

    # 同 stop_loss 撈一份(open position 才有自己的停損)— 缺則略
    stop_map: dict[str, float] = {}
    try:
        for r in conn.execute(
            "SELECT stock_id, stop_loss FROM user_positions "
            "WHERE is_open=1 AND stop_loss IS NOT NULL"
        ).fetchall():
            try:
                stop_map[str(r["stock_id"])] = float(r["stop_loss"])
            except (TypeError, ValueError):
                continue
    except sqlite3.OperationalError:
        pass

    out: list[dict] = []
    for sid in sorted(sids):
        cur = _latest_close(conn, sid)
        if cur is None:
            continue
        change = _current_change_pct(conn, sid, cur)
        if change is None or change > threshold_pct:
            continue
        name = _stock_name(conn, sid)
        stop_val = stop_map.get(sid)
        stop_part = f"(停損 {stop_val:.2f})" if stop_val else ""
        # 訊息精簡(spec):「⚠️ 2330 急跌 -5.2%,建議檢視持倉」
        sid_label = f"{sid} {name}".strip() if name else sid
        msg = (
            f"⚠️ {sid_label} 急跌 {change:.1f}%,建議檢視持倉{stop_part}"
        )
        out.append({
            "alert_id": None,
            "stock_id": sid,
            "name": name,
            "alert_type": "holding_severe_drop",
            "target_value": float(threshold_pct),
            "current_price": cur,
            "change_pct": change,
            "message": msg,
        })
    return out


def check_intraday_drop(
    conn: sqlite3.Connection,
    threshold_pct: float = DEFAULT_INTRADAY_DROP_PCT,
) -> list[dict]:
    """對所有 open user_positions 算當日跌幅。跌幅 ≤ threshold → 警報。

    threshold_pct 預設 -3.0(負數)。系統算的(無對應 price_alerts row),
    caller 應走 alert_dedup 防同日重推。回 triggered list(同 _eval_single_alert
    的格式,但 alert_id=None)。
    """
    if not is_enabled():
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT stock_id FROM user_positions WHERE is_open=1"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[dict] = []
    for r in rows:
        sid = str(r["stock_id"]) if r["stock_id"] else ""
        if not sid:
            continue
        cur = _latest_close(conn, sid)
        if cur is None:
            continue
        change = _current_change_pct(conn, sid, cur)
        if change is None or change > threshold_pct:
            continue
        name = _stock_name(conn, sid)
        out.append({
            "alert_id": None,
            "stock_id": sid,
            "name": name,
            "alert_type": "intraday_drop",
            "target_value": float(threshold_pct),
            "current_price": cur,
            "change_pct": change,
            "message": format_alert_message(
                sid, name, "intraday_drop",
                float(threshold_pct), cur,
                extra=f"當日跌幅 {change:.2f}%・確認是否觸停損",
            ),
        })
    return out


def _is_ex_dividend_within(
    conn: sqlite3.Connection, sid: str, days_ahead: int,
) -> bool:
    """檢查 sid 是否在 N 日內除權息(從 dividend.ex_dividend_date 算)。"""
    try:
        rows = conn.execute(
            "SELECT ex_dividend_date FROM dividend "
            "WHERE stock_id=? AND ex_dividend_date IS NOT NULL",
            (sid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    today = _date.today()
    for r in rows:
        raw = r["ex_dividend_date"]
        if not raw:
            continue
        try:
            ex = _date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError):
            continue
        delta = (ex - today).days
        if 0 <= delta <= int(days_ahead):
            return True
    return False


def check_ex_dividend_alerts(
    conn: sqlite3.Connection,
    days_ahead: int = DEFAULT_EX_DIVIDEND_DAYS_AHEAD,
) -> list[dict]:
    """對持倉 + watchlist 股票算 N 日內除權息提醒。

    TODO: 目前 dividend.ex_dividend_date 來源是 FinMind 年度配息表,可能無
    當年最新 ex 日;沒資料就靜默 skip。改用 TWSE 即時除權息日程 endpoint 可
    更準(尚未接)。
    """
    if not is_enabled():
        return []
    sids: set[str] = set()
    try:
        for r in conn.execute(
            "SELECT DISTINCT stock_id FROM user_positions WHERE is_open=1"
        ).fetchall():
            if r["stock_id"]:
                sids.add(str(r["stock_id"]))
    except sqlite3.OperationalError:
        pass
    try:
        for r in conn.execute("SELECT stock_id FROM watchlist").fetchall():
            if r["stock_id"]:
                sids.add(str(r["stock_id"]))
    except sqlite3.OperationalError:
        pass

    out: list[dict] = []
    for sid in sorted(sids):
        if not _is_ex_dividend_within(conn, sid, days_ahead):
            continue
        cur = _latest_close(conn, sid)
        name = _stock_name(conn, sid)
        out.append({
            "alert_id": None,
            "stock_id": sid,
            "name": name,
            "alert_type": "ex_dividend",
            "target_value": float(days_ahead),
            "current_price": cur,
            "message": format_alert_message(
                sid, name, "ex_dividend",
                float(days_ahead), cur,
                extra=f"{days_ahead} 日內除權息・留意是否要參與",
            ),
        })
    return out


__all__ = [
    "DEFAULT_INTRADAY_DROP_PCT",
    "DEFAULT_HOLDING_SEVERE_DROP_PCT",
    "DEFAULT_EX_DIVIDEND_DAYS_AHEAD",
    "is_enabled",
    "check_price_alerts",
    "check_intraday_drop",
    "check_holding_severe_drop",
    "check_ex_dividend_alerts",
    "format_alert_message",
]
