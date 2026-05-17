"""D 績效分析 — 真實交易 log + 策略歸因 + drawdown(2026-05-17 加)。

跟既有「📈 簡易回測」/「🧪 實測追蹤」區隔:
- backtester.py     → 歷史 N 日策略勝率(N=126 預設),純 historical 回測。
- paper_trading.py  → 系統自動 seed 的 paper trade,驗 ML 過濾效果。
- performance_analysis.py(本檔)→ **主公真實平倉 user_positions 的損益**,
  歸因到當時推薦的策略,讓主公看自己真實表現好不好 + 哪個策略最會賺。

Kill-switch:env `PERFORMANCE_ENABLED=true`(預設 on)。off 時所有 compute_*
回空 df / dict,Streamlit page 走 warning 不擋畫面。

主要 API:
- `is_enabled() -> bool`
- `compute_user_pnl(conn, start_date, end_date)`              — 每筆已平倉 P&L df
- `compute_user_win_rate(conn, window_days=30)`               — 真實勝率(滾動窗)
- `compute_attribution(conn, start_date)`                     — 策略歸因 dict
- `compute_drawdown_curve(conn)`                              — equity / drawdown 時序

所有函式只 read,不寫表(report layer)。caller 自管 conn 生命週期。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date as _date, datetime, timedelta
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """讀 env PERFORMANCE_ENABLED(預設 true)。"""
    raw = os.getenv("PERFORMANCE_ENABLED", "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def _today_iso() -> str:
    return _date.today().isoformat()


def _empty_pnl_df() -> pd.DataFrame:
    """空 df schema(讓 caller 不用判 None)。"""
    return pd.DataFrame(columns=[
        "position_id", "date", "sid", "entry_date", "exit_date",
        "entry_price", "exit_price", "shares", "side",
        "pnl", "pnl_pct", "holding_days",
    ])


# === 1. user_positions P&L(已平倉) ===

def compute_user_pnl(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """從 user_positions 已平倉(is_open=0)算每筆實際 P&L。

    Args:
        conn: SQLite connection。
        start_date / end_date: 'YYYY-MM-DD',濾 exit_date 區間(含)。None 不濾。

    Returns:
        DataFrame columns:
            position_id, date(= exit_date), sid, entry_date, exit_date,
            entry_price, exit_price, shares, side,
            pnl(NTD), pnl_pct(%), holding_days

        kill-switch off 或無 closed 部位 → 空 df。
    """
    if not is_enabled():
        return _empty_pnl_df()

    where = ["is_open=0", "exit_date IS NOT NULL", "exit_price IS NOT NULL"]
    args: list[Any] = []
    if start_date:
        where.append("exit_date >= ?")
        args.append(start_date)
    if end_date:
        where.append("exit_date <= ?")
        args.append(end_date)

    sql = (
        "SELECT id, stock_id, entry_date, exit_date, entry_price, exit_price, "
        "shares, side FROM user_positions WHERE "
        + " AND ".join(where)
        + " ORDER BY exit_date ASC, id ASC"
    )
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("compute_user_pnl: 表不存在或 schema 異常: %s", e)
        return _empty_pnl_df()

    if not rows:
        return _empty_pnl_df()

    records: list[dict[str, Any]] = []
    for r in rows:
        rec = dict(r)
        side = (rec.get("side") or "long").lower()
        sign = 1.0 if side == "long" else -1.0
        try:
            entry = float(rec["entry_price"])
            exit_px = float(rec["exit_price"])
            shares = int(rec["shares"])
        except (TypeError, ValueError):
            continue
        if entry <= 0 or exit_px <= 0 or shares <= 0:
            continue
        pnl = (exit_px - entry) * shares * sign
        pnl_pct = (exit_px - entry) / entry * 100.0 * sign
        holding = _holding_days(rec.get("entry_date"), rec.get("exit_date"))
        records.append({
            "position_id": int(rec["id"]),
            "date": rec["exit_date"],
            "sid": rec["stock_id"],
            "entry_date": rec["entry_date"],
            "exit_date": rec["exit_date"],
            "entry_price": entry,
            "exit_price": exit_px,
            "shares": shares,
            "side": side,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "holding_days": holding,
        })

    if not records:
        return _empty_pnl_df()
    return pd.DataFrame.from_records(records)


def _holding_days(entry_iso: str | None, exit_iso: str | None) -> int | None:
    """算進場 → 出場日曆天差(整數)。任一 None 或解析失敗 → None。"""
    if not entry_iso or not exit_iso:
        return None
    try:
        d_in = datetime.strptime(str(entry_iso)[:10], "%Y-%m-%d").date()
        d_out = datetime.strptime(str(exit_iso)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (d_out - d_in).days


# === 2. 真實勝率(滾動窗) ===

def compute_user_win_rate(
    conn: sqlite3.Connection,
    window_days: int = 30,
) -> dict[str, Any]:
    """主公真實平倉的勝率(window_days 滾動窗,基於 exit_date)。

    跟 pick_outcomes 的「系統推薦勝率」分開 — 這裡是主公**真的買賣**結果。

    Returns:
        {
          "window_days": int,
          "n_trades": int,
          "n_wins": int,
          "win_rate": float | None,    # 0~1,n=0 → None
          "avg_pnl": float | None,     # 每筆平均 NTD
          "avg_return_pct": float | None,
          "total_pnl": float,
          "start_date": str | None,
          "end_date": str | None,
        }
    """
    end_date = _today_iso()
    start_date = (
        (_date.today() - timedelta(days=int(window_days))).isoformat()
        if window_days > 0 else None
    )

    df = compute_user_pnl(conn, start_date=start_date, end_date=end_date)
    n = int(len(df))
    if n == 0:
        return {
            "window_days": int(window_days),
            "n_trades": 0,
            "n_wins": 0,
            "win_rate": None,
            "avg_pnl": None,
            "avg_return_pct": None,
            "total_pnl": 0.0,
            "start_date": start_date,
            "end_date": end_date,
        }
    wins = int((df["pnl"] > 0).sum())
    return {
        "window_days": int(window_days),
        "n_trades": n,
        "n_wins": wins,
        "win_rate": round(wins / n, 4),
        "avg_pnl": round(float(df["pnl"].mean()), 2),
        "avg_return_pct": round(float(df["pnl_pct"].mean()), 4),
        "total_pnl": round(float(df["pnl"].sum()), 2),
        "start_date": start_date,
        "end_date": end_date,
    }


# === 3. 策略歸因 ===

def compute_attribution(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    window_days_before: int = 5,
    window_days_after: int = 5,
) -> dict[str, dict[str, Any]]:
    """對每筆已平倉 user_position 找對應 daily_picks(同 sid,entry_date ± window)
    歸因到觸發策略 → 加總每策略貢獻。

    一筆部位若命中 N 個策略 → 平均分配(P&L / N),避免雙計。

    Args:
        conn: SQLite。
        start_date: 'YYYY-MM-DD',濾 exit_date 起點。None 不濾。
        window_days_before / after: 從 entry_date 往前 / 後幾日找 daily_picks
            (預設 ± 5 日)。

    Returns:
        dict[strategy_key, {
            "total_pnl": float (NTD),
            "count": int,                # 該策略相關的部位數
            "n_wins": int,
            "win_rate": float | None,
            "avg_return_pct": float | None,
            "avg_pnl": float | None,
        }]

        無資料 → {}。kill-switch off → {}。
    """
    if not is_enabled():
        return {}

    df = compute_user_pnl(conn, start_date=start_date)
    if df.empty:
        return {}

    attribution: dict[str, dict[str, Any]] = {}

    for _, row in df.iterrows():
        sid = row["sid"]
        entry_iso = row["entry_date"]
        pnl = float(row["pnl"])
        pnl_pct = float(row["pnl_pct"])
        if not entry_iso:
            continue
        # 找該 sid entry_date ± window 內所有 daily_picks distinct strategy
        try:
            d_in = datetime.strptime(str(entry_iso)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        d_lo = (d_in - timedelta(days=int(window_days_before))).isoformat()
        d_hi = (d_in + timedelta(days=int(window_days_after))).isoformat()
        try:
            strategy_rows = conn.execute(
                "SELECT DISTINCT strategy FROM daily_picks "
                "WHERE sid=? AND trade_date BETWEEN ? AND ?",
                (str(sid), d_lo, d_hi),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        strategies = [r["strategy"] for r in strategy_rows if r["strategy"]]
        if not strategies:
            # 無歸因 → 落到 "_unknown" bucket(讓主公知道有多少筆找不到)
            strategies = ["_unknown"]
        n_strat = len(strategies)
        share_pnl = pnl / n_strat
        share_ret = pnl_pct / n_strat
        win = 1 if pnl > 0 else 0
        for s in strategies:
            d = attribution.setdefault(s, {
                "total_pnl": 0.0, "count": 0, "n_wins": 0,
                "_returns": [], "_pnls": [],
            })
            d["total_pnl"] += share_pnl
            d["count"] += 1
            d["n_wins"] += win
            d["_returns"].append(share_ret)
            d["_pnls"].append(share_pnl)

    # finalize
    for s, d in attribution.items():
        n = d["count"]
        rets = d.pop("_returns")
        pnls = d.pop("_pnls")
        d["total_pnl"] = round(d["total_pnl"], 2)
        d["win_rate"] = round(d["n_wins"] / n, 4) if n else None
        d["avg_return_pct"] = round(sum(rets) / n, 4) if rets else None
        d["avg_pnl"] = round(sum(pnls) / n, 2) if pnls else None
    return attribution


# === 4. Drawdown / equity curve ===

def compute_drawdown_curve(
    conn: sqlite3.Connection,
) -> pd.DataFrame:
    """從 user_positions 已平倉算 equity curve + drawdown 時序。

    一筆 closed position 在 exit_date 那天「實現」P&L。累積 P&L = equity。
    drawdown_pct = (equity - peak) / peak * 100(峰值不為 0 時),
    peak <= 0 時用絕對 NTD drawdown(`drawdown_abs`)。

    Returns:
        DataFrame columns: date, equity, peak, drawdown, drawdown_pct
        無資料 → 空 df(同 schema)。
    """
    empty = pd.DataFrame(columns=[
        "date", "equity", "peak", "drawdown", "drawdown_pct",
    ])
    if not is_enabled():
        return empty

    df = compute_user_pnl(conn)
    if df.empty:
        return empty

    # 按 exit_date 排序後 cumsum
    df = df.sort_values("exit_date").copy()
    daily = df.groupby("exit_date", as_index=False)["pnl"].sum()
    daily = daily.rename(columns={"exit_date": "date"})
    daily["equity"] = daily["pnl"].cumsum()
    daily["peak"] = daily["equity"].cummax()
    daily["drawdown"] = daily["equity"] - daily["peak"]

    def _dd_pct(row: pd.Series) -> float:
        peak = row["peak"]
        if peak is None or peak <= 0:
            return 0.0
        return float((row["equity"] - peak) / peak * 100.0)

    daily["drawdown_pct"] = daily.apply(_dd_pct, axis=1).round(4)
    return daily[["date", "equity", "peak", "drawdown", "drawdown_pct"]].reset_index(drop=True)


def compute_summary_metrics(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """總覽 metrics:總損益 / 勝率 / Sharpe / max drawdown / 持倉天數中位數。

    Sharpe 為簡化日 sharpe(P&L 序列):mean / std × √252。samples < 2 → None。

    Returns:
        {total_pnl, n_trades, win_rate, avg_pnl, avg_return_pct, sharpe,
         max_drawdown, max_drawdown_pct, median_holding_days}
    """
    df = compute_user_pnl(conn)
    if df.empty:
        return {
            "total_pnl": 0.0, "n_trades": 0, "win_rate": None,
            "avg_pnl": None, "avg_return_pct": None,
            "sharpe": None, "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0, "median_holding_days": None,
        }
    n = int(len(df))
    wins = int((df["pnl"] > 0).sum())
    total_pnl = float(df["pnl"].sum())
    avg_pnl = float(df["pnl"].mean())
    avg_ret = float(df["pnl_pct"].mean())
    pnl_std = float(df["pnl"].std(ddof=1)) if n >= 2 else 0.0
    sharpe = None
    if n >= 2 and pnl_std > 0:
        # 對 NTD 數列做標準 Sharpe(rf=0),× √252 年化(每日近似)
        sharpe = round(avg_pnl / pnl_std * (252 ** 0.5), 4)

    dd_df = compute_drawdown_curve(conn)
    max_dd = float(dd_df["drawdown"].min()) if not dd_df.empty else 0.0
    max_dd_pct = float(dd_df["drawdown_pct"].min()) if not dd_df.empty else 0.0
    hold_series = df["holding_days"].dropna()
    median_hold = (
        float(hold_series.median()) if not hold_series.empty else None
    )
    return {
        "total_pnl": round(total_pnl, 2),
        "n_trades": n,
        "win_rate": round(wins / n, 4) if n else None,
        "avg_pnl": round(avg_pnl, 2),
        "avg_return_pct": round(avg_ret, 4),
        "sharpe": sharpe,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "median_holding_days": median_hold,
    }


def best_strategy_by_pnl(
    attribution: dict[str, dict[str, Any]],
    min_count: int = 1,
) -> tuple[str, dict[str, Any]] | None:
    """從 attribution dict 挑 total_pnl 最高的策略(count >= min_count)。

    給軍師判讀用。沒結果 → None。"_unknown" bucket 自動排除。
    """
    candidates = [
        (k, v) for k, v in attribution.items()
        if k != "_unknown" and v.get("count", 0) >= min_count
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[1].get("total_pnl") or 0.0), reverse=True)
    return candidates[0]


__all__ = [
    "is_enabled",
    "compute_user_pnl",
    "compute_user_win_rate",
    "compute_attribution",
    "compute_drawdown_curve",
    "compute_summary_metrics",
    "best_strategy_by_pnl",
]
