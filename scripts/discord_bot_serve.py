"""輕量 HTTP server 接 Discord interaction webhook(C 互動命令)。

Discord 把 slash command 轉成 HTTP POST 打到我們設的 endpoint(Application →
Interactions Endpoint URL),這支 script 就是那個 endpoint。

用法:
    PORT=8000 python scripts/discord_bot_serve.py

驗章 + dispatch 都在 src/discord_bot.py 完成,這裡只負責 HTTP I/O。

部署:
- 本機 + ngrok / cloudflared expose → 設給 Discord interactions endpoint url
- 或丟 Cloudflare Workers / Vercel(可後續做,先給 PoC)
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from src import discord_bot

logger = logging.getLogger(__name__)


class _InteractionHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        signature = self.headers.get("X-Signature-Ed25519", "")
        timestamp = self.headers.get("X-Signature-Timestamp", "")

        if not discord_bot.verify_signature(None, signature, timestamp, body):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"invalid signature")
            return

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"bad json")
            return

        resp = discord_bot.handle_interaction(payload)
        body_out = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def log_message(self, fmt, *args):  # noqa: A003
        # 走 logger,別吐 stdout
        logger.info("[DISCORD-BOT-HTTP] " + fmt, *args)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    if not discord_bot.is_enabled():
        logger.warning("[DISCORD-BOT] 未啟用(env DISCORD_BOT_ENABLED=false 或缺 token)")
        return 1

    port = int(os.getenv("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), _InteractionHandler)
    logger.info("[DISCORD-BOT] HTTP server on :%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("[DISCORD-BOT] shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
