"""已淘汰策略(rsi_recovery / inst_oversold_reversal)止血 regression tests。

2026-05-18 audit:扣完成本 hold=5 ROI 為負 → 不再進 run_all_strategies / 不推播 /
不算 verdict consensus(歷史 paper_trades 紀錄保留,直接呼叫 ALL_STRATEGIES[key](...)
可繼續 backtest)。
"""
from __future__ import annotations

import logging

import pandas as pd

from src import strategies as strat


def test_deprecated_strategies_registered_with_reasons():
    """DEPRECATED_STRATEGIES 必含 2026-05-18 audit 淘汰的兩支策略。"""
    assert "rsi_recovery" in strat.DEPRECATED_STRATEGIES
    assert "inst_oversold_reversal" in strat.DEPRECATED_STRATEGIES
    # reason 不空(commit message / log 用得到)
    for key, reason in strat.DEPRECATED_STRATEGIES.items():
        assert reason and isinstance(reason, str), f"{key} 缺 reason"


def test_deprecated_strategies_stay_in_registry():
    """ALL_STRATEGIES / STRATEGY_LABELS / STRATEGY_CATEGORY 保留 deprecated 鍵,
    讓歷史 paper_trades 渲染 / backtest opt-in 仍能用。"""
    for key in strat.DEPRECATED_STRATEGIES:
        assert key in strat.ALL_STRATEGIES, f"{key} 不該從 ALL_STRATEGIES 移除"
        assert key in strat.STRATEGY_LABELS, f"{key} 不該從 STRATEGY_LABELS 移除"
        assert key in strat.STRATEGY_CATEGORY, f"{key} 不該從 STRATEGY_CATEGORY 移除"


def test_run_all_strategies_default_skips_deprecated(monkeypatch):
    """run_all_strategies(enabled=None) 預設不會呼叫 deprecated 策略 callable。"""
    called: list[str] = []

    def _make_stub(name: str):
        def _stub(date, params=None, stock_ids=None):
            called.append(name)
            return pd.DataFrame(columns=["stock_id"])
        return _stub

    stub_registry = {k: _make_stub(k) for k in strat.ALL_STRATEGIES}
    monkeypatch.setattr(strat, "ALL_STRATEGIES", stub_registry)

    strat.run_all_strategies("2026-05-18")

    # deprecated 策略 callable 一次都不該被呼叫
    for dep in strat.DEPRECATED_STRATEGIES:
        assert dep not in called, (
            f"{dep} 已淘汰但 default run_all_strategies 仍呼叫了它"
        )
    # 非 deprecated 策略應被呼叫
    expected_called = set(stub_registry) - set(strat.DEPRECATED_STRATEGIES)
    assert set(called) == expected_called


def test_run_all_strategies_explicit_deprecated_logs_warning_and_skips(
    monkeypatch, caplog,
):
    """顯式把 deprecated key 放進 enabled → 印 warning + 一樣跳過(止血優先)。"""
    called: list[str] = []

    def _make_stub(name: str):
        def _stub(date, params=None, stock_ids=None):
            called.append(name)
            return pd.DataFrame(columns=["stock_id"])
        return _stub

    stub_registry = {k: _make_stub(k) for k in strat.ALL_STRATEGIES}
    monkeypatch.setattr(strat, "ALL_STRATEGIES", stub_registry)

    with caplog.at_level(logging.WARNING, logger="src.strategies"):
        strat.run_all_strategies(
            "2026-05-18",
            enabled=["rsi_recovery", "inst_oversold_reversal", "ma_alignment"],
        )

    # deprecated 策略不被呼叫,健康策略仍跑
    assert "rsi_recovery" not in called
    assert "inst_oversold_reversal" not in called
    assert "ma_alignment" in called

    # warning log 留證據
    warning_text = " ".join(r.message for r in caplog.records)
    assert "rsi_recovery" in warning_text
    assert "inst_oversold_reversal" in warning_text
    assert "已淘汰" in warning_text


def test_deprecated_strategies_still_callable_directly():
    """直接從 ALL_STRATEGIES dict 拿 callable 不受 deprecation gate 影響
    (給歷史 backtest / paper trades 對照用)。"""
    for dep in strat.DEPRECATED_STRATEGIES:
        fn = strat.ALL_STRATEGIES[dep]
        assert callable(fn), (
            f"ALL_STRATEGIES['{dep}'] 應為 callable;deprecation 只該 gate "
            f"run_all_strategies,不該把 .py impl 移除"
        )


def test_aggregated_picks_dont_include_deprecated_labels(monkeypatch):
    """run_all_strategies 回傳的 aggregated 結果 signals/details 不會出現 deprecated
    策略 — 保證 daily_notify caption / verdict consensus 不會看到淘汰策略名。"""

    def _stub_fire(name: str):
        # 模擬該策略命中 2330
        def _stub(date, params=None, stock_ids=None):
            return pd.DataFrame([{"stock_id": "2330", "name": "TSMC"}])
        return _stub

    stub_registry = {k: _stub_fire(k) for k in strat.ALL_STRATEGIES}
    monkeypatch.setattr(strat, "ALL_STRATEGIES", stub_registry)

    agg = strat.run_all_strategies("2026-05-18")

    assert "2330" in agg, "stub 設定 2330 必中"
    details_keys = set(agg["2330"]["details"].keys())
    signals = agg["2330"]["signals"]
    for dep in strat.DEPRECATED_STRATEGIES:
        assert dep not in details_keys, (
            f"{dep} 已淘汰但 details 內仍出現 — daily_notify / verdict 會看到"
        )
        assert strat.STRATEGY_LABELS[dep] not in signals, (
            f"{strat.STRATEGY_LABELS[dep]} (label of {dep}) 不該出現在 signals"
        )
