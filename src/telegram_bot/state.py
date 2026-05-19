"""Telegram bot daemon runtime state — SQLite key-value + CSV snapshot 持久化。

主要 use case 是 `last_update_id`(Telegram getUpdates offset)。GHA runner 是
ephemeral container,每次 cron run 都拿到 fresh SQLite;若不 dump 進 repo,
update_id 永遠歸零 → 每次都從頭拉,重複處理同一條訊息。

跟 watchlist / news 一樣兩層保護:
  1. SQLite table `telegram_bot_state` 為 runtime authority
  2. CSV `data/twse_snapshot/telegram_bot_state.csv` 跨 run 持久化(workflow 結尾
     commit + push)
  3. preload_snapshots 在 boot 時把 CSV ingest 回 SQLite(regression-guarded)

公開 API:
- get_state(key, default=None) -> str | None
- set_state(key, value) -> None
- get_last_update_id() -> int                    convenience
- set_last_update_id(uid: int) -> None           convenience
- dump_to_csv() -> int                            silent skip pattern,回寫入 row 數 / -1
- dump_to_string() -> str                         in-memory CSV(Streamlit download_button 用)
- load_from_csv() -> int                          給 preload_snapshots 呼叫
- load_from_string(csv_text: str) -> int          給遠端拉取用
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src import config, database as db

logger = logging.getLogger(__name__)

SNAPSHOT_DIR: Path = config.PROJECT_ROOT / "data" / "twse_snapshot"
STATE_CSV: Path = SNAPSHOT_DIR / "telegram_bot_state.csv"

KEY_LAST_UPDATE_ID = "last_update_id"

# 跟 watchlist_snapshot 同樣的 race-condition guard:load 期間抑制 dump 回呼
_LOAD_IN_PROGRESS: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolved_db_path(db_path: str | Path | None) -> Path:
    raw = str(db_path) if db_path is not None else str(config.DATABASE_PATH)
    p = Path(raw)
    if not p.is_absolute():
        p = config.PROJECT_ROOT / p
    return p


def _db_inside_project(db_path: str | Path | None) -> bool:
    """tests 用 tmp_path 會回 False,跟 watchlist_snapshot 對稱避免污染 commit。"""
    try:
        _resolved_db_path(db_path).resolve().relative_to(
            config.PROJECT_ROOT.resolve()
        )
        return True
    except ValueError:
        return False


# === KV API ===

def get_state(
    key: str,
    default: str | None = None,
    db_path: str | Path | None = None,
) -> str | None:
    """Read `value` for `key`. Missing → return `default`."""
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM telegram_bot_state WHERE key=?", (key,),
        ).fetchone()
    if row is None:
        return default
    return row["value"]


def set_state(
    key: str,
    value: str,
    db_path: str | Path | None = None,
) -> None:
    """Upsert state row。寫完不主動 dump CSV — caller(daemon main loop)結尾
    才呼叫 dump_to_csv,避免每寫一筆都觸發 disk I/O。"""
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO telegram_bot_state (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (str(key), str(value), _now_iso()),
        )


def get_last_update_id(db_path: str | Path | None = None) -> int:
    """Telegram getUpdates offset — 沒紀錄回 0(代表「從頭拉」)。"""
    raw = get_state(KEY_LAST_UPDATE_ID, default="0", db_path=db_path)
    try:
        return int(raw or 0)
    except ValueError:
        logger.warning("[TG-STATE] last_update_id %r 不是整數,回 0", raw)
        return 0


def set_last_update_id(uid: int, db_path: str | Path | None = None) -> None:
    set_state(KEY_LAST_UPDATE_ID, str(int(uid)), db_path=db_path)


# === CSV dump / load ===

def _state_to_dataframe(db_path: str | Path | None) -> pd.DataFrame:
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM telegram_bot_state ORDER BY key"
        ).fetchall()
    return pd.DataFrame(
        [
            {
                "key": r["key"],
                "value": r["value"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ],
        columns=["key", "value", "updated_at"],
    )


def dump_to_string(db_path: str | Path | None = None) -> str:
    """In-memory CSV 字串。Streamlit cloud download_button 用。"""
    return _state_to_dataframe(db_path).to_csv(index=False)


def dump_to_csv(db_path: str | Path | None = None) -> int:
    """把 SQLite telegram_bot_state dump 成 CSV。

    Silent skip 條件(回 -1):
      - SNAPSHOT_DIR 不存在(主公還沒做 weekly snapshot)
      - DB 路徑不在 PROJECT_ROOT 底下(pytest tmp_path)
      - load_from_csv 進行中(防 N 筆 row 觸發 N 次寫)

    Returns: 寫入的 row 數;skip 時回 -1。
    """
    if _LOAD_IN_PROGRESS:
        return -1
    if not SNAPSHOT_DIR.exists():
        return -1
    if not _db_inside_project(db_path):
        return -1

    df = _state_to_dataframe(db_path)
    df.to_csv(STATE_CSV, index=False)
    return len(df)


def _ingest_dataframe(
    df: pd.DataFrame, db_path: str | Path | None,
) -> int:
    """逐筆 upsert,_LOAD_IN_PROGRESS 期間抑制 dump_to_csv 回呼。"""
    global _LOAD_IN_PROGRESS
    if df.empty:
        return 0
    _LOAD_IN_PROGRESS = True
    try:
        n = 0
        for _, r in df.iterrows():
            key = str(r.get("key") or "").strip()
            if not key:
                continue
            val_raw = r.get("value")
            val = "" if val_raw is None or pd.isna(val_raw) else str(val_raw)
            set_state(key, val, db_path=db_path)
            n += 1
        return n
    finally:
        _LOAD_IN_PROGRESS = False


def load_from_csv(db_path: str | Path | None = None) -> int:
    """讀 STATE_CSV 灌進 SQLite。idempotent on key。

    Returns:處理的 row 數(檔不存在 / 空 / DB 在 tmp_path → 0)。
    """
    if not STATE_CSV.exists():
        return 0
    if not _db_inside_project(db_path):
        return 0
    try:
        df = pd.read_csv(STATE_CSV, dtype=str)
    except (pd.errors.EmptyDataError, OSError):
        return 0
    return _ingest_dataframe(df, db_path)


def load_from_string(
    csv_text: str, db_path: str | Path | None = None,
) -> int:
    """從 CSV 字串(遠端拉取 / 測試用)灌進 SQLite。

    跳過 _db_inside_project guard,caller(boot)自己判斷。
    """
    if not csv_text or not csv_text.strip():
        return 0
    try:
        df = pd.read_csv(io.StringIO(csv_text), dtype=str)
    except Exception:  # noqa: BLE001
        return 0
    return _ingest_dataframe(df, db_path)


__all__ = [
    "SNAPSHOT_DIR",
    "STATE_CSV",
    "KEY_LAST_UPDATE_ID",
    "get_state",
    "set_state",
    "get_last_update_id",
    "set_last_update_id",
    "dump_to_csv",
    "dump_to_string",
    "load_from_csv",
    "load_from_string",
]
