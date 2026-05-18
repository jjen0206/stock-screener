"""sync_log_heartbeat:cron task 成功/失敗時間戳記。

封死「silent fail 但主公不知道」盲點:每個排程任務(morning_brief、daily_notify、
backfill_revenue 等)收尾呼叫 record_success / record_failure,daily cron
(scripts/cron_health_alert.py)掃「last_success > expected_interval * 2」推 Telegram。

設計:
  - **CSV 為 source of truth**(GHA runner 是 fresh container,SQLite ephemeral
    → 每個 workflow 直接讀寫 CSV 然後 commit + push,跨 runner 持久化靠 git)
  - SQLite(sync_log_heartbeat 表)透過 preload_snapshots 從 CSV 載入,
    給 Streamlit Dashboard / cron_health_alert.py 查詢用
  - dump_to_csv / load_from_csv 完整 e2e wire pattern,遵守
    `feedback_watchlist_persistence_invariants` + `feedback_e2e_test_isolation`

Schema (CSV + SQLite 對齊):
  task_name              TEXT PRIMARY KEY  (e.g. "morning_brief")
  last_success_at        TEXT              ISO 8601 UTC (Z 結尾,e.g. 2026-05-18T10:00:00Z)
  last_failure_at        TEXT (nullable)
  last_failure_reason    TEXT (nullable, 截 200 字)
  expected_interval_hours REAL              預期間隔(supports 0.5h = intraday)
  updated_at             TEXT              最後一次 record 寫入時間

關於時區:**全用 UTC**,顯示時 caller 自己轉台北。
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src import config

SNAPSHOT_DIR: Path = config.PROJECT_ROOT / "data" / "twse_snapshot"
HEARTBEAT_CSV: Path = SNAPSHOT_DIR / "sync_log_heartbeat.csv"

_COLUMNS = [
    "task_name",
    "last_success_at",
    "last_failure_at",
    "last_failure_reason",
    "expected_interval_hours",
    "updated_at",
]

_REASON_MAX = 200  # 截 reason 避免 CSV row 爆;200 chars 對 Telegram 推播也夠長


def _now_utc_iso() -> str:
    """產生 ISO 8601 UTC 字串(Z 結尾,秒級精度,方便 str 比大小排序)。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_df() -> pd.DataFrame:
    """空 DataFrame,所有 str 欄位明確 object dtype,interval 欄位 float64。

    避免 pandas 在空 df concat / loc 賦值時 dtype 推斷成 float64 然後
    碰到字串值就 FutureWarning(2026 後會 raise)。
    """
    df = pd.DataFrame({
        "task_name": pd.Series(dtype="object"),
        "last_success_at": pd.Series(dtype="object"),
        "last_failure_at": pd.Series(dtype="object"),
        "last_failure_reason": pd.Series(dtype="object"),
        "expected_interval_hours": pd.Series(dtype="float64"),
        "updated_at": pd.Series(dtype="object"),
    })
    return df[_COLUMNS]


def _read_csv(csv_path: Path) -> pd.DataFrame:
    """讀 CSV,缺檔/空檔回空 DataFrame(schema + dtype 對齊)。

    強制 string dtype on text 欄位:reason 可能是 "503" → pandas 預設會推
    int64,破壞「reason 是字串」的 contract。
    """
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return _empty_df()
    try:
        df = pd.read_csv(
            csv_path,
            dtype={
                "task_name": "object",
                "last_success_at": "object",
                "last_failure_at": "object",
                "last_failure_reason": "object",
                "updated_at": "object",
            },
        )
    except pd.errors.EmptyDataError:
        return _empty_df()
    # 補欄位(舊版本 CSV 可能缺欄)
    for col in _COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[_COLUMNS]


def _write_csv(df: pd.DataFrame, csv_path: Path) -> None:
    """寫 CSV;sort by task_name 讓 git diff 穩定。caller 保證 parent dir 存在。"""
    df = df.sort_values("task_name").reset_index(drop=True)
    df.to_csv(csv_path, index=False)


