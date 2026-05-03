"""
Discord Webhook 推播模組(Telegram 備援)。

API:
- POST 到 webhook URL,JSON body {content, username, avatar_url}
- 不需要 API token / OAuth,只要 webhook URL 即可
- Discord 訊息上限 2000 字元,本模組會自動截斷

提供:
- send_discord_message(content, webhook_url) -> bool
- format_short_picks_discord(picks, date) -> str
- notify_short_picks_discord(date, params) -> bool

robust HTTP:requests + httpx fallback(同 financial_fetcher_free 思路)
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import date as _date

import pandas as pd
import requests

from src import config
from src.screener_short import screen_short
from src.universe import TW_TOP_50


logger = logging.getLogger(__name__)

DISCORD_BOT_AVATAR = "https://cdn.discordapp.com/embed/avatars/0.png"
DISCORD_MSG_LIMIT = 2000

# 推播失敗 retry 設定:總共最多 3 次嘗試,失敗等 1s 再重試
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECS = 1.0


def _post_with_fallback(url: str, payload: dict, timeout: int = 15):
    """先試 requests,失敗 fallback httpx,兩個都失敗 raise。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) stock-screener/1.0",
        "Content-Type": "application/json",
    }
    errors: list[tuple[str, Exception]] = []
    # try 1: requests
    try:
        return requests.post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        errors.append(("requests", e))
        msg = f"[DISCORD-WARN-REQUESTS] ({type(e).__name__}) {str(e)[:200]}"
        logger.warning(msg)
        print(msg, file=sys.stderr, flush=True)
    # try 2: httpx
    try:
        import httpx
        with httpx.Client(timeout=timeout) as c:
            return c.post(url, json=payload, headers=headers)
    except Exception as e:  # noqa: BLE001
        errors.append(("httpx", e))
        msg = f"[DISCORD-ERROR-HTTPX] ({type(e).__name__}) {str(e)[:200]}"
        logger.error(msg)
        print(msg, file=sys.stderr, flush=True)
    raise errors[0][1]


def send_discord_message(
    content: str,
    webhook_url: str | None = None,
) -> bool:
    """發送訊息到 Discord webhook。

    參數優先序:傳入 webhook_url > config.DISCORD_WEBHOOK_URL。
    缺 URL → 印 warning 回 False;HTTP / 網路錯誤回 False。
    成功(HTTP 200/204)回 True。
    """
    url = webhook_url or config.DISCORD_WEBHOOK_URL
    if not url:
        logger.warning("[DISCORD] 缺 DISCORD_WEBHOOK_URL,跳過")
        return False

    # Discord 上限 2000 字
    if len(content) > DISCORD_MSG_LIMIT - 50:
        content = content[: DISCORD_MSG_LIMIT - 50] + "\n... (訊息過長已截斷)"

    payload = {
        "content": content,
        "username": "Stock Screener",
        "avatar_url": DISCORD_BOT_AVATAR,
    }

    # 失敗 retry:exception 或 5xx 才重試,4xx 直接 fail
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = _post_with_fallback(url, payload)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            logger.warning(
                "[DISCORD] 發送失敗 (attempt %d/%d): %s",
                attempt + 1, _MAX_ATTEMPTS, e,
            )
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAY_SECS)
                continue
            logger.error("[DISCORD] 放棄重試: %s", last_exc)
            return False

        if r.status_code in (200, 204):
            return True
        if r.status_code < 500:
            # 4xx → client error 不重試;不要把 r.text 暴露(可能含 webhook 資訊)
            logger.error(
                "[DISCORD] HTTP %d: %s",
                r.status_code, str(r.text)[:200],
            )
            return False
        # 5xx → retry
        logger.warning(
            "[DISCORD] HTTP %d (attempt %d/%d),retry...",
            r.status_code, attempt + 1, _MAX_ATTEMPTS,
        )
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_RETRY_DELAY_SECS)
    return False


