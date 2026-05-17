"""C-Discord 互動命令(2026-05-17 加):透過 Discord Interactions HTTP endpoint
回應主公的 slash commands,不需要常駐 bot daemon。

設計:
- Discord 把 slash command 互動轉成 HTTP POST 打到我們設定的 endpoint
- endpoint 用 Ed25519 簽章驗證(DISCORD_PUBLIC_KEY)
- 我們回 JSON {type, data},Discord 顯示給主公

不做事的時候 0 成本:
- Streamlit Cloud 不適合長駐,所以走 webhook 互動模式
- 部署選項:Cloudflare Worker / Vercel serverless / GitHub Actions(配 ngrok)
  — 由主公自行選一個跑 `scripts/discord_bot_serve.py`

公開 API:
- is_enabled() -> bool                          DISCORD_BOT_ENABLED kill-switch
- get_slash_command_definitions() -> list[dict] 給 register_commands 用
- register_commands(...) -> bool                呼叫 Discord API 註冊 slash command
- verify_signature(public_key, signature, timestamp, body_bytes) -> bool
- handle_interaction(payload: dict) -> dict     主入口:dispatch 各 slash command

每個 _cmd_* 都回 Discord interaction response 結構:
    {"type": 4, "data": {"content": "...", "flags": 64?}}   type=4 = CHANNEL_MESSAGE
flags=64 → ephemeral(只主公看得到)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

from src import config

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"

# Interaction response types(Discord 文檔)
RESP_PONG = 1                  # ACK PING
RESP_CHANNEL_MESSAGE = 4       # 立即顯示訊息給 user
RESP_DEFERRED_MESSAGE = 5      # 「Thinking…」,後續 PATCH original message

# Interaction types
INTERACTION_PING = 1
INTERACTION_APP_COMMAND = 2
INTERACTION_COMPONENT = 3

# Discord 上限
DISCORD_MSG_LIMIT = 2000


def is_enabled() -> bool:
    """kill-switch:env DISCORD_BOT_ENABLED(預設 true)。

    缺 DISCORD_APPLICATION_ID / DISCORD_BOT_TOKEN / DISCORD_PUBLIC_KEY 也視為停用。
    """
    raw = os.getenv("DISCORD_BOT_ENABLED", "true").strip().lower()
    if raw not in ("true", "1", "yes", "on"):
        return False
    return bool(
        config.DISCORD_APPLICATION_ID
        and config.DISCORD_BOT_TOKEN
        and config.DISCORD_PUBLIC_KEY
    )


# === Slash command definitions ===

def get_slash_command_definitions() -> list[dict[str, Any]]:
    """slash command JSON 定義 — 給 register_commands 一次性 PUT 到 Discord。

    Discord application command type=1 (CHAT_INPUT)
    options type:3=STRING, 4=INTEGER, 10=NUMBER(float)
    """
    return [
        {
            "name": "picks",
            "type": 1,
            "description": "顯示今日推薦短線 / 長線 / 關注",
        },
        {
            "name": "watchlist",
            "type": 1,
            "description": "顯示我的關注列表 + 現價",
        },
        {
            "name": "chart",
            "type": 1,
            "description": "顯示該股 K 線(ASCII 縮圖)",
            "options": [
                {
                    "name": "sid", "type": 3, "required": True,
                    "description": "股票代號(如 2330)",
                }
            ],
        },
        {
            "name": "stats",
            "type": 1,
            "description": "系統健康 + 30 天 hit rate",
        },
        {
            "name": "positions",
            "type": 1,
            "description": "當前持倉 + 損益",
        },
        {
            "name": "alert",
            "type": 1,
            "description": "設定價格警報(price_above / price_below)",
            "options": [
                {
                    "name": "sid", "type": 3, "required": True,
                    "description": "股票代號",
                },
                {
                    "name": "type", "type": 3, "required": True,
                    "description": "警報類型",
                    "choices": [
                        {"name": "price_above (價格高於)", "value": "price_above"},
                        {"name": "price_below (價格低於)", "value": "price_below"},
                    ],
                },
                {
                    "name": "value", "type": 10, "required": True,
                    "description": "目標價",
                },
            ],
        },
        {
            "name": "ask",
            "type": 1,
            "description": "問軍師(Gemini AI 對答)",
            "options": [
                {
                    "name": "question", "type": 3, "required": True,
                    "description": "你的問題(可帶 sid 如 '2330 怎麼樣')",
                }
            ],
        },
    ]


def register_commands(
    application_id: str | None = None,
    bot_token: str | None = None,
    guild_id: str | None = None,
) -> bool:
    """PUT slash command 定義到 Discord(global / guild)。

    guild_id 給:會即時生效(< 1s),適合測試。
    global commands:會 cache 1 小時左右。

    成功回 True,失敗(401 / 5xx)回 False。
    """
    app_id = application_id or config.DISCORD_APPLICATION_ID
    tok = bot_token or config.DISCORD_BOT_TOKEN
    if not app_id or not tok:
        logger.warning("[DISCORD-BOT] 缺 application_id / bot_token,跳過註冊")
        return False
    if guild_id:
        url = f"{DISCORD_API_BASE}/applications/{app_id}/guilds/{guild_id}/commands"
    else:
        url = f"{DISCORD_API_BASE}/applications/{app_id}/commands"
    headers = {
        "Authorization": f"Bot {tok}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.put(
            url, headers=headers, json=get_slash_command_definitions(), timeout=15,
        )
    except requests.RequestException as e:
        logger.error("[DISCORD-BOT] register 網路錯誤: %s", e)
        return False
    if r.status_code in (200, 201):
        logger.info("[DISCORD-BOT] 註冊成功(%d commands)", len(get_slash_command_definitions()))
        return True
    logger.error("[DISCORD-BOT] register HTTP %d: %s", r.status_code, r.text[:200])
    return False


# === Signature verification ===

def verify_signature(
    public_key: str | None,
    signature: str,
    timestamp: str,
    body: bytes,
) -> bool:
    """Ed25519 驗證 Discord interaction request。

    缺 public_key / PyNaCl 套件 → 回 False(等同沒部署 bot)。

    Discord docs:
      X-Signature-Ed25519: hex
      X-Signature-Timestamp: unix sec str
      verify(public_key_hex, signature_hex.encode(), timestamp + body)
    """
    pk = public_key or config.DISCORD_PUBLIC_KEY
    if not pk:
        logger.warning("[DISCORD-BOT] 缺 DISCORD_PUBLIC_KEY,verify_signature 永遠回 False")
        return False
    try:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError
    except ImportError:
        logger.error("[DISCORD-BOT] PyNaCl 未安裝,無法驗章")
        return False
    try:
        verify_key = VerifyKey(bytes.fromhex(pk))
        verify_key.verify(
            f"{timestamp}".encode() + body,
            bytes.fromhex(signature),
        )
        return True
    except (BadSignatureError, ValueError, Exception) as e:  # noqa: BLE001
        logger.debug("[DISCORD-BOT] verify 失敗: %s", e)
        return False


# === Interaction dispatch ===

def _msg(content: str, ephemeral: bool = True) -> dict[str, Any]:
    """Build a CHANNEL_MESSAGE response,有截斷保護。"""
    if len(content) > DISCORD_MSG_LIMIT - 50:
        content = content[: DISCORD_MSG_LIMIT - 50] + "\n... (截斷)"
    data = {"content": content}
    if ephemeral:
        data["flags"] = 64
    return {"type": RESP_CHANNEL_MESSAGE, "data": data}


def _extract_options(payload: dict) -> dict[str, Any]:
    """從 interaction payload.data.options 拿成 {name: value} dict。"""
    opts = (payload.get("data") or {}).get("options") or []
    out: dict[str, Any] = {}
    for o in opts:
        if "value" in o:
            out[o["name"]] = o["value"]
    return out


# --- 各 slash command 處理 ---

def _cmd_picks() -> dict[str, Any]:
    """顯示今日推薦(daily_picks 最新一天)。"""
    try:
        from src import database as db
        db.init_db()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) AS d FROM daily_picks"
            ).fetchone()
            d = row["d"] if row else None
            if not d:
                return _msg("📭 daily_picks 表沒資料 — 跑過 daily 排程了嗎?")
            rows = conn.execute(
                "SELECT sid, strategy, ml_prob, score FROM daily_picks "
                "WHERE trade_date=? ORDER BY ml_prob DESC NULLS LAST LIMIT 15",
                (d,),
            ).fetchall()
        if not rows:
            return _msg(f"📭 {d} 無 picks")
        lines = [f"📊 **今日推薦** ({d}) — top {len(rows)}"]
        for r in rows:
            ml = f" ml={r['ml_prob']:.2f}" if r["ml_prob"] is not None else ""
            lines.append(f"`{r['sid']}` {r['strategy']}{ml}")
        return _msg("\n".join(lines))
    except Exception as e:  # noqa: BLE001
        logger.exception("[DISCORD-BOT] /picks failed")
        return _msg(f"❌ /picks 失敗:{type(e).__name__}: {e}")


def _cmd_watchlist() -> dict[str, Any]:
    try:
        from src import database as db
        wl = db.get_watchlist()
        if not wl:
            return _msg("📭 關注列表是空的")
        lines = [f"⭐ **關注列表** ({len(wl)} 檔)"]
        with db.get_conn() as conn:
            for w in wl[:20]:
                sid = w["stock_id"]
                row = conn.execute(
                    "SELECT close FROM daily_prices WHERE stock_id=? "
                    "ORDER BY date DESC LIMIT 1",
                    (sid,),
                ).fetchone()
                close = f"{row['close']}" if row and row["close"] else "—"
                name = w.get("name") or ""
                lines.append(f"`{sid}` {name} 收 {close}")
        if len(wl) > 20:
            lines.append(f"_(僅顯示前 20,共 {len(wl)} 檔)_")
        return _msg("\n".join(lines))
    except Exception as e:  # noqa: BLE001
        logger.exception("[DISCORD-BOT] /watchlist failed")
        return _msg(f"❌ /watchlist 失敗:{type(e).__name__}: {e}")


def _cmd_chart(sid: str) -> dict[str, Any]:
    """ASCII sparkline(20 天 close)— 行動裝置友善。"""
    if not sid:
        return _msg("❌ 缺 sid")
    try:
        from src import database as db
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT date, close FROM daily_prices WHERE stock_id=? "
                "ORDER BY date DESC LIMIT 20",
                (sid,),
            ).fetchall()
        if not rows:
            return _msg(f"❓ 找不到 {sid} 歷史")
        closes = [float(r["close"]) for r in reversed(rows) if r["close"] is not None]
        if not closes:
            return _msg(f"❓ {sid} 無有效收盤")
        spark = _sparkline(closes)
        chg = (closes[-1] / closes[0] - 1) * 100
        arrow = "📈" if chg >= 0 else "📉"
        return _msg(
            f"{arrow} **{sid}** 近 {len(closes)} 日 K 線\n"
            f"```\n{spark}\n```\n"
            f"低 {min(closes):.2f} / 高 {max(closes):.2f} / "
            f"今 {closes[-1]:.2f} ({chg:+.2f}%)"
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[DISCORD-BOT] /chart failed")
        return _msg(f"❌ /chart 失敗:{type(e).__name__}: {e}")


def _sparkline(values: list[float]) -> str:
    """8 階 Unicode block ASCII sparkline。"""
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    if hi == lo:
        return blocks[3] * len(values)
    out = []
    for v in values:
        idx = int((v - lo) / (hi - lo) * (len(blocks) - 1))
        out.append(blocks[max(0, min(len(blocks) - 1, idx))])
    return "".join(out)


def _cmd_stats() -> dict[str, Any]:
    """系統健康 + 30 天 hit rate。"""
    try:
        from src import database as db
        db.init_db()
        with db.get_conn() as conn:
            # cache health
            health = db.cache_health_summary()
            b = health["buckets"]

            # 30d hit rate(pick_outcomes hit_target avg)
            row = conn.execute(
                "SELECT AVG(hit_target) AS hr, COUNT(*) AS n FROM pick_outcomes "
                "WHERE pick_date >= date('now', '-30 days')"
            ).fetchone()
            hr = row["hr"] if row and row["hr"] is not None else None
            n = row["n"] if row else 0

            # picks today
            row = conn.execute(
                "SELECT MAX(trade_date) AS d, COUNT(DISTINCT sid) AS c "
                "FROM daily_picks "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM daily_picks)"
            ).fetchone()
            picks_d = row["d"] if row else "—"
            picks_c = row["c"] if row else 0

        lines = ["📋 **系統健康**"]
        lines.append(f"Cache: 60+天 {b['60+']}・20-59天 {b['20-59']}・<20天 {b['<14'] + b['14-19']}")
        lines.append(f"今日 picks: {picks_c} 檔 ({picks_d})")
        if hr is not None:
            lines.append(f"30 天 hit rate: {hr*100:.1f}% (N={n})")
        else:
            lines.append("30 天 hit rate: 無資料")
        return _msg("\n".join(lines))
    except Exception as e:  # noqa: BLE001
        logger.exception("[DISCORD-BOT] /stats failed")
        return _msg(f"❌ /stats 失敗:{type(e).__name__}: {e}")


def _cmd_positions() -> dict[str, Any]:
    """user_positions(主公手動建倉)+ paper_trades(系統自動 seed)— 損益。"""
    try:
        from src import database as db
        db.init_db()
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT stock_id, entry_date, entry_price, shares, "
                "stop_loss, take_profit FROM user_positions "
                "WHERE is_open=1 ORDER BY entry_date DESC"
            ).fetchall()
            if not rows:
                return _msg("📭 目前沒有未平倉部位")
            lines = [f"💼 **持倉** ({len(rows)} 檔)"]
            for r in rows:
                sid = r["stock_id"]
                ep = float(r["entry_price"])
                sh = int(r["shares"])
                # 現價
                p = conn.execute(
                    "SELECT close FROM daily_prices WHERE stock_id=? "
                    "ORDER BY date DESC LIMIT 1",
                    (sid,),
                ).fetchone()
                close = float(p["close"]) if p and p["close"] else None
                if close is not None:
                    pnl_pct = (close / ep - 1) * 100
                    pnl_amt = (close - ep) * sh
                    lines.append(
                        f"`{sid}` 進場 {ep:.2f} × {sh} | 現 {close:.2f} | "
                        f"{pnl_pct:+.2f}% / {pnl_amt:+,.0f}"
                    )
                else:
                    lines.append(f"`{sid}` 進場 {ep:.2f} × {sh} | 現價無資料")
            return _msg("\n".join(lines))
    except Exception as e:  # noqa: BLE001
        logger.exception("[DISCORD-BOT] /positions failed")
        return _msg(f"❌ /positions 失敗:{type(e).__name__}: {e}")


def _cmd_alert(sid: str, alert_type: str, value: float) -> dict[str, Any]:
    """寫 price_alerts row。alert_type ∈ {price_above, price_below}。"""
    if alert_type not in ("price_above", "price_below"):
        return _msg(f"❌ 不支援的 type: {alert_type}")
    try:
        from src import database as db
        from datetime import datetime, timezone
        db.init_db()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO price_alerts (stock_id, alert_type, target_value, "
                "created_at, is_active) VALUES (?, ?, ?, ?, 1)",
                (
                    str(sid).strip(), alert_type, float(value),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        return _msg(
            f"✅ 警報已設定:`{sid}` {alert_type} {value}\n"
            f"觸發後會推 Telegram / Discord。"
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[DISCORD-BOT] /alert failed")
        return _msg(f"❌ /alert 失敗:{type(e).__name__}: {e}")


def _cmd_ask(question: str) -> dict[str, Any]:
    """問軍師 — 走 ai_assistant。"""
    if not question:
        return _msg("❌ 請給問題,例如 `/ask 2330 怎麼樣`")
    try:
        from src import ai_assistant
        # 簡單 sid 偵測:第一個 4-6 位數字 token
        sid = _detect_sid(question)
        if sid:
            res = ai_assistant.ask_about_stock(sid, question)
            header = f"💬 **軍師 — {sid}**\n"
        else:
            res = ai_assistant.ask_about_market(question)
            header = "💬 **軍師 — 大盤**\n"
        return _msg(header + res.get("answer", "(無回應)"), ephemeral=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("[DISCORD-BOT] /ask failed")
        return _msg(f"❌ /ask 失敗:{type(e).__name__}: {e}")


def _detect_sid(text: str) -> str | None:
    """從 free-form 文字撈 4-6 位數字當 sid。簡單啟發式,夠用。"""
    import re
    m = re.search(r"\b(\d{4,6})\b", text)
    return m.group(1) if m else None


def handle_interaction(payload: dict[str, Any]) -> dict[str, Any]:
    """Discord interaction 主入口。

    payload.type == 1 (PING)         → {"type": 1} ACK
    payload.type == 2 (APPLICATION_COMMAND) → dispatch slash command
    其他 → 回 4xx-ish content(Discord 仍要 200 才不重送)
    """
    if not is_enabled():
        return _msg("💤 Discord 軍師 bot 暫時停用(主公設了 kill-switch 或缺 token)")

    itype = payload.get("type")
    if itype == INTERACTION_PING:
        return {"type": RESP_PONG}

    if itype != INTERACTION_APP_COMMAND:
        return _msg(f"❓ 不支援的 interaction type: {itype}")

    data = payload.get("data") or {}
    name = data.get("name")
    opts = _extract_options(payload)

    if name == "picks":
        return _cmd_picks()
    if name == "watchlist":
        return _cmd_watchlist()
    if name == "chart":
        return _cmd_chart(str(opts.get("sid", "")))
    if name == "stats":
        return _cmd_stats()
    if name == "positions":
        return _cmd_positions()
    if name == "alert":
        try:
            value = float(opts.get("value"))
        except (TypeError, ValueError):
            return _msg("❌ value 必須是數字")
        return _cmd_alert(
            str(opts.get("sid", "")).strip(),
            str(opts.get("type", "")).strip(),
            value,
        )
    if name == "ask":
        return _cmd_ask(str(opts.get("question", "")).strip())

    return _msg(f"❓ 未知指令:/{name}")


__all__ = [
    "is_enabled",
    "get_slash_command_definitions",
    "register_commands",
    "verify_signature",
    "handle_interaction",
    "RESP_PONG",
    "RESP_CHANNEL_MESSAGE",
    "INTERACTION_PING",
    "INTERACTION_APP_COMMAND",
]
