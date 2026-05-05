"""scripts/backfill_dividend.py + scripts/aggregate_dividend_shards.py 單元測試。

scripts/ 不是 package,用 importlib 載入。mock fetch_dividend / pure_stock_universe,
用 tmp DB + tmp snapshot dir。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db


_BACKFILL_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "backfill_dividend.py"
)
_b_spec = importlib.util.spec_from_file_location(
    "backfill_dividend", _BACKFILL_SCRIPT,
)
backfill = importlib.util.module_from_spec(_b_spec)
_b_spec.loader.exec_module(backfill)

_AGG_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "aggregate_dividend_shards.py"
)
_a_spec = importlib.util.spec_from_file_location(
    "aggregate_dividend_shards", _AGG_SCRIPT,
)
agg = importlib.util.module_from_spec(_a_spec)
_a_spec.loader.exec_module(agg)


@pytest.fixture
def tmp_setup(monkeypatch, tmp_path):
    """tmp DB + tmp snapshot dir。"""
    db_file = tmp_path / "div.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    snapshot_dir = tmp_path / "twse_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backfill, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(agg, "SNAPSHOT_DIR", snapshot_dir)
    db._reset_path_cache()
    db.init_db()
    yield snapshot_dir
    db._reset_path_cache()


# === backfill_dividend ===

def test_shard_filter_distributes_evenly(tmp_setup):
    """sorted(universe)[shard::total_shards] 切片均勻 + 不重疊。"""
    universe = [f"sid{i:04d}" for i in range(100)]
    shards = [
        backfill._shard_filter(universe, k, 8)
        for k in range(8)
    ]
    # 各 shard 大小 12-13 之間(100 / 8)
    sizes = [len(s) for s in shards]
    assert max(sizes) - min(sizes) <= 1
    # 全 shard 合起來 = universe(沒漏)
    union = set()
    for s in shards:
        union.update(s)
    assert union == set(universe)


def test_backfill_calls_fetch_dividend_for_shard_sids(tmp_setup, monkeypatch):
    """shard 內每檔該 fetch_dividend 一次。"""
    fake_universe = [f"sid{i}" for i in range(8)]  # 8 檔均分 8 shard
    monkeypatch.setattr(
        backfill, "pure_stock_universe", lambda min_history=20: fake_universe,
    )
    fetch_calls = []
    monkeypatch.setattr(
        backfill, "fetch_dividend",
        lambda sid, s, e: fetch_calls.append(sid),
    )
    # shard 0 → 拿到 sid0(sorted index 0)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_dividend.py", "--shard", "0", "--total-shards", "8"],
    )
    code = backfill.main()
    assert code == 0
    # shard 0 = sorted(universe)[0::8] = [sid0]
    assert fetch_calls == ["sid0"]


def test_backfill_dump_shard_csv_contains_only_shard_data(
    tmp_setup, monkeypatch,
):
    """dump 的 shard csv 只該含此 shard 內 sids,不該洩漏其他 shard 資料。"""
    snapshot_dir = tmp_setup
    # 灌兩檔 dividend(2330 + 2454)
    db.upsert_dividend([
        {"stock_id": "2330", "year": 2025,
         "cash_dividend": 10.0, "stock_dividend": 0, "ex_dividend_date": None},
        {"stock_id": "2454", "year": 2025,
         "cash_dividend": 5.0, "stock_dividend": 0, "ex_dividend_date": None},
    ])
    monkeypatch.setattr(
        backfill, "pure_stock_universe",
        lambda min_history=20: ["2330", "2454"],
    )
    monkeypatch.setattr(
        backfill, "fetch_dividend", lambda sid, s, e: None,
    )
    # shard 0/2 → 只跑 2330(sorted [2330,2454][0::2] = [2330])
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_dividend.py", "--shard", "0", "--total-shards", "2"],
    )
    backfill.main()

    shard0 = snapshot_dir / "dividend_shard_0.csv"
    assert shard0.exists()
    df = pd.read_csv(shard0, dtype={"stock_id": str})
    sids_in_shard = sorted(set(df["stock_id"]))
    assert sids_in_shard == ["2330"], f"shard 0 不該含 2454,實際: {sids_in_shard}"


def test_backfill_handles_fetch_error(tmp_setup, monkeypatch):
    """fetch_dividend raise → 算 fail,不阻斷其他 sids。"""
    fake_universe = ["sid0", "sid1"]
    monkeypatch.setattr(
        backfill, "pure_stock_universe", lambda min_history=20: fake_universe,
    )
    from src.data_fetcher import FinMindAPIError

    def _flaky_fetch(sid, s, e):
        if sid == "sid0":
            raise FinMindAPIError("FinMind 429")
    monkeypatch.setattr(backfill, "fetch_dividend", _flaky_fetch)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_dividend.py", "--shard", "0", "--total-shards", "1"],
    )
    code = backfill.main()
    # 1/2 = 50% > 10%,return 0(部分失敗仍 commit)
    assert code == 0


def test_backfill_shard_param_validation(tmp_setup, monkeypatch):
    """shard 參數錯誤(超出範圍)→ exit 2。"""
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_dividend.py", "--shard", "10", "--total-shards", "8"],
    )
    assert backfill.main() == 2


# === aggregate_dividend_shards ===

def _make_shard_csv(snapshot_dir: Path, shard: int, rows: list[dict]) -> None:
    """寫一個 shard csv 給 aggregator 讀。"""
    df = pd.DataFrame(rows or [], columns=[
        "stock_id", "year", "cash_dividend",
        "stock_dividend", "ex_dividend_date",
    ])
    df.to_csv(snapshot_dir / f"dividend_shard_{shard}.csv", index=False)


def test_aggregate_merges_shards_dedup_by_stock_year(tmp_setup, monkeypatch):
    """8 shard CSV 合併,(stock_id, year) 同 key keep last。"""
    snapshot_dir = tmp_setup
    # shard 0:2330 / 2025 cash=10
    _make_shard_csv(snapshot_dir, 0, [
        {"stock_id": "2330", "year": 2025,
         "cash_dividend": 10.0, "stock_dividend": 0, "ex_dividend_date": "2025-07-15"},
    ])
    # shard 1:2454 / 2025 cash=5
    _make_shard_csv(snapshot_dir, 1, [
        {"stock_id": "2454", "year": 2025,
         "cash_dividend": 5.0, "stock_dividend": 0, "ex_dividend_date": "2025-08-01"},
    ])
    # shard 2-7 空
    for k in range(2, 8):
        _make_shard_csv(snapshot_dir, k, [])

    monkeypatch.setattr(
        "sys.argv",
        ["aggregate_dividend_shards.py", "--total-shards", "8", "--no-cleanup"],
    )
    code = agg.main()
    assert code == 0

    out = snapshot_dir / "dividend.csv"
    assert out.exists()
    df = pd.read_csv(out, dtype={"stock_id": str})
    assert sorted(df["stock_id"].tolist()) == ["2330", "2454"]
    assert df.set_index("stock_id").loc["2330", "cash_dividend"] == 10.0


def test_aggregate_keeps_last_when_duplicate_key(tmp_setup, monkeypatch):
    """同 (stock_id, year) 在不同 shard,keep last(最新 shard 蓋過早的)。"""
    snapshot_dir = tmp_setup
    # shard 0:2330 / 2025 cash=10(舊)
    _make_shard_csv(snapshot_dir, 0, [
        {"stock_id": "2330", "year": 2025,
         "cash_dividend": 10.0, "stock_dividend": 0, "ex_dividend_date": None},
    ])
    # shard 1:2330 / 2025 cash=12(新,該蓋過)
    _make_shard_csv(snapshot_dir, 1, [
        {"stock_id": "2330", "year": 2025,
         "cash_dividend": 12.0, "stock_dividend": 0, "ex_dividend_date": None},
    ])
    for k in range(2, 8):
        _make_shard_csv(snapshot_dir, k, [])

    monkeypatch.setattr(
        "sys.argv",
        ["aggregate_dividend_shards.py", "--total-shards", "8", "--no-cleanup"],
    )
    agg.main()

    df = pd.read_csv(snapshot_dir / "dividend.csv", dtype={"stock_id": str})
    assert len(df) == 1
    assert df.iloc[0]["cash_dividend"] == 12.0  # last 蓋過 first


def test_aggregate_returns_1_when_no_shard_csv(tmp_setup, monkeypatch):
    """8 個 shard CSV 一個都沒 → exit 1。"""
    monkeypatch.setattr(
        "sys.argv",
        ["aggregate_dividend_shards.py", "--total-shards", "8"],
    )
    assert agg.main() == 1


def test_aggregate_cleanup_removes_shard_files(tmp_setup, monkeypatch):
    """default cleanup → shard csv / last_dividend_shard txt 都該被刪。"""
    snapshot_dir = tmp_setup
    _make_shard_csv(snapshot_dir, 0, [
        {"stock_id": "2330", "year": 2025,
         "cash_dividend": 10, "stock_dividend": 0, "ex_dividend_date": None},
    ])
    for k in range(1, 8):
        _make_shard_csv(snapshot_dir, k, [])
    # 多寫一個 last_dividend_shard 讓 cleanup 也驗 txt 刪除
    (snapshot_dir / "last_dividend_shard_0.txt").write_text("ok=1\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        ["aggregate_dividend_shards.py", "--total-shards", "8"],
    )
    agg.main()

    # shard csv / txt 應全被刪
    for k in range(8):
        assert not (snapshot_dir / f"dividend_shard_{k}.csv").exists()
    assert not (snapshot_dir / "last_dividend_shard_0.txt").exists()
    # 但 dividend.csv 該保留
    assert (snapshot_dir / "dividend.csv").exists()


# === preload_snapshots dividend.csv ===

def test_preload_snapshots_loads_dividend_csv(tmp_setup, monkeypatch):
    """preload_snapshots 該讀 dividend.csv 寫進 SQLite dividend 表。"""
    snapshot_dir = tmp_setup
    df = pd.DataFrame([
        {"stock_id": "2330", "year": 2024,
         "cash_dividend": 13.0, "stock_dividend": 0, "ex_dividend_date": "2024-07-15"},
        {"stock_id": "2454", "year": 2024,
         "cash_dividend": 30.0, "stock_dividend": 0, "ex_dividend_date": "2024-08-01"},
    ])
    df.to_csv(snapshot_dir / "dividend.csv", index=False)

    counts = db.preload_snapshots(snapshot_dir=snapshot_dir)
    assert counts.get("dividend") == 2

    # 驗 SQLite 確實寫入
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id, year, cash_dividend FROM dividend "
            "ORDER BY stock_id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["stock_id"] == "2330"
    assert rows[0]["cash_dividend"] == 13.0