def format_short_picks_discord(picks: pd.DataFrame, date: str) -> str:
    """Discord Markdown 格式;與 Telegram 類似但開頭加 banner。"""
    from src.notifier import _empty_pick_suffix
    banner = f"📊 **stock-screener** | {date}"
    if picks is None or picks.empty:
        return f"{banner}\n\n📭 今日無符合條件的個股{_empty_pick_suffix()}"

    lines = [banner, f"📈 短線推薦 ({len(picks)} 檔)", ""]
    for i, (_, row) in enumerate(picks.iterrows(), start=1):
        sid = row.get("stock_id", "?")
        name = row.get("name", "")
        close = float(row.get("close", 0) or 0)
        vol = float(row.get("volume", 0) or 0)
        ma_vol = float(row.get("ma_volume_5", 0) or 0)
        vol_ratio = (vol / ma_vol) if ma_vol > 0 else 0.0
        k = float(row.get("k", 0) or 0)
        d = float(row.get("d", 0) or 0)
        inst = float(row.get("inst_total_3d", 0) or 0)

        lines.append(f"`{i}.` **{sid} {name}**")
        lines.append(
            f"    收 {close:.2f} | 量比 {vol_ratio:.1f}x | "
            f"K {k:.1f} > D {d:.1f} | 法人 {inst / 1000:.0f}K"
        )
        # 詳細分析(reuse 個股頁 helper)
        from src.individual_sections import format_pick_summary
        detail = format_pick_summary(str(sid), indent="    ")
        if detail:
            lines.append(detail)
    lines.append("")
    lines.append("⚠️ 僅供研究,非投資建議")
    return "\n".join(lines)


def format_multi_strategy_picks_discord(
    aggregated: dict[str, dict],
    date: str,
) -> str:
    """多策略結果的 Discord 版本(含 🔥 信號數視覺)。"""
    try:
        d = _date.fromisoformat(date)
        week_zh = ["一", "二", "三", "四", "五", "六", "日"][d.weekday()]
        date_label = f"{date} (週{week_zh})"
    except Exception:  # noqa: BLE001
        date_label = date
    banner = f"📊 **stock-screener** | {date_label}"

    from src.notifier import _empty_pick_suffix
    if not aggregated:
        return f"{banner}\n\n📭 今日無任一策略選中個股{_empty_pick_suffix()}"

    sorted_items = sorted(
        aggregated.items(),
        key=lambda kv: (-len(kv[1]["signals"]), kv[0]),
    )
    n = len(sorted_items)
    lines = [
        banner,
        f"📈 短線推薦 ({n} 檔,多策略並行)",
        "",
    ]
    for i, (sid, info) in enumerate(sorted_items, start=1):
        close = None
        target_low = target_high = stop_loss = risk_reward = None
        for d in info["details"].values():
            if close is None and d.get("close"):
                close = d["close"]
            if target_low is None and d.get("target_low"):
                target_low = d.get("target_low")
                target_high = d.get("target_high")
                stop_loss = d.get("stop_loss")
                risk_reward = d.get("risk_reward")
        signals = " + ".join(info["signals"])
        confidence = "🔥" * len(info["signals"])
        lines.append(f"`{i}.` **{sid} {info['name']}** {confidence}")
        if close:
            lines.append(f"    收 {close:.2f} | 信號: {signals}")
        else:
            lines.append(f"    信號: {signals}")
        if target_low and target_high and stop_loss:
            rr_str = f" (R:R {risk_reward:.1f}:1)" if risk_reward else ""
            lines.append(
                f"    🎯 目標 {target_low:.2f}~{target_high:.2f}"
                f" / 🛑 停損 {stop_loss:.2f}{rr_str}"
            )
        # 詳細分析(reuse 個股頁 helper)
        from src.individual_sections import format_pick_summary
        detail = format_pick_summary(str(sid), indent="    ")
        if detail:
            lines.append(detail)
    lines.append("")
    lines.append("⚠️ 僅供研究,非投資建議。目標價為 ATR 統計參考,非實際預測。")
    return "\n".join(lines)


def notify_short_picks_discord(
    date: str | None = None,
    params: dict | None = None,
) -> bool:
    """跑短線選股 → 推 Discord(獨立函式;若要並行 Telegram 用 notifier.notify_short_picks)。"""
    if date is None:
        date = _date.today().isoformat()
    sids = [s for s, _ in TW_TOP_50]
    picks = screen_short(date, params=params, stock_ids=sids)
    text = format_short_picks_discord(picks, date)
    return send_discord_message(text)


__all__ = [
    "send_discord_message",
    "format_short_picks_discord",
    "format_multi_strategy_picks_discord",
    "notify_short_picks_discord",
]
