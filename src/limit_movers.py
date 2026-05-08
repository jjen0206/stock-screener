"""熱門股 / 漲停股 / 跌停反轉股(逃命波)三個首頁 widget 的資料源。

設計:
- **熱門股**:當日 trading_money(成交金額)Top N(default 30)。
  trading_money 是 daily_prices 既有欄位 → 直接 ORDER BY DESC,不用算 close*volume。
- **漲停股**:當日 ret_1d ≥ +9.95%(允許小數誤差 / 含跳空後鎖死也算)。
- **跌停反轉股(主公命名「跌停反轉」)**:當日 ret_1d ≤ -9.95% AND
  前 N=5 個交易日內有 ≥ 1 日 ret ≥ +9.95% → 飆完反轉的逃命波警訊。

所有查詢純走 SQLite daily_prices,不打 FinMind / yfinance。回傳 schema 對齊
watchlist 表格(編號/名稱/目前股價/漲幅/備註欄,備註欄各情境內容不同)。

target_date 預設 = MAX(date) — 推播 / 平日跑時拿到「最新交易日」資料,
週末 / 假日不會撞 today 沒資料的問題。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from src import database as db


# 漲停 / 跌停閾值(允許小數誤差,例 9.95% 而非剛好 10%)
_LIMIT_THRESHOLD = 0.0995


def _get_latest_trade_date(db_path: str | None = None) -> str | None:
    """取 daily_prices 最大 date(系統最新交易日)。"""
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM daily_prices"
        ).fetchone()
    return row["d"] if row and row["d"] else None


def _get_prev_trade_date(
    target_date: str, db_path: str | None = None,
) -> str | None:
    """target_date 前一個有資料的交易日(算 ret_1d 用)。"""
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM daily_prices WHERE date < ?",
            (target_date,),
        ).fetchone()
    return row["d"] if row and row["d"] else None


def _names_for(
    sids: list[str], db_path: str | None = None,
) -> dict[str, str]:
    """bulk lookup stock_id → name from stocks 表;缺名 → 空字串。"""
    if not sids:
        return {}
    with db.get_conn(db_path) as conn:
        placeholders = ",".join("?" * len(sids))
        rows = conn.execute(
            f"SELECT stock_id, name FROM stocks WHERE stock_id IN ({placeholders})",
            sids,
        ).fetchall()
    return {r["stock_id"]: (r["name"] or "") for r in rows}


# === 熱門股 ===

def get_hot_stocks(
    n: int = 30,
    target_date: str | None = None,
    db_path: str | None = None,
) -> pd.DataFrame:
    """當日 trading_money Top N。

    回 DataFrame:編號 / 名稱 / 目前股價 / 漲幅 / 成交金額(億)。
    target_date None → 系統最新交易日。沒資料 → 空 DataFrame。
    """
    if target_date is None:
        target_date = _get_latest_trade_date(db_path)
    if not target_date:
        return pd.DataFrame(columns=[
            "編號", "名稱", "目前股價", "漲幅", "成交金額(億)",
        ])

    prev_date = _get_prev_trade_date(target_date, db_path)
    with db.get_conn(db_path) as conn:
        if prev_date:
            rows = conn.execute(
                """
                SELECT t.stock_id AS sid,
                       t.close    AS close,
                       p.close    AS prev_close,
                       t.trading_money AS tm
                FROM daily_prices t
                LEFT JOIN daily_prices p
                  ON p.stock_id = t.stock_id AND p.date = ?
                WHERE t.date = ?
                  AND t.stock_id != 'TAIEX'
                  AND t.trading_money IS NOT NULL
                  AND t.trading_money > 0
                ORDER BY t.trading_money DESC
                LIMIT ?
                """,
                (prev_date, target_date, n),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT stock_id AS sid, close AS close,
                       NULL AS prev_close, trading_money AS tm
                FROM daily_prices
                WHERE date = ? AND trading_money IS NOT NULL AND trading_money > 0
                ORDER BY trading_money DESC
                LIMIT ?
                """,
                (target_date, n),
            ).fetchall()

    if not rows:
        return pd.DataFrame(columns=[
            "編號", "名稱", "目前股價", "漲幅", "成交金額(億)",
        ])

    sids = [r["sid"] for r in rows]
    name_map = _names_for(sids, db_path)
    out: list[dict[str, Any]] = []
    for r in rows:
        sid = r["sid"]
        close = float(r["close"]) if r["close"] is not None else None
        prev = float(r["prev_close"]) if r["prev_close"] is not None else None
        chg = (close - prev) / prev * 100 if (close and prev and prev > 0) else None
        # trading_money 單位「元」 → 顯示成「億」
        tm_yi = float(r["tm"]) / 1e8 if r["tm"] else None
        out.append({
            "編號": sid,
            "名稱": name_map.get(sid, ""),
            "目前股價": close,
            "漲幅": chg,
            "成交金額(億)": tm_yi,
        })
    return pd.DataFrame(out)


# === 漲停股 ===

