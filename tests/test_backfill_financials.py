"""scripts/backfill_financials.py + scripts/aggregate_financials_shards.py 單元測試。

scripts/ 不是 package,用 importlib 載入。tmp DB + tmp snapshot dir。
跟 test_backfill_dividend.py 同模式,只差 fetcher / 表 / key_cols 不同。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db


_BACKFILL_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "backfill_financials.py"
)
_b_spec = importlib.util.spec_from_file_location(
    "backfill_financials", _BACKFILL_SCRIPT,
)
backfill = importlib.util.module_from_spec(_b_spec)
_b_spec.loader.exec_module(backfill)

_AGG_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "aggregate_financials_shards.py"
)
_a_spec = importlib.util.spec_from_file_location(
    "aggregate_financials_shards", _AGG_SCRIPT,
)
agg = importlib.util.module_from_spec(_a_spec)
_a_spec.loader.exec_module(agg)


@pytest.fixture
def tmp_setup(monkeypatch, tmp_path):
    db_file = tmp_path / "fin.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    snapshot_dir = tmp_path / "twse_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backfill, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(agg, "SNAPSHOT_DIR", snapshot_dir)
    db._reset_path_cache()
    db.init_db()
    yield snapshot_dir
    db._reset_path_cache()


# === backfill_financials ===

def test_shard_filter_distributes_evenly(tmp_setup):
    universe = [f"sid{i:04d}" for i in range(100)]
    shards = [
        backfill._shard_filter(universe, k, 8)
        for k in range(8)
    ]
    sizes = [len(s) for s in shards]
    assert max(sizes) - min(sizes) <= 1
    union = set()
    for s in shards:
        union.update(s)
    assert union == set(universe)


def test_backfill_dump_shard_csv_contains_only_shard_data(
    tmp_setup, monkeypatch,
):
    """dump 的 shard csv 只含此 shard 內 sids,且只 quarterly 不抓 monthly_revenue。"""
    snapshot_dir = tmp_setup
    db.upsert_financials([
        {"stock_id": "2330", "period_type": "quarterly", "period": "2024-Q4",
         "revenue": 1.0e8, "revenue_yoy": 30.0, "eps": 5.0, "roe": 25.0},
        {"stock_id": "2454", "period_type": "quarterly", "period": "2024-Q4",
         "revenue": 5.0e7, "revenue_yoy": 15.0, "eps": 8.0, "roe": 20.0},
        # 同 sid 不同 period_type 的污染列(不該入 shard csv)
        {"stock_id": "2330", "period_type": "monthly_revenue", "period": "2024-12",
         "revenue": 3.0e7, "revenue_yoy": 25.0, "eps": None, "roe": None},
    ])
    backfill._dump_shard_csv(0, ["2330"])
    out = snapshot_dir / "financials_shard_0.csv"
    assert out.exists()
    df = pd.read_csv(out, dtype={"stock_id": str})
    assert df["stock_id"].tolist() == ["2330"]
    assert df.iloc[0]["period"] == "2024-Q4"
    # monthly_revenue 那筆不該洩漏
    assert "monthly_revenue" not in df.columns
    assert len(df) == 1


def test_backfill_calls_fetch_quarterly_for_shard_sids(tmp_setup, monkeypatch):
    fake_universe = [f"sid{i}" for i in range(8)]
    monkeypatch.setattr(
        backfill, "pure_stock_universe", lambda min_history=20: fake_universe,
    )
    fetch_calls: list[str] = []
    monkeypatch.setattr(
        backfill, "fetch_quarterly_financials",
        lambda sid, s, e: fetch_calls.append(sid),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_financials.py", "--shard", "0", "--total-shards", "8"],
    )
    code = backfill.main()
    assert code == 0
    assert fetch_calls == ["sid0"]


# === aggregate ===

def _make_shard_csv(snapshot_dir: Path, shard: int, rows: list[dict]) -> None:
    df = pd.DataFrame(rows, columns=[
        "stock_id", "period", "revenue", "revenue_yoy", "eps", "roe",
    ])
    df.to_csv(snapshot_dir / f"financials_shard_{shard}.csv", index=False)


def test_aggregate_merges_shards_dedup_by_stock_period(tmp_setup, monkeypatch):
    """8 shard CSV 合併,(stock_id, period) 同 key keep last。"""
    snapshot_dir = tmp_setup
    _make_shard_csv(snapshot_dir, 0, [
        {"stock_id": "2330", "period": "2024-Q4",
         "revenue": 1e8, "revenue_yoy": 30.0, "eps": 5.0, "roe": 25.0},
    ])
    _make_shard_csv(snapshot_dir, 1, [
        {"stock_id": "2454", "period": "2024-Q4",
         "revenue": 5e7, "revenue_yoy": 15.0, "eps": 8.0, "roe": 20.0},
    ])
    for k in range(2, 8):
        _make_shard_csv(snapshot_dir, k, [])

    monkeypatch.setattr(
        "sys.argv",
        ["aggregate_financials_shards.py", "--total-shards", "8", "--no-cleanup"],
    )
    assert agg.main() == 0

    out = snapshot_dir / "financials_quarterly.csv"
    assert out.exists()
    df = pd.read_csv(out, dtype={"stock_id": str})
    assert sorted(df["stock_id"].tolist()) == ["2330", "2454"]


def test_aggregate_cleanup_removes_shard_files(tmp_setup, monkeypatch):
    snapshot_dir = tmp_setup
    _make_shard_csv(snapshot_dir, 0, [
        {"stock_id": "2330", "period": "2024-Q4",
         "revenue": 1e8, "revenue_yoy": 30.0, "eps": 5.0, "roe": 25.0},
    ])
    for k in range(1, 8):
        _make_shard_csv(snapshot_dir, k, [])
    (snapshot_dir / "last_financials_shard_0.txt").write_text("ok=1\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        ["aggregate_financials_shards.py", "--total-shards", "8"],
    )
    agg.main()

    for k in range(8):
        assert not (snapshot_dir / f"financials_shard_{k}.csv").exists()
    assert not (snapshot_dir / "last_financials_shard_0.txt").exists()
    assert (snapshot_dir / "financials_quarterly.csv").exists()
