"""D 績效分析 — 策略組合回測 + 命中相關性(2026-05-17 加)。

跟既有 `src/backtester.py` 區隔:
- backtester.backtest_short:跑 screener_short() 重算當日 signals,需要完整
  institutional/daily_prices 資料,慢且窄。
- strategy_backtest(本檔):**直接拿 daily_picks 歷史命中**(nightly precompute
  已寫表),算每筆 holding_days 後實際報酬;支援多策略**交集 / 聯集**。

Kill-switch:跟 performance_analysis 共用 `PERFORMANCE_ENABLED`。

主要 API:
- `backtest_combination(conn, strategies, start_date, end_date, holding_days=5,
   mode='union')` — 多策略組合回測。
- `compute_strategy_correlation(conn, strategies, days=180)` — 同期同 sid 命中
  pair frequency(歸一化 0~1)。
"""
from __future__ import annotations

import logging
import math
import sqlite3
from datetime import date as _date, datetime, timedelta
from typing import Any, Sequence

import pandas as pd

from src.performance_analysis import is_enabled

logger = logging.getLogger(__name__)


# === helpers ===


def _empty_result(strategies: Sequence[str], mode: str, holding_days: int) -> dict[str, Any]:
    return {
        "strategies": list(strategies),
        "mode": mode,
        "holding_days": int(holding_days),
        "n_trades": 0,
        "win_rate": None,
        "avg_return_pct": None,
        "total_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "sharpe": None,
        "trades": [],
    }


def _resolve_exit_price(
    conn: sqlite3.Connection,
    sid: str,
    pick_date: str,
    holding_days: int,
) -> tuple[float | None, str | None]:
    """從 daily_prices 拿 entry 日後第 holding_days 個交易日的收盤。

    daily_prices 只記交易日,所以用 ORDER BY date LIMIT 抓「之後第 N+1 筆」
    (idx 0 是 entry,idx holding_days 是出場)。任一缺資料 → (None, None)。

    Returns (exit_price, exit_date_iso)。
    """
    try:
        rows = conn.execute(
            "SELECT date, close FROM daily_prices "
            "WHERE stock_id=? AND date >= ? "
            "ORDER BY date ASC LIMIT ?",
            (sid, pick_date, int(holding_days) + 1),
        ).fetchall()
    except sqlite3.OperationalError:
        return None, None
    if len(rows) < int(holding_days) + 1:
        return None, None
    last = rows[int(holding_days)]
    if last["close"] is None:
        return None, None
    try:
        return float(last["close"]), str(last["date"])
    except (TypeError, ValueError):
        return None, None