def _upsert_row(df: pd.DataFrame, row: dict[str, Any]) -> pd.DataFrame:
    """以 task_name 為 PK 在 DataFrame 上 upsert 一筆。回新 DataFrame(不原地改)。

    新 row 用 _empty_df() 起手保證 dtype 對齊,避免 concat 觸發 FutureWarning。
    """
    task = row["task_name"]
    if df.empty or task not in set(df["task_name"]):
        new_row = _empty_df()
        new_row.loc[0] = row
        return pd.concat([df, new_row], ignore_index=True)
    df = df.copy()
    mask = df["task_name"] == task
    for col, val in row.items():
        df.loc[mask, col] = val
    return df


def record_success(
    task_name: str,
    expected_interval_hours: float,
    csv_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """記一筆「success」到 sync_log_heartbeat.csv。

    保留既有 last_failure_at / last_failure_reason(成功不清掉,讓 caller 知道
    最近一次失敗發生過 — alert script 自己決定要不要顯示)。

    Args:
      task_name: 識別字串 (e.g. "morning_brief"),CSV 主鍵
      expected_interval_hours: 預期成功間隔;> interval * 2 → stale
      csv_path: 覆寫 CSV 位置(test 用 tmp_path)
      now: 覆寫當前時間(test 用)

    Returns: 寫入後該 row 的 dict
    """
    csv_path = csv_path or HEARTBEAT_CSV
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    ts = (
        now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if now is not None
        else _now_utc_iso()
    )
    df = _read_csv(csv_path)

    existing = df[df["task_name"] == task_name]
    last_failure_at = (
        existing["last_failure_at"].iloc[0] if not existing.empty else None
    )
    last_failure_reason = (
        existing["last_failure_reason"].iloc[0] if not existing.empty else None
    )
    if pd.isna(last_failure_at):
        last_failure_at = None
    if pd.isna(last_failure_reason):
        last_failure_reason = None

    row = {
        "task_name": task_name,
        "last_success_at": ts,
        "last_failure_at": last_failure_at,
        "last_failure_reason": last_failure_reason,
        "expected_interval_hours": float(expected_interval_hours),
        "updated_at": ts,
    }
    df = _upsert_row(df, row)
    _write_csv(df, csv_path)
    return row


def record_failure(
    task_name: str,
    reason: str,
    csv_path: Path | None = None,
    now: datetime | None = None,
    expected_interval_hours: float | None = None,
) -> dict[str, Any]:
    """記一筆「failure」到 sync_log_heartbeat.csv。

    保留既有 last_success_at + expected_interval_hours(只刷新 failure 欄位)。
    若該 task 從未成功過(CSV 沒這 row),強制要 expected_interval_hours,否則
    無從判斷 stale。

    Args:
      task_name: 識別字串
      reason: 失敗原因(會截 200 字)
      csv_path / now: 同 record_success
      expected_interval_hours: 該 task 首次出現時必填;既有 row 留 None 表「不覆寫」

    Returns: 寫入後該 row 的 dict
    """
    csv_path = csv_path or HEARTBEAT_CSV
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    ts = (
        now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if now is not None
        else _now_utc_iso()
    )
    truncated = (reason or "")[:_REASON_MAX]
    df = _read_csv(csv_path)

    existing = df[df["task_name"] == task_name]
    if existing.empty:
        if expected_interval_hours is None:
            raise ValueError(
                f"record_failure({task_name!r}) 首次出現須給 expected_interval_hours"
            )
        last_success_at = None
        interval = float(expected_interval_hours)
    else:
        last_success_at = existing["last_success_at"].iloc[0]
        if pd.isna(last_success_at):
            last_success_at = None
        interval = (
            float(expected_interval_hours)
            if expected_interval_hours is not None
            else float(existing["expected_interval_hours"].iloc[0])
        )

    row = {
        "task_name": task_name,
        "last_success_at": last_success_at,
        "last_failure_at": ts,
        "last_failure_reason": truncated,
        "expected_interval_hours": interval,
        "updated_at": ts,
    }
    df = _upsert_row(df, row)
    _write_csv(df, csv_path)
    return row


def load_from_csv(
    db_path: str | Path | None = None, csv_path: Path | None = None
) -> int:
    """把 sync_log_heartbeat.csv 灌進 SQLite。

    給 preload_snapshots / streamlit boot 用,跟 watchlist_snapshot.load_from_csv
    對稱。idempotent on task_name。

    Returns: 處理的 row 數(空檔/不存在回 0)。
    """
    csv_path = csv_path or HEARTBEAT_CSV
    if not csv_path.exists():
        return 0
    df = _read_csv(csv_path)
    if df.empty:
        return 0

    from src import database as db
    db.init_db(db_path)

    records = []
    for _, r in df.iterrows():
        task = str(r["task_name"]).strip()
        if not task:
            continue

        def _none_if_na(v: Any) -> Any:
            return None if v is None or pd.isna(v) else v

        records.append({
            "task_name": task,
            "last_success_at": _none_if_na(r.get("last_success_at")),
            "last_failure_at": _none_if_na(r.get("last_failure_at")),
            "last_failure_reason": _none_if_na(r.get("last_failure_reason")),
            "expected_interval_hours": (
                float(r["expected_interval_hours"])
                if pd.notna(r.get("expected_interval_hours"))
                else 24.0
            ),
            "updated_at": _none_if_na(r.get("updated_at")) or _now_utc_iso(),
        })

    if not records:
        return 0

    with db.get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO sync_log_heartbeat (
                task_name, last_success_at, last_failure_at,
                last_failure_reason, expected_interval_hours, updated_at
            ) VALUES (
                :task_name, :last_success_at, :last_failure_at,
                :last_failure_reason, :expected_interval_hours, :updated_at
            )
            ON CONFLICT(task_name) DO UPDATE SET
                last_success_at=excluded.last_success_at,
                last_failure_at=excluded.last_failure_at,
                last_failure_reason=excluded.last_failure_reason,
                expected_interval_hours=excluded.expected_interval_hours,
                updated_at=excluded.updated_at
            """,
            records,
        )
    return len(records)


def dump_to_csv(
    db_path: str | Path | None = None, csv_path: Path | None = None
) -> int:
    """把 SQLite sync_log_heartbeat 表 dump 成 CSV。

    Streamlit Dashboard 改寫時用(目前 record_success/failure 是直接寫 CSV,
    所以正常 cron 路徑用不到 dump);保留對稱 API 給未來 UI 編輯。

    Returns: 寫入的 row 數;表不存在/SNAPSHOT_DIR 不存在 → -1。
    """
    csv_path = csv_path or HEARTBEAT_CSV
    if not csv_path.parent.exists():
        return -1

    from src import database as db
    try:
        with db.get_conn(db_path) as conn:
            rows = conn.execute(
                """
                SELECT task_name, last_success_at, last_failure_at,
                       last_failure_reason, expected_interval_hours, updated_at
                  FROM sync_log_heartbeat
                """
            ).fetchall()
    except Exception:  # noqa: BLE001  -- 表不存在等 → silent
        return -1

    df = pd.DataFrame([dict(r) for r in rows], columns=_COLUMNS)
    _write_csv(df, csv_path)
    return len(df)


def _parse_iso(s: str | None) -> datetime | None:
    """把 ISO 8601 UTC 字串轉 timezone-aware datetime;失敗回 None。"""
    if not s:
        return None
    try:
        # 支援 "2026-05-18T10:00:00Z" / "2026-05-18T10:00:00+00:00"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def find_stale_tasks(
    db_path: str | Path | None = None,
    now: datetime | None = None,
    stale_multiplier: float = 2.0,
) -> list[dict[str, Any]]:
    """掃 SQLite sync_log_heartbeat,回「now - last_success_at > interval * multiplier」的 task。

    從未成功過(last_success_at IS NULL)直接視為 stale,給訊息特殊處理。

    Args:
      db_path: 走 config.DATABASE_PATH 預設
      now: 覆寫當前(test 用),預設 datetime.now(UTC)
      stale_multiplier: 容忍倍數,預設 2.0(24h task → 48h 未成功才警告)

    Returns: list of dicts,含 task_name / last_success_at / hours_since_success /
             expected_interval_hours / last_failure_at / last_failure_reason
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    from src import database as db
    try:
        with db.get_conn(db_path) as conn:
            rows = conn.execute(
                """
                SELECT task_name, last_success_at, last_failure_at,
                       last_failure_reason, expected_interval_hours
                  FROM sync_log_heartbeat
                """
            ).fetchall()
    except Exception:  # noqa: BLE001  -- 表不存在 → 視為「沒任何 task」
        return []

    stale: list[dict[str, Any]] = []
    for r in rows:
        interval = float(r["expected_interval_hours"] or 24.0)
        threshold_hours = interval * stale_multiplier
        last_success = _parse_iso(r["last_success_at"])

        if last_success is None:
            stale.append({
                "task_name": r["task_name"],
                "last_success_at": None,
                "hours_since_success": None,
                "expected_interval_hours": interval,
                "last_failure_at": r["last_failure_at"],
                "last_failure_reason": r["last_failure_reason"],
                "threshold_hours": threshold_hours,
            })
            continue

        delta = (now - last_success).total_seconds() / 3600.0
        if delta > threshold_hours:
            stale.append({
                "task_name": r["task_name"],
                "last_success_at": r["last_success_at"],
                "hours_since_success": delta,
                "expected_interval_hours": interval,
                "last_failure_at": r["last_failure_at"],
                "last_failure_reason": r["last_failure_reason"],
                "threshold_hours": threshold_hours,
            })

    stale.sort(key=lambda x: (x["hours_since_success"] is None, -(x["hours_since_success"] or 0)))
    return stale


def find_recent_failures(
    db_path: str | Path | None = None,
    now: datetime | None = None,
    window_hours: float = 24.0,
) -> list[dict[str, Any]]:
    """掃 SQLite,回「last_failure_at 在 window 內」的 task。

    給 alert script 顯示「最近失敗但已恢復」的 task — 主公看了知道哪裡 flaky。
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    from src import database as db
    try:
        with db.get_conn(db_path) as conn:
            rows = conn.execute(
                """
                SELECT task_name, last_success_at, last_failure_at,
                       last_failure_reason, expected_interval_hours
                  FROM sync_log_heartbeat
                 WHERE last_failure_at IS NOT NULL
                """
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []

    recent: list[dict[str, Any]] = []
    for r in rows:
        last_failure = _parse_iso(r["last_failure_at"])
        if last_failure is None:
            continue
        delta = (now - last_failure).total_seconds() / 3600.0
        if 0 <= delta <= window_hours:
            recent.append({
                "task_name": r["task_name"],
                "last_failure_at": r["last_failure_at"],
                "last_failure_reason": r["last_failure_reason"],
                "hours_since_failure": delta,
                "last_success_at": r["last_success_at"],
                "expected_interval_hours": float(r["expected_interval_hours"] or 24.0),
            })
    recent.sort(key=lambda x: x["hours_since_failure"])
    return recent


# 字串輸入 helper(workflow 用 bash 包 reason 時方便)
def dump_to_string(db_path: str | Path | None = None) -> str:
    """SQLite 內容 dump 成 CSV 字串(in-memory),不寫檔。給 UI download_button 用。"""
    from src import database as db
    try:
        with db.get_conn(db_path) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM sync_log_heartbeat"
            ).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    df = pd.DataFrame([dict(r) for r in rows], columns=_COLUMNS)
    buf = io.StringIO()
    df.sort_values("task_name").to_csv(buf, index=False)
    return buf.getvalue()


__all__ = [
    "SNAPSHOT_DIR",
    "HEARTBEAT_CSV",
    "record_success",
    "record_failure",
    "load_from_csv",
    "dump_to_csv",
    "dump_to_string",
    "find_stale_tasks",
    "find_recent_failures",
]
