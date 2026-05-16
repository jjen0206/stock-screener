"""
設定載入模組。

讀取優先順序:
  1) Streamlit Secrets (st.secrets) — Streamlit Cloud 部署用
  2) 環境變數 / .env — 本機開發用

未設定的 token 會印 warning 但不拋例外(走無 token / 未啟用模式)。
"""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

from dotenv import load_dotenv

# 專案根目錄(本檔位於 src/config.py,往上一層即為專案根)
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# 載入 .env(若存在);本機開發用,雲端部署不需要
_ENV_PATH: Path = PROJECT_ROOT / ".env"
load_dotenv(_ENV_PATH)


def _from_secrets(name: str) -> str | None:
    """嘗試從 st.secrets 讀取;雲端有值則回字串,否則回 None。

    Cold-start 優化:只有當 streamlit 已經被父程序載入(亦即真的跑在
    Streamlit runtime 裡)才會去讀 st.secrets。pytest / CLI 腳本(notifier、
    intraday_alerts、data_health_alert 等)沒有 streamlit context,跳過後省下
    ~0.5s 的 streamlit cold import。
    """
    if "streamlit" not in sys.modules:
        return None
    try:
        import streamlit as st
        v = st.secrets.get(name)
    except Exception:
        return None
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _get(name: str, default: str = "") -> str:
    """先讀 st.secrets,再 fallback 到環境變數 / .env;前後空白會被去除。"""
    from_secrets = _from_secrets(name)
    if from_secrets is not None:
        return from_secrets
    return os.getenv(name, default).strip()


# === 對外公開常數 ===
FINMIND_TOKEN: str = _get("FINMIND_TOKEN")
TELEGRAM_BOT_TOKEN: str = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _get("TELEGRAM_CHAT_ID")
DISCORD_WEBHOOK_URL: str = _get("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY: str = _get("GEMINI_API_KEY")
DATABASE_PATH: str = _get("DATABASE_PATH", "data/cache.db")
DEFAULT_MARKET: str = _get("DEFAULT_MARKET", "TW").upper()


# === 啟動檢查:缺值印 warning,不拋例外 ===
if not FINMIND_TOKEN:
    warnings.warn(
        "FINMIND_TOKEN 未設定,將以無 token 模式呼叫 FinMind API "
        "(會受到較嚴格的頻率限制)。如要升級,請至 https://finmindtrade.com/ 申請。",
        stacklevel=2,
    )

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    warnings.warn(
        "Telegram Bot 未完整設定(需同時提供 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID),"
        "通知功能將停用。",
        stacklevel=2,
    )

if DEFAULT_MARKET not in {"TW", "US"}:
    warnings.warn(
        f"DEFAULT_MARKET='{DEFAULT_MARKET}' 不是合法值(TW/US),已退回預設 TW。",
        stacklevel=2,
    )
    DEFAULT_MARKET = "TW"


__all__ = [
    "PROJECT_ROOT",
    "FINMIND_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DISCORD_WEBHOOK_URL",
    "GEMINI_API_KEY",
    "DATABASE_PATH",
    "DEFAULT_MARKET",
]
