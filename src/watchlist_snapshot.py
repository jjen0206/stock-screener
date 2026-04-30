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


def load_from_csv(db_path: str | Path | None = None) -> int:
    """讀 watchlist.csv 灌進 SQLite。idempotent on stock_id。

    add_to_watchlist 會更新 note 但保留原 added_at,所以本機既有 watchlist
    不會被覆蓋(只可能新增 CSV 裡有但 SQLite 沒有的;note 以 CSV 為準)。

    Returns: 處理的 row 數(空檔/不存在/DB 不在 repo 內回 0)。
    """
    global _LOAD_IN_PROGRESS
    if not WATCHLIST_CSV.exists():
        return 0
    if not _db_inside_project(db_path):
        # 跟 dump_to_csv 對稱:tests 用 tmp_path 走這條,避免污染測試 SQLite
        return 0
    df = pd.read_csv(WATCHLIST_CSV, dtype={"stock_id": str})
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
    finally:
        _LOAD_IN_PROGRESS = False
    return n


__all__ = [
    "SNAPSHOT_DIR",
    "WATCHLIST_CSV",
    "dump_to_csv",
    "dump_to_string",
    "load_from_csv",
]
