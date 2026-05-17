"""風險管理(停損 / 停利 / 支撐壓力 / drawdown)模組。

設計原則:
- 不下單,只算數字 + 建議,讓使用者拍板。
- ATR 為主軸:停損 = entry − ATR × stop_multiplier;停利 = entry + ATR × tp_multiplier。
  預設 2.0 / 4.0(2:1 風報比)— 給賺得多賠得少的傾向。
- 支撐 / 壓力:swing low / swing high(60 日 window)— 純可選,
  不擋停損停利的「主規則」。
- drawdown:整組持倉的「總損益 / 總投入」%,> 10% 黃燈,> 20% 紅燈。

Kill-switch:env `RISK_MGMT_ENABLED=true`(預設 on)。off 時 notifier 推播
跳過 drawdown 警報 section,callers 應檢查 `is_enabled()`。

公開 API:
- `is_enabled() -> bool`
- `compute_atr_stop_loss(sid, entry_price, days=14, atr_multiplier=2.0,
                         db_path=None) -> dict`
- `compute_atr_take_profit(sid, entry_price, days=14, atr_multiplier=4.0,
                           db_path=None) -> dict`
- `compute_support_resistance(sid, lookback=60, db_path=None) -> dict`
- `should_take_profit(entry_price, current_price, take_profit) -> bool`
- `should_stop_loss(entry_price, current_price, stop_loss) -> bool`
- `drawdown_pct(positions) -> dict` — 整體 P&L vs 總 invested

回傳格式範例(compute_atr_stop_loss):
    {
        "sid": "2330",
        "entry_price": 600.0,
        "atr": 6.0,
        "atr_multiplier": 2.0,
        "stop_loss": 588.0,
        "stop_loss_pct": -2.0,
        "n_bars": 30,
        "rationale": "ATR(14)=6.0,停損 = 600 - 2.0×6.0 = 588 (-2.0%)"
    }
None on insufficient data.
"""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path

from src import database as db

logger = logging.getLogger(__name__)


_DEFAULT_ATR_DAYS = 14
_DEFAULT_STOP_MULTIPLIER = 2.0
_DEFAULT_TAKE_PROFIT_MULTIPLIER = 4.0
_DEFAULT_SR_LOOKBACK = 60

DRAWDOWN_WARN_PCT = 10.0   # > 10% 黃燈
DRAWDOWN_DANGER_PCT = 20.0  # > 20% 紅燈


def is_enabled() -> bool:
    """讀 env RISK_MGMT_ENABLED(預設 true)。"""
    raw = os.getenv("RISK_MGMT_ENABLED", "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def _fetch_ohlc(
    sid: str,
    days: int,
    db_path: str | Path | None = None,
) -> list[dict]:
    """撈 sid 最近 `days` 筆 daily_prices(high/low/close + date)。

    SQL 反序撈最近 N,Python 端 reverse 成時間正序給 indicators.atr 用。
    """
    try:
        with db.get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT date, high, low, close FROM daily_prices "
                "WHERE stock_id=? AND high IS NOT NULL AND low IS NOT NULL "
                "ORDER BY date DESC LIMIT ?",
                (str(sid).strip(), int(days)),
            ).fetchall()
    except Exception:  # noqa: BLE001
        logger.exception("[RISK_MGMT] _fetch_ohlc 失敗 sid=%s", sid)
        return []
    return [dict(r) for r in reversed(rows)]


def _compute_atr_value(
    sid: str,
    period: int = _DEFAULT_ATR_DAYS,
    db_path: str | Path | None = None,
) -> tuple[float | None, int]:
    """算 sid 最新 ATR(period 日 Wilder),回 (atr, n_bars_used)。

    撈 period × 3 + 5 筆給 ATR 暖機(Wilder seed 需要 period+1 筆,留 buffer)。
    資料不足 → (None, n)。
    """
    n_fetch = max(int(period) * 3 + 5, 50)
    bars = _fetch_ohlc(sid, days=n_fetch, db_path=db_path)
    if len(bars) < int(period) + 1:
        return (None, len(bars))

    import pandas as pd
    from src.indicators import atr as _atr_fn
    df = pd.DataFrame(bars)
    try:
        atr_series = _atr_fn(df, period=int(period))
    except Exception:  # noqa: BLE001
        logger.exception("[RISK_MGMT] atr() 失敗 sid=%s", sid)
        return (None, len(bars))
    if atr_series.empty or atr_series.dropna().empty:
        return (None, len(bars))
    val = float(atr_series.dropna().iloc[-1])
    if math.isnan(val) or val <= 0:
        return (None, len(bars))
    return (val, len(bars))


