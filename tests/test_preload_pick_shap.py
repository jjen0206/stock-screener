"""Regression test:src/database.py::preload_snapshots 必須讀
data/twse_snapshot/pick_shap_explanations.csv,否則雲端 boot 後 SHAP 表永遠空。

Bug 2026-05-18:weekly backfill_pick_shap.py dump 出 ~4000 rows 進 CSV,但
preload_snapshots 漏這支 CSV，雲端容器重啟 SQLite 空、整個歷史 SHAP cache
看不到。守住:csv 存在 → upsert 進 pick_shap_explanations 表。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "test_preload_shap.db"
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


def _sample_rows() -> list[dict]:
    return [
        {
            "pick_date": "2026-05-15",
            "sid": "2330",
            "strategy": "general",
            "top_features": '[{"feature":"rsi_14","value":62.3,"contribution":0.04,"contribution_pct":33.0,"direction":"+"}]',
            "generated_at": "2026-05-16T22:48:13+00:00",
        },
        {
            "pick_date": "2026-05-15",
            "sid": "00679B",
            "strategy": "bias_convergence",
            "top_features": '[{"feature":"atr_normalized","value":0.49,"contribution":-0.04,"contribution_pct":21.1,"direction":"-"}]',
            "generated_at": "2026-05-16T22:48:13+00:00",
        },
    ]


def test_preload_pick_shap_csv_to_db(tmp_db, snap_dir):
    """csv 存在 → preload_snapshots 寫進 pick_shap_explanations 表 → 撈得到。"""
    rows = _sample_rows()
    pd.DataFrame(rows).to_csv(snap_dir / "pick_shap_explanations.csv", index=False)

    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert counts.get("pick_shap_explanations") == 2

    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM pick_shap_explanations"
        ).fetchone()
        assert cur["n"] == 2

    fetched = db.get_shap_explanation("2026-05-15", "2330", "general")
    assert isinstance(fetched, list)
    assert fetched[0]["feature"] == "rsi_14"


def test_preload_pick_shap_missing_csv_is_skip(tmp_db, snap_dir):
    """csv 不存在 → 不爆,counts 也不含 pick_shap_explanations key。"""
    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert "pick_shap_explanations" not in counts


def test_preload_pick_shap_empty_csv_is_skip(tmp_db, snap_dir):
    """csv 存在但 size=0(空檔)→ 不爆 + skip。"""
    (snap_dir / "pick_shap_explanations.csv").write_text("", encoding="utf-8")
    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert "pick_shap_explanations" not in counts


def test_preload_pick_shap_header_only_skip(tmp_db, snap_dir):
    """只有 header 沒 data row → upsert 0 rows,不寫 counts。"""
    pd.DataFrame(
        columns=[
            "pick_date", "sid", "strategy", "top_features", "generated_at",
        ],
    ).to_csv(snap_dir / "pick_shap_explanations.csv", index=False)
    counts = db.preload_snapshots(snapshot_dir=snap_dir)
    assert "pick_shap_explanations" not in counts


def test_preload_pick_shap_idempotent(tmp_db, snap_dir):
    """重複 preload 同份 CSV → ON CONFLICT 走 UPDATE,行數穩定不爆 PK。"""
    rows = _sample_rows()
    pd.DataFrame(rows).to_csv(snap_dir / "pick_shap_explanations.csv", index=False)

    db.preload_snapshots(snapshot_dir=snap_dir)
    db.preload_snapshots(snapshot_dir=snap_dir)

    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM pick_shap_explanations"
        ).fetchone()
        assert cur["n"] == 2


def test_preload_pick_shap_upsert_overwrites(tmp_db, snap_dir):
    """同 (pick_date, sid, strategy) 第二次 preload 新 top_features → UPDATE 覆蓋。"""
    pd.DataFrame([_sample_rows()[0]]).to_csv(
        snap_dir / "pick_shap_explanations.csv", index=False,
    )
    db.preload_snapshots(snapshot_dir=snap_dir)

    new_row = dict(_sample_rows()[0])
    new_row["top_features"] = (
        '[{"feature":"macd","value":0.1,"contribution":0.05,"contribution_pct":40.0,"direction":"+"}]'
    )
    new_row["generated_at"] = "2026-05-18T00:00:00+00:00"
    pd.DataFrame([new_row]).to_csv(
        snap_dir / "pick_shap_explanations.csv", index=False,
    )
    db.preload_snapshots(snapshot_dir=snap_dir)

    fetched = db.get_shap_explanation("2026-05-15", "2330", "general")
    assert fetched[0]["feature"] == "macd"
