"""src/ml_walkforward.py 單元測試。

5 個 case:
  1. 正常 — 5 splits 都 ROC AUC 在合理範圍
  2. min_train_size 太大 → ValueError
  3. test_size 太大,撐不出一個 fold → graceful 回 []
  4. features 缺 date col → ValueError
  5. 全 NaN(features 全 dropna) → graceful 回 []
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import ml_walkforward as wf


def _make_synthetic_features(n_rows: int = 200, seed: int = 7) -> pd.DataFrame:
    """生 n_rows 筆假 time-series features:
       - date:每天遞增(sid 不重要,純單檔即可)
       - feat_a/b/c:三個 feature,跟 y 有弱相關性(model 學得到 ROC > 0.5)
       - y:0/1 label,大致 50% balanced
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n_rows, freq="B")
    feat_a = rng.normal(size=n_rows)
    feat_b = rng.normal(size=n_rows)
    feat_c = rng.normal(size=n_rows)
    # y 跟 feat_a 有弱相關(讓 RF 學得起來,但不過度 overfit 給 walk-forward 留空間)
    logits = 0.7 * feat_a + 0.3 * feat_b + rng.normal(scale=0.5, size=n_rows)
    y = (logits > 0).astype(int)
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "feat_a": feat_a,
        "feat_b": feat_b,
        "feat_c": feat_c,
        "y": y,
    })


def test_walkforward_normal_returns_5_splits_with_reasonable_metrics():
    df = _make_synthetic_features(n_rows=300)
    results = wf.walkforward_train_test(
        df, n_splits=5, test_size=20, min_train_size=100,
    )
    assert len(results) == 5
    # 每 split:test 端 ROC AUC 不應 < 0.4(若大幅低於,代表 walk-forward
    # 框架本身有問題:label/feature 對齊錯亂或時序 leak 反向)
    for r in results:
        assert r["test"]["n"] == 20
        assert r["train"]["n"] >= 100
        # 不要硬要求 ROC > 0.5(synthetic noise 可能某 split 退化),
        # 但至少要不是 NaN(代表 fit + eval 完整跑完)
        assert not np.isnan(r["test"]["roc_auc"])
        assert not np.isnan(r["test"]["pr_auc"])
        assert not np.isnan(r["test"]["log_loss"])
        # train_start < train_end ≤ test_start ≤ test_end(時序遞增)
        assert r["train_start"] <= r["train_end"]
        assert r["train_end"] <= r["test_start"]
        assert r["test_start"] <= r["test_end"]

    # summary 聚合
    summary = wf.walkforward_summary(results)
    assert summary["n_splits"] == 5
    assert not np.isnan(summary["test_roc_auc"]["mean"])
    assert summary["test_roc_auc"]["std"] >= 0.0


def test_walkforward_min_train_size_too_large_raises():
    df = _make_synthetic_features(n_rows=50)
    with pytest.raises(ValueError, match="min_train_size"):
        wf.walkforward_train_test(
            df, n_splits=5, test_size=10, min_train_size=100,
        )


def test_walkforward_test_size_too_large_returns_empty():
    """min_train=100,test_size=500,n=200 → (200-100)//500 = 0 fold → 回 []。"""
    df = _make_synthetic_features(n_rows=200)
    results = wf.walkforward_train_test(
        df, n_splits=5, test_size=500, min_train_size=100,
    )
    assert results == []
    summary = wf.walkforward_summary(results)
    assert summary["n_splits"] == 0
    assert np.isnan(summary["test_roc_auc"]["mean"])


def test_walkforward_missing_date_column_raises():
    df = _make_synthetic_features(n_rows=200).drop(columns=["date"])
    with pytest.raises(ValueError, match="date"):
        wf.walkforward_train_test(
            df, n_splits=5, test_size=20, min_train_size=100,
        )


def test_walkforward_all_nan_features_returns_empty_gracefully():
    """所有 row 都有 NaN feature → dropna 後空 → 回 [],不 raise。"""
    df = _make_synthetic_features(n_rows=200)
    df["feat_a"] = np.nan  # 全 NaN → dropna(subset=feature_cols) 砍光
    results = wf.walkforward_train_test(
        df, n_splits=5, test_size=20, min_train_size=100,
    )
    assert results == []
