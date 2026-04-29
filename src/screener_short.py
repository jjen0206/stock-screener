"""
短線選股策略。

預設策略「量價突破 + 法人買超 + KD 黃金交叉」(三條件 AND):
  條件 A: 當日成交量 > 過去 5 日均量(不含當日) × volume_multiplier  (預設 1.5)
  條件 B: K 黃金交叉 D — 前日 K ≤ D、今日 K > D,且今日 K > kd_threshold_low (預設 20)
  條件 C: 三大法人合計連續 inst_buy_days 日買超 (total_buy_sell > 0,預設 3 日)

資料來源:
  從 SQLite (data_fetcher 預先抓進去) 讀取,**不直接打 API**。
  缺資料的個股直接跳過,結束時印一次摘要 warning。

調整參數:
  傳入 params 覆蓋 DEFAULT_SHORT_PARAMS 任何 key。UI 可直接讀此常數渲染預設值。
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from src import database as db, indicators as ind
from src._bulk_load import bulk_load_institutional_totals, bulk_load_prices


logger = logging.getLogger(__name__)


DEFAULT_SHORT_PARAMS: dict[str, Any] = {
    "volume_multiplier": 1.5,
    "kd_threshold_low": 20.0,
    "inst_buy_days": 3,
    "kd_period": 9,
    "ma_volume_period": 5,
}

# 取多少日歷史用來算 KD;9 日 KD 在 ~30 日後完全收斂,60 日綽綽有餘
_LOOKBACK_DAYS = 60

OUTPUT_COLUMNS = [
    "stock_id", "name", "close", "volume", "ma_volume_5",
    "k", "d", "inst_total_3d", "matched_at",
]


class _SkipStock(Exception):
    """資料不足,本檔股票跳過。"""


def screen_short(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """短線選股。

    參數:
        date: 'YYYY-MM-DD' — 視為「當日收盤」的選股基準日
        params: 覆蓋 DEFAULT_SHORT_PARAMS 的參數;None 用全部預設
        stock_ids: 限縮選股範圍(只跑這些股號);None = 跑 stocks 表所有 TW 股

    回傳:
        DataFrame[stock_id, name, close, volume, ma_volume_5,
                  k, d, inst_total_3d, matched_at]
        無入選時回空 DataFrame(欄位仍存在)。
    """
    p = {**DEFAULT_SHORT_PARAMS, **(params or {})}

    with db.get_conn() as conn:
        if stock_ids:
            if len(stock_ids) <= 500:
                placeholders = ",".join(["?"] * len(stock_ids))
                stocks = conn.execute(
                    f"SELECT stock_id, name FROM stocks "
                    f"WHERE market='TW' AND stock_id IN ({placeholders})",
                    stock_ids,
                ).fetchall()
            else:
                # 大 universe 不用 IN clause(避開 SQLITE_LIMIT_VARIABLE_NUMBER 風險)
                rows_meta = conn.execute(
                    "SELECT stock_id, name FROM stocks WHERE market='TW'"
                ).fetchall()
                wanted = set(stock_ids)
                stocks = [r for r in rows_meta if r["stock_id"] in wanted]
        else:
            stocks = conn.execute(
                "SELECT stock_id, name FROM stocks WHERE market='TW'"
            ).fetchall()

        if not stocks:
            logger.warning("[SCREEN_SHORT] stocks 表為空,無法選股")
            return pd.DataFrame(columns=OUTPUT_COLUMNS)

        sids = [s["stock_id"] for s in stocks]

        # **Bulk load** — 一次拉所有 sids 的歷史(避免 N 次 SELECT)
        prices_by_sid = bulk_load_prices(conn, sids, date, _LOOKBACK_DAYS)
        # institutional 只需最近 inst_buy_days 筆,但 bulk lookback 多取一點防漏
        inst_by_sid = bulk_load_institutional_totals(
            conn, sids, date, max(p["inst_buy_days"] * 3, 14),
        )

    rows: list[dict] = []
    skipped = 0
    min_price_rows = max(p["ma_volume_period"] + 1, p["kd_period"] + 1)

    for s in stocks:
        sid = s["stock_id"]
        name = s["name"]
        price_df = prices_by_sid.get(sid)
        inst_list = inst_by_sid.get(sid, [])

        if (price_df is None or len(price_df) < min_price_rows
                or len(inst_list) < p["inst_buy_days"]):
            skipped += 1
            continue

        try:
            row = _evaluate_short_from_data(
                sid, name, date, p, price_df, inst_list,
            )
        except _SkipStock:
            skipped += 1
            continue
        if row is not None:
            rows.append(row)

    if skipped:
        logger.warning(
            "[SCREEN_SHORT] %d 檔資料不足跳過(需至少 %d 日價格 + %d 筆法人)",
            skipped, min_price_rows, p["inst_buy_days"],
        )

    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def _evaluate_short_from_data(
    stock_id: str,
    name: str,
    target_date: str,
    p: dict,
    price_df: pd.DataFrame,
    inst_list: list[dict],
) -> dict | None:
    """評估單檔(資料已 bulk-loaded)。回 dict=入選 / None=未過 / raise _SkipStock=資料不足。

    price_df: 日線歷史(date 升序),最後一筆必須是 target_date。
    inst_list: 法人歷史(date 升序),取最後 inst_buy_days 筆當「最近 N 日」。
    """
    # target_date 必須剛好是當日收盤(否則表示 target_date 沒交易)
    if price_df["date"].iloc[-1] != target_date:
        raise _SkipStock()

    # === 條件 A: 量價突破 ===
    today_vol = float(price_df["volume"].iloc[-1])
    ma_window = p["ma_volume_period"]
    ma_vol = float(price_df["volume"].iloc[-(ma_window + 1):-1].mean())
    cond_a = today_vol > ma_vol * p["volume_multiplier"]

    # === 條件 B: KD 黃金交叉 ===
    kd_df = ind.kd(price_df, n=p["kd_period"])
    if len(kd_df) < 2:
        raise _SkipStock()
    prev_k = kd_df["K"].iloc[-2]
    prev_d = kd_df["D"].iloc[-2]
    curr_k = kd_df["K"].iloc[-1]
    curr_d = kd_df["D"].iloc[-1]
    if any(pd.isna(x) for x in (prev_k, prev_d, curr_k, curr_d)):
        raise _SkipStock()
    cond_b = (
        prev_k <= prev_d
        and curr_k > curr_d
        and curr_k > p["kd_threshold_low"]
    )

    # === 條件 C: 法人連續買超(取最近 inst_buy_days 筆) ===
    recent_inst = inst_list[-p["inst_buy_days"]:]
    inst_totals = [float(r["total_buy_sell"] or 0) for r in recent_inst]
    cond_c = all(t > 0 for t in inst_totals)
    inst_sum = float(sum(inst_totals))

    if not (cond_a and cond_b and cond_c):
        return None

    return {
        "stock_id": stock_id,
        "name": name,
        "close": float(price_df["close"].iloc[-1]),
        "volume": int(today_vol),
        "ma_volume_5": ma_vol,
        "k": float(curr_k),
        "d": float(curr_d),
        "inst_total_3d": inst_sum,
        "matched_at": target_date,
    }


def _evaluate_short(
    stock_id: str,
    name: str,
    target_date: str,
    p: dict,
    conn=None,
) -> dict | None:
    """評估單檔(向後相容入口;內部走 bulk_load + _evaluate_short_from_data)。

    保留給舊測試 / 外部呼叫。新流程 screen_short 已直接走 bulk path。
    """
    min_price_rows = max(p["ma_volume_period"] + 1, p["kd_period"] + 1)
    inst_lookback = max(p["inst_buy_days"] * 3, 14)

    if conn is not None:
        prices = bulk_load_prices(conn, [stock_id], target_date, _LOOKBACK_DAYS)
        insts = bulk_load_institutional_totals(
            conn, [stock_id], target_date, inst_lookback,
        )
    else:
        with db.get_conn() as own_conn:
            prices = bulk_load_prices(
                own_conn, [stock_id], target_date, _LOOKBACK_DAYS,
            )
            insts = bulk_load_institutional_totals(
                own_conn, [stock_id], target_date, inst_lookback,
            )

    price_df = prices.get(stock_id)
    inst_list = insts.get(stock_id, [])
    if (price_df is None or len(price_df) < min_price_rows
            or len(inst_list) < p["inst_buy_days"]):
        raise _SkipStock()

    return _evaluate_short_from_data(
        stock_id, name, target_date, p, price_df, inst_list,
    )


__all__ = ["screen_short", "DEFAULT_SHORT_PARAMS", "OUTPUT_COLUMNS"]
