"""C-Telegram 雙向問答 daemon(2026-05-18 加)。

`scripts/telegram_bot_serve.py` 是 entry,本 package 內為各 layer:
- state.py    Telegram update_id offset 持久化 + CSV snapshot dump/load
- intent.py   user message → intent(STOCK_QUERY / PAGE_DIGEST / HELP / FREEFORM)
- handlers.py 各 intent / callback_query handler 實作

設計決策參考:docs/telegram-bot-serve-decision-2026-05-18.md
"""
from __future__ import annotations

from src.telegram_bot import handlers, intent, state

__all__ = ["handlers", "intent", "state"]
