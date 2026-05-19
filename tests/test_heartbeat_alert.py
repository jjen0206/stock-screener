"""scripts/cron_health_alert.py + find_stale_tasks / find_recent_failures 測試。

涵蓋:
  - 全 fresh → silent(無 stale + 無 recent failure + 非 weekly_checkpoint)
  - stale task (last_success > interval * 2) → 推訊息
  - 從未成功的 task → 也算 stale
  - 24h 內 failure 但已成功 → 列在「最近失敗」section
  - weekly_checkpoint=True + 全綠 → 推「全綠」訊息
  - 訊息含 task_name + 時間 + 原因
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import cron_health_alert  # noqa: E402
from src import config, database as db  # noqa: E402
from src.system_monitoring import heartbeat  # noqa: E402


@pytest.fixture
def tmp_hb_db(tmp_path: Path, monkeypatch):
    """tmp DB + init schema + 重設 heartbeat path → tmp。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()

    snap = tmp_path / "twse_snapshot"
    snap.mkdir(parents=True)
    monkeypatch.setattr(heartbeat, "SNAPSHOT_DIR", snap)
    monkeypatch.setattr(heartbeat, "HEARTBEAT_CSV", snap / "sync_log_heartbeat.csv")

    yield snap
    db._reset_path_cache()


def _patch_pushers(monkeypatch, tg: list, dc: list) -> None:
    monkeypatch.setattr(
        cron_health_alert, "send_telegram_message",
        lambda text, **kw: (tg.append(text), True)[1],
    )
    monkeypatch.setattr(
        cron_health_alert, "send_discord_message",
        lambda content, **kw: (dc.append(content), True)[1],
    )


def _seed(task: str, last_success: str | None, interval: float,
          last_failure: str | None = None, reason: str | None = None) -> None:
    """直接寫 SQLite(避免 record_success 用 now() 抓真實時間)。"""
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sync_log_heartbeat (
                task_name, last_success_at, last_failure_at,
                last_failure_reason, expected_interval_hours, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task, last_success, last_failure, reason, interval,
             last_success or last_failure or "2026-05-18T00:00:00Z"),
        )


def test_no_stale_no_failure_silent(tmp_hb_db, monkeypatch) -> None:
    """全 fresh + 平日(非 weekly checkpoint)→ run 不推。"""
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    _seed("morning_brief", "2026-05-18T00:00:00Z", 24)  # 10h 前成功,fresh

    tg: list[str] = []
    dc: list[str] = []
    _patch_pushers(monkeypatch, tg, dc)

    r = cron_health_alert.run(now=now)
    assert r["stale_count"] == 0
    assert r["pushed"] is False
    assert tg == [] and dc == []


def test_stale_task_pushes_alert(tmp_hb_db, monkeypatch) -> None:
    """morning_brief 上次成功 72h 前,interval=24h → 72 > 48 → stale。"""
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    _seed("morning_brief", "2026-05-15T10:00:00Z", 24)  # 72h 前

    tg: list[str] = []
    dc: list[str] = []
    _patch_pushers(monkeypatch, tg, dc)

    r = cron_health_alert.run(now=now)
    assert r["stale_count"] == 1
    assert r["stale"][0]["task_name"] == "morning_brief"
    # 72h since success
    assert 71.9 < r["stale"][0]["hours_since_success"] < 72.1
    assert r["pushed"] is True
    assert len(tg) == 1 and len(dc) == 1
    assert "morning_brief" in tg[0]
    assert "⚠️" in tg[0]


def test_never_succeeded_task_counts_as_stale(tmp_hb_db, monkeypatch) -> None:
    """從未成功(last_success_at IS NULL)+ 有 failure 紀錄 → stale。"""
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    _seed(
        "brand_new_task", None, 24,
        last_failure="2026-05-18T09:00:00Z", reason="init fail",
    )

    tg: list[str] = []
    dc: list[str] = []
    _patch_pushers(monkeypatch, tg, dc)

    r = cron_health_alert.run(now=now)
    assert r["stale_count"] == 1
    assert r["stale"][0]["task_name"] == "brand_new_task"
    assert r["stale"][0]["hours_since_success"] is None
    assert "從未成功" in tg[0]


def test_recent_failure_but_recovered_listed_separately(
    tmp_hb_db, monkeypatch,
) -> None:
    """task 1h 前 fail 但 30 min 前 success → 不算 stale,但列入「最近 failure」。"""
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    _seed(
        "news_notify", "2026-05-18T09:30:00Z", 1,  # 0.5h 前成功 → fresh
        last_failure="2026-05-18T09:00:00Z",
        reason="TWSE 503 transient",
    )

    tg: list[str] = []
    dc: list[str] = []
    _patch_pushers(monkeypatch, tg, dc)

    r = cron_health_alert.run(now=now)
    assert r["stale_count"] == 0
    assert len(r["recent_failures"]) == 1
    assert r["recent_failures"][0]["task_name"] == "news_notify"
    assert r["pushed"] is True
    assert "news_notify" in tg[0]
    assert "TWSE 503" in tg[0]
    assert "🟡" in tg[0]


def test_weekly_checkpoint_pushes_even_when_green(tmp_hb_db, monkeypatch) -> None:
    """weekly_checkpoint=True 即使全綠也推。"""
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    _seed("morning_brief", "2026-05-18T00:00:00Z", 24)

    tg: list[str] = []
    dc: list[str] = []
    _patch_pushers(monkeypatch, tg, dc)

    r = cron_health_alert.run(now=now, weekly_checkpoint=True)
    assert r["stale_count"] == 0
    assert r["pushed"] is True
    assert "✅" in tg[0]
    assert "週報" in tg[0]


def test_message_format_lists_multiple_stale(tmp_hb_db, monkeypatch) -> None:
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    _seed("morning_brief", "2026-05-15T10:00:00Z", 24)  # 72h stale
    _seed("backfill_revenue", "2026-04-25T10:00:00Z", 168)  # ~552h stale

    tg: list[str] = []
    dc: list[str] = []
    _patch_pushers(monkeypatch, tg, dc)

    r = cron_health_alert.run(now=now)
    assert r["stale_count"] == 2
    msg = tg[0]
    assert "morning_brief" in msg
    assert "backfill_revenue" in msg
    # 排序:hours_since_success 大的在前
    assert msg.index("backfill_revenue") < msg.index("morning_brief")


def test_dry_run_does_not_push(tmp_hb_db, monkeypatch) -> None:
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    _seed("morning_brief", "2026-05-15T10:00:00Z", 24)

    tg: list[str] = []
    dc: list[str] = []
    _patch_pushers(monkeypatch, tg, dc)

    r = cron_health_alert.run(now=now, dry_run=True)
    assert r["stale_count"] == 1
    assert r["pushed"] is False
    assert tg == [] and dc == []


def test_find_stale_respects_multiplier(tmp_hb_db) -> None:
    """multiplier=1.0 → 24h task 過 24h 就 stale;multiplier=3.0 → 過 72h 才 stale。"""
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    _seed("morning_brief", "2026-05-16T10:00:00Z", 24)  # 48h 前

    s1 = heartbeat.find_stale_tasks(now=now, stale_multiplier=1.0)
    s3 = heartbeat.find_stale_tasks(now=now, stale_multiplier=3.0)
    s2 = heartbeat.find_stale_tasks(now=now, stale_multiplier=2.0)

    assert len(s1) == 1  # 48 > 24
    assert len(s2) == 0  # 48 <= 48
    assert len(s3) == 0  # 48 < 72
