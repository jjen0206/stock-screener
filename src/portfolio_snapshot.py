"""trades 表 → CSV snapshot 永久化(個人 P&L 追蹤)。

雲端 SQLite ephemeral 重啟清光,trades 跟 watchlist 一樣需要 snapshot 機制
讓使用者本地新增的交易跨容器保留。

Pattern 跟 src/watchlist_snapshot.py 對齊:
- dump_to_csv() 把 trades 表 dump 進 data/twse_snapshot/trades.csv
- safe_boot_load() Streamlit boot 時讀回 SQLite(silent fallback,絕不 raise)
- load_from_csv() 只在 trades 表為空時灌 → 避免覆蓋使用者本地新加的交易

GitHub auto-push 雙向同步(讓雲端容器重啟也保留)留下個 commit (Commit 2b)
做,先用本地 CSV snapshot 即可。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from src import config, database as db

logger = logging.getLogger(__name__)


def _csv_path(snapshot_dir: str | Path | None = None) -> Path:
    if snapshot_dir is None:
        snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    return Path(snapshot_dir) / "trades.csv"


def _db_inside_project(db_path: str | Path | None) -> bool:
    """判斷 SQLite 路徑是否在 PROJECT_ROOT 底下;test 用 tmp_path 會回 False
    → 跳過 dump 避免污染 repo trades.csv。仿 watchlist_snapshot 同名 helper。
    """
    raw = str(db_path) if db_path is not None else str(config.DATABASE_PATH)
    p = Path(raw)
    if not p.is_absolute():
        p = config.PROJECT_ROOT / p
    try:
        p.resolve().relative_to(config.PROJECT_ROOT.resolve())
        return True
    except ValueError:
        return False


def dump_to_csv(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> int:
    """SQLite trades 表 → CSV(覆寫)。回行數;skip 時回 -1。

    Silent skip(回 -1):
    - DB 路徑不在 PROJECT_ROOT 底下(pytest tmp_path)— 避免 test 污染 repo
      trades.csv。caller 拿到 -1 知道沒實際寫,不該觸發 GitHub push。
    - 但 caller 顯式傳 snapshot_dir(test 用 tmp_path)→ 不檢查 DB 路徑,
      允許 round-trip test。
    """
    if snapshot_dir is None and not _db_inside_project(db_path):
        return -1

    trades = db.get_trades(db_path=db_path)
    if trades:
        df = pd.DataFrame(trades)
    else:
        df = pd.DataFrame(columns=[
            "id", "stock_id", "direction", "price", "quantity",
            "trade_date", "note", "created_at",
        ])
    path = _csv_path(snapshot_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return len(df)


def load_from_csv(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> int:
    """CSV → SQLite trades 表。

    **只在 trades 表為空時灌**,避免覆蓋本機使用者新加的交易。
    回灌入 row 數;CSV 不存在或表已有資料 → 0。
    """
    path = _csv_path(snapshot_dir)
    if not path.exists():
        return 0
    db.init_db(db_path)

    with db.get_conn(db_path) as conn:
        existing = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    if existing > 0:
        return 0

    df = pd.read_csv(path, dtype={"stock_id": str})
    if df.empty:
        return 0

    n = 0
    with db.get_conn(db_path) as conn:
        for _, r in df.iterrows():
            try:
                conn.execute(
                    "INSERT INTO trades "
                    "(stock_id, direction, price, quantity, "
                    "trade_date, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(r["stock_id"]),
                        str(r["direction"]),
                        float(r["price"]),
                        int(r["quantity"]),
                        str(r["trade_date"]),
                        str(r["note"]) if pd.notna(r.get("note")) else None,
                        str(r["created_at"]) if pd.notna(r.get("created_at"))
                        else "",
                    ),
                )
                n += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("[PORTFOLIO] trade row 灌入失敗:%s", e)
    return n


def load_from_string(
    csv_text: str,
    db_path: str | Path | None = None,
) -> int:
    """從 CSV 字串(github_sync 拉回的)灌進 SQLite trades 表。

    跟 load_from_csv 邏輯一致(只灌空表),但接 string 不接檔案路徑。
    """
    if not csv_text or not csv_text.strip():
        return 0
    db.init_db(db_path)

    with db.get_conn(db_path) as conn:
        existing = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    if existing > 0:
        return 0

    try:
        from io import StringIO
        df = pd.read_csv(StringIO(csv_text), dtype={"stock_id": str})
    except Exception as e:  # noqa: BLE001
        logger.warning("[PORTFOLIO] parse csv 字串失敗:%s", e)
        return 0
    if df.empty:
        return 0

    n = 0
    with db.get_conn(db_path) as conn:
        for _, r in df.iterrows():
            try:
                conn.execute(
                    "INSERT INTO trades "
                    "(stock_id, direction, price, quantity, "
                    "trade_date, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(r["stock_id"]),
                        str(r["direction"]),
                        float(r["price"]),
                        int(r["quantity"]),
                        str(r["trade_date"]),
                        str(r["note"]) if pd.notna(r.get("note")) else None,
                        str(r["created_at"]) if pd.notna(r.get("created_at"))
                        else "",
                    ),
                )
                n += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("[PORTFOLIO] trade row(string)灌入失敗:%s", e)
    return n


def safe_boot_load(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Boot 容錯入口:remote-first(GitHub watchlist-sync) → 失敗 fallback 本機 CSV。

    跟 watchlist_snapshot.safe_boot_load 同 pattern。承諾「絕不 raise」(boot
    路徑禁止 crash)。任何例外 → fallback 本機 CSV。

    Returns: 描述本次走哪條路徑的短字串(供 caller log,不影響功能)。
    """
    try:
        from src.github_sync import fetch_trades_from_github
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[BOOT] github_sync import 失敗 (%s),fallback 本機 trades.csv",
            ex,
        )
        load_from_csv(snapshot_dir=snapshot_dir, db_path=db_path)
        return "fallback-import-error"

    try:
        remote_csv = fetch_trades_from_github()
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[BOOT] fetch_trades_from_github 拋例外 (%s),fallback 本機",
            ex,
        )
        load_from_csv(snapshot_dir=snapshot_dir, db_path=db_path)
        return "fallback-fetch-exception"

    if remote_csv is None:
        load_from_csv(snapshot_dir=snapshot_dir, db_path=db_path)
        return "fallback-no-remote"

    try:
        load_from_string(remote_csv, db_path=db_path)
        return "remote"
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[BOOT] load_from_string 失敗 (%s),補跑本機 load_from_csv",
            ex,
        )
        load_from_csv(snapshot_dir=snapshot_dir, db_path=db_path)
        return "fallback-load-error"


__all__ = [
    "dump_to_csv",
    "load_from_csv",
    "load_from_string",
    "safe_boot_load",
]
