"""
簡易回測模組(短線策略)。

設計範圍:
- 對「歷史每個交易日跑一次選股,入選後持有 N 天平倉」的最小可行版本
- **不**含交易成本、滑價、資金管理、停損停利;只看單一參數的歷史表現
- 不做最佳化參數搜尋(避免 overfitting)

主要函式:
- backtest_short(start, end, params, hold_days, universe, on_progress) -> dict
    回傳 {summary: {...}, trades: pd.DataFrame, equity_curve: pd.Series}

統計:
- 總報酬:複利((1 + r1) × (1 + r2) × ... − 1)
- 夏普比率:簡化版 mean / std × √252(以「每筆交易報酬」當樣本,非真實年化)

特殊狀況:
- 持有期間遇停牌(資料缺日)→ 跳到下一個有資料的交易日
- 取不到 hold_days 個交易日(個股延展不足或期間結尾)→ 強制取最後一筆當平倉
- 整個 universe 該日都查不到平倉資料 → 該筆交易不計入
"""
from __future__ import annotations

import math
from typing import Callable

import pandas as pd

from src import database as db
from src.screener_short import screen_short
from src.universe import TW_TOP_50


ProgressCallback = Callable[[int, int, str], None]


# 持有 hold_days 個交易日找不到資料時,最多再寬限這麼多個交易日(主公規格 7 個)
_HOLD_BUFFER_DAYS = 7


def backtest_short(
    start_date: str,
    end_date: str,
    params: dict | None = None,
    hold_days: int = 5,
    universe: list[tuple[str, str]] | None = None,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """跑短線策略歷史回測。

    參數:
        start_date, end_date: 'YYYY-MM-DD' 回測區間
        params: 覆蓋短線預設參數
        hold_days: 持有交易日數(預設 5)
        universe: [(stock_id, name), ...] 限縮個股範圍;None 用 TW_TOP_50
        on_progress: callback(idx_1based, total, current_date)

    回傳 dict:
        summary       : 總報酬、勝率、夏普…等(預設值見 _empty_summary)
        trades        : DataFrame[buy_date, stock_id, name, buy_price,
                                   sell_date, sell_price, return_pct]
        equity_curve  : Series indexed by date,值為「累積報酬 %」(複利)
    """
    db.init_db()
    if universe is None:
        universe = TW_TOP_50
    if not universe:
        return _empty_result()

    # 確保 universe 個股在 stocks 表(screen_short 從 stocks 表掃)
    db.upsert_stocks([
        {"stock_id": sid, "name": name, "market": "TW"}
        for sid, name in universe
    ])

    sids = [s for s, _ in universe]
    trading_days = _get_trading_days(start_date, end_date, sids)
    if not trading_days:
        return _empty_result()

    trades: list[dict] = []
    n = len(trading_days)
    for i, d in enumerate(trading_days):
        if on_progress is not None:
            try:
                on_progress(i + 1, n, d)
            except Exception:  # noqa: BLE001
                pass
        try:
            picked = screen_short(d, params=params, stock_ids=sids)
        except Exception:  # noqa: BLE001 — 單日失敗不中斷整個回測
            continue
        if picked.empty:
            continue
        for _, row in picked.iterrows():
            sid = str(row["stock_id"])
            buy_price = float(row["close"])
            sell_d, sell_price = _find_sell(sid, d, hold_days)
            if sell_d is None or buy_price <= 0:
                continue
            trades.append({
                "buy_date": d,
                "stock_id": sid,
                "name": row.get("name", sid),
                "buy_price": buy_price,
                "sell_date": sell_d,
                "sell_price": sell_price,
                "return_pct": (sell_price - buy_price) / buy_price * 100.0,
            })

    trades_df = pd.DataFrame(
        trades,
        columns=[
            "buy_date", "stock_id", "name", "buy_price",
            "sell_date", "sell_price", "return_pct",
        ],
    )

    return {
        "summary": _compute_summary(trades_df),
        "trades": trades_df,
        "equity_curve": _compute_equity_curve(trades_df, trading_days),
    }


# === 內部工具 ===

def _get_trading_days(start: str, end: str, stock_ids: list[str]) -> list[str]:
    """取 [start, end] 區間內,universe 內任一股有交易的所有日期(升序)。"""
    if not stock_ids:
        return []
    placeholders = ",".join(["?"] * len(stock_ids))
    sql = (
        f"SELECT DISTINCT date FROM daily_prices "
        f"WHERE stock_id IN ({placeholders}) AND date BETWEEN ? AND ? "
        f"ORDER BY date"
    )
    with db.get_conn() as conn:
        rows = conn.execute(sql, (*stock_ids, start, end)).fetchall()
    return [r["date"] for r in rows]


def _find_sell(
    stock_id: str,
    buy_date: str,
    hold_days: int,
) -> tuple[str | None, float | None]:
    """找買進日後第 hold_days 個交易日的收盤當賣出價。

    - 跳過停牌(資料缺日 → 自動取下一個有資料的)
    - 取不到 hold_days 個但有資料 → 強制取最後一筆當平倉(寬限 _HOLD_BUFFER_DAYS)
    - 完全沒下一個交易日 → 回 (None, None)
    """
    limit = hold_days + _HOLD_BUFFER_DAYS
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT date, close FROM daily_prices "
            "WHERE stock_id=? AND date>? "
            "ORDER BY date LIMIT ?",
            (stock_id, buy_date, limit),
        ).fetchall()
    if not rows:
        return None, None
    if len(rows) >= hold_days:
        target = rows[hold_days - 1]
    else:
        target = rows[-1]
    return target["date"], float(target["close"])


