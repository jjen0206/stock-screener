"""Telegram bot serve — 雙向問答 daemon(2026-05-18 加)。

設計決策參考:docs/telegram-bot-serve-decision-2026-05-18.md
  方案 D:GitHub Actions cron `*/5 * * * *` 跑 `--once`(純拉模式 getUpdates),
  狀態靠 SQLite + CSV snapshot 跨 run 持久化。

用法:
  python scripts/telegram_bot_serve.py --once          # GHA cron 一次性拉
  python scripts/telegram_bot_serve.py --once --dry    # 不真送、不更新 offset(調試用)
  python scripts/telegram_bot_serve.py --loop          # 本機長駐(每 5 秒 long-poll)

環境變數:
  TELEGRAM_BOT_TOKEN     (必填)
  TELEGRAM_CHAT_ID       (必填,用來 ACL — 只回應這個 chat,避免不認識的人撈資料)
  TG_BOT_POLL_TIMEOUT    (選,getUpdates 的 long-poll 秒數,預設 25)
  TG_BOT_MAX_UPDATES     (選,單次拉的上限,預設 50)

流程:
  1. preload_snapshots(從 CSV 還原 last_update_id 到 SQLite,雲端 / 新 runner 需要)
  2. getUpdates(offset=last+1)
  3. 對每筆 update 走 dispatch:
       - message.text → parse_intent → handlers.handle_intent → send_telegram_message_with_keyboard
       - callback_query → notifier.handle_callback_query → answer + edited message
  4. 推進 last_update_id
  5. dump_to_csv(讓 workflow 後續步驟 commit + push)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

# 確保 src/ 可 import(GHA runner 預設 cwd = repo root,本機 dev 直接跑也 OK)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import requests  # noqa: E402

from src import config, database as db, notifier  # noqa: E402
from src.telegram_bot import handlers, intent as intent_mod, state  # noqa: E402

logger = logging.getLogger(__name__)

_DEFAULT_POLL_TIMEOUT = int(os.getenv("TG_BOT_POLL_TIMEOUT", "25") or 25)
_DEFAULT_MAX_UPDATES = int(os.getenv("TG_BOT_MAX_UPDATES", "50") or 50)
_HTTP_TIMEOUT = _DEFAULT_POLL_TIMEOUT + 10


def _is_enabled() -> bool:
    """Bot 啟用條件:有 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID。"""
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _allowed_chat(chat_id: Any) -> bool:
    """ACL — 只回應 config.TELEGRAM_CHAT_ID。avoid stranger crawl。

    TELEGRAM_CHAT_ID 字串比對。group chat id 可能是負數。
    """
    if not config.TELEGRAM_CHAT_ID:
        return False
    return str(chat_id) == str(config.TELEGRAM_CHAT_ID)


def get_updates(
    offset: int,
    timeout: int = _DEFAULT_POLL_TIMEOUT,
    limit: int = _DEFAULT_MAX_UPDATES,
    token: str | None = None,
) -> list[dict]:
    """Telegram getUpdates。long-poll timeout 秒。

    回 [] 表示無 update / API 失敗。caller 不需要 retry — GHA cron 每 5 min 自動跑。
    """
    tok = token or config.TELEGRAM_BOT_TOKEN
    if not tok:
        return []
    url = f"https://api.telegram.org/bot{tok}/getUpdates"
    params = {
        "offset": int(offset),
        "timeout": int(timeout),
        "limit": int(limit),
        # allowed_updates 篩掉不需要的 type,降流量
        "allowed_updates": '["message","callback_query"]',
    }
    try:
        r = requests.get(url, params=params, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        logger.warning("[TG-BOT] getUpdates 網路錯誤: %s", e)
        return []
    if r.status_code != 200:
        logger.warning("[TG-BOT] getUpdates HTTP %d: %s", r.status_code, r.text[:200])
        return []
    try:
        payload = r.json()
    except ValueError:
        return []
    if not payload.get("ok"):
        logger.warning("[TG-BOT] getUpdates not ok: %s", payload.get("description"))
        return []
    return list(payload.get("result") or [])


def _send_reply(reply: dict[str, Any], chat_id: str) -> bool:
    """共用 send 包裝 — 走 send_telegram_message_with_keyboard(支援 inline kb)。"""
    text = (reply or {}).get("text") or ""
    if not text:
        return False
    kb = (reply or {}).get("reply_markup")
    return notifier.send_telegram_message_with_keyboard(
        text=text, keyboard=kb, chat_id=chat_id,
    )


def dispatch_update(update: dict[str, Any], dry: bool = False) -> dict[str, Any]:
    """處理一筆 Telegram update,回 {handled, kind, reason}。

    handled=False 代表已過濾(非 allowed chat、空 message、無內容)。
    handled=True 代表已嘗試回覆(dry-run 時不真送)。
    """
    # callback_query(主公點 inline button)優先處理 —— 已經有專門 dispatcher
    if "callback_query" in update:
        cb = update["callback_query"] or {}
        msg = cb.get("message") or {}
        chat = (msg.get("chat") or {})
        chat_id = chat.get("id")
        if not _allowed_chat(chat_id):
            return {"handled": False, "kind": "callback", "reason": "acl"}
        res = notifier.handle_callback_query(update)
        cb_id = res.get("callback_query_id") or cb.get("id") or ""
        reply_text = res.get("reply_text") or ""
        if not dry:
            if cb_id:
                # 先 answerCallbackQuery(< 30s 限制),用 toast 顯示
                notifier.answer_callback_query(cb_id, text="✓")
            if reply_text:
                notifier.send_telegram_message_with_keyboard(
                    text=reply_text, keyboard=None, chat_id=str(chat_id),
                )
        return {"handled": True, "kind": "callback", "reason": res.get("action")}

    # 一般 message
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return {"handled": False, "kind": "unknown", "reason": "no-message"}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not _allowed_chat(chat_id):
        return {"handled": False, "kind": "message", "reason": "acl"}
    text = (msg.get("text") or "").strip()
    if not text:
        return {"handled": False, "kind": "message", "reason": "no-text"}

    intent = intent_mod.parse_intent(text)
    reply = handlers.handle_intent(intent)
    if not dry:
        _send_reply(reply, chat_id=str(chat_id))
    return {"handled": True, "kind": "message", "reason": intent.kind}


def run_once(dry: bool = False) -> dict[str, Any]:
    """單次 poll & dispatch — GHA cron 主入口。

    回 {polled, handled, max_update_id} — log + 測試用。
    """
    if not _is_enabled():
        logger.warning("[TG-BOT] 缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID,abort")
        return {"polled": 0, "handled": 0, "max_update_id": 0}

    db.init_db()
    # 雲端 / 新 runner — 確保從 CSV 還原 offset。本機跑 SQLite 已存就 no-op。
    try:
        state.load_from_csv()
    except Exception as e:  # noqa: BLE001
        logger.warning("[TG-BOT] state.load_from_csv 失敗(忽略): %s", e)

    last_id = state.get_last_update_id()
    offset = last_id + 1 if last_id > 0 else 0
    logger.info("[TG-BOT] poll offset=%d", offset)

    # GHA cron 模式:把 long-poll timeout 壓到 0,馬上回 — 避免 cron run idle 浪費分鐘
    timeout = 0 if _is_running_in_actions() else _DEFAULT_POLL_TIMEOUT
    updates = get_updates(offset=offset, timeout=timeout)
    handled = 0
    max_uid = last_id
    for u in updates:
        uid = int(u.get("update_id") or 0)
        if uid <= last_id:
            continue
        try:
            res = dispatch_update(u, dry=dry)
        except Exception as e:  # noqa: BLE001
            logger.exception("[TG-BOT] dispatch failed for update_id=%s", uid)
            res = {"handled": False, "kind": "exception", "reason": str(e)}
        if res.get("handled"):
            handled += 1
        max_uid = max(max_uid, uid)

    if max_uid > last_id and not dry:
        state.set_last_update_id(max_uid)
        try:
            n = state.dump_to_csv()
            logger.info("[TG-BOT] state dumped to CSV (rows=%d)", n)
        except Exception as e:  # noqa: BLE001
            logger.warning("[TG-BOT] state.dump_to_csv 失敗: %s", e)

    logger.info(
        "[TG-BOT] polled=%d handled=%d max_update_id=%d dry=%s",
        len(updates), handled, max_uid, dry,
    )
    return {
        "polled": len(updates),
        "handled": handled,
        "max_update_id": max_uid,
    }


def _is_running_in_actions() -> bool:
    return os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"


def run_loop(dry: bool = False) -> None:
    """本機長駐迴圈 — long-poll、Ctrl-C 退出。"""
    if not _is_enabled():
        logger.warning("[TG-BOT] 缺 token / chat_id,abort")
        return
    logger.info("[TG-BOT] loop mode (long-poll %ds)", _DEFAULT_POLL_TIMEOUT)
    while True:
        try:
            run_once(dry=dry)
        except KeyboardInterrupt:
            logger.info("[TG-BOT] interrupted")
            return
        except Exception:  # noqa: BLE001
            logger.exception("[TG-BOT] run_once iteration failed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram bot serve daemon")
    parser.add_argument(
        "--once", action="store_true",
        help="跑一次 poll-dispatch 後退出(GHA cron 模式)",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="本機長駐迴圈模式",
    )
    parser.add_argument(
        "--dry", action="store_true",
        help="dry-run:不真送、不更新 offset / CSV",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if args.loop:
        run_loop(dry=args.dry)
        return 0

    # 預設 once(GHA cron 模式)— --once 跟「不給」一樣行為
    _ = args.once
    res = run_once(dry=args.dry)
    # 0 update 也 exit 0 — GHA cron 不該因為「沒新訊息」就標紅
    return 0 if res is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
