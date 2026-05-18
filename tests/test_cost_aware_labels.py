"""Cost-aware labels regression guard — scripts/train_per_strategy_ml.py 確保
label 由 simulate_outcome(apply_costs=True) 產出。

2026-05-18 rescue:把原本 implicit (走 default) 的 apply_costs=True 改成
explicit,避免有人改 simulate_outcome 預設值後靜默讓 label 變成「沒扣費虛胖版」。

詳見 docs/strategy-rescue-bias-convergence-2026-05-18.md。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

from src import ml_predictor
from src.backtest import simulate_outcome
from src.backtest_costs import round_trip_cost_rate

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "train_per_strategy_ml.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "train_per_strategy_ml", _SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["train_per_strategy_ml"] = module
    spec.loader.exec_module(module)
    return module


tps = _load_script_module()


# --- simulate_outcome cost-asymmetry sanity check ---

def test_simulate_outcome_cost_aware_marginal_gross_becomes_lose():
    """gross +0.4% net -0.185%(< 0)→ apply_costs=True 應該 label=lose;
    apply_costs=False 應該 label=win。
    """
    cost = round_trip_cost_rate()
    assert cost == 0.001425 * 2 + 0.003 == 0.00585

    # 構造 hold_days=5 個 OHLC,close 持平到最後一天才上漲 0.4%
    entry = 100.0
    final_close = 100.4  # +0.4% gross
    future = pd.DataFrame([
        # 4 天區間裡 high/low 都沒觸 target/stop(±5%/-3%)
        {"date": f"d{i}", "high": 100.5, "low": 99.5, "close": 100.0}
        for i in range(4)
    ] + [
        {"date": "d5", "high": 100.5, "low": 99.5, "close": final_close},
    ])

    # 不扣費:gross +0.4% > 0 → win
    outcome_raw, ret_raw = simulate_outcome(
        future, entry, target_pct=0.05, stop_pct=0.03, apply_costs=False,
    )
    assert outcome_raw == "win"
    assert abs(ret_raw - 0.004) < 1e-6

    # 扣費:net = +0.4% - 0.585% < 0 → lose(關鍵差異)
    outcome_cost, ret_cost = simulate_outcome(
        future, entry, target_pct=0.05, stop_pct=0.03, apply_costs=True,
    )
    assert outcome_cost == "lose"
    assert ret_cost < 0


def test_simulate_outcome_target_hit_still_wins_after_costs():
    """+5% target 觸到:gross +5% / net +4.415% > 0,仍 label=win(觸目標路徑)。"""
    entry = 100.0
    future = pd.DataFrame([
        {"date": "d1", "high": 106.0, "low": 100.0, "close": 105.0},  # 觸目標
    ] + [
        {"date": f"d{i}", "high": 100.0, "low": 100.0, "close": 100.0}
        for i in range(2, 6)
    ])

    outcome, ret = simulate_outcome(
        future, entry, target_pct=0.05, stop_pct=0.03, apply_costs=True,
    )
    assert outcome == "win"
    # +5% gross 扣 0.585% cost - 滑價 ≈ +4.4%
    assert 0.04 < ret < 0.05


def test_simulate_outcome_stop_hit_loses():
    """-3% stop 觸到:gross -3% / net -3.585%,label=lose。"""
    entry = 100.0
    future = pd.DataFrame([
        {"date": "d1", "high": 100.0, "low": 96.0, "close": 97.0},  # 觸 -3%
    ] + [
        {"date": f"d{i}", "high": 100.0, "low": 100.0, "close": 100.0}
        for i in range(2, 6)
    ])

    outcome, ret = simulate_outcome(
        future, entry, target_pct=0.05, stop_pct=0.03, apply_costs=True,
    )
    assert outcome == "lose"
    # -3% gross 扣 cost ≈ -3.6%
    assert -0.045 < ret < -0.03


# --- train script wiring ---

def test_train_script_has_cost_aware_constant():
    """COST_AWARE_LABELS 常數存在且為 True。"""
    assert hasattr(tps, "COST_AWARE_LABELS")
    assert tps.COST_AWARE_LABELS is True


def test_gather_training_set_calls_simulate_outcome_with_apply_costs_explicit(monkeypatch):
    """覆蓋 gather_training_set → 驗證它 explicitly 傳 apply_costs=True
    (或對齊 COST_AWARE_LABELS 常數)給 simulate_outcome。
    """
    seen_apply_costs: list[bool] = []

    def _fake_simulate(future, entry_price, target_pct, stop_pct, apply_costs=True):
        seen_apply_costs.append(apply_costs)
        return ("win", 0.05)

    monkeypatch.setattr(tps, "simulate_outcome", _fake_simulate)

    # Stub support deps
    fake_dates = [f"2026-04-{d:02d}" for d in range(1, 16)]
    monkeypatch.setattr(tps, "_list_trading_dates", lambda end, lb: fake_dates)
    monkeypatch.setattr(tps.db, "get_latest_trading_date", lambda: fake_dates[-1])

    future_df = pd.DataFrame([
        {"date": d, "high": 110, "low": 99, "close": 105} for d in fake_dates
    ])
    monkeypatch.setattr(
        tps, "bulk_load_prices",
        lambda conn, sids, end, lookback_days: {sid: future_df.copy() for sid in sids},
    )

    def _fake_screen(date, params=None, stock_ids=None):
        return pd.DataFrame([{"stock_id": "2330", "close": 100.0, "name": "TSMC"}])

    monkeypatch.setitem(tps.ALL_STRATEGIES, "test_strat", _fake_screen)
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, td, db_path=None: {f: 0.0 for f in ml_predictor.FEATURE_NAMES},
    )

    tps.gather_training_set(
        "test_strat",
        lookback_days=len(fake_dates),
        period_end=fake_dates[-1],
        universe=["2330"],
    )

    assert seen_apply_costs, "simulate_outcome 沒被呼叫"
    assert all(ac == tps.COST_AWARE_LABELS for ac in seen_apply_costs), (
        f"simulate_outcome 收到 apply_costs={seen_apply_costs[:3]}... 但應為 "
        f"{tps.COST_AWARE_LABELS}(COST_AWARE_LABELS 常數)"
    )


def test_meta_json_records_cost_aware_flag(tmp_path):
    """train_one dump meta.json 必須有 cost_aware_labels 欄(audit 用)。"""
    import json

    n = 150
    rows = []
    for i in range(n):
        rows.append({
            **{f: float(i % 10) for f in ml_predictor.FEATURE_NAMES},
            "label": 1 if i < n // 2 else 0,
            "stock_id": "2330",
            "date": "2026-04-01",
        })
    train_df = pd.DataFrame(rows)
    tps.train_one("test_meta", train_df, output_dir=tmp_path, min_samples=100)

    meta = json.loads((tmp_path / "test_meta.meta.json").read_text(encoding="utf-8"))
    assert meta.get("cost_aware_labels") is True


def test_meta_json_fallback_path_also_records_cost_aware_flag(tmp_path):
    """sample < min_samples 的 fallback meta 也要記 cost_aware_labels。"""
    import json

    rows = [
        {
            **{f: 0.0 for f in ml_predictor.FEATURE_NAMES},
            "label": i % 2, "stock_id": "2330", "date": "2026-04-01",
        }
        for i in range(50)
    ]
    train_df = pd.DataFrame(rows)
    tps.train_one("test_fallback", train_df, output_dir=tmp_path, min_samples=100)

    meta = json.loads((tmp_path / "test_fallback.meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "fallback"
    assert meta.get("cost_aware_labels") is True
