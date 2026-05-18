"""bias_convergence ML gate regression guard。

2026-05-18 rescue 把 STRATEGY_ML_THRESHOLDS["bias_convergence"] 從 0.65 改成
0.55(cost-aware retrain 後 sweep 顯示 0.55 是 sweet spot,WR 92.6% / fires 408)。

這組測試守:
1. threshold 不被誤改
2. backtest_strategy(per_strategy_ml=True) 真的用該 threshold 過濾
3. ML prob 低於 threshold 的 picks 不會進 fires
詳見 docs/strategy-rescue-bias-convergence-2026-05-18.md。
"""
from __future__ import annotations

import pandas as pd

from src import backtest
from src.strategies import STRATEGY_ML_THRESHOLDS


def test_bias_convergence_threshold_is_055():
    """rescue 後 threshold 應為 0.55。"""
    assert STRATEGY_ML_THRESHOLDS["bias_convergence"] == 0.55


def test_all_thresholds_in_sane_range():
    """所有 per-strategy thresholds 都在 [0.50, 0.80] 之內(避免誤改成 0 或 1)。"""
    for strat, thr in STRATEGY_ML_THRESHOLDS.items():
        assert 0.50 <= thr <= 0.80, f"{strat}: threshold {thr} 超出合理範圍"


def test_ml_filter_drops_picks_below_threshold(monkeypatch):
    """模擬 ML prob 分佈,驗證 backtest_strategy(ml_filter=X) 真的把 prob < X 的擋掉。

    這是 STRATEGY_ML_THRESHOLDS 套用機制的 unit guard — 確保「設了 threshold,
    backtest 真的按 threshold 過濾」。
    """
    # Fake 5 picks per day,ml_prob 分散在 0.3 - 0.9
    fake_dates = [f"2026-04-{d:02d}" for d in range(1, 11)]
    fake_universe = ["S0", "S1", "S2", "S3", "S4"]

    monkeypatch.setattr(backtest, "_list_trading_dates", lambda end, lb: fake_dates)

    # bulk_load_prices: future OHLC 對每 sid 都是「+5% target 觸到」(win)
    future_df = pd.DataFrame([
        {"date": d, "high": 110, "low": 99, "close": 105} for d in fake_dates
    ])
    monkeypatch.setattr(
        backtest, "bulk_load_prices",
        lambda conn, sids, end, lookback_days: {sid: future_df.copy() for sid in sids},
    )

    # screen_fn: 每天 fire 全 5 sids
    def _fake_screen(date, params=None, stock_ids=None):
        return pd.DataFrame([
            {"stock_id": sid, "close": 100.0, "name": sid}
            for sid in fake_universe
        ])

    monkeypatch.setitem(backtest.ALL_STRATEGIES, "test_strat", _fake_screen)

    # predict_for_strategy: prob 跟 sid index 線性遞增 — S0=0.3, S1=0.45, S2=0.6, S3=0.75, S4=0.9
    sid_to_prob = {f"S{i}": 0.3 + i * 0.15 for i in range(5)}

    def _fake_predict_for_strategy(
        strategy_name, stock_ids, target_date,
        fallback_model=None, strategy_model=None,
    ):
        return {sid: sid_to_prob.get(sid) for sid in stock_ids}

    import src.ml_predictor
    monkeypatch.setattr(src.ml_predictor, "predict_for_strategy", _fake_predict_for_strategy)

    # threshold = 0.55 → 只 S2/S3/S4 過(0.6, 0.75, 0.9)
    result = backtest.backtest_strategy(
        "test_strat", fake_universe, fake_dates[-1],
        lookback_days=len(fake_dates), hold_days=5,
        ml_filter=0.55, ml_model="dummy",  # ml_model 只要 truthy
    )

    # pickable_dates = fake_dates[:-5] = 5 天 × 3 過濾後 = 15 fires
    assert result["n_fires"] == 15
    # 全 win (future +5% target)
    assert result["n_wins"] == 15


def test_ml_filter_no_threshold_runs_all_picks(monkeypatch):
    """ml_filter=None → 不過濾,backtest 應跑全部 picks(對照組)。"""
    fake_dates = [f"2026-04-{d:02d}" for d in range(1, 11)]
    fake_universe = ["S0", "S1", "S2", "S3", "S4"]

    monkeypatch.setattr(backtest, "_list_trading_dates", lambda end, lb: fake_dates)

    future_df = pd.DataFrame([
        {"date": d, "high": 110, "low": 99, "close": 105} for d in fake_dates
    ])
    monkeypatch.setattr(
        backtest, "bulk_load_prices",
        lambda conn, sids, end, lookback_days: {sid: future_df.copy() for sid in sids},
    )

    def _fake_screen(date, params=None, stock_ids=None):
        return pd.DataFrame([
            {"stock_id": sid, "close": 100.0, "name": sid}
            for sid in fake_universe
        ])

    monkeypatch.setitem(backtest.ALL_STRATEGIES, "test_strat", _fake_screen)

    result = backtest.backtest_strategy(
        "test_strat", fake_universe, fake_dates[-1],
        lookback_days=len(fake_dates), hold_days=5,
        ml_filter=None, ml_model=None,
    )
    # pickable_dates = 5 天 × 5 picks/day = 25
    assert result["n_fires"] == 25
