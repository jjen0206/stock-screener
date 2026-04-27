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
from datetime import date as _date

import pandas as pd
import requests

from src import config
from src.screener_short import screen_short
from src.universe import TW_TOP_50


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
    try:
        r = requests.post(
            url,
            json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except requests.RequestException as ex:
        logger.error("[NOTIFIER] 網路錯誤: %s", ex)
        return False

    if r.status_code != 200:
        # 注意:不要在外層 UI 把 r.text 顯示出來,避免暴露 token
        logger.error(
            "[NOTIFIER] Telegram API 回 %d: %s",
            r.status_code, r.text[:200],
        )
        return False

    return True


def format_short_picks(picks: pd.DataFrame, date: str) -> str:
    """把短線選股結果包成 Telegram Markdown 訊息。

    空 picks → 回「📭 今日無符合條件」訊息。
    """
    if picks is None or picks.empty:
        return f"📭 *{date}* 今日無符合條件的個股"

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

    lines.append("")
    lines.append("⚠️ 僅供研究,非投資建議")
    return "\n".join(lines)


def notify_short_picks(
    date: str | None = None,
    params: dict | None = None,
) -> bool:
    """跑短線選股 → 格式化 → 推播。回 True 表整段成功。

    universe 固定用 TW_TOP_50(避免無 token 模式被打太兇)。
    """
    if date is None:
        date = _date.today().isoformat()
    sids = [s for s, _ in TW_TOP_50]
    picks = screen_short(date, params=params, stock_ids=sids)
    text = format_short_picks(picks, date)
    return send_telegram_message(text)


__all__ = [
    "send_telegram_message",
    "format_short_picks",
    "notify_short_picks",
]
