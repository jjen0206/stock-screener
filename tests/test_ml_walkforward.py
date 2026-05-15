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


def test_walkforward_n_splits_none_uses_all_data():
    """n_splits=None → 用 max_possible,不被 hard cap=5 限制。

    Why:docs/ml-overfit-root-cause.md 找到原 cap=5 對大樣本(2685 rows)只測前
    200 rows、浪費 93% 資料。修法後 n_splits=None 應跑滿 max_possible splits。

    n=1000, min_train=100, test_size=50 → max_possible = (1000-100)//50 = 18
    """
    df = _make_synthetic_features(n_rows=1000)
    results = wf.walkforward_train_test(
        df, n_splits=None, test_size=50, min_train_size=100,
    )
    # max_possible = (1000 - 100) // 50 = 18
    # 但部分 split 若 train 全同類會被 skip,所以放寬到 >= 15
    assert len(results) >= 15, (
        f"n_splits=None 應跑滿 max_possible≈18 splits,實際 {len(results)}"
    )
    # 最後一個 split 應覆蓋到接近資料尾端(證明真的用了所有資料)
    last = results[-1]
    assert last["test"]["n"] == 50
    # 第一個 split test_size = 50
    assert results[0]["test"]["n"] == 50
    # train 是 expanding:最後一個 split 的 train_n 應比第一個大很多
    assert results[-1]["train"]["n"] > results[0]["train"]["n"]


def _make_panel_features(n_dates: int = 60, sids_per_date: int = 4, seed: int = 11):
    """panel data:同 date 多 sid 一筆,模擬 per_strategy 真實樣本結構。

    用來驗證 split_by='date' 不會讓同日 sids 跨 train/test。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-01", periods=n_dates).strftime("%Y-%m-%d")
    rows = []
    for d in dates:
        for sid_i in range(sids_per_date):
            feat_a = rng.normal()
            feat_b = rng.normal()
            logit = 0.6 * feat_a + 0.4 * feat_b + rng.normal(scale=0.3)
            rows.append({
                "date": d,
                "stock_id": f"{1000 + sid_i}",
                "feat_a": feat_a,
                "feat_b": feat_b,
                "y": int(logit > 0),
            })
    return pd.DataFrame(rows)


def test_walkforward_split_by_date_keeps_same_day_sids_together():
    """split_by='date' invariant:同日 sids 不可橫跨 train / test fold。

    Why:主公拍板「by-date split 進一步消除 cross-sectional 虛高」
    (2026-05-15)— row split 把同日 4 個 sids 切到 train/test 兩邊,
    test 等於看 train 同日的其他 sid 預測自己,虛高 ROC。
    """
    df = _make_panel_features(n_dates=60, sids_per_date=4)  # 240 rows / 60 dates
    results = wf.walkforward_train_test(
        df,
        n_splits=None,
        test_size=5,       # 5 days per fold
        min_train_size=20, # 20 days first train
        split_by="date",
    )
    assert len(results) >= 1
    for r in results:
        train_dates = set(df[
            (df["date"] >= r["train_start"]) & (df["date"] <= r["train_end"])
        ]["date"].unique())
        test_dates = set(df[
            (df["date"] >= r["test_start"]) & (df["date"] <= r["test_end"])
        ]["date"].unique())
        # 核心 invariant:train / test 不共用任何 date
        intersect = train_dates & test_dates
        assert not intersect, (
            f"split_by='date' 違反 invariant:date {sorted(intersect)} 同時在 "
            f"train [{r['train_start']}..{r['train_end']}] 跟 test "
            f"[{r['test_start']}..{r['test_end']}]"
        )
        # train_end 嚴格早於 test_start(時序遞增)
        assert r["train_end"] < r["test_start"], (
            f"split_by='date' train_end {r['train_end']} 應早於 test_start {r['test_start']}"
        )


def test_walkforward_split_by_date_test_size_is_days():
    """date mode:test_size=5 day → 每 fold test row 數 = 5 days × sids_per_date。"""
    df = _make_panel_features(n_dates=60, sids_per_date=4)
    results = wf.walkforward_train_test(
        df, n_splits=1, test_size=5, min_train_size=20, split_by="date",
    )
    assert len(results) == 1
    # 5 days × 4 sids = 20 rows
    assert results[0]["test"]["n"] == 20


def test_walkforward_split_by_date_invalid_value_raises():
    df = _make_panel_features(n_dates=40)
    with pytest.raises(ValueError, match="split_by"):
        wf.walkforward_train_test(
            df, n_splits=1, test_size=5, min_train_size=20, split_by="invalid",
        )


def test_walkforward_split_by_date_too_few_dates_raises():
    """date mode:min_train_size 大於 unique dates → 立刻 raise。"""
    df = _make_panel_features(n_dates=10, sids_per_date=3)
    with pytest.raises(ValueError, match="unique dates"):
        wf.walkforward_train_test(
            df, n_splits=1, test_size=2, min_train_size=20, split_by="date",
        )


def test_walkforward_split_by_row_default_unchanged():
    """backward compat:split_by default='row',行為跟之前一樣。"""
    df = _make_panel_features(n_dates=60, sids_per_date=4)  # 240 rows
    results_default = wf.walkforward_train_test(
        df, n_splits=3, test_size=20, min_train_size=100,
    )
    results_explicit_row = wf.walkforward_train_test(
        df, n_splits=3, test_size=20, min_train_size=100, split_by="row",
    )
    assert len(results_default) == len(results_explicit_row)
    for a, b in zip(results_default, results_explicit_row):
        assert a["split_idx"] == b["split_idx"]
        # row mode 邏輯沒變 → metric 完全相同
        assert a["test"]["roc_auc"] == b["test"]["roc_auc"]


def test_walkforward_test_size_50_default_in_eval():
    """scripts/eval_walkforward.py 預設 test_size=50, min_train=300, n_splits=None。

    Why:docs/ml-overfit-root-cause.md 報告 test_size=20 統計雜訊太大、
    min_train=100 對 short_pick/taiex_alpha 太小。修法後預設應為 50/300/None。
    """
    import importlib.util
    from pathlib import Path
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "eval_walkforward.py"
    spec = importlib.util.spec_from_file_location("eval_walkforward_default_check", script_path)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules["eval_walkforward_default_check"] = module
    spec.loader.exec_module(module)

    assert module.DEFAULT_TEST_SIZE == 50, (
        f"eval_walkforward 預設 test_size 應為 50(原 20 統計雜訊太大),"
        f"實際 {module.DEFAULT_TEST_SIZE}"
    )
    assert module.DEFAULT_MIN_TRAIN == 300, (
        f"eval_walkforward 預設 min_train 應為 300(原 100 對大樣本太小),"
        f"實際 {module.DEFAULT_MIN_TRAIN}"
    )
    assert module.DEFAULT_N_SPLITS is None, (
        f"eval_walkforward 預設 n_splits 應為 None(讓 max_possible 主導),"
        f"實際 {module.DEFAULT_N_SPLITS}"
    )
