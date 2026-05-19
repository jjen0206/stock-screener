"""新增警示 diff 推播(2026-05-19 方案 B 精英化)單元測試。

涵蓋:
- compute_new_warnings:set diff 純函式
- run() 整合:今日新增推、昨日已有不推、dedup 防重推
"""
from __future__ import annotations

import sys
import importlib
from datetime import date, timedelta
from pathlib import Path

import pytest


# scripts/ 不在 default import path,run 中也會自己 sys.path.insert
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    from src import config, database as db
    db_file = tmp_path / "test_warnings.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    yield db
    # auto cleanup via tmp_path


def _insert_warning(conn, sid: str, wt: str, announced: str, effective_to=None):
    conn.execute(
        "INSERT INTO stock_warnings (stock_id, warning_type, announced_date, "
        "effective_from, effective_to, reason, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, wt, announced, announced, effective_to, "test", "2026-05-19T08:00:00"),
    )


def test_compute_new_warnings_basic():
    import notify_new_warnings as nnw  # type: ignore
    today = {("2330", "disposition"), ("2317", "attention")}
    yesterday = {("2330", "disposition")}
    diff = nnw.compute_new_warnings(today, yesterday)
    assert diff == [("2317", "attention")]


def test_compute_new_warnings_empty_diff():
    import notify_new_warnings as nnw  # type: ignore
    today = {("2330", "disposition")}
    yesterday = {("2330", "disposition")}
    assert nnw.compute_new_warnings(today, yesterday) == []


def test_compute_new_warnings_handles_empty_yesterday():
    """昨日無 warnings(系統初次跑)→ 全部當新增。"""
    import notify_new_warnings as nnw  # type: ignore
    today = {("2330", "disposition"), ("2317", "attention")}
    diff = nnw.compute_new_warnings(today, set())
    assert sorted(diff) == [("2317", "attention"), ("2330", "disposition")]


def test_active_warning_set_filters_by_date(isolated_db):
    """昨日已過期 (effective_to < as_of) 不算 active。"""
    import notify_new_warnings as nnw  # type: ignore
    db = isolated_db
    with db.get_conn() as conn:
        # 已過期:effective_to=2026-05-18,as_of=2026-05-19 後不算 active
        _insert_warning(conn, "1111", "disposition", "2026-05-10", "2026-05-18")
        # 仍 active(effective_to=NULL)
        _insert_warning(conn, "2222", "attention", "2026-05-18", None)
        # 仍 active(effective_to >= as_of)
        _insert_warning(conn, "3333", "disposition", "2026-05-15", "2026-05-25")
        conn.commit()
        active_today = nnw._active_warning_set(conn, "2026-05-19")
    assert ("2222", "attention") in active_today
    assert ("3333", "disposition") in active_today
    assert ("1111", "disposition") not in active_today


def test_run_silent_when_no_new_warnings(isolated_db, monkeypatch):
    """昨日 vs 今日 active set 一樣 → silent(不推)。"""
    import notify_new_warnings as nnw  # type: ignore
    db = isolated_db
    with db.get_conn() as conn:
        # 一條長期 active warning,昨日就有,今天也在
        _insert_warning(conn, "5555", "attention", "2026-05-10", None)
        conn.commit()

    # mock taipei time
    monkeypatch.setattr(nnw, "_today_taipei_iso", lambda: "2026-05-19")
    monkeypatch.setattr(nnw, "_yesterday_taipei_iso", lambda: "2026-05-18")

    result = nnw.run(dry_run=True, send_telegram=False, send_discord=False)
    assert result["n_new"] == 0
    assert result["n_pushed"] == 0
    assert result["tg_msg"] == ""


def test_run_pushes_only_new_warnings(isolated_db, monkeypatch):
    """今日新增 1 條 → 推 1 條;昨日已有的不推。"""
    import notify_new_warnings as nnw  # type: ignore
    db = isolated_db
    with db.get_conn() as conn:
        # 昨日已有(announced 在昨天前)
        _insert_warning(conn, "5555", "attention", "2026-05-10", None)
        # 今日新增(announced=今日)
        _insert_warning(conn, "8888", "disposition", "2026-05-19", None)
        conn.commit()

    monkeypatch.setattr(nnw, "_today_taipei_iso", lambda: "2026-05-19")
    monkeypatch.setattr(nnw, "_yesterday_taipei_iso", lambda: "2026-05-18")

    result = nnw.run(dry_run=True, send_telegram=False, send_discord=False)
    assert result["n_new"] == 1
    assert "8888" in result["tg_msg"]
    assert "5555" not in result["tg_msg"]
    assert "處置股" in result["tg_msg"] or "disposition" in result["tg_msg"]


def test_run_dedup_prevents_same_day_repush(isolated_db, monkeypatch):
    """已寫入 alert_dedup → 同日重跑不再推。"""
    import notify_new_warnings as nnw  # type: ignore
    db = isolated_db
    with db.get_conn() as conn:
        _insert_warning(conn, "9999", "disposition", "2026-05-19", None)
        # 預先寫 dedup row
        conn.execute(
            "INSERT INTO alert_dedup (sid, alert_type, alert_date, sent_at, "
            "ref_price, threshold) VALUES (?, ?, ?, ?, ?, ?)",
            ("9999", "new_warning:disposition", "2026-05-19",
             "2026-05-19T08:00:00", 0.0, 0.0),
        )
        conn.commit()

    monkeypatch.setattr(nnw, "_today_taipei_iso", lambda: "2026-05-19")
    monkeypatch.setattr(nnw, "_yesterday_taipei_iso", lambda: "2026-05-18")

    result = nnw.run(dry_run=True, send_telegram=False, send_discord=False)
    # 雖 n_new=1 但 dedup 過後 filtered 空 → 不推
    assert result["n_new"] == 1
    assert result["n_dedup_skip"] == 1
    assert result["n_pushed"] == 0
    assert result["tg_msg"] == ""


def test_run_kill_switch_disabled(isolated_db, monkeypatch):
    import notify_new_warnings as nnw  # type: ignore
    monkeypatch.setenv("NEW_WARNINGS_NOTIFY_ENABLED", "false")
    result = nnw.run(dry_run=True, send_telegram=False, send_discord=False)
    assert result["n_new"] == 0
    assert result["n_pushed"] == 0


def test_format_message_truncates_when_many():
    """超過 15 檔 → 截斷顯前 15 + 「其他 N 檔」尾。"""
    import notify_new_warnings as nnw  # type: ignore
    pairs = [(f"{i:04d}", "disposition", f"股{i}") for i in range(20)]
    msg = nnw.format_new_warnings_message(pairs, "2026-05-19", channel="telegram")
    assert "其他 5 檔" in msg


def test_format_message_empty_returns_empty():
    import notify_new_warnings as nnw  # type: ignore
    assert nnw.format_new_warnings_message([], "2026-05-19") == ""
