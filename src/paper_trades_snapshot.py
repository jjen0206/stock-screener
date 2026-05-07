"""paper_trades 表 ↔ CSV snapshot 永久化(實測追蹤跨容器重啟保留)。

Pattern 跟 src/portfolio_snapshot.py / src/watchlist_snapshot.py 對齊:
- dump_to_csv() 把 paper_trades 表 dump 進 data/twse_snapshot/paper_trades.csv
- safe_boot_load() Streamlit boot 時讀回 SQLite(silent fallback,絕不 raise)
- load_from_csv() 只在 paper_trades 表為空時灌 → 避免覆蓋使用者本地新加的紀錄

修復背景:雲端 SQLite ephemeral,容器重啟清光,paper_trades 跟 watchlist /
trades 一樣需要 snapshot 機制 — 主公 2026-05-07 發現「昨天加的實測追蹤不見了」,
診斷確認 paper_trades 從未被 dump 進 git snapshot。

GitHub auto-push 雙向同步走 push_paper_trades_to_github(同 watchlist-sync 分支)。
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pandas as pd

from src import config, database as db

logger = logging.getLogger(__name__)

# 跟 watchlist_snapshot 對齊:load_from_csv 期間抑制 dump 回呼,避免 N 筆 row 觸發 N 次寫
_LOAD_IN_PROGRESS: bool = False

_COLUMNS = [
    "id", "sid", "name", "entry_date", "entry_price",
    "matched_strategies", "ml_prob",
    "target_price", "stop_price", "current_stop", "trailing_level",
    "hold_days", "expected_exit_date",
    "actual_exit_date", "actual_exit_price",
    "status", "return_pct", "notes",
    "created_at", "updated_at",
]


def _csv_path(snapshot_dir: str | Path | None = None) -> Path:
    if snapshot_dir is None:
        snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    return Path(snapshot_dir) / "paper_trades.csv"


def _db_inside_project(db_path: str | Path | None) -> bool:
    """判斷 SQLite 路徑是否在 PROJECT_ROOT 底下;test 用 tmp_path 會回 False
    → 跳過 dump 避免污染 repo paper_trades.csv。
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


def _paper_trades_to_dataframe(db_path: str | Path | None) -> pd.DataFrame:
    """把 SQLite paper_trades 表轉成 schema 對齊的 DataFrame(共用 dump 路徑)。"""
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM paper_trades ORDER BY entry_date ASC, id ASC"
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=_COLUMNS)
    return pd.DataFrame([dict(r) for r in rows], columns=_COLUMNS)


def dump_to_string(db_path: str | Path | None = None) -> str:
    """SQLite paper_trades 表 → CSV 字串(in-memory,不寫檔)。供 GitHub push 用。"""
    return _paper_trades_to_dataframe(db_path).to_csv(index=False)


