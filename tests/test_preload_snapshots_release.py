"""src/database.py::preload_snapshots 的 3-tier fallback 測試。

驗證新加 `_ensure_snapshot_present` helper:
1. 本地 parquet 優先
2. release fallback(無本地檔 → 從 release 下載)
3. CSV 最後 fallback(向後相容)
4. kill-switch off 時跳過 release

不打真 GitHub — 用 monkeypatch fake `snapshot_release` 函式。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db
from src import snapshot_release as sr


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "preload_release.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


@pytest.fixture
def snap_dir(tmp_path):
    d = tmp_path / "twse_snapshot"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("SNAPSHOT_USE_RELEASES_ENABLED",):
        monkeypatch.delenv(k, raising=False)


# === 樣本資料 helpers ===

INST_ROWS = [
    {
        "stock_id": "2330",
        "date": "2025-11-04",
        "foreign_buy_sell": 1000,
        "trust_buy_sell": 200,
        "dealer_buy_sell": 50,
        "total_buy_sell": 1250,
    },
    {
        "stock_id": "2317",
        "date": "2025-11-04",
        "foreign_buy_sell": -300,
        "trust_buy_sell": 0,
        "dealer_buy_sell": 10,
        "total_buy_sell": -290,
    },
]

FIN_ROWS = [
    {
        "stock_id": "2330",
        "period": "2025Q3",
        "revenue": 800000,
        "revenue_yoy": 0.18,
        "eps": 13.5,
        "roe": 0.32,
    },
    {
        "stock_id": "2317",
        "period": "2025Q3",
        "revenue": 1500000,
        "revenue_yoy": 0.05,
        "eps": 2.1,
        "roe": 0.11,
    },
]


def _write_inst_parquet(path: Path) -> None:
    pd.DataFrame(INST_ROWS).to_parquet(path, index=False)


def _write_inst_csv(path: Path) -> None:
    pd.DataFrame(INST_ROWS).to_csv(path, index=False)


def _write_fin_parquet(path: Path) -> None:
    pd.DataFrame(FIN_ROWS).to_parquet(path, index=False)


def _write_fin_csv(path: Path) -> None:
    pd.DataFrame(FIN_ROWS).to_csv(path, index=False)


# === Tier 1: 本地 parquet 優先 ===

def test_local_parquet_preferred_over_csv(tmp_db, snap_dir, monkeypatch):
    """本地兩個格式都有 → 用 parquet,不去叫 release(對 institutional kind)。"""
    _write_inst_parquet(snap_dir / "institutional.parquet")
    _write_inst_csv(snap_dir / "institutional.csv")

    def picky(prefix):
        if prefix.startswith("snapshot-institutional-"):
            pytest.fail("local parquet 已存在,release lookup 不該被叫")
        return None  # financials 等 kind 沒檔 → 回 None 走正常 skip

    monkeypatch.setattr(sr, "get_latest_snapshot_tag", picky)

    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert counts.get("institutional") == 2


# === Tier 2: release fallback ===

def test_release_fallback_when_no_local_parquet(tmp_db, snap_dir, monkeypatch):
    """本地兩個格式都沒 → 試 release → 拉回 parquet 再 load。"""
    # release mock:get_latest 回 tag,download 把檔案寫到 dest
    monkeypatch.setattr(
        sr, "get_latest_snapshot_tag",
        lambda prefix: "snapshot-institutional-2026-05-17"
        if prefix.startswith("snapshot-institutional-") else None,
    )

    def fake_download(tag, asset, dest, snapshot_dir=None, **kw):
        p = Path(dest) / asset
        _write_inst_parquet(p)
        return p

    monkeypatch.setattr(sr, "download_snapshot_from_release", fake_download)

    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert counts.get("institutional") == 2


# === Tier 3: CSV 最後 fallback ===

def test_csv_only_fallback(tmp_db, snap_dir, monkeypatch):
    """無本地 parquet + 無 release → 用 CSV。"""
    _write_inst_csv(snap_dir / "institutional.csv")
    monkeypatch.setattr(sr, "get_latest_snapshot_tag", lambda prefix: None)
    monkeypatch.setattr(
        sr, "download_snapshot_from_release",
        lambda *a, **k: pytest.fail("release 不該被叫 download"),
    )

    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert counts.get("institutional") == 2


# === Kill-switch ===

def test_kill_switch_disables_release_lookup(tmp_db, snap_dir, monkeypatch):
    """SNAPSHOT_USE_RELEASES_ENABLED=false → 不打 release(`is_releases_enabled` gates),
    本地 CSV 仍 load。"""
    monkeypatch.setenv("SNAPSHOT_USE_RELEASES_ENABLED", "false")
    _write_inst_csv(snap_dir / "institutional.csv")

    def boom(*a, **k):
        pytest.fail("kill-switch off,不該叫 release")

    monkeypatch.setattr(sr, "get_latest_snapshot_tag", boom)
    monkeypatch.setattr(sr, "download_snapshot_from_release", boom)

    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert counts.get("institutional") == 2


# === financials 同套邏輯 ===

def test_financials_parquet_preferred(tmp_db, snap_dir, monkeypatch):
    _write_fin_parquet(snap_dir / "financials_quarterly.parquet")

    def picky(prefix):
        if prefix.startswith("snapshot-financials-"):
            pytest.fail("local parquet 已存在,release lookup 不該被叫")
        return None

    monkeypatch.setattr(sr, "get_latest_snapshot_tag", picky)
    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert counts.get("financials_quarterly") == 2


def test_financials_csv_fallback(tmp_db, snap_dir, monkeypatch):
    _write_fin_csv(snap_dir / "financials_quarterly.csv")
    monkeypatch.setattr(sr, "get_latest_snapshot_tag", lambda prefix: None)
    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert counts.get("financials_quarterly") == 2


def test_financials_release_fallback(tmp_db, snap_dir, monkeypatch):
    monkeypatch.setattr(
        sr, "get_latest_snapshot_tag",
        lambda prefix: "snapshot-financials-2026-05-17"
        if prefix.startswith("snapshot-financials-") else None,
    )

    def fake_download(tag, asset, dest, snapshot_dir=None, **kw):
        p = Path(dest) / asset
        _write_fin_parquet(p)
        return p

    monkeypatch.setattr(sr, "download_snapshot_from_release", fake_download)
    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert counts.get("financials_quarterly") == 2


# === release exception 不爆 preload ===

def test_release_lookup_exception_falls_back_to_csv(tmp_db, snap_dir, monkeypatch):
    _write_inst_csv(snap_dir / "institutional.csv")

    def boom(*a, **k):
        raise RuntimeError("simulated network error")

    monkeypatch.setattr(sr, "get_latest_snapshot_tag", boom)

    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert counts.get("institutional") == 2


def test_ensure_snapshot_present_returns_none_when_all_missing(tmp_db, snap_dir,
                                                                monkeypatch):
    monkeypatch.setattr(sr, "get_latest_snapshot_tag", lambda prefix: None)
    path, fmt = db._ensure_snapshot_present(
        snap_dir, "institutional", "institutional",
    )
    assert path is None and fmt is None
