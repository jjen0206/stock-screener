"""sync_log_heartbeat e2e persistence test。

防 regression:
  1. CSV → SQLite 透過 preload_snapshots 完整載入(boot path)
  2. heartbeat 表 schema 存在 init_db
  3. preload 後 find_stale_tasks 能 query 到資料
  4. load_from_csv 對 same task_name idempotent(重複 boot 不出錯)

遵守 `feedback_e2e_test_isolation_for_persistence`:必須 monkeypatch
SNAPSHOT_DIR / HEARTBEAT_CSV / DB,避免污染專案真實 CSV。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.system_monitoring import heartbeat  # noqa: E402


@pytest.fixture
def isolated_snapshot(tmp_path: Path, monkeypatch):
    """tmp_path / twse_snapshot/ + patch SNAPSHOT_DIR + HEARTBEAT_CSV + DATABASE_PATH。

    Returns: (snapshot_dir, csv_path, db_file)
    """
    snap = tmp_path / "twse_snapshot"
    snap.mkdir(parents=True)
    csv_file = snap / "sync_log_heartbeat.csv"

    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()  # 建 sync_log_heartbeat 表

    monkeypatch.setattr(heartbeat, "SNAPSHOT_DIR", snap)
    monkeypatch.setattr(heartbeat, "HEARTBEAT_CSV", csv_file)

    yield snap, csv_file, db_file
    db._reset_path_cache()


def test_schema_includes_sync_log_heartbeat(isolated_snapshot) -> None:
    """init_db 必須建 sync_log_heartbeat 表(沒這個 preload 會炸)。"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='sync_log_heartbeat'"
        ).fetchall()
    assert len(rows) == 1


def test_record_then_load_e2e(isolated_snapshot) -> None:
    """record_success → CSV → preload_snapshots → SQLite → find_stale。

    這個 test 是 watchlist persistence invariants 的等效版:確認整條
    CSV-as-source-of-truth 路徑 wired up 沒漏。
    """
    snap, csv_file, _ = isolated_snapshot
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    heartbeat.record_success(
        "morning_brief", 24, csv_path=csv_file, now=now,
    )
    heartbeat.record_success(
        "intraday_alerts", 0.5, csv_path=csv_file, now=now,
    )

    # 模擬「cloud boot」:fresh SQLite + preload_snapshots
    db._reset_path_cache()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sync_log_heartbeat")  # 清掉再灌

    counts = db.preload_snapshots(snapshot_dir=snap)
    assert counts.get("sync_log_heartbeat") == 2

    # 再 query 確認
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT task_name, last_success_at, expected_interval_hours "
            "  FROM sync_log_heartbeat ORDER BY task_name"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["task_name"] == "intraday_alerts"
    assert float(rows[0]["expected_interval_hours"]) == 0.5
    assert rows[1]["task_name"] == "morning_brief"
    assert rows[1]["last_success_at"] == "2026-05-18T10:00:00Z"


def test_preload_idempotent(isolated_snapshot) -> None:
    """跑 preload 兩次,row 數一樣(同 task_name UPSERT 不重複)。"""
    snap, csv_file, _ = isolated_snapshot
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    heartbeat.record_success(
        "morning_brief", 24, csv_path=csv_file, now=now,
    )

    db.preload_snapshots(snapshot_dir=snap)
    db.preload_snapshots(snapshot_dir=snap)  # 再來一次

    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM sync_log_heartbeat"
        ).fetchone()["c"]
    assert n == 1


def test_load_from_csv_returns_zero_when_csv_missing(isolated_snapshot) -> None:
    """CSV 不存在 → load 回 0,不 raise(第一次部署的情境)。"""
    _, csv_file, _ = isolated_snapshot
    assert not csv_file.exists()
    n = heartbeat.load_from_csv(csv_path=csv_file)
    assert n == 0


def test_dump_to_csv_round_trip(isolated_snapshot) -> None:
    """dump_to_csv 把 SQLite 內容寫出,再 load 回來資料一致。"""
    snap, csv_file, _ = isolated_snapshot
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)

    # 直接寫 SQLite 一筆(模擬 Streamlit UI 編輯)
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sync_log_heartbeat (
                task_name, last_success_at, expected_interval_hours, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            ("ui_edited_task", "2026-05-18T10:00:00Z", 6.0,
             "2026-05-18T10:00:00Z"),
        )

    # dump → CSV
    n_dumped = heartbeat.dump_to_csv(csv_path=csv_file)
    assert n_dumped == 1
    assert csv_file.exists()

    # 清掉 SQLite → load → 資料還在
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sync_log_heartbeat")
    n_loaded = heartbeat.load_from_csv(csv_path=csv_file)
    assert n_loaded == 1

    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM sync_log_heartbeat WHERE task_name='ui_edited_task'"
        ).fetchone()
    assert r is not None
    assert float(r["expected_interval_hours"]) == 6.0


def test_numeric_looking_reason_kept_as_string_through_load(
    isolated_snapshot,
) -> None:
    """reason='503' 透過 CSV → load_from_csv → SQLite 必須仍是字串「503」。

    regression guard:pandas 預設 read_csv 會把 "503" 推 int64,if
    load_from_csv 沒 force string dtype,SQLite 就會存進 int → query 出來
    型別不對 → cron_health_alert format 訊息時可能崩。
    """
    _, csv_file, _ = isolated_snapshot
    heartbeat.record_failure(
        "fragile_task", "503", csv_path=csv_file,
        expected_interval_hours=24,
    )
    n = heartbeat.load_from_csv(csv_path=csv_file)
    assert n == 1

    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT last_failure_reason FROM sync_log_heartbeat "
            "WHERE task_name='fragile_task'"
        ).fetchone()
    assert r["last_failure_reason"] == "503"
    assert isinstance(r["last_failure_reason"], str)


def test_real_snapshot_csv_untouched(isolated_snapshot, tmp_path) -> None:
    """regression guard:e2e test 跑完真實 data/twse_snapshot/ 不被污染。

    memory rule: e2e test 必須 monkeypatch SNAPSHOT_DIR / HEARTBEAT_CSV 完整。
    這個 assertion 確認 monkeypatch 真有效 — 寫入的位置在 tmp_path 而非 PROJECT_ROOT。
    """
    snap, csv_file, _ = isolated_snapshot
    heartbeat.record_success("test_sentinel_task", 24, csv_path=csv_file)
    # csv_file 必須在 tmp_path 底下(否則 monkeypatch 沒生效)
    csv_file.resolve().relative_to(tmp_path.resolve())  # raises if outside

    # 確認專案真實 CSV 沒有被寫到 test_sentinel_task
    real_csv = config.PROJECT_ROOT / "data" / "twse_snapshot" / "sync_log_heartbeat.csv"
    if real_csv.exists():
        import pandas as pd
        df = pd.read_csv(real_csv)
        assert "test_sentinel_task" not in set(df.get("task_name", []))
