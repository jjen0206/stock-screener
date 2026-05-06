"""throwaway audit:全 universe 資料新鮮度統計(交易日落差)。

對 universe 內每檔股票算「**交易日**」落差(非日曆天)— 5/1-5/3 連假本來
就沒交易,該扣掉。trading_dates = daily_prices 內所有 distinct date 集合,
落差 = sid 的 MAX(date) 在排序 trading_dates 內位置與最新位置差。

分桶:0 交易日(完整) / 1 / 2+ / 完全沒資料。
列出落差最大 TOP 30。

執行:
    python scripts/audit/diagnose_data_freshness.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.universe import pure_stock_universe  # noqa: E402


def _gap_bucket(gap_trading_days: int | None) -> str:
    """以「交易日」落差分桶(連假不算)。"""
    if gap_trading_days is None:
        return "no_data"
    if gap_trading_days == 0:
        return "fresh"
    if gap_trading_days == 1:
        return "lag_1td"
    return "lag_2td_plus"


def _gap_in_trading_days(
    sid_latest: str | None, sorted_trading_dates: list[str],
) -> int | None:
    """sid 最新日期距 sorted_trading_dates 最後一個的交易日落差。"""
    if not sid_latest:
        return None
    if not sorted_trading_dates:
        return None
    if sid_latest not in sorted_trading_dates:
        # sid 的最新日期不在「全市場 distinct trading date」集合內 — 不該發生
        return len(sorted_trading_dates)
    idx = sorted_trading_dates.index(sid_latest)
    return len(sorted_trading_dates) - 1 - idx


def _audit_table(
    table: str, universe: list[str], universe_names: dict[str, str],
    latest_market: str,
) -> None:
    """對給定 table 跑 freshness audit(交易日落差)+ print 結果。"""
    print("\n" + "=" * 80, flush=True)
    print(f"📋 {table} 資料新鮮度 audit(交易日落差)", flush=True)
    print("=" * 80, flush=True)

    with db.get_conn() as conn:
        # 全市場 distinct trading dates(該表內出現過的全部日期升冪)
        date_rows = conn.execute(
            f"SELECT DISTINCT date FROM {table} ORDER BY date ASC"
        ).fetchall()
        sorted_dates = [r["date"] for r in date_rows]

        # 一次撈全 universe MAX(date) per sid
        placeholder = ",".join("?" * len(universe))
        rows = conn.execute(
            f"SELECT stock_id, MAX(date) AS latest "
            f"FROM {table} WHERE stock_id IN ({placeholder}) "
            f"GROUP BY stock_id",
            universe,
        ).fetchall()

    sid_to_latest: dict[str, str | None] = {sid: None for sid in universe}
    for r in rows:
        sid_to_latest[r["stock_id"]] = r["latest"]

    bucket = Counter()
    rows_with_gap: list[tuple[str, str, str | None, int | None]] = []
    for sid in universe:
        latest = sid_to_latest.get(sid)
        gap = _gap_in_trading_days(latest, sorted_dates)
        bucket[_gap_bucket(gap)] += 1
        rows_with_gap.append((sid, universe_names.get(sid, ""), latest, gap))

    total = len(universe)
    table_latest = sorted_dates[-1] if sorted_dates else "—"
    print(
        f"Universe size: {total} 檔 | "
        f"{table} 內 distinct trading dates: {len(sorted_dates)} | "
        f"latest in {table}: {table_latest}",
        flush=True,
    )
    print("\n交易日落差分桶:", flush=True)
    print(
        f"  ✅ 完整(0 交易日)        {bucket['fresh']:>5d} 檔  "
        f"({bucket['fresh']/total*100:.1f}%)",
        flush=True,
    )
    print(
        f"  ⚠ 缺 1 個交易日          {bucket['lag_1td']:>5d} 檔  "
        f"({bucket['lag_1td']/total*100:.1f}%)  "
        f"← {table} 5/4 nightly fetch 漏抓的就在這桶",
        flush=True,
    )
    print(
        f"  ❌ 缺 2+ 個交易日         {bucket['lag_2td_plus']:>5d} 檔  "
        f"({bucket['lag_2td_plus']/total*100:.1f}%)  "
        f"← 長期漏 / 停牌 / 下市嫌疑",
        flush=True,
    )
    print(
        f"  ❌ 完全沒資料            {bucket['no_data']:>5d} 檔  "
        f"({bucket['no_data']/total*100:.1f}%)",
        flush=True,
    )

    # TOP 30 落差最大(no_data 算最前)
    def _sort_key(row):
        _, _, _, g = row
        return (-99999 if g is None else -g)

    rows_with_gap.sort(key=_sort_key)
    print("\nTOP 30 落差最大 / 沒資料的:", flush=True)
    print(
        f"  {'SID':<7} {'Name':<20} {'Latest':<12} {'交易日落差':<10}",
        flush=True,
    )
    print(f"  {'-' * 55}", flush=True)
    for sid, name, latest, gap in rows_with_gap[:30]:
        gap_str = "no_data" if gap is None else str(gap)
        print(
            f"  {sid:<7} {(name or '—')[:20]:<20} "
            f"{(latest or '—'):<12} {gap_str:<10}",
            flush=True,
        )


def main() -> int:
    db.init_db()
    counts = db.preload_snapshots()
    if counts:
        print(f"[AUDIT] preload: {counts}", flush=True)

    latest_market = db.get_latest_trading_date()
    if not latest_market:
        print("[AUDIT] daily_prices 表空", flush=True)
        return 1

    universe = pure_stock_universe(min_history=20)
    print(f"[AUDIT] universe size = {len(universe)}", flush=True)

    # 撈名字一次給 TOP 30 用
    with db.get_conn() as conn:
        placeholder = ",".join("?" * len(universe))
        name_rows = conn.execute(
            f"SELECT stock_id, name FROM stocks "
            f"WHERE stock_id IN ({placeholder})",
            universe,
        ).fetchall()
    universe_names = {r["stock_id"]: r["name"] or "" for r in name_rows}

    # 跑 daily_prices
    _audit_table("daily_prices", universe, universe_names, latest_market)
    # 跑 institutional
    _audit_table("institutional", universe, universe_names, latest_market)

    return 0


if __name__ == "__main__":
    sys.exit(main())
