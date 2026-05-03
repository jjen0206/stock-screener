"""
Telegram Bot 推播模組。

提供:
- send_telegram_message(text, bot_token, chat_id) -> bool
    底層發送函式;缺 token 印 warning 回 False,網路 / API 錯誤回 False
- format_short_picks(picks, date) -> str
    把 screen_short 結果包成 Markdown 訊息(每檔一段)
- notify_short_picks(date, params) -> bool
    整合短線選股 + 推播,給排程腳本用

排程方式:
- Streamlit Cloud 不支援自定 cron,要用主機 crontab 或 GitHub Actions
  (yaml 範例見 README「Telegram 推播」章節)
"""
from __future__ import annotations

import logging
import time
from datetime import date as _date

import pandas as pd
import requests

from src import config, database as db
from src.screener_short import screen_short
from src.universe import TW_TOP_50


# 推播失敗 retry 設定:總共最多 3 次嘗試,失敗等 1s 再重試
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECS = 1.0


logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_message(
    text: str,
    bot_token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """發送 Telegram 訊息(Markdown)。

    參數優先順序:傳入參數 > config(由 .env / Streamlit Secrets 載入)。
    缺 token 或 chat_id 印 warning 回 False;網路/API 錯誤回 False。
    """
    token = bot_token or config.TELEGRAM_BOT_TOKEN
    cid = chat_id or config.TELEGRAM_CHAT_ID

    if not token or not cid:
        logger.warning(
            "[NOTIFIER] 缺 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID,跳過推播"
        )
        return False

    url = TELEGRAM_API_URL.format(token=token)
    payload = {"chat_id": cid, "text": text, "parse_mode": "Markdown"}

    # 失敗 retry:網路 exception 或 5xx 才重試,4xx 是 client error 不重試
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.post(url, json=payload, timeout=15)
        except requests.RequestException as ex:
            logger.warning(
                "[NOTIFIER] 網路錯誤 (attempt %d/%d): %s",
                attempt + 1, _MAX_ATTEMPTS, ex,
            )
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAY_SECS)
                continue
            logger.error("[NOTIFIER] 網路錯誤,放棄重試: %s", ex)
            return False

        if r.status_code == 200:
            return True
        if r.status_code < 500:
            # 4xx (401/400 等)→ client error, 重試也沒用
            logger.error(
                "[NOTIFIER] Telegram API client error %d: %s",
                r.status_code, r.text[:200],
            )
            return False
        # 5xx → 重試
        logger.warning(
            "[NOTIFIER] Telegram API %d (attempt %d/%d),retry...",
            r.status_code, attempt + 1, _MAX_ATTEMPTS,
        )
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_RETRY_DELAY_SECS)
    return False


def _empty_pick_suffix() -> str:
    """0 入選時的補充說明:多數情況不是 bug 而是 cache 歷史不足。

    顯示 cache 健康度;若多數個股 < 60 天,加註「歷史累積中」避免誤判。
    """
    try:
        health = db.cache_health_summary()
    except Exception:  # noqa: BLE001
        return ""
    b = health["buckets"]
    eligible = b["60+"] + b["20-59"]
    suffix = (
        f"\n\n📦 Cache: 60+天 {b['60+']}・20-59天 {b['20-59']}"
        f"・<20天 {b['<14'] + b['14-19']}"
    )
    if eligible < 100:
        suffix += "\n⏳ 多數個股歷史累積中(短線策略需 14-60 天),請等待 1-2 週"
    return suffix


def format_short_picks(picks: pd.DataFrame, date: str) -> str:
    """把短線選股結果包成 Telegram Markdown 訊息。

    空 picks → 回「📭 今日無符合條件」訊息。
    """
    if picks is None or picks.empty:
        return f"📭 *{date}* 今日無符合條件的個股{_empty_pick_suffix()}"

    lines: list[str] = [f"📈 *{date} 短線推薦* ({len(picks)} 檔)", ""]
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

        lines.append(f"{i}. *{sid} {name}*")
        lines.append(
            f"   收 {close:.2f} | 量比 {vol_ratio:.1f}x | "
            f"K {k:.1f} > D {d:.1f} | 法人 3 日 {inst / 1000:.0f}K"
        )
        # 詳細分析(歷史不足 / 無法人籌碼會回空字串,append 空字串無害但用 if 保險)
        from src.individual_sections import format_pick_summary
        detail = format_pick_summary(str(sid), indent="   ")
        if detail:
            lines.append(detail)

    lines.append("")
    lines.append("⚠️ 僅供研究,非投資建議")
    return "\n".join(lines)


