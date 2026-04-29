"""Bulk SQL load helpers — 一次拉多檔股票歷史,避免 N 次 per-stock SELECT。

Profile 顯示:全市場(2360 檔)選股時,per-stock SELECT × 2360 = 4720 次 SQL
是雲端 SQLite IO 的主要瓶頸。改成 1 次 bulk SELECT + pandas groupby 後,
本機 0.22s → 0.04s,雲端從「感覺很慢」→ 數秒內。

設計:
- sids 數量 ≤ 500 → 用 IN clause(精準,只拉需要的)
- sids > 500 → 不用 IN(避開 SQLITE_LIMIT_VARIABLE_NUMBER 風險),
                  直接拉日期區間全部資料,在 pandas filter
- 日期上限不用 LIMIT N,改成 date BETWEEN start AND target;
  start = target − lookback_days × 2(含週末/假日緩衝)
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _td

import pandas as pd


# SQLite ≥ 3.32 預設 SQLITE_LIMIT_VARIABLE_NUMBER = 32766。Streamlit Cloud
# Python sqlite3 版本足夠,2360 檔全市場直接走 IN clause 沒問題。設保守上限
# 防超大 universe 觸發底層 SQL parser 限制。
_IN_CLAUSE_LIMIT = 10000


def _start_date(target_date: str, lookback_days: int) -> str:
    return (
        _date.fromisoformat(target_date) - _td(days=lookback_days * 2)
    ).isoformat()


def bulk_load_prices(
    conn,
    sids: list[str],
    target_date: str,
    lookback_days: int,
) -> dict[str, pd.DataFrame]:
    """一次載入多檔 daily_prices,回 dict[sid] -> DataFrame(date 升序)。

    DataFrame 欄位:stock_id, date, open, high, low, close, volume
    缺資料的 sid 不會出現在回傳 dict 中(caller 用 .get() 處理)。

    實作:單次 SQL fetchall + Python 手動 group + per-sid pandas DataFrame。
    本機 SSD 不一定贏 per-stock SELECT,但雲端 IO 慢時 N×SELECT 是主要瓶頸,
    單次 SELECT 是必勝。pandas 延後到 per-sid 構建,避免大 DataFrame 開銷。
    """
    if not sids:
        return {}
    start_date = _start_date(target_date, lookback_days)

    if len(sids) <= _IN_CLAUSE_LIMIT:
        placeholders = ",".join(["?"] * len(sids))
        cursor = conn.execute(
            f"SELECT stock_id, date, open, high, low, close, volume "
            f"FROM daily_prices "
            f"WHERE stock_id IN ({placeholders}) "
            f"AND date BETWEEN ? AND ? "
            f"ORDER BY stock_id, date",
            (*sids, start_date, target_date),
        )
    else:
        cursor = conn.execute(
            "SELECT stock_id, date, open, high, low, close, volume "
            "FROM daily_prices WHERE date BETWEEN ? AND ? "
            "ORDER BY stock_id, date",
            (start_date, target_date),
        )

    sid_set = set(sids)
    grouped: dict[str, list[dict]] = {}
    for r in cursor:
        sid = r["stock_id"]
        if sid not in sid_set:
            continue
        grouped.setdefault(sid, []).append({
            "stock_id": sid,
            "date": r["date"],
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "volume": r["volume"],
        })

    return {
        sid: pd.DataFrame(rows_list)
        for sid, rows_list in grouped.items()
    }


def bulk_load_institutional_totals(
    conn,
    sids: list[str],
    target_date: str,
    lookback_days: int,
) -> dict[str, list[dict]]:
    """一次載入多檔 institutional 法人總和,回 dict[sid] -> list[{date, total_buy_sell}]。

    list 已按 date 升序排列。screener_short 算「最近 N 日連續買超」用的是最後 N 筆。
    """
    if not sids:
        return {}
    start_date = _start_date(target_date, lookback_days)

    if len(sids) <= _IN_CLAUSE_LIMIT:
        placeholders = ",".join(["?"] * len(sids))
        rows = conn.execute(
            f"SELECT stock_id, date, total_buy_sell FROM institutional "
            f"WHERE stock_id IN ({placeholders}) "
            f"AND date BETWEEN ? AND ? "
            f"ORDER BY stock_id, date",
            (*sids, start_date, target_date),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT stock_id, date, total_buy_sell FROM institutional "
            "WHERE date BETWEEN ? AND ? "
            "ORDER BY stock_id, date",
            (start_date, target_date),
        ).fetchall()

    if not rows:
        return {}

    out: dict[str, list[dict]] = {}
    sid_set = set(sids)
    for r in rows:
        sid = r["stock_id"]
        if sid not in sid_set:
            continue
        out.setdefault(sid, []).append(
            {"date": r["date"], "total_buy_sell": r["total_buy_sell"]}
        )
    return out


__all__ = ["bulk_load_prices", "bulk_load_institutional_totals"]
