"""analyst_targets 表 ↔ CSV snapshot 永久化(雲端容器重啟保留目標價)。

Pattern 跟 paper_trades_snapshot.py 對齊,差別:
- analyst_targets 是覆蓋式更新(同 sid+source 永遠 INSERT OR REPLACE),不是
  歷史紀錄,所以 load_from_csv 不檢查表是否為空 — 永遠把 CSV 灌進去 reconcile,
  讓後跑的 fetch_and_store 蓋掉舊 fetched_at 即可。
- GitHub auto-push 走 push_analyst_targets_to_github(同 watchlist-sync 分支)。
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

import pandas as pd

from src import config, database as db

logger = logging.getLogger(__name__)

# load 期間抑制 dump 回呼,避免 N 筆 row 觸發 N 次寫
_LOAD_IN_PROGRESS: bool = False

_COLUMNS = [
    "stock_id", "target_mean", "target_median", "target_high", "target_low",
    "num_analysts", "source", "fetched_at",
]


def _csv_path(snapshot_dir: str | Path | None = None) -> Path:
    if snapshot_dir is None:
        snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    return Path(snapshot_dir) / "analyst_targets.csv"


def _db_inside_project(db_path: str | Path | None) -> bool:
    raw = str(db_path) if db_path is not None else str(config.DATABASE_PATH)
    p = Path(raw)
    if not p.is_absolute():
        p = config.PROJECT_ROOT / p
    try:
        p.resolve().relative_to(config.PROJECT_ROOT.resolve())
        return True
    except ValueError:
        return False


def _to_dataframe(db_path: str | Path | None) -> pd.DataFrame:
    """SQLite analyst_targets → schema 對齊的 DataFrame。"""
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT stock_id, target_mean, target_median, target_high, target_low, "
            "num_analysts, source, fetched_at "
            "FROM analyst_targets ORDER BY stock_id ASC, source ASC"
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)
    return pd.DataFrame([dict(r) for r in rows], columns=_COLUMNS)


def dump_to_string(db_path: str | Path | None = None) -> str:
    """SQLite → CSV 字串(in-memory,給 GitHub push 用)。"""
    return _to_dataframe(db_path).to_csv(index=False)


def dump_to_csv(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> int:
    """SQLite → CSV(覆寫)。回行數;skip 時回 -1。

    跟 paper_trades_snapshot.dump_to_csv 同 silent-skip 邏輯:
    - load 進行中
    - 預設 snapshot_dir + DB 不在 PROJECT_ROOT 底下(pytest tmp_path)
    """
    if _LOAD_IN_PROGRESS:
        return -1
    if snapshot_dir is None and not _db_inside_project(db_path):
        return -1

    df = _to_dataframe(db_path)
    path = _csv_path(snapshot_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return len(df)


def _ingest_dataframe(
    df: pd.DataFrame, db_path: str | Path | None,
) -> int:
    """把 schema 對齊的 DataFrame 灌進 analyst_targets(INSERT OR REPLACE)。

    跟 paper_trades 不同:analyst_targets 是覆蓋式快照,不檢查表是否為空 —
    每次都把 CSV 內容覆蓋進去,後跑的 fetch_and_store 再蓋掉。
    """
    global _LOAD_IN_PROGRESS
    if df.empty:
        return 0
    db.init_db(db_path)

    _LOAD_IN_PROGRESS = True
    try:
        n = 0
        with db.get_conn(db_path) as conn:
            for _, r in df.iterrows():
                sid = str(r.get("stock_id", "") or "").strip()
                source = str(r.get("source", "") or "").strip()
                if not sid or not source:
                    continue

                def _opt_float(key: str) -> float | None:
                    v = r.get(key)
                    if v is None or pd.isna(v):
                        return None
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None

                def _opt_int(key: str) -> int | None:
                    v = r.get(key)
                    if v is None or pd.isna(v):
                        return None
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return None

                target_mean = _opt_float("target_mean")
                if target_mean is None:
                    continue

                fetched_at = r.get("fetched_at")
                if fetched_at is None or pd.isna(fetched_at):
                    fetched_at = ""
                else:
                    fetched_at = str(fetched_at)

                try:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO analyst_targets
                            (stock_id, target_mean, target_median,
                             target_high, target_low, num_analysts,
                             source, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sid,
                            target_mean,
                            _opt_float("target_median"),
                            _opt_float("target_high"),
                            _opt_float("target_low"),
                            _opt_int("num_analysts"),
                            source,
                            fetched_at,
                        ),
                    )
                    n += 1
                except Exception as ex:  # noqa: BLE001
                    logger.warning("[ANALYST_TARGETS] row 灌入失敗:%s", ex)
        return n
    finally:
        _LOAD_IN_PROGRESS = False


def load_from_csv(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> int:
    """CSV → SQLite。CSV 不存在 → 0。"""
    path = _csv_path(snapshot_dir)
    if not path.exists():
        return 0
    try:
        df = pd.read_csv(path, dtype={"stock_id": str, "source": str})
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as ex:
        logger.warning("[ANALYST_TARGETS] 讀 CSV 失敗:%s", ex)
        return 0
    return _ingest_dataframe(df, db_path)


def load_from_string(
    csv_text: str,
    db_path: str | Path | None = None,
) -> int:
    """CSV 字串(github_sync 拉回的)→ SQLite。"""
    if not csv_text or not csv_text.strip():
        return 0
    try:
        df = pd.read_csv(
            io.StringIO(csv_text),
            dtype={"stock_id": str, "source": str},
        )
    except Exception as ex:  # noqa: BLE001
        logger.warning("[ANALYST_TARGETS] parse csv 字串失敗:%s", ex)
        return 0
    return _ingest_dataframe(df, db_path)


def safe_boot_load(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Boot 容錯入口:remote-first(GitHub watchlist-sync)→ 失敗 fallback 本機。

    跟 paper_trades_snapshot.safe_boot_load 同 pattern。承諾「絕不 raise」。
    Returns: 描述本次走哪條路徑的短字串。
    """
    try:
        from src.github_sync import fetch_analyst_targets_from_github
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[BOOT] github_sync.fetch_analyst_targets 不存在 (%s),fallback 本機",
            ex,
        )
        load_from_csv(snapshot_dir=snapshot_dir, db_path=db_path)
        return "fallback-import-error"

    try:
        remote_csv = fetch_analyst_targets_from_github()
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[BOOT] fetch_analyst_targets_from_github 拋例外 (%s),fallback 本機",
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
            "[BOOT] load_from_string 失敗 (%s),補跑本機 load_from_csv", ex,
        )
        load_from_csv(snapshot_dir=snapshot_dir, db_path=db_path)
        return "fallback-load-error"


__all__ = [
    "dump_to_csv",
    "dump_to_string",
    "load_from_csv",
    "load_from_string",
    "safe_boot_load",
]