def dump_to_csv(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> int:
    """SQLite paper_trades 表 → CSV(覆寫)。回行數;skip 時回 -1。

    Silent skip(回 -1):
    - load_from_csv 進行中(避免 boot 期間 N 筆觸發 N 次寫)
    - DB 路徑不在 PROJECT_ROOT 底下(pytest tmp_path)— 避免 test 污染 repo
    - 但 caller 顯式傳 snapshot_dir(test 用 tmp_path)→ 不檢查 DB 路徑,
      允許 round-trip test。
    """
    if _LOAD_IN_PROGRESS:
        return -1
    if snapshot_dir is None and not _db_inside_project(db_path):
        return -1

    df = _paper_trades_to_dataframe(db_path)
    path = _csv_path(snapshot_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return len(df)


def _ingest_paper_trades_dataframe(
    df: pd.DataFrame, db_path: str | Path | None,
) -> int:
    """把 schema 對齊的 DataFrame 灌進 paper_trades(只在表為空時)。

    跟 portfolio_snapshot 同邏輯:**只在 paper_trades 表為空時灌**,避免
    覆蓋本機使用者新加的紀錄。

    matched_strategies / status 等欄位保留原值;id 不還原(讓 SQLite 自動分配)
    避免跟新進的紀錄 PK 衝突。
    """
    global _LOAD_IN_PROGRESS
    if df.empty:
        return 0
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT COUNT(*) FROM paper_trades"
        ).fetchone()[0]
    if existing > 0:
        return 0

    _LOAD_IN_PROGRESS = True
    try:
        n = 0
        with db.get_conn(db_path) as conn:
            for _, r in df.iterrows():
                sid = str(r.get("sid", "") or "").strip()
                if not sid:
                    continue
                try:
                    entry_price = float(r["entry_price"])
                    target_price = float(r["target_price"])
                    stop_price = float(r["stop_price"])
                except (TypeError, ValueError, KeyError):
                    continue
                if entry_price <= 0:
                    continue

                def _opt_str(key: str) -> str | None:
                    v = r.get(key)
                    return None if v is None or pd.isna(v) else str(v)

                def _opt_float(key: str) -> float | None:
                    v = r.get(key)
                    if v is None or pd.isna(v):
                        return None
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None

                def _opt_int(key: str, default: int = 0) -> int:
                    v = r.get(key)
                    if v is None or pd.isna(v):
                        return default
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return default

                # matched_strategies 來自 CSV 可能是 JSON 字串、亦可能是空 → 規範化
                matched_raw = r.get("matched_strategies")
                if matched_raw is None or pd.isna(matched_raw):
                    matched_json: str | None = None
                else:
                    s = str(matched_raw).strip()
                    if not s:
                        matched_json = None
                    else:
                        # 驗證 parsable;parse 不過就照 raw 存(向前相容)
                        try:
                            json.loads(s)
                            matched_json = s
                        except (TypeError, ValueError):
                            matched_json = None

                current_stop = _opt_float("current_stop")
                if current_stop is None:
                    current_stop = stop_price
                trailing_level = _opt_int("trailing_level", 0)
                hold_days = _opt_int("hold_days", 5) or 5

                status = _opt_str("status") or "active"
                if status not in (
                    "active", "win", "lose", "timeout_win", "timeout_lose",
                ):
                    status = "active"

                created_at = _opt_str("created_at") or ""
                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO paper_trades
                            (sid, name, entry_date, entry_price,
                             matched_strategies, ml_prob,
                             target_price, stop_price,
                             current_stop, trailing_level,
                             hold_days, expected_exit_date,
                             actual_exit_date, actual_exit_price,
                             status, return_pct, notes,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sid,
                            _opt_str("name"),
                            str(r["entry_date"]),
                            entry_price,
                            matched_json,
                            _opt_float("ml_prob"),
                            target_price,
                            stop_price,
                            current_stop,
                            trailing_level,
                            hold_days,
                            _opt_str("expected_exit_date"),
                            _opt_str("actual_exit_date"),
                            _opt_float("actual_exit_price"),
                            status,
                            _opt_float("return_pct"),
                            _opt_str("notes"),
                            created_at,
                            _opt_str("updated_at"),
                        ),
                    )
                    n += 1
                except Exception as ex:  # noqa: BLE001
                    logger.warning(
                        "[PAPER_TRADES] row 灌入失敗:%s", ex,
                    )
        return n
    finally:
        _LOAD_IN_PROGRESS = False


def load_from_csv(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> int:
    """CSV → SQLite paper_trades 表(只在表為空時灌)。

    跟 portfolio_snapshot.load_from_csv 同 pattern。回灌入 row 數;CSV 不存在
    或表已有資料 → 0。
    """
    path = _csv_path(snapshot_dir)
    if not path.exists():
        return 0
    try:
        df = pd.read_csv(path, dtype={"sid": str})
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as ex:
        logger.warning("[PAPER_TRADES] 讀 CSV 失敗:%s", ex)
        return 0
    return _ingest_paper_trades_dataframe(df, db_path)


def load_from_string(
    csv_text: str,
    db_path: str | Path | None = None,
) -> int:
    """CSV 字串(github_sync 拉回的)→ SQLite paper_trades 表(只在表為空時灌)。"""
    if not csv_text or not csv_text.strip():
        return 0
    try:
        df = pd.read_csv(io.StringIO(csv_text), dtype={"sid": str})
    except Exception as ex:  # noqa: BLE001
        logger.warning("[PAPER_TRADES] parse csv 字串失敗:%s", ex)
        return 0
    return _ingest_paper_trades_dataframe(df, db_path)


def safe_boot_load(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Boot 容錯入口:remote-first(GitHub watchlist-sync)→ 失敗 fallback 本機 CSV。

    跟 portfolio_snapshot / watchlist_snapshot.safe_boot_load 同 pattern。
    承諾「絕不 raise」(boot 路徑禁止 crash)。任何例外 → fallback 本機 CSV。

    Returns: 描述本次走哪條路徑的短字串(供 caller log,不影響功能)。
    """
    try:
        from src.github_sync import fetch_paper_trades_from_github
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[BOOT] github_sync.fetch_paper_trades 不存在或 import 失敗 (%s),"
            "fallback 本機 paper_trades.csv",
            ex,
        )
        load_from_csv(snapshot_dir=snapshot_dir, db_path=db_path)
        return "fallback-import-error"

    try:
        remote_csv = fetch_paper_trades_from_github()
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[BOOT] fetch_paper_trades_from_github 拋例外 (%s),fallback 本機",
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
    "dump_to_string",
    "load_from_csv",
    "load_from_string",
    "safe_boot_load",
]
