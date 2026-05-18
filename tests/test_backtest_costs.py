"""src/backtest_costs.py — 台股交易成本與滑價模型單元測試。

驗證:
- round_trip_cost_rate:雙邊手續費 + 賣方證交稅 = 0.585% (default)
- apply_buy_cost / apply_sell_cost:滑價方向正確 + 數值精確
- adjust_pnl:從 gross 扣 round_trip,不重複扣
- broker_fee_discount 變數:折扣後成本正確下降
- vbt parity:vbt_fees_per_side + vbt_slippage 對齊 backtest_costs 預設
"""
from __future__ import annotations

import pytest

from src.backtest_costs import (
    BROKER_FEE_RATE,
    SECURITIES_TAX_RATE,
    SLIPPAGE_BPS_DEFAULT,
    adjust_pnl,
    adjust_pnl_percentage,
    apply_buy_cost,
    apply_sell_cost,
    round_trip_cost_rate,
    vbt_fees_per_side,
    vbt_slippage,
)


# === round_trip_cost_rate ===

def test_round_trip_cost_rate_default_is_585bps():
    """預設(broker_fee_discount=1.0):
        0.001425 × 2 + 0.003 = 0.00585 (58.5 bps)。"""
    assert round_trip_cost_rate() == pytest.approx(0.00585, rel=1e-9)


def test_round_trip_cost_rate_with_discount():
    """券商 28 折(broker_fee_discount=0.28):
        0.001425 × 0.28 × 2 + 0.003 = 0.0037980 (37.98 bps)。"""
    expected = BROKER_FEE_RATE * 0.28 * 2 + SECURITIES_TAX_RATE
    assert round_trip_cost_rate(broker_fee_discount=0.28) == pytest.approx(expected)


def test_round_trip_cost_rate_zero_discount_only_tax():
    """broker_fee_discount=0.0(假設免手續費)→ 只有證交稅 0.3%。"""
    assert round_trip_cost_rate(0.0) == pytest.approx(SECURITIES_TAX_RATE)


# === apply_buy_cost / apply_sell_cost ===

def test_apply_buy_cost_adds_slippage_to_price():
    """進場價套滑價向上 — 5 bps → ×1.0005。"""
    assert apply_buy_cost(100.0, slippage_bps=5) == pytest.approx(100.05)


def test_apply_sell_cost_subtracts_slippage_from_price():
    """出場價套滑價向下 — 5 bps → ×0.9995。"""
    assert apply_sell_cost(100.0, slippage_bps=5) == pytest.approx(99.95)


def test_apply_buy_then_sell_eats_slippage_both_sides():
    """買 100 滑 +0.05 → 100.05;隔天賣 100 滑 −0.05 → 99.95。
    來回滑價損失 = (99.95 − 100.05) / 100.05 ≈ -0.0999% (約 10 bps,= 2×5 bps)。
    """
    buy = apply_buy_cost(100.0, 5)
    sell = apply_sell_cost(100.0, 5)
    round_trip_slip_pct = (sell - buy) / buy * 100
    assert round_trip_slip_pct == pytest.approx(-0.09995, rel=1e-3)


def test_apply_buy_cost_custom_slippage():
    """自訂 10 bps → 進場價 ×1.001。"""
    assert apply_buy_cost(50.0, slippage_bps=10) == pytest.approx(50.05)


def test_apply_sell_cost_zero_slippage_passthrough():
    """slippage_bps=0 → 價格不變。"""
    assert apply_sell_cost(123.45, slippage_bps=0) == pytest.approx(123.45)


# === adjust_pnl ===

def test_adjust_pnl_subtracts_round_trip_cost():
    """gross +5%(decimal 0.05)→ net 0.05 − 0.00585 = 0.04415。"""
    assert adjust_pnl(0.05) == pytest.approx(0.04415)


def test_adjust_pnl_negative_pnl_eats_more():
    """gross -3% → net -0.03 - 0.00585 = -0.03585(成本讓虧損更大)。"""
    assert adjust_pnl(-0.03) == pytest.approx(-0.03585)


def test_adjust_pnl_with_broker_discount():
    """28 折(broker_fee_discount=0.28)gross 5%:
        cost = 0.00585 × 0.28 fee_part 的差 → 直接算 round_trip(0.28)。"""
    cost = round_trip_cost_rate(0.28)
    assert adjust_pnl(0.05, broker_fee_discount=0.28) == pytest.approx(0.05 - cost)


def test_adjust_pnl_zero_gross_yields_pure_cost():
    """gross 0 → net = -0.00585(只交易就賠成本)。"""
    assert adjust_pnl(0.0) == pytest.approx(-0.00585)


def test_adjust_pnl_percentage_works_in_pct_units():
    """adjust_pnl_percentage 輸入輸出單位都是 %(decimal × 100)。
       gross +5.0% → net 5.0 - 0.585 = 4.415%。"""
    assert adjust_pnl_percentage(5.0) == pytest.approx(4.415)


def test_adjust_pnl_percentage_negative():
    """gross -3.0% → net -3.585%。"""
    assert adjust_pnl_percentage(-3.0) == pytest.approx(-3.585)


# === defaults ===

def test_default_constants_match_spec():
    """主公規格鎖定的常數 — 不該被亂改。"""
    assert BROKER_FEE_RATE == 0.001425
    assert SECURITIES_TAX_RATE == 0.003
    assert SLIPPAGE_BPS_DEFAULT == 5


# === vbt parity ===

def test_vbt_fees_per_side_round_trip_equals_full_cost():
    """vbt 雙邊收 fees → 2 × vbt_fees_per_side = round_trip_cost_rate。"""
    assert 2 * vbt_fees_per_side() == pytest.approx(round_trip_cost_rate())


def test_vbt_fees_per_side_with_discount():
    """28 折 → 2 × vbt_fees_per_side(0.28) = round_trip_cost_rate(0.28)。"""
    assert 2 * vbt_fees_per_side(0.28) == pytest.approx(round_trip_cost_rate(0.28))


def test_vbt_slippage_default_matches_5_bps():
    """vbt_slippage 預設 = 5 bps = 0.0005。"""
    assert vbt_slippage() == pytest.approx(0.0005)


def test_vbt_slippage_custom_bps():
    """自訂 10 bps → 0.001。"""
    assert vbt_slippage(10) == pytest.approx(0.001)


# === 商業合理性 sanity ===

def test_round_trip_cost_is_meaningful_for_5pct_target():
    """主公短線目標 5%,扣完成本剩 ~4.4%,確保成本「吃 11% 的目標」這個量級合理。
       (cost / target = 0.585 / 5 = 11.7%)"""
    target_pct = 0.05
    cost = round_trip_cost_rate()
    eaten_fraction = cost / target_pct
    assert 0.10 < eaten_fraction < 0.13, (
        f"成本應吃 5% 目標的 10-13% 區間,實際 {eaten_fraction:.1%}"
    )
