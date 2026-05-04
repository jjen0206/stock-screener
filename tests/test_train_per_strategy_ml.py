"""scripts/train_per_strategy_ml.py 測試 — gather + train_one 行為驗證。"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from src import ml_predictor


_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "train_per_strategy_ml.py"


def _load_script_module():
    """Load scripts/train_per_strategy_ml.py 當 module(scripts/ 不是 package)。"""
    spec = importlib.util.spec_from_file_location(
        "train_per_strategy_ml", _SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["train_per_strategy_ml"] = module
    spec.loader.exec_module(module)
    return module


tps = _load_script_module()


def test_gather_training_set_returns_features_and_labels(monkeypatch):
    """gather_training_set:mock screener + bulk + extract + simulate,驗欄位齊全。"""
    # Fake universe + dates
    fake_universe = ["2330", "2317"]
    fake_dates = ["2026-04-01", "2026-04-02", "2026-04-03",
                  "2026-04-04", "2026-04-05", "2026-04-06", "2026-04-07"]

    monkeypatch.setattr(tps, "_list_trading_dates", lambda end, lb: fake_dates)
    monkeypatch.setattr(tps.db, "get_latest_trading_date", lambda: "2026-04-07")

    # Fake bulk_load_prices: 對每 sid 給足 hold_days+ 天的 OHLC
    future_df = pd.DataFrame([
        {"date": d, "high": 110, "low": 99, "close": 106}  # win:high 觸 105
        for d in fake_dates
    ])

    def _fake_bulk(conn, sids, end, lookback_days):
        return {sid: future_df.copy() for sid in sids}

    monkeypatch.setattr(tps, "bulk_load_prices", _fake_bulk)

    # Fake screener fn:每天回 fix 1 pick
    def _fake_screen(date, params=None, stock_ids=None):
        return pd.DataFrame([
            {"stock_id": "2330", "close": 100.0, "name": "TSMC"},
        ])

    monkeypatch.setitem(tps.ALL_STRATEGIES, "test_strat", _fake_screen)

    # Fake extract_features:回 11 維 zeros
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, td, db_path=None: {f: 0.0 for f in ml_predictor.FEATURE_NAMES},
    )

    df = tps.gather_training_set(
        "test_strat",
        lookback_days=7,
        period_end="2026-04-07",
        universe=fake_universe,
    )

    # pickable_dates = fake_dates[:-hold_days(5)] = 前 2 天 → 2 picks
    assert len(df) == 2
    # 11 個 feature columns + label + stock_id + date
    for f in ml_predictor.FEATURE_NAMES:
        assert f in df.columns
    assert "label" in df.columns
    assert "stock_id" in df.columns
    assert "date" in df.columns
    # 全 win(future high=110 ≥ 105 = 100×1.05)
    assert (df["label"] == 1).all()


def test_train_one_dumps_pkl_and_meta(tmp_path):
    """train_one 樣本 ≥ min_samples → dump pkl + meta.json with status='trained'。"""
    n = 200  # ≥ MIN_TRAIN_SAMPLES
    rows = []
    for i in range(n):
        row = {f: float(i % 10) for f in ml_predictor.FEATURE_NAMES}
        # 雙類:前半 win(label=1)後半 lose(label=0)
        row["label"] = 1 if i < n // 2 else 0
        row["stock_id"] = "2330"
        row["date"] = "2026-04-01"
        rows.append(row)
    train_df = pd.DataFrame(rows)

    result = tps.train_one(
        "test_strat", train_df,
        output_dir=tmp_path, min_samples=100,
    )

    assert result["status"] == "trained"
    assert result["samples"] == n
    assert result["wins"] == n // 2
    assert result["oob_score"] is not None and 0.0 <= result["oob_score"] <= 1.0

    pkl_path = tmp_path / "test_strat.pkl"
    meta_path = tmp_path / "test_strat.meta.json"
    assert pkl_path.exists()
    assert meta_path.exists()

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["strategy"] == "test_strat"
    assert meta["status"] == "trained"
    assert meta["samples"] == n
    assert meta["model_type"] == "RandomForestClassifier"
    assert "feature_importances" in meta
    assert len(meta["feature_importances"]) == len(ml_predictor.FEATURE_NAMES)


def test_train_one_returns_fallback_when_below_min_samples(tmp_path):
    """樣本 < min_samples → status='fallback',不 dump pkl,只 dump meta。"""
    rows = []
    for i in range(50):  # < default 100
        row = {f: 0.0 for f in ml_predictor.FEATURE_NAMES}
        row["label"] = i % 2
        row["stock_id"] = "2330"
        row["date"] = "2026-04-01"
        rows.append(row)
    train_df = pd.DataFrame(rows)

    result = tps.train_one(
        "low_fire_strat", train_df,
        output_dir=tmp_path, min_samples=100,
    )

    assert result["status"] == "fallback"
    assert result["samples"] == 50
    assert result["pkl_path"] is None
    assert result["oob_score"] is None

    pkl_path = tmp_path / "low_fire_strat.pkl"
    meta_path = tmp_path / "low_fire_strat.meta.json"
    assert not pkl_path.exists()
    assert meta_path.exists()

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "fallback"
    assert "samples 50 < 100" in meta["reason"]


def test_train_one_fallback_when_only_one_label_class(tmp_path):
    """樣本 ≥ min_samples 但 label 只有單一 class → fallback(避免 sklearn 訓不起來)。"""
    rows = []
    for i in range(150):
        row = {f: 0.0 for f in ml_predictor.FEATURE_NAMES}
        row["label"] = 1  # 全 win
        row["stock_id"] = "2330"
        row["date"] = "2026-04-01"
        rows.append(row)
    train_df = pd.DataFrame(rows)

    result = tps.train_one(
        "all_win_strat", train_df,
        output_dir=tmp_path, min_samples=100,
    )

    assert result["status"] == "fallback"
    pkl_path = tmp_path / "all_win_strat.pkl"
    assert not pkl_path.exists()
    meta = json.loads((tmp_path / "all_win_strat.meta.json").read_text(encoding="utf-8"))
    assert "one class" in meta["reason"]
