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


def dump_to_csv(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> int:
    """SQLite trades 表 → CSV(覆寫)。回行數。空表 → 寫只 header 的 CSV。"""
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


def safe_boot_load(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> int:
    """Boot path 用 — 任何錯誤 silent fallback 回 0,絕不 raise。

    跟 watchlist_snapshot.safe_boot_load 同 pattern。
    """
    try:
        return load_from_csv(snapshot_dir=snapshot_dir, db_path=db_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("[BOOT] portfolio_snapshot.safe_boot_load 失敗:%s", e)
        return 0


__all__ = ["dump_to_csv", "load_from_csv", "safe_boot_load"]
