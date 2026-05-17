"""一次性 PUT slash command 定義到 Discord(C 互動命令)。

跑法:
    python scripts/discord_bot_register.py              # global commands(cache 1h)
    DISCORD_GUILD_ID=12345 python scripts/discord_bot_register.py   # guild 即時

需要 env:
    DISCORD_APPLICATION_ID
    DISCORD_BOT_TOKEN
"""
from __future__ import annotations

import logging
import os

from src import discord_bot


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    guild = os.getenv("DISCORD_GUILD_ID") or None
    ok = discord_bot.register_commands(guild_id=guild)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
