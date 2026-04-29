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
            placeholders = ",".join(["?"] * len(stock_ids))
            stocks = conn.execute(
                f"SELECT stock_id, name FROM stocks "
                f"WHERE market='TW' AND stock_id IN ({placeholders})",
                stock_ids,
            ).fetchall()
        else:
            stocks = conn.execute(
                "SELECT stock_id, name FROM stocks WHERE market='TW'"
            ).fetchall()

    if not stocks:
        logger.warning("[SCREEN_SHORT] stocks 表為空,無法選股")
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    rows: list[dict] = []
    skipped = 0
    # 單一 connection 包整個 loop(profile 顯示 per-stock connection 是 hot path)
    with db.get_conn() as conn:
        for s in stocks:
            try:
                row = _evaluate_short(
                    s["stock_id"], s["name"], date, p, conn=conn,
                )
            except _SkipStock:
                skipped += 1
                continue
            if row is not None:
                rows.append(row)

    if skipped:
        logger.warning(
            "[SCREEN_SHORT] %d 檔資料不足跳過(需至少 %d 日價格 + %d 筆法人)",
            skipped,
            max(p["ma_volume_period"] + 1, p["kd_period"] + 1),
            p["inst_buy_days"],
        )

    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def _evaluate_short(
    stock_id: str,
    name: str,
    target_date: str,
    p: dict,
    conn=None,
) -> dict | None:
    """評估單檔。回 dict = 入選;回 None = 三條件未全部過;raise _SkipStock = 資料不足。

    conn 可由 caller 傳入(避免 per-stock 開新 connection,大 universe 顯著加速)。
    """
    min_price_rows = max(p["ma_volume_period"] + 1, p["kd_period"] + 1)

    if conn is not None:
        price_rows = conn.execute(
            "SELECT date, open, high, low, close, volume "
            "FROM daily_prices WHERE stock_id=? AND date<=? "
            "ORDER BY date DESC LIMIT ?",
            (stock_id, target_date, _LOOKBACK_DAYS),
        ).fetchall()
        inst_rows = conn.execute(
            "SELECT date, total_buy_sell FROM institutional "
            "WHERE stock_id=? AND date<=? ORDER BY date DESC LIMIT ?",
            (stock_id, target_date, p["inst_buy_days"]),
        ).fetchall()
    else:
        # 向後相容:沒傳 conn 自己開
        with db.get_conn() as own_conn:
            price_rows = own_conn.execute(
                "SELECT date, open, high, low, close, volume "
                "FROM daily_prices WHERE stock_id=? AND date<=? "
                "ORDER BY date DESC LIMIT ?",
                (stock_id, target_date, _LOOKBACK_DAYS),
            ).fetchall()
            inst_rows = own_conn.execute(
                "SELECT date, total_buy_sell FROM institutional "
                "WHERE stock_id=? AND date<=? ORDER BY date DESC LIMIT ?",
                (stock_id, target_date, p["inst_buy_days"]),
            ).fetchall()

    if len(price_rows) < min_price_rows or len(inst_rows) < p["inst_buy_days"]:
        raise _SkipStock()

    # SQL 取的是 DESC,反轉成升序給指標計算
    price_df = (
        pd.DataFrame([dict(r) for r in price_rows])
        .iloc[::-1]
        .reset_index(drop=True)
    )

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

    # === 條件 C: 法人連續買超 ===
    inst_totals = [float(r["total_buy_sell"] or 0) for r in inst_rows]
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


__all__ = ["screen_short", "DEFAULT_SHORT_PARAMS", "OUTPUT_COLUMNS"]
