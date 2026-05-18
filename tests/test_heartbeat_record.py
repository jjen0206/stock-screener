"""src/system_monitoring/heartbeat.py record_success / record_failure 寫入測試。

涵蓋:
  - record_success 寫入新 row(CSV 從空 → 一筆)
  - record_success 對既有 row idempotent upsert(同 task_name 不重複)
  - record_success 保留既有 failure 欄位(失敗紀錄不被成功清掉)
  - record_failure 寫入新 row 必須給 interval
  - record_failure 對既有 row 保留 last_success_at
  - record_failure 截斷過長 reason
  - now 參數覆寫(test 用)
  - CSV sort by task_name(stable diff)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.system_monitoring import heartbeat  # noqa: E402


@pytest.fixture
def tmp_csv(tmp_path: Path) -> Path:
    """tmp_path 下的 sync_log_heartbeat.csv,parent dir 已 mkdir。"""
    snap = tmp_path / "twse_snapshot"
    snap.mkdir(parents=True)
    return snap / "sync_log_heartbeat.csv"


def test_record_success_writes_new_row(tmp_csv: Path) -> None:
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    row = heartbeat.record_success(
        "morning_brief", expected_interval_hours=24, csv_path=tmp_csv, now=now,
    )

    assert row["task_name"] == "morning_brief"
    assert row["last_success_at"] == "2026-05-18T10:00:00Z"
    assert row["last_failure_at"] is None
    assert row["expected_interval_hours"] == 24.0

    df = pd.read_csv(tmp_csv)
    assert len(df) == 1
    assert df.iloc[0]["task_name"] == "morning_brief"
    assert df.iloc[0]["last_success_at"] == "2026-05-18T10:00:00Z"


def test_record_success_upserts_same_task(tmp_csv: Path) -> None:
    t1 = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 19, 10, 0, 0, tzinfo=timezone.utc)
    heartbeat.record_success("morning_brief", 24, csv_path=tmp_csv, now=t1)
    heartbeat.record_success("morning_brief", 24, csv_path=tmp_csv, now=t2)

    df = pd.read_csv(tmp_csv)
    assert len(df) == 1  # 同 task_name 不重複
    assert df.iloc[0]["last_success_at"] == "2026-05-19T10:00:00Z"


def test_record_success_preserves_existing_failure_columns(tmp_csv: Path) -> None:
    """先 fail 再 success → failure 欄位不該被清掉(主公看了知道最近 flaky)。"""
    t_fail = datetime(2026, 5, 18, 9, 0, 0, tzinfo=timezone.utc)
    t_success = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)

    heartbeat.record_failure(
        "morning_brief", "API 503",
        csv_path=tmp_csv, now=t_fail, expected_interval_hours=24,
    )
    heartbeat.record_success("morning_brief", 24, csv_path=tmp_csv, now=t_success)

    df = pd.read_csv(tmp_csv)
    assert len(df) == 1
    r = df.iloc[0]
    assert r["last_success_at"] == "2026-05-18T10:00:00Z"
    assert r["last_failure_at"] == "2026-05-18T09:00:00Z"
    assert r["last_failure_reason"] == "API 503"


def test_record_failure_requires_interval_on_first_appearance(tmp_csv: Path) -> None:
    with pytest.raises(ValueError, match="expected_interval_hours"):
        heartbeat.record_failure(
            "brand_new_task", "boom", csv_path=tmp_csv,
        )


def test_record_failure_preserves_last_success(tmp_csv: Path) -> None:
    t_s = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    t_f = datetime(2026, 5, 19, 10, 0, 0, tzinfo=timezone.utc)

    heartbeat.record_success("morning_brief", 24, csv_path=tmp_csv, now=t_s)
    heartbeat.record_failure(
        "morning_brief", "TWSE 503 transient", csv_path=tmp_csv, now=t_f,
    )

    # 用 heartbeat._read_csv 走 production path(force str dtype)— 純 pd.read_csv
    # 對「reason='503'」會把它推 int64,production load_from_csv 不會掛。
    df = heartbeat._read_csv(tmp_csv)
    r = df.iloc[0]
    assert r["last_success_at"] == "2026-05-18T10:00:00Z"  # 保留
    assert r["last_failure_at"] == "2026-05-19T10:00:00Z"
    assert r["last_failure_reason"] == "TWSE 503 transient"
    assert r["expected_interval_hours"] == 24.0


def test_record_failure_truncates_long_reason(tmp_csv: Path) -> None:
    long = "x" * 500
    heartbeat.record_failure(
        "morning_brief", long,
        csv_path=tmp_csv,
        expected_interval_hours=24,
    )
    df = pd.read_csv(tmp_csv)
    assert len(df.iloc[0]["last_failure_reason"]) == 200


def test_csv_sorted_by_task_name(tmp_csv: Path) -> None:
    """多 task 寫入後,CSV 應該 sort by task_name(git diff 穩定)。"""
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    heartbeat.record_success("z_task", 24, csv_path=tmp_csv, now=now)
    heartbeat.record_success("a_task", 24, csv_path=tmp_csv, now=now)
    heartbeat.record_success("m_task", 24, csv_path=tmp_csv, now=now)

    df = pd.read_csv(tmp_csv)
    assert list(df["task_name"]) == ["a_task", "m_task", "z_task"]


def test_record_success_handles_fractional_interval(tmp_csv: Path) -> None:
    """intraday_alerts 用 0.5h interval,必須能存。"""
    row = heartbeat.record_success("intraday_alerts", 0.5, csv_path=tmp_csv)
    assert row["expected_interval_hours"] == 0.5
    df = pd.read_csv(tmp_csv)
    assert float(df.iloc[0]["expected_interval_hours"]) == 0.5