def compute_atr_stop_loss(
    sid: str,
    entry_price: float,
    days: int = _DEFAULT_ATR_DAYS,
    atr_multiplier: float = _DEFAULT_STOP_MULTIPLIER,
    db_path: str | Path | None = None,
) -> dict | None:
    """從 daily_prices 算 ATR-based 停損。

    停損價 = entry_price − ATR × atr_multiplier。

    Args:
        sid: 股號。
        entry_price: 進場價(主公自填)。
        days: ATR 平滑天數,預設 14。
        atr_multiplier: 預設 2.0(常見保守值)。
        db_path: 給測試用。

    回傳 dict(見模組 docstring)。資料不足 → None。
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price 必須 > 0,got {entry_price}")
    if atr_multiplier <= 0:
        raise ValueError(f"atr_multiplier 必須 > 0,got {atr_multiplier}")

    atr_val, n_bars = _compute_atr_value(sid, period=days, db_path=db_path)
    if atr_val is None:
        return None

    stop = float(entry_price) - atr_val * float(atr_multiplier)
    if stop <= 0:
        # 異常情況(極端波動 / entry 過低):clamp 到 1% 之上避免 0 / 負數
        stop = max(stop, float(entry_price) * 0.01)
    pct = (stop - float(entry_price)) / float(entry_price) * 100.0
    return {
        "sid": str(sid).strip(),
        "entry_price": float(entry_price),
        "atr": float(atr_val),
        "atr_multiplier": float(atr_multiplier),
        "stop_loss": float(stop),
        "stop_loss_pct": float(pct),
        "n_bars": int(n_bars),
        "rationale": (
            f"ATR({days})={atr_val:.2f},停損 = "
            f"{entry_price:.2f} - {atr_multiplier:.1f}×{atr_val:.2f} = "
            f"{stop:.2f} ({pct:+.1f}%)"
        ),
    }


def compute_atr_take_profit(
    sid: str,
    entry_price: float,
    days: int = _DEFAULT_ATR_DAYS,
    atr_multiplier: float = _DEFAULT_TAKE_PROFIT_MULTIPLIER,
    db_path: str | Path | None = None,
) -> dict | None:
    """從 daily_prices 算 ATR-based 停利。

    停利價 = entry_price + ATR × atr_multiplier。
    預設 atr_multiplier=4.0 對應 stop 2.0,風報比 2:1。

    回傳 dict;資料不足 → None。
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price 必須 > 0,got {entry_price}")
    if atr_multiplier <= 0:
        raise ValueError(f"atr_multiplier 必須 > 0,got {atr_multiplier}")

    atr_val, n_bars = _compute_atr_value(sid, period=days, db_path=db_path)
    if atr_val is None:
        return None

    target = float(entry_price) + atr_val * float(atr_multiplier)
    pct = (target - float(entry_price)) / float(entry_price) * 100.0
    return {
        "sid": str(sid).strip(),
        "entry_price": float(entry_price),
        "atr": float(atr_val),
        "atr_multiplier": float(atr_multiplier),
        "take_profit": float(target),
        "take_profit_pct": float(pct),
        "n_bars": int(n_bars),
        "rationale": (
            f"ATR({days})={atr_val:.2f},停利 = "
            f"{entry_price:.2f} + {atr_multiplier:.1f}×{atr_val:.2f} = "
            f"{target:.2f} ({pct:+.1f}%)"
        ),
    }


def compute_support_resistance(
    sid: str,
    lookback: int = _DEFAULT_SR_LOOKBACK,
    db_path: str | Path | None = None,
) -> dict | None:
    """從最近 lookback 日的 high/low 抓 swing low(支撐)/ swing high(壓力)。

    定義:swing high = max(high[-lookback:]),swing low = min(low[-lookback:])。
    純粹從 daily_prices 撈,不算 fractal pivot(對個人工具 MVP 已夠)。

    回 dict {sid, support, resistance, n_bars}。資料不足 → None。
    """
    if lookback < 5:
        raise ValueError(f"lookback 必須 >= 5,got {lookback}")
    bars = _fetch_ohlc(sid, days=int(lookback), db_path=db_path)
    if not bars:
        return None
    highs = [float(b["high"]) for b in bars if b.get("high") is not None]
    lows = [float(b["low"]) for b in bars if b.get("low") is not None]
    if not highs or not lows:
        return None
    return {
        "sid": str(sid).strip(),
        "support": float(min(lows)),
        "resistance": float(max(highs)),
        "n_bars": int(len(bars)),
        "lookback": int(lookback),
    }


def should_take_profit(
    entry_price: float,
    current_price: float,
    take_profit: float | None,
) -> bool:
    """current_price >= take_profit → True(達停利)。

    take_profit is None / entry_price <= 0 → False(不觸發)。
    """
    if take_profit is None:
        return False
    try:
        cp = float(current_price)
        tp = float(take_profit)
    except (TypeError, ValueError):
        return False
    if cp <= 0 or tp <= 0:
        return False
    return cp >= tp


def should_stop_loss(
    entry_price: float,
    current_price: float,
    stop_loss: float | None,
) -> bool:
    """current_price <= stop_loss → True(達停損)。"""
    if stop_loss is None:
        return False
    try:
        cp = float(current_price)
        sl = float(stop_loss)
    except (TypeError, ValueError):
        return False
    if cp <= 0 or sl <= 0:
        return False
    return cp <= sl


