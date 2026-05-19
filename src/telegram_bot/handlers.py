"""Telegram bot message handlers — Intent → reply text + optional inline keyboard。

不直接 send,所有 handler 回 dict `{"text": str, "reply_markup": dict | None}`。
caller(scripts/telegram_bot_serve.py 或 testharness)拿去呼叫 notifier.send_*。
"""
from __future__ import annotations

import logging
from typing import Any

from src.telegram_bot.intent import (
    INTENT_FREEFORM,
    INTENT_HELP,
    INTENT_PAGE_DIGEST,
    INTENT_STOCK_QUERY,
    Intent,
    help_text,
)

logger = logging.getLogger(__name__)


def _reply(text: str, reply_markup: dict | None = None) -> dict[str, Any]:
    return {"text": text, "reply_markup": reply_markup}


# === STOCK_QUERY ===

def _handle_stock_query(sid: str) -> dict[str, Any]:
    """純股票代號 → 最新價 + 軍師判讀 + inline keyboard 快捷操作。"""
    from src import database as db, notifier
    try:
        from src import individual_stock_verdict as iv
    except Exception:  # noqa: BLE001
        iv = None  # type: ignore[assignment]

    try:
        db.init_db()
        with db.get_conn() as conn:
            name_row = conn.execute(
                "SELECT name FROM stocks WHERE stock_id=?", (sid,),
            ).fetchone()
            name = name_row["name"] if name_row else ""
            price_row = conn.execute(
                "SELECT date, close, volume FROM daily_prices "
                "WHERE stock_id=? ORDER BY date DESC LIMIT 1",
                (sid,),
            ).fetchone()
    except Exception as e:  # noqa: BLE001
        logger.exception("[TG-HANDLER] stock_query db read failed %s", sid)
        return _reply(f"❌ 讀取 `{sid}` 失敗:{type(e).__name__}")

    if not price_row:
        return _reply(
            f"❓ 找不到 `{sid}` 歷史資料 — 可能未抓取過,或代號不存在。"
        )

    close = float(price_row["close"]) if price_row["close"] is not None else None
    date = str(price_row["date"]) if price_row["date"] else "—"

    lines = [f"📈 *{sid}* {name}".rstrip()]
    if close is not None:
        lines.append(f"收 {close:.2f}({date})")
    else:
        lines.append(f"({date} 無收盤資料)")

    # 軍師判讀(verdict_tag_for_card,失敗 silent)
    if iv is not None:
        try:
            tag = iv.verdict_tag_for_card(sid)
        except Exception:  # noqa: BLE001
            tag = ""
        if tag:
            lines.append(f"🎯 {tag}")

    # inline keyboard:K 線 / 加關注 / 警報 / 問軍師
    try:
        keyboard = notifier.build_stock_inline_keyboard(sid)
    except Exception:  # noqa: BLE001
        keyboard = None

    lines.append("\n⚠️ 僅供研究,非投資建議")
    return _reply("\n".join(lines), reply_markup=keyboard)


# === PAGE_DIGEST ===

def _digest_strong_follower() -> str:
    from src import database as db
    try:
        rows = db.get_strong_follower_composite(min_inst_days=2, limit=5)
    except Exception as e:  # noqa: BLE001
        return f"❌ 強者跟蹤讀取失敗:{type(e).__name__}"
    if not rows:
        return "📭 強者跟蹤:目前無交集(法人 + 千張戶都進)"
    lines = ["📊 *強者跟蹤* Top 5"]
    for r in rows:
        sid = r.get("sid")
        name = r.get("name") or ""
        close = r.get("close")
        score = r.get("composite_score")
        c_str = f"{float(close):.2f}" if close is not None else "—"
        s_str = f" score={float(score):.2f}" if score is not None else ""
        lines.append(f"`{sid}` {name} {c_str}{s_str}")
    return "\n".join(lines)