def _empty_summary() -> dict:
    return {
        "trades": 0,
        "win_rate": 0.0,
        "avg_return": 0.0,
        "total_return": 0.0,
        "max_win": 0.0,
        "max_loss": 0.0,
        "sharpe": 0.0,
    }


def _empty_result() -> dict:
    return {
        "summary": _empty_summary(),
        "trades": pd.DataFrame(
            columns=[
                "buy_date", "stock_id", "name", "buy_price",
                "sell_date", "sell_price", "return_pct",
            ]
        ),
        "equity_curve": pd.Series(dtype=float),
    }


def _compute_summary(trades_df: pd.DataFrame) -> dict:
    """從交易明細算統計。"""
    if trades_df.empty:
        return _empty_summary()
    returns = trades_df["return_pct"]
    n = len(returns)
    wins = int((returns > 0).sum())
    win_rate = wins / n * 100.0
    avg_return = float(returns.mean())
    total_return = float(((1 + returns / 100.0).prod() - 1) * 100.0)
    max_win = float(returns.max())
    max_loss = float(returns.min())
    std = float(returns.std()) if n > 1 else 0.0
    sharpe = (returns.mean() / std) * math.sqrt(252) if std > 0 else 0.0
    return {
        "trades": n,
        "win_rate": float(win_rate),
        "avg_return": avg_return,
        "total_return": total_return,
        "max_win": max_win,
        "max_loss": max_loss,
        "sharpe": float(sharpe),
    }


def _compute_equity_curve(
    trades_df: pd.DataFrame,
    trading_days: list[str],
) -> pd.Series:
    """日累積報酬(複利,百分比)。

    當天的累積報酬 = 截至當天所有 sell_date ≤ 當天 的交易連乘後 − 1。
    每筆交易視為「全部資金壓進去」(沒做資金分配),純粹看策略複利潛力。
    """
    if trades_df.empty:
        return pd.Series(0.0, index=trading_days)
    sorted_trades = trades_df.sort_values("sell_date").reset_index(drop=True)
    curve: list[float] = []
    cumulative = 1.0
    settled = 0
    n_trades = len(sorted_trades)
    for d in trading_days:
        while settled < n_trades and sorted_trades.iloc[settled]["sell_date"] <= d:
            r = sorted_trades.iloc[settled]["return_pct"] / 100.0
            cumulative *= 1 + r
            settled += 1
        curve.append((cumulative - 1) * 100.0)
    return pd.Series(curve, index=trading_days)


__all__ = ["backtest_short"]
