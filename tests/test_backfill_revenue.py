"""scripts/backfill_revenue.py + scripts/aggregate_revenue_shards.py 單元測試。

scripts/ 不是 package,用 importlib 載入。tmp DB + tmp snapshot dir。
跟 test_backfill_dividend.py / test_backfill_financials.py 同模式,
差在 fetcher / period_type='monthly_revenue' / 不抓 eps roe。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db


_BACKFILL_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "backfill_revenue.py"
)
_b_spec = importlib.util.spec_from_file_location(
    "backfill_revenue", _BACKFILL_SCRIPT,
)
backfill = importlib.util.module_from_spec(_b_spec)
_b_spec.loader.exec_module(backfill)

_AGG_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "aggregate_revenue_shards.py"
)
_a_spec = importlib.util.spec_from_file_location(
    "aggregate_revenue_shards", _AGG_SCRIPT,
)
agg = importlib.util.module_from_spec(_a_spec)
_a_spec.loader.exec_module(agg)


@pytest.fixture
def tmp_setup(monkeypatch, tmp_path):
    db_file = tmp_path / "rev.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    snapshot_dir = tmp_path / "twse_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backfill, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(agg, "SNAPSHOT_DIR", snapshot_dir)
    db._reset_path_cache()
    db.init_db()
    yield snapshot_dir
    db._reset_path_cache()


# === backfill_revenue ===

def test_backfill_dump_shard_csv_monthly_revenue_only(tmp_setup, monkeypatch):
    """dump 的 shard csv 只含 period_type='monthly_revenue',quarterly 不洩漏。"""
    snapshot_dir = tmp_setup
    db.upsert_financials([
        {"stock_id": "2330", "period_type": "monthly_revenue",
         "period": "2024-12", "revenue": 1e8, "revenue_yoy": 30.0,
         "eps": None, "roe": None},
        {"stock_id": "2330", "period_type": "monthly_revenue",
         "period": "2024-11", "revenue": 9e7, "revenue_yoy": 25.0,
         "eps": None, "roe": None},
        # quarterly 那筆不該 leak
        {"stock_id": "2330", "period_type": "quarterly",
         "period": "2024-Q4", "revenue": 3e8, "revenue_yoy": 20.0,
         "eps": 5.0, "roe": 25.0},
    ])
    backfill._dump_shard_csv(0, ["2330"])
    out = snapshot_dir / "revenue_shard_0.csv"
    assert out.exists()
    df = pd.read_csv(out, dtype={"stock_id": str})
    assert len(df) == 2  # 兩個月,quarterly 那筆不 leak
    assert sorted(df["period"].tolist()) == ["2024-11", "2024-12"]
    # revenue.shard CSV 沒 eps/roe 欄(只 4 欄)
    assert set(df.columns) == {"stock_id", "period", "revenue", "revenue_yoy"}


def test_backfill_calls_fetch_revenue_for_shard_sids(tmp_setup, monkeypatch):
    fake_universe = [f"sid{i}" for i in range(8)]
    monkeypatch.setattr(
        backfill, "pure_stock_universe", lambda min_history=20: fake_universe,
    )
    fetch_calls: list[str] = []
    monkeypatch.setattr(
        backfill, "fetch_monthly_revenue",
        lambda sid, s, e: fetch_calls.append(sid),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_revenue.py", "--shard", "0", "--total-shards", "8"],
    )
    code = backfill.main()
    assert code == 0
    assert fetch_calls == ["sid0"]


# === P0-3 regression: quota fail-fast + delta_rows instrumentation ===

def test_backfill_revenue_quota_error_fail_fast(tmp_setup, monkeypatch):
    """連續 5 次 FinMindQuotaError → 提前 break,避免 60 min timeout 撞牆。

    5/11 cron run cancelled root-cause:單 shard 卡 quota long-backoff
    × 多檔 → 超過 60 min timeout → GH 全 cancel。fail-fast 後 shard
    早點結束,aggregate job 仍能跑完。"""
    fake_universe = [f"sid{i:03d}" for i in range(100)]
    monkeypatch.setattr(
        backfill, "pure_stock_universe", lambda min_history=20: fake_universe,
    )
    from src.data_fetcher import FinMindQuotaError

    call_count = {"n": 0}

    def _quota_fetch(sid, s, e):
        call_count["n"] += 1
        raise FinMindQuotaError("402 quota 爆")

    monkeypatch.setattr(backfill, "fetch_monthly_revenue", _quota_fetch)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_revenue.py", "--shard", "0", "--total-shards", "1"],
    )
    backfill.main()
    # 連續 5 quota_fail → break,call_count 遠少於 100
    assert call_count["n"] <= 10, (
        f"quota fail-fast 沒生效,call_count={call_count['n']}"
    )


def test_backfill_revenue_records_quota_and_delta_in_shard_txt(
    tmp_setup, monkeypatch,
):
    """last_revenue_shard_K.txt 必含 quota_fail / delta_rows 給 aggregator + 主公看。"""
    snapshot_dir = tmp_setup
    monkeypatch.setattr(
        backfill, "pure_stock_universe", lambda min_history=20: ["sid0"],
    )

    def _stub(sid, s, e):
        db.upsert_financials([{
            "stock_id": sid, "period_type": "monthly_revenue",
            "period": "2026-04", "revenue": 1e7, "revenue_yoy": 10.0,
            "eps": None, "roe": None,
        }])

    monkeypatch.setattr(backfill, "fetch_monthly_revenue", _stub)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_revenue.py", "--shard", "0", "--total-shards", "1"],
    )
    backfill.main()

    txt = (snapshot_dir / "last_revenue_shard_0.txt").read_text(encoding="utf-8")
    assert "delta_rows=1" in txt
    assert "quota_fail=0" in txt
    assert "ok=1" in txt


# === aggregate ===

def _make_shard_csv(snapshot_dir: Path, shard: int, rows: list[dict]) -> None:
    df = pd.DataFrame(rows, columns=[
        "stock_id", "period", "revenue", "revenue_yoy",
    ])
    df.to_csv(snapshot_dir / f"revenue_shard_{shard}.csv", index=False)


def test_aggregate_merges_shards_dedup_by_stock_period(tmp_setup, monkeypatch):
    snapshot_dir = tmp_setup
    _make_shard_csv(snapshot_dir, 0, [
        {"stock_id": "2330", "period": "2024-12",
         "revenue": 1e8, "revenue_yoy": 30.0},
    ])
    _make_shard_csv(snapshot_dir, 1, [
        {"stock_id": "2454", "period": "2024-12",
         "revenue": 5e7, "revenue_yoy": 15.0},
    ])
    for k in range(2, 8):
        _make_shard_csv(snapshot_dir, k, [])

    monkeypatch.setattr(
        "sys.argv",
        ["aggregate_revenue_shards.py", "--total-shards", "8", "--no-cleanup"],
    )
    assert agg.main() == 0

    out = snapshot_dir / "monthly_revenue.csv"
    assert out.exists()
    df = pd.read_csv(out, dtype={"stock_id": str})
    assert sorted(df["stock_id"].tolist()) == ["2330", "2454"]


# === preload_snapshots monthly_revenue.csv ===

def test_preload_snapshots_loads_monthly_revenue_csv(tmp_setup, monkeypatch):
    """preload_snapshots 該讀 monthly_revenue.csv 寫進 SQLite financials 表。"""
    snapshot_dir = tmp_setup
    df = pd.DataFrame([
        {"stock_id": "2330", "period": "2024-12",
         "revenue": 1.5e8, "revenue_yoy": 50.0},
        {"stock_id": "2330", "period": "2024-11",
         "revenue": 1.0e8, "revenue_yoy": 35.0},
        {"stock_id": "2454", "period": "2024-12",
         "revenue": 5.0e7, "revenue_yoy": 25.0},
    ])
    df.to_csv(snapshot_dir / "monthly_revenue.csv", index=False)

    counts = db.preload_snapshots(snapshot_dir=snapshot_dir)
    assert counts.get("monthly_revenue") == 3

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id, period, revenue_yoy FROM financials "
            "WHERE period_type='monthly_revenue' "
            "ORDER BY stock_id, period DESC"
        ).fetchall()
    assert len(rows) == 3
    assert rows[0]["stock_id"] == "2330"
    assert rows[0]["period"] == "2024-12"
    assert rows[0]["revenue_yoy"] == 50.0