def _digest_today_perf() -> str:
    from src import database as db
    try:
        from src.market_regime import compute_regime
    except Exception:  # noqa: BLE001
        compute_regime = None  # type: ignore[assignment]

    bits: list[str] = ["📊 *今天表現*"]
    # 大盤 regime
    if compute_regime is not None:
        try:
            regime = compute_regime()
            label = regime.get("label", "未知")
            emoji = regime.get("badge_emoji", "❔")
            bits.append(f"大盤: {emoji} {label}")
        except Exception:  # noqa: BLE001
            pass

    # 今日 picks 統計
    try:
        db.init_db()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT trade_date, COUNT(DISTINCT sid) AS c FROM daily_picks "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM daily_picks)"
            ).fetchone()
            if row and row["trade_date"]:
                bits.append(f"今日 picks: {row['c']} 檔 ({row['trade_date']})")
            row = conn.execute(
                "SELECT AVG(hit_target) AS hr, COUNT(*) AS n FROM pick_outcomes "
                "WHERE pick_date >= date('now', '-30 days')"
            ).fetchone()
            if row and row["hr"] is not None:
                bits.append(
                    f"30 天 hit rate: {float(row['hr'])*100:.1f}% (N={row['n']})"
                )
    except Exception as e:  # noqa: BLE001
        logger.warning("[TG-HANDLER] today_perf db failed: %s", e)

    return "\n".join(bits)


def _digest_watchlist() -> str:
    from src import database as db
    try:
        wl = db.get_watchlist()
    except Exception as e:  # noqa: BLE001
        return f"❌ 關注列表讀取失敗:{type(e).__name__}"
    if not wl:
        return "📭 關注列表是空的"
    lines = [f"⭐ *關注列表* ({len(wl)} 檔)"]
    try:
        with db.get_conn() as conn:
            for w in wl[:10]:
                sid = w["stock_id"]
                row = conn.execute(
                    "SELECT close FROM daily_prices WHERE stock_id=? "
                    "ORDER BY date DESC LIMIT 1",
                    (sid,),
                ).fetchone()
                close = f"{row['close']}" if row and row["close"] else "—"
                name = w.get("name") or ""
                lines.append(f"`{sid}` {name} {close}")
    except Exception as e:  # noqa: BLE001
        logger.warning("[TG-HANDLER] watchlist enrich failed: %s", e)
    if len(wl) > 10:
        lines.append(f"_(僅顯示前 10,共 {len(wl)} 檔)_")
    return "\n".join(lines)


def _digest_positions() -> str:
    from src import database as db
    try:
        db.init_db()
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT stock_id, entry_price, shares FROM user_positions "
                "WHERE is_open=1 ORDER BY entry_date DESC"
            ).fetchall()
            if not rows:
                return "📭 目前沒有未平倉部位"
            lines = [f"💼 *持倉* ({len(rows)} 檔)"]
            for r in rows:
                sid = r["stock_id"]
                ep = float(r["entry_price"])
                sh = int(r["shares"])
                p = conn.execute(
                    "SELECT close FROM daily_prices WHERE stock_id=? "
                    "ORDER BY date DESC LIMIT 1",
                    (sid,),
                ).fetchone()
                close = float(p["close"]) if p and p["close"] else None
                if close is not None:
                    pnl_pct = (close / ep - 1) * 100
                    lines.append(
                        f"`{sid}` 進 {ep:.2f}×{sh} 現 {close:.2f} "
                        f"({pnl_pct:+.2f}%)"
                    )
                else:
                    lines.append(f"`{sid}` 進 {ep:.2f}×{sh}(無現價)")
            return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"❌ 持倉讀取失敗:{type(e).__name__}"