def drawdown_pct(positions: list[dict]) -> dict:
    """算整體未實現 + 已實現的 drawdown %。

    positions 每筆預期 dict 含:
      - entry_price (float, > 0)
      - shares      (int, > 0)
      - current_price (float, > 0)  caller 自己撈 daily_prices 並注入
      - is_open     (int 0/1) — open 部位才算市值,closed 算 exit_price
      - exit_price  (float | None) — closed 用
      - side        ("long" / "short") — 預設 long

    回傳:
        {
            "total_invested": float,    Σ entry × shares (open + closed)
            "current_value": float,     Σ (open: current_price × shares,
                                            closed: exit_price × shares)
            "realized_pnl": float,      closed 部位 (exit-entry)×shares
            "unrealized_pnl": float,    open 部位 (current-entry)×shares
            "total_pnl": float,         realized + unrealized
            "drawdown_pct": float,      total_pnl / total_invested × 100(正 = 賺,負 = 虧)
            "severity": "ok"|"warn"|"danger",
            "n_open": int,
            "n_closed": int,
        }

    空 list → 全 0,severity='ok'。
    """
    total_invested = 0.0
    realized = 0.0
    unrealized = 0.0
    current_value = 0.0
    n_open = 0
    n_closed = 0

    for p in positions or []:
        try:
            entry = float(p.get("entry_price") or 0.0)
            shares = int(p.get("shares") or 0)
        except (TypeError, ValueError):
            continue
        if entry <= 0 or shares <= 0:
            continue
        invested = entry * shares
        total_invested += invested
        side = str(p.get("side") or "long").lower()
        sign = 1.0 if side == "long" else -1.0
        is_open = int(p.get("is_open", 1) or 0)
        if is_open == 1:
            n_open += 1
            cp = p.get("current_price")
            try:
                cp_val = float(cp) if cp is not None else None
            except (TypeError, ValueError):
                cp_val = None
            if cp_val is not None and cp_val > 0:
                pnl = (cp_val - entry) * shares * sign
                unrealized += pnl
                current_value += cp_val * shares
            else:
                current_value += invested  # 沒 current_price → 視作打平
        else:
            n_closed += 1
            ep = p.get("exit_price")
            try:
                ep_val = float(ep) if ep is not None else None
            except (TypeError, ValueError):
                ep_val = None
            if ep_val is not None and ep_val > 0:
                pnl = (ep_val - entry) * shares * sign
                realized += pnl
                current_value += ep_val * shares
            else:
                current_value += invested

    total_pnl = realized + unrealized
    dd = (total_pnl / total_invested * 100.0) if total_invested > 0 else 0.0

    # severity:dd 為「正 = 賺,負 = 虧」,所以警報看 -dd
    loss_pct = -dd  # 損失百分比(正值 = 虧錢)
    if loss_pct >= DRAWDOWN_DANGER_PCT:
        severity = "danger"
    elif loss_pct >= DRAWDOWN_WARN_PCT:
        severity = "warn"
    else:
        severity = "ok"

    return {
        "total_invested": float(total_invested),
        "current_value": float(current_value),
        "realized_pnl": float(realized),
        "unrealized_pnl": float(unrealized),
        "total_pnl": float(total_pnl),
        "drawdown_pct": float(dd),
        "severity": severity,
        "n_open": int(n_open),
        "n_closed": int(n_closed),
    }


def check_single_concentration(
    positions: list[dict],
    max_single_pct: float = 0.20,
) -> list[dict]:
    """檢查單檔部位占比超標(預設 > 20%)。

    回超標 dict list:[{sid, position_pct, invested, total_invested}],
    無超標 / 空 positions → 空 list。
    """
    by_sid: dict[str, dict] = {}
    total_invested = 0.0
    for p in positions or []:
        if int(p.get("is_open", 1) or 0) != 1:
            continue
        try:
            entry = float(p.get("entry_price") or 0.0)
            shares = int(p.get("shares") or 0)
        except (TypeError, ValueError):
            continue
        if entry <= 0 or shares <= 0:
            continue
        sid = str(p.get("stock_id") or p.get("sid") or "").strip()
        if not sid:
            continue
        invested = entry * shares
        total_invested += invested
        slot = by_sid.setdefault(sid, {"sid": sid, "invested": 0.0})
        slot["invested"] += invested

    if total_invested <= 0:
        return []

    over: list[dict] = []
    for sid, slot in by_sid.items():
        pct = slot["invested"] / total_invested
        if pct > max_single_pct:
            over.append({
                "sid": sid,
                "position_pct": float(pct),
                "invested": float(slot["invested"]),
                "total_invested": float(total_invested),
                "max_single_pct": float(max_single_pct),
            })
    over.sort(key=lambda r: -r["position_pct"])
    return over


__all__ = [
    "is_enabled",
    "compute_atr_stop_loss",
    "compute_atr_take_profit",
    "compute_support_resistance",
    "should_take_profit",
    "should_stop_loss",
    "drawdown_pct",
    "check_single_concentration",
    "DRAWDOWN_WARN_PCT",
    "DRAWDOWN_DANGER_PCT",
]
