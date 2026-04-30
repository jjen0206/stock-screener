"""watchlist 表 ↔ CSV snapshot 互轉。

讓使用者 ☆ 加入的關注清單能跨容器重啟保留:
  - SQLite (本機 / Streamlit Cloud 容器內) 是 ephemeral
  - data/twse_snapshot/watchlist.csv (commit 進 repo) 是 persistent

呼叫時機:
  - dump_to_csv: 每次 add_to_watchlist / remove_from_watchlist 後 (在 src.database 內呼叫)
  - load_from_csv: app boot (_load_snapshot_if_needed)、weekly script 開頭

Schema: stock_id, added_at, note
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

from src import config

SNAPSHOT_DIR: Path = config.PROJECT_ROOT / "data" / "twse_snapshot"
WATCHLIST_CSV: Path = SNAPSHOT_DIR / "watchlist.csv"

# load_from_csv 會逐筆呼叫 db.add_to_watchlist,而後者又會反呼 dump_to_csv;
# 用此 flag 在 load 期間抑制 dump,避免 N 筆 row 觸發 N 次重複寫檔。
_LOAD_IN_PROGRESS: bool = False


def _resolved_db_path(db_path: str | Path | None) -> Path:
    """把 db_path (可能是相對路徑) 轉成絕對 Path,若 None 則用 config.DATABASE_PATH。"""
    raw = str(db_path) if db_path is not None else str(config.DATABASE_PATH)
    p = Path(raw)
    if not p.is_absolute():
        p = config.PROJECT_ROOT / p
    return p


def _db_inside_project(db_path: str | Path | None) -> bool:
    """判斷 SQLite 路徑是不是在 PROJECT_ROOT 底下;tests 用 tmp_path 會回 False。"""
    try:
        _resolved_db_path(db_path).resolve().relative_to(
            config.PROJECT_ROOT.resolve()
        )
        return True
    except ValueError:
        return False


def _watchlist_to_dataframe(db_path: str | Path | None) -> pd.DataFrame:
    """把 SQLite watchlist 表轉成 schema 對齊的 DataFrame(共用給 dump_to_csv / dump_to_string)。"""
    from src import database as db
    items = db.get_watchlist(db_path=db_path)
    return pd.DataFrame(
        [
            {
                "stock_id": it["stock_id"],
                "added_at": it["added_at"],
                "note": it.get("note"),
            }
            for it in items
        ],
        columns=["stock_id", "added_at", "note"],
    )


def dump_to_string(db_path: str | Path | None = None) -> str:
    """把 SQLite watchlist 表 dump 成 CSV 字串(in-memory,不寫檔)。

    供 Streamlit 雲端 download_button 用 — 雲端容器無法直接 git push,
    改讓使用者下載後自行 commit 永久化。不受 SNAPSHOT_DIR / db path guard 限制。
    """
    return _watchlist_to_dataframe(db_path).to_csv(index=False)


def dump_to_csv(db_path: str | Path | None = None) -> int:
    """把 SQLite watchlist 表 dump 成 watchlist.csv。

    Silent skip 條件 (回 -1):
      - SNAPSHOT_DIR 不存在 (使用者沒做 weekly snapshot)
      - DB 路徑不在 PROJECT_ROOT 底下 (pytest tmp_path)
      - load_from_csv 進行中 (避免 boot 期間 N 次重複寫)

    SQLite 為空時也寫一份(空 csv 含 header),反映「使用者刪光」狀態。

    Returns: 寫入的 row 數;skip 時回 -1。
    """
    if _LOAD_IN_PROGRESS:
        return -1
    if not SNAPSHOT_DIR.exists():
        return -1
    if not _db_inside_project(db_path):
        return -1

    df = _watchlist_to_dataframe(db_path)
    df.to_csv(WATCHLIST_CSV, index=False)
    return len(df)


def _ingest_watchlist_dataframe(
    df: pd.DataFrame, db_path: str | Path | None,
) -> int:
    """把 schema 對齊的 DataFrame 逐筆 add_to_watchlist。共用給 load_from_csv / load_from_string。

    add_to_watchlist 是 idempotent:既有 row 保留 added_at,只覆寫 note。
    過程中設 _LOAD_IN_PROGRESS 抑制 dump_to_csv 的回呼(避免 N 筆 row 觸發 N 次寫檔
    + N 次 GitHub push)。
    """
    global _LOAD_IN_PROGRESS
    if df.empty:
        return 0
    from src import database as db
    _LOAD_IN_PROGRESS = True
    try:
        n = 0
        for _, r in df.iterrows():
            sid = str(r["stock_id"]).strip()
            if not sid:
                continue
            note_val = r.get("note")
            note = (
                None if note_val is None or pd.isna(note_val)
                else str(note_val)
            )
            ts_val = r.get("added_at")
            added_at = (
                None
                if ts_val is None or pd.isna(ts_val) or not str(ts_val).strip()
                else str(ts_val).strip()
            )
            db.add_to_watchlist(
                sid, note=note, added_at=added_at, db_path=db_path,
            )
            n += 1
        return n
    finally:
        _LOAD_IN_PROGRESS = False


def load_from_csv(db_path: str | Path | None = None) -> int:
    """讀本機 watchlist.csv 灌進 SQLite。idempotent on stock_id。

    本機 dev / weekly script 用;雲端 boot 時優先走 load_from_string + 遠端 fetch。

    Returns: 處理的 row 數(空檔/不存在/DB 不在 repo 內回 0)。
    """
    if not WATCHLIST_CSV.exists():
        return 0
    if not _db_inside_project(db_path):
        # 跟 dump_to_csv 對稱:tests 用 tmp_path 走這條,避免污染測試 SQLite
        return 0
    df = pd.read_csv(WATCHLIST_CSV, dtype={"stock_id": str})
    return _ingest_watchlist_dataframe(df, db_path)


def load_from_string(
    csv_text: str, db_path: str | Path | None = None,
) -> int:
    """把 CSV 字串(通常來自 watchlist-sync 分支的遠端拉取)灌進 SQLite。

    跟 load_from_csv 同 schema:stock_id, added_at, note。
    跳過 _db_inside_project 檢查 — caller(app.py boot)需自行決定何時呼叫。

    Returns: 處理的 row 數(空字串/解析失敗回 0)。
    """
    if not csv_text or not csv_text.strip():
        return 0
    try:
        df = pd.read_csv(io.StringIO(csv_text), dtype={"stock_id": str})
    except Exception:  # noqa: BLE001
        return 0
    return _ingest_watchlist_dataframe(df, db_path)


def safe_boot_load(db_path: str | Path | None = None) -> str:
    """雲端 boot 容錯入口:嘗試從 watchlist-sync 遠端拉,失敗一律 fallback 本機 seed。

    任何例外(ImportError、AttributeError、HTTP error、parse error)都吞掉並
    fallback 到 load_from_csv。這個函式承諾「絕不 raise」,因為 caller 是
    app.py 的 boot 路徑,raise 等於整個 Streamlit app crash。

    Returns: 描述本次走哪條路徑的短字串(供 caller log / debug 用),
             非錯誤訊號 — caller 不需要 branch 任何邏輯。
    """
    import logging
    logger = logging.getLogger(__name__)
    try:
        from src.github_sync import fetch_watchlist_from_github
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[BOOT] github_sync import 失敗 (%s),fallback seed CSV", ex,
        )
        load_from_csv(db_path=db_path)
        return "fallback-import-error"

    try:
        remote_csv = fetch_watchlist_from_github()
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[BOOT] fetch_watchlist_from_github 拋例外 (%s),fallback seed CSV",
            ex,
        )
        load_from_csv(db_path=db_path)
        return "fallback-fetch-exception"

    if remote_csv is None:
        load_from_csv(db_path=db_path)
        return "fallback-no-remote"

    try:
        load_from_string(remote_csv, db_path=db_path)
        return "remote"
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[BOOT] load_from_string 失敗 (%s),補跑 load_from_csv", ex,
        )
        load_from_csv(db_path=db_path)
        return "fallback-load-error"


__all__ = [
    "SNAPSHOT_DIR",
    "WATCHLIST_CSV",
    "dump_to_csv",
    "dump_to_string",
    "load_from_csv",
    "load_from_string",
    "safe_boot_load",
]