def get_limit_up(
    target_date: str | None = None,
    db_path: str | None = None,
) -> pd.DataFrame:
    """當日 ret_1d ≥ +9.95% 的股票。

    回 DataFrame:編號 / 名稱 / 目前股價 / 漲幅 / 成交金額(億)。
    """
    if target_date is None:
        target_date = _get_latest_trade_date(db_path)
    if not target_date:
        return pd.DataFrame(columns=[
            "編號", "名稱", "目前股價", "漲幅", "成交金額(億)",
        ])
    prev_date = _get_prev_trade_date(target_date, db_path)
    if not prev_date:
        # 第一日沒前一日 ret 算不出
        return pd.DataFrame(columns=[
            "編號", "名稱", "目前股價", "漲幅", "成交金額(億)",
        ])

    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT t.stock_id AS sid,
                   t.close    AS close,
                   p.close    AS prev_close,
                   t.trading_money AS tm
            FROM daily_prices t
            JOIN daily_prices p
              ON p.stock_id = t.stock_id AND p.date = ?
            WHERE t.date = ?
              AND t.stock_id != 'TAIEX'
              AND p.close > 0
              AND (t.close - p.close) / p.close >= ?
            ORDER BY (t.close - p.close) / p.close DESC, t.trading_money DESC
            """,
            (prev_date, target_date, _LIMIT_THRESHOLD),
        ).fetchall()

    if not rows:
        return pd.DataFrame(columns=[
            "編號", "名稱", "目前股價", "漲幅", "成交金額(億)",
        ])

    sids = [r["sid"] for r in rows]
    name_map = _names_for(sids, db_path)
    out: list[dict[str, Any]] = []
    for r in rows:
        sid = r["sid"]
        close = float(r["close"])
        prev = float(r["prev_close"])
        chg = (close - prev) / prev * 100
        tm_yi = float(r["tm"]) / 1e8 if r["tm"] else None
        out.append({
            "編號": sid,
            "名稱": name_map.get(sid, ""),
            "目前股價": close,
            "漲幅": chg,
            "成交金額(億)": tm_yi,
        })
    return pd.DataFrame(out)


# === 跌停反轉股 ===

def get_limit_down_after_up(
    window: int = 5,
    target_date: str | None = None,
    db_path: str | None = None,
) -> pd.DataFrame:
    """當日 ret_1d ≤ -9.95% AND 前 window 個交易日內 ≥ 1 日 ret ≥ +9.95%。

    語意「飆完反轉」/ 逃命波警訊。回 DataFrame:
    編號 / 名稱 / 目前股價 / 漲幅 / 前 N 日漲停日。

    window 默認 5(主公拍板),可調。target_date None → 系統最新交易日。
    """
    if target_date is None:
        target_date = _get_latest_trade_date(db_path)
    if not target_date:
        return pd.DataFrame(columns=[
            "編號", "名稱", "目前股價", "漲幅", "前 N 日漲停日",
        ])
    prev_date = _get_prev_trade_date(target_date, db_path)
    if not prev_date:
        return pd.DataFrame(columns=[
            "編號", "名稱", "目前股價", "漲幅", "前 N 日漲停日",
        ])

    # 先撈當日跌停 ≤ -9.95% 的清單
    with db.get_conn(db_path) as conn:
        td_rows = conn.execute(
            """
            SELECT t.stock_id AS sid, t.close AS close, p.close AS prev_close
            FROM daily_prices t
            JOIN daily_prices p
              ON p.stock_id = t.stock_id AND p.date = ?
            WHERE t.date = ?
              AND t.stock_id != 'TAIEX'
              AND p.close > 0
              AND (t.close - p.close) / p.close <= ?
            """,
            (prev_date, target_date, -_LIMIT_THRESHOLD),
        ).fetchall()

    if not td_rows:
        return pd.DataFrame(columns=[
            "編號", "名稱", "目前股價", "漲幅", "前 N 日漲停日",
        ])

    # 對每檔掃前 window 個交易日(嚴格 < target_date)— 找 ≥ +9.95% 漲停日
    # 用單次 SQL with 滾動 prev_close JOIN 太複雜,改用 Python 逐檔 query
    # (跌停股當日通常 < 50 檔,N×window query 量可接受)
    out: list[dict[str, Any]] = []
    sids = [r["sid"] for r in td_rows]
    name_map = _names_for(sids, db_path)

    with db.get_conn(db_path) as conn:
        for r in td_rows:
            sid = r["sid"]
            # 取該 sid 在 target_date 前的 window+1 個交易日 close(算 ret 用)
            # +1 是為了 LAG 算前一日 ret
            recent = conn.execute(
                """
                SELECT date, close FROM daily_prices
                WHERE stock_id = ? AND date < ?
                ORDER BY date DESC
                LIMIT ?
                """,
                (sid, target_date, window + 1),
            ).fetchall()
            if len(recent) < 2:
                continue
            # recent[0] = 前一日, recent[1..N] = 更早。算各日 ret = (close[i-1] - close[i]) / close[i]
            limit_up_dates: list[str] = []
            for i in range(len(recent) - 1):
                close_i = recent[i]["close"]
                close_prev = recent[i + 1]["close"]
                if close_prev and close_prev > 0:
                    ret = (close_i - close_prev) / close_prev
                    if ret >= _LIMIT_THRESHOLD:
                        limit_up_dates.append(recent[i]["date"])
            if not limit_up_dates:
                continue
            close = float(r["close"])
            prev = float(r["prev_close"])
            chg = (close - prev) / prev * 100
            out.append({
                "編號": sid,
                "名稱": name_map.get(sid, ""),
                "目前股價": close,
                "漲幅": chg,
                "前 N 日漲停日": ",".join(limit_up_dates[:3]),
            })

    if not out:
        return pd.DataFrame(columns=[
            "編號", "名稱", "目前股價", "漲幅", "前 N 日漲停日",
        ])
    return pd.DataFrame(out).sort_values("漲幅", ascending=True).reset_index(drop=True)


__all__ = [
    "get_hot_stocks",
    "get_limit_up",
    "get_limit_down_after_up",
]