def _entry_price(
    conn: sqlite3.Connection,
    sid: str,
    pick_date: str,
) -> float | None:
    """拿 pick_date 當天 daily_prices.close 當 entry(>= pick_date 第一筆)。"""
    try:
        row = conn.execute(
            "SELECT close FROM daily_prices "
            "WHERE stock_id=? AND date >= ? "
            "ORDER BY date ASC LIMIT 1",
            (sid, pick_date),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or row["close"] is None:
        return None
    try:
        return float(row["close"])
    except (TypeError, ValueError):
        return None


# === 1. 多策略組合回測 ===

def backtest_combination(
    conn: sqlite3.Connection,
    strategies: Sequence[str],
    start_date: str,
    end_date: str,
    holding_days: int = 5,
    mode: str = "union",
) -> dict[str, Any]:
    """對選定策略組合從 daily_picks 撈歷史推薦,算 holding_days 後實際報酬。

    Args:
        conn: SQLite。
        strategies: 策略 key list(對應 STRATEGY_LABELS)。
        start_date / end_date: 'YYYY-MM-DD',濾 pick_date 區間(含)。
        holding_days: 持有 N 個**交易日**後出場(預設 5)。
        mode: 'union' = 任一策略命中 / 'intersect' = 所有策略同日同 sid 都命中。

    Returns:
        {strategies, mode, holding_days, n_trades, win_rate,
         avg_return_pct, total_return_pct, max_drawdown_pct, sharpe, trades:[...]}

        kill-switch off / 無 strategies / mode 錯 → 空 result。
    """
    if not is_enabled():
        return _empty_result(strategies, mode, holding_days)
    if not strategies:
        return _empty_result(strategies, mode, holding_days)
    mode = mode.lower().strip()
    if mode not in ("union", "intersect"):
        raise ValueError(f"mode 必須 'union' 或 'intersect',got {mode!r}")
    if int(holding_days) < 1:
        raise ValueError(f"holding_days 必須 >= 1,got {holding_days}")

    # 撈所有命中 row
    placeholders = ",".join("?" * len(strategies))
    sql = (
        f"SELECT trade_date, sid, strategy FROM daily_picks "
        f"WHERE strategy IN ({placeholders}) "
        f"AND trade_date BETWEEN ? AND ? "
        f"ORDER BY trade_date ASC, sid ASC"
    )
    args = list(strategies) + [start_date, end_date]
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("backtest_combination: daily_picks 表異常: %s", e)
        return _empty_result(strategies, mode, holding_days)

    # 同 (date, sid) 多策略合併
    hits: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        key = (str(r["trade_date"]), str(r["sid"]))
        hits.setdefault(key, set()).add(str(r["strategy"]))

    target_set = set(strategies)
    if mode == "intersect":
        pairs = [k for k, s in hits.items() if target_set.issubset(s)]
    else:  # union
        pairs = [k for k, s in hits.items() if s & target_set]

    if not pairs:
        return _empty_result(strategies, mode, holding_days)

    trades: list[dict[str, Any]] = []
    for pick_date, sid in pairs:
        entry = _entry_price(conn, sid, pick_date)
        if entry is None or entry <= 0:
            continue
        exit_px, exit_date = _resolve_exit_price(
            conn, sid, pick_date, int(holding_days),
        )
        if exit_px is None:
            continue
        ret_pct = (exit_px - entry) / entry * 100.0
        trades.append({
            "pick_date": pick_date,
            "sid": sid,
            "entry_price": round(entry, 2),
            "exit_price": round(exit_px, 2),
            "exit_date": exit_date,
            "return_pct": round(ret_pct, 4),
            "matched": sorted(hits[(pick_date, sid)] & target_set),
        })

    n = len(trades)
    if n == 0:
        return _empty_result(strategies, mode, holding_days)

    rets = [t["return_pct"] for t in trades]
    wins = sum(1 for r in rets if r > 0)
    avg = sum(rets) / n
    # max drawdown(對 trade-by-trade cumsum,簡化版)
    cum = []
    acc = 0.0
    for r in rets:
        acc += r
        cum.append(acc)
    if cum:
        peak = cum[0]
        dd_pct = 0.0
        for v in cum:
            if v > peak:
                peak = v
            dd = v - peak  # 已是 %,直接減
            if dd < dd_pct:
                dd_pct = dd
        max_dd_pct = dd_pct
        total_ret = cum[-1]
    else:
        max_dd_pct = 0.0
        total_ret = 0.0

    # Sharpe 簡化(per-trade σ × √(252/holding_days) 對齊既有 backtester)
    sharpe: float | None = None
    if n >= 2:
        mean = avg
        variance = sum((r - mean) ** 2 for r in rets) / (n - 1)
        sd = math.sqrt(variance) if variance > 0 else 0.0
        if sd > 0:
            sharpe = round(mean / sd * math.sqrt(252.0 / max(1, int(holding_days))), 4)

    return {
        "strategies": list(strategies),
        "mode": mode,
        "holding_days": int(holding_days),
        "n_trades": n,
        "win_rate": round(wins / n, 4),
        "avg_return_pct": round(avg, 4),
        "total_return_pct": round(total_ret, 4),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "sharpe": sharpe,
        "trades": trades,
    }


# === 2. 策略命中相關性 ===

def compute_strategy_correlation(
    conn: sqlite3.Connection,
    strategies: Sequence[str] | None = None,
    days: int = 180,
) -> pd.DataFrame:
    """看 strategies 兩兩在同 (date, sid) 命中的相關性 — 給「策略是否重疊」
    的視覺化用。

    相關係數定義(簡化版):
        corr(A, B) = |A ∩ B| / |A ∪ B|       (Jaccard,介於 0~1)
    集合內元素為 (trade_date, sid) tuple。對角線 = 1.0。

    Args:
        strategies: 策略 key list;None → 用 daily_picks 表內全部 strategy。
        days: 從今往前 N 日(預設 180)。

    Returns:
        DataFrame index=columns=strategies,值 0~1。
        無資料 → 空 df(空 strategy 集合)。

    kill-switch off → 空 df。
    """
    if not is_enabled():
        return pd.DataFrame()

    end_date = _date.today().isoformat()
    start_date = (_date.today() - timedelta(days=int(days))).isoformat()

    if not strategies:
        try:
            rows = conn.execute(
                "SELECT DISTINCT strategy FROM daily_picks "
                "WHERE trade_date BETWEEN ? AND ?",
                (start_date, end_date),
            ).fetchall()
        except sqlite3.OperationalError:
            return pd.DataFrame()
        strategies = sorted({r["strategy"] for r in rows if r["strategy"]})

    strategies = list(strategies)
    if not strategies:
        return pd.DataFrame()

    # 對每 strategy 拿 (date, sid) 集合
    sets: dict[str, set[tuple[str, str]]] = {}
    for s in strategies:
        try:
            rows = conn.execute(
                "SELECT trade_date, sid FROM daily_picks "
                "WHERE strategy=? AND trade_date BETWEEN ? AND ?",
                (s, start_date, end_date),
            ).fetchall()
        except sqlite3.OperationalError:
            sets[s] = set()
            continue
        sets[s] = {(str(r["trade_date"]), str(r["sid"])) for r in rows}

    n = len(strategies)
    matrix = [[0.0] * n for _ in range(n)]
    for i, a in enumerate(strategies):
        for j, b in enumerate(strategies):
            if i == j:
                matrix[i][j] = 1.0
                continue
            sa, sb = sets[a], sets[b]
            union = sa | sb
            if not union:
                matrix[i][j] = 0.0
            else:
                matrix[i][j] = round(len(sa & sb) / len(union), 4)
    return pd.DataFrame(matrix, index=strategies, columns=strategies)


__all__ = [
    "backtest_combination",
    "compute_strategy_correlation",
]