def _digest_stats() -> str:
    from src import database as db
    try:
        db.init_db()
        health = db.cache_health_summary()
        b = health["buckets"]
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT AVG(hit_target) AS hr, COUNT(*) AS n FROM pick_outcomes "
                "WHERE pick_date >= date('now', '-30 days')"
            ).fetchone()
            hr = row["hr"] if row and row["hr"] is not None else None
            n = row["n"] if row else 0
        lines = ["📋 *系統健康*"]
        lines.append(
            f"Cache: 60+天 {b['60+']}・20-59天 {b['20-59']}"
            f"・<20天 {b['<14'] + b['14-19']}"
        )
        if hr is not None:
            lines.append(f"30 天 hit rate: {hr*100:.1f}% (N={n})")
        else:
            lines.append("30 天 hit rate: 無資料")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"❌ 健康讀取失敗:{type(e).__name__}"


def _digest_picks() -> str:
    from src import database as db
    try:
        db.init_db()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) AS d FROM daily_picks"
            ).fetchone()
            d = row["d"] if row else None
            if not d:
                return "📭 daily_picks 無資料"
            rows = conn.execute(
                "SELECT sid, strategy, ml_prob, score FROM daily_picks "
                "WHERE trade_date=? ORDER BY ml_prob DESC NULLS LAST LIMIT 10",
                (d,),
            ).fetchall()
        if not rows:
            return f"📭 {d} 無 picks"
        lines = [f"📊 *今日推薦* ({d}) Top {len(rows)}"]
        for r in rows:
            ml = f" ml={r['ml_prob']:.2f}" if r["ml_prob"] is not None else ""
            lines.append(f"`{r['sid']}` {r['strategy']}{ml}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"❌ picks 讀取失敗:{type(e).__name__}"


_PAGE_DIGEST_DISPATCH = {
    "strong_follower": _digest_strong_follower,
    "today_perf": _digest_today_perf,
    "watchlist": _digest_watchlist,
    "positions": _digest_positions,
    "stats": _digest_stats,
    "picks": _digest_picks,
}


def _handle_page_digest(page: str) -> dict[str, Any]:
    fn = _PAGE_DIGEST_DISPATCH.get(page)
    if fn is None:
        return _reply(f"❓ 未知頁面:{page}")
    try:
        return _reply(fn())
    except Exception as e:  # noqa: BLE001
        logger.exception("[TG-HANDLER] page digest %s failed", page)
        return _reply(f"❌ {page} 處理失敗:{type(e).__name__}")


# === FREEFORM (走 Gemini) ===

def _handle_freeform(text: str, sid: str | None) -> dict[str, Any]:
    """走 ai_assistant — 若 sid 有 → ask_about_stock,否則 ask_about_market。"""
    try:
        from src import ai_assistant
    except Exception:  # noqa: BLE001
        return _reply("🤖 軍師模組未就緒")

    if not ai_assistant.is_enabled():
        return _reply("💤 軍師暫時下班(AI_ASSISTANT_ENABLED=false)")

    try:
        if sid:
            res = ai_assistant.ask_about_stock(sid, text)
            header = f"💬 *軍師 — {sid}*\n"
        else:
            res = ai_assistant.ask_about_market(text)
            header = "💬 *軍師 — 大盤*\n"
    except Exception as e:  # noqa: BLE001
        logger.exception("[TG-HANDLER] ai_assistant failed")
        return _reply(f"🤖 軍師失敗:{type(e).__name__}: {e}")

    answer = (res or {}).get("answer") or "(軍師無回應)"
    return _reply(header + answer)


# === Public dispatch ===

def handle_intent(intent: Intent) -> dict[str, Any]:
    """單一 entrypoint — Intent → reply dict。"""
    if intent.kind == INTENT_HELP:
        return _reply(help_text())
    if intent.kind == INTENT_STOCK_QUERY and intent.sid:
        return _handle_stock_query(intent.sid)
    if intent.kind == INTENT_PAGE_DIGEST and intent.page:
        return _handle_page_digest(intent.page)
    if intent.kind == INTENT_FREEFORM:
        return _handle_freeform(intent.raw_text, intent.sid)
    return _reply(help_text())


__all__ = [
    "handle_intent",
]
