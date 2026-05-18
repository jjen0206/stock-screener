"""台股交易成本與滑價模型 — 統一被四個 backtest 模組使用。

設計重點:
- 台股實際成本拆兩半:
    1. 滑價(slippage)— 進場 / 出場各 ±N bps 撒在價格上
    2. 手續費 + 證交稅 — 從 gross PnL % 扣
  兩邊**各扣一次**,不要重複扣。

- 來回成本(round_trip_cost_rate)= 雙邊手續費 + 賣方證交稅
    = BROKER_FEE_RATE × 2 + SECURITIES_TAX_RATE
    = 0.001425 × 2 + 0.003
    = 0.005850 (0.585%)

- 滑價 SLIPPAGE_BPS_DEFAULT = 5 bps(大型股偏低 / 小型股偏高的折衷)
  → 進場 ×(1 + 0.0005),出場 ×(1 − 0.0005)
  滑價來回成本 ≈ 0.1%(已內含在價格,不要再從 PnL 扣)

- broker_fee_discount = 1.0 (不折扣,保守估;券商 28 折是 0.28)

對既有 vbt_backtest.py 的相容:
- vectorbt 原生 fees / slippage 是 fraction per side
- 對應到本模組:
    fees = BROKER_FEE_RATE × broker_fee_discount + SECURITIES_TAX_RATE / 2
    slippage = slippage_bps / 10000
  (證交稅均攤雙邊,因 vbt 沒分買賣)

API:
- round_trip_cost_rate(broker_fee_discount=1.0) -> float
- apply_buy_cost(entry_price, slippage_bps=5) -> float
- apply_sell_cost(exit_price, slippage_bps=5) -> float
- adjust_pnl(gross_pnl_pct, broker_fee_discount=1.0) -> float
- vbt_fees_per_side(broker_fee_discount=1.0) -> float
- vbt_slippage(slippage_bps=5) -> float
"""
from __future__ import annotations


BROKER_FEE_RATE: float = 0.001425
"""券商手續費(單邊),0.1425%。"""

SECURITIES_TAX_RATE: float = 0.003
"""證交稅(僅賣方),0.3%。"""

SLIPPAGE_BPS_DEFAULT: int = 5
"""滑價預設 5 bps(0.05%)— 大型股偏低小型股偏高的折衷。"""


def round_trip_cost_rate(broker_fee_discount: float = 1.0) -> float:
    """來回成本率(雙邊手續費 + 賣方證交稅)。

    Args:
        broker_fee_discount: 券商折扣(1.0 = 不折扣;0.28 = 28 折)。

    Returns:
        decimal,e.g. 0.00585 = 0.585%。
    """
    fee = BROKER_FEE_RATE * broker_fee_discount * 2
    tax = SECURITIES_TAX_RATE
    return fee + tax


def apply_buy_cost(entry_price: float, slippage_bps: int = SLIPPAGE_BPS_DEFAULT) -> float:
    """進場價加上滑價(向上)。

    模擬「實際成交價比理想 close 稍高一些」。

    Args:
        entry_price: 理想進場價(e.g. 收盤價)。
        slippage_bps: 滑價 basis points(預設 5)。

    Returns:
        加完滑價的進場價。
    """
    return entry_price * (1 + slippage_bps / 10000)


def apply_sell_cost(exit_price: float, slippage_bps: int = SLIPPAGE_BPS_DEFAULT) -> float:
    """出場價扣掉滑價(向下)。

    模擬「實際成交價比理想 close 稍低一些」。

    Args:
        exit_price: 理想出場價(e.g. 收盤價)。
        slippage_bps: 滑價 basis points(預設 5)。

    Returns:
        扣完滑價的出場價。
    """
    return exit_price * (1 - slippage_bps / 10000)


def adjust_pnl(gross_pnl_pct: float, broker_fee_discount: float = 1.0) -> float:
    """從 gross 報酬率扣除來回手續費 + 證交稅(滑價已在價格上扣,**不要再扣**)。

    Args:
        gross_pnl_pct: 原始報酬率(decimal,e.g. 0.05 = +5%)。
        broker_fee_discount: 券商折扣。

    Returns:
        扣完成本的 net PnL(decimal)。
    """
    return gross_pnl_pct - round_trip_cost_rate(broker_fee_discount)


def adjust_pnl_percentage(gross_pnl_percent: float, broker_fee_discount: float = 1.0) -> float:
    """同 adjust_pnl,但輸入 / 輸出單位是「百分比」而非 decimal。

    e.g. 5.0 (= +5%) → 5.0 - 0.585 = 4.415。

    給既有 backtester 用 `return_pct * 100` 思維的整合用。
    """
    return gross_pnl_percent - round_trip_cost_rate(broker_fee_discount) * 100.0


def vbt_fees_per_side(broker_fee_discount: float = 1.0) -> float:
    """vectorbt 用 fees(per side)— 把證交稅(只賣方)均攤兩邊。

    vbt.Portfolio.from_signals(fees=X) 對買賣兩邊都收 X,所以:
        fees_per_side = broker_fee × discount + tax / 2
    買入時被多收一半稅、賣出時被少收一半稅,合計總成本仍正確。
    """
    return BROKER_FEE_RATE * broker_fee_discount + SECURITIES_TAX_RATE / 2.0


def vbt_slippage(slippage_bps: int = SLIPPAGE_BPS_DEFAULT) -> float:
    """vectorbt 用 slippage(per side,fraction)。"""
    return slippage_bps / 10000.0


__all__ = [
    "BROKER_FEE_RATE",
    "SECURITIES_TAX_RATE",
    "SLIPPAGE_BPS_DEFAULT",
    "round_trip_cost_rate",
    "apply_buy_cost",
    "apply_sell_cost",
    "adjust_pnl",
    "adjust_pnl_percentage",
    "vbt_fees_per_side",
    "vbt_slippage",
]