def notify_short_picks(
    date: str | None = None,
    params: dict | None = None,
    send_telegram: bool = True,
    send_discord: bool = True,
) -> dict[str, bool]:
    """跑短線選股 → 並行送 Telegram + Discord。

    回 {'telegram': bool, 'discord': bool} — 只包含實際送的通道。
    若兩者 secrets 都沒設 → 回 {}(空 dict 視為沒推任何東西)。
    """
    if date is None:
        date = _date.today().isoformat()
    sids = [s for s, _ in TW_TOP_50]
    picks = screen_short(date, params=params, stock_ids=sids)

    results: dict[str, bool] = {}
    if send_telegram and config.TELEGRAM_BOT_TOKEN:
        results["telegram"] = send_telegram_message(
            format_short_picks(picks, date)
        )
    if send_discord and config.DISCORD_WEBHOOK_URL:
        # lazy import 避免 module import cycle / 拖慢啟動
        from src.discord_notifier import (
            format_short_picks_discord,
            send_discord_message,
        )
        results["discord"] = send_discord_message(
            format_short_picks_discord(picks, date)
        )
    return results


# === 多策略並行推播 ===

def format_multi_strategy_picks(
    aggregated: dict[str, dict],
    date: str,
) -> str:
    """把 run_all_strategies 聚合結果包成 Telegram Markdown。

    aggregated: {sid: {"name", "signals": [...], "details": {...}}}
    優先列 信號數 多的(多策略同時看好 = 信心強)。
    """
    if not aggregated:
        return f"📭 *{date}* 今日無任一策略選中個股{_empty_pick_suffix()}"

    # 按信號數降序、stock_id 升序
    sorted_items = sorted(
        aggregated.items(),
        key=lambda kv: (-len(kv[1]["signals"]), kv[0]),
    )
    n = len(sorted_items)
    # 加週幾(資料日期,非執行日期)
    try:
        d = _date.fromisoformat(date)
        week_zh = ["一", "二", "三", "四", "五", "六", "日"][d.weekday()]
        date_label = f"{date} (週{week_zh})"
    except Exception:  # noqa: BLE001
        date_label = date
    lines = [
        f"📈 *{date_label} 短線推薦* ({n} 檔,多策略並行)",
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
        lines.append(f"{i}. *{sid} {info['name']}* {confidence}")
        if close:
            lines.append(f"   收 {close:.2f} | 信號: {signals}")
        else:
            lines.append(f"   信號: {signals}")
        if target_low and target_high and stop_loss:
            rr_str = f" (R:R {risk_reward:.1f}:1)" if risk_reward else ""
            lines.append(
                f"   🎯 目標 {target_low:.2f}~{target_high:.2f}"
                f" / 🛑 停損 {stop_loss:.2f}{rr_str}"
            )
        # 詳細分析(reuse 個股頁 helper)
        from src.individual_sections import format_pick_summary
        detail = format_pick_summary(str(sid), indent="   ")
        if detail:
            lines.append(detail)
    lines.append("")
    lines.append("⚠️ 僅供研究,非投資建議。目標價為 ATR 統計參考,非實際預測。")
    return "\n".join(lines)


def notify_multi_strategy(
    date: str | None = None,
    enabled: list[str] | None = None,
    params: dict | None = None,
    send_telegram: bool = True,
    send_discord: bool = True,
) -> dict[str, bool]:
    """跑多策略 → 聚合 → 並行送 Telegram + Discord。

    回 {'telegram': bool, 'discord': bool} — 只包含實際送的通道。
    """
    from src.strategies import run_all_strategies
    if date is None:
        date = _date.today().isoformat()
    sids = [s for s, _ in TW_TOP_50]
    agg = run_all_strategies(
        date, enabled=enabled, params=params, stock_ids=sids,
    )

    results: dict[str, bool] = {}
    if send_telegram and config.TELEGRAM_BOT_TOKEN:
        results["telegram"] = send_telegram_message(
            format_multi_strategy_picks(agg, date)
        )
    if send_discord and config.DISCORD_WEBHOOK_URL:
        from src.discord_notifier import (
            format_multi_strategy_picks_discord,
            send_discord_message,
        )
        results["discord"] = send_discord_message(
            format_multi_strategy_picks_discord(agg, date)
        )
    return results


def format_manual_picks(picks_df: "pd.DataFrame", date: str, limit: int = 7) -> str:
    """把雲端 App 的當前推薦 DataFrame 包成 Telegram 訊息(手動推播專用)。

    跟 cron 推播訊息差別:
      - 限制 limit 檔(避免使用者選一堆把訊息撐爆 / Telegram 4096 字元)
      - footer 加 `📲 來源:雲端 App 手動推播` 區別自動推播
    """
    if picks_df is None or picks_df.empty:
        return f"📭 *{date}* 雲端 App 手動推播:當前無推薦個股"

    # 接受兩種 schema:cron 用的「stock_id/name/close + 量價技術指標」或
    # 短線頁 aggregated_to_dataframe 出的「stock_id/name/close + 信號數/信號 + targets」
    df = picks_df.head(limit)
    n_total = len(picks_df)
    n_show = len(df)
    truncated = f"(顯示前 {n_show} / 共 {n_total})" if n_total > limit else f"({n_show} 檔)"

    try:
        d = _date.fromisoformat(date)
        wk = ["一", "二", "三", "四", "五", "六", "日"][d.weekday()]
        date_label = f"{date} (週{wk})"
    except Exception:  # noqa: BLE001
        date_label = date

    lines = [f"📈 *{date_label} 短線推薦* {truncated}", ""]
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        sid = r.get("stock_id", "?")
        name = r.get("name", "")
        close = r.get("close")
        n_sig = r.get("信號數") or r.get("n_signals") or 0
        signals = r.get("信號") or r.get("signals") or ""
        target_low = r.get("target_low")
        target_high = r.get("target_high")
        stop_loss = r.get("stop_loss")
        rr = r.get("risk_reward")

        confidence = "🔥" * int(n_sig) if n_sig else ""
        lines.append(f"{i}. *{sid} {name}* {confidence}".rstrip())
        if close is not None:
            try:
                close_str = f"{float(close):.2f}"
                if signals:
                    lines.append(f"   收 {close_str} | {signals}")
                else:
                    lines.append(f"   收 {close_str}")
            except (TypeError, ValueError):
                pass
        if target_low and target_high and stop_loss:
            try:
                rr_str = f" (R:R {float(rr):.1f}:1)" if rr else ""
                lines.append(
                    f"   🎯 {float(target_low):.2f}~{float(target_high):.2f}"
                    f" / 🛑 {float(stop_loss):.2f}{rr_str}"
                )
            except (TypeError, ValueError):
                pass

    lines.append("")
    lines.append("⚠️ 僅供研究,非投資建議")
    lines.append("📲 來源:雲端 App 手動推播")
    return "\n".join(lines)


def notify_manual_picks(
    picks_df: "pd.DataFrame",
    date: str | None = None,
    limit: int = 7,
    send_telegram: bool = True,
    send_discord: bool = True,
) -> dict[str, bool]:
    """雲端 App「立即推播」按鈕專用:把當前頁面的 picks 推到 Telegram+Discord。

    Returns: {'telegram': bool, 'discord': bool} — 只含實際送的通道;
             兩者 secrets 都沒設 → 回 {} (caller 該提示沒推任何東西)。
    """
    if date is None:
        date = _date.today().isoformat()
    msg = format_manual_picks(picks_df, date, limit=limit)
    results: dict[str, bool] = {}
    if send_telegram and config.TELEGRAM_BOT_TOKEN:
        results["telegram"] = send_telegram_message(msg)
    if send_discord and config.DISCORD_WEBHOOK_URL:
        from src.discord_notifier import send_discord_message
        results["discord"] = send_discord_message(msg)
    return results


__all__ = [
    "send_telegram_message",
    "format_short_picks",
    "format_multi_strategy_picks",
    "format_manual_picks",
    "notify_short_picks",
    "notify_multi_strategy",
    "notify_manual_picks",
]
