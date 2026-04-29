"""
短線多策略並行入口。

三套策略:
- volume_kd          量價突破 + KD 黃金交叉 + 法人連 N 日買超(原 screen_short)
- ma_alignment       均線多頭排列(MA5>MA10>MA20>MA60 + 全部上揚 + 收盤站上 MA5)
- bias_convergence   20 日乖離率收斂(-5% ~ +1%)+ 量比 > 1.2

run_all_strategies() 把多策略結果聚合,輸出 {stock_id: {name, signals: [...], details}}
信號數越多 = 多套策略同時看好 = 信心越強。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

from src import database as db, indicators as ind
from src.screener_short import screen_short
from src.universe import TW_TOP_50


logger = logging.getLogger(__name__)


# === 策略預設參數 ===

DEFAULT_MA_PARAMS: dict[str, Any] = {
    "lookback_days": 80,  # 至少要 60 + 緩衝
}

DEFAULT_BIAS_PARAMS: dict[str, Any] = {
    "bias_low": -5.0,    # 乖離下限 (%)
    "bias_high": 1.0,    # 乖離上限 (%)
    "vol_ratio_min": 1.2,  # 量比門檻
}


# === 策略 1:量價 + KD + 法人(包裝既有 screen_short) ===

def screen_volume_kd(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 1:量價突破 + KD 黃金交叉 + 法人連 N 日買超(原 screen_short 邏輯)。"""
    df = screen_short(date, params=params, stock_ids=stock_ids)
    return _enrich_with_targets(df, date)


# === 共用:加目標價 / 停損 / R:R ===

# 目標價計算係數(基於 ATR 倍數,純價量統計;非真實預測)
TARGET_LOW_MULT = 1.5    # 保守目標 = close + 1.5×ATR (約 1 週)
TARGET_HIGH_MULT = 3.0   # 積極目標 = close + 3.0×ATR (約 2-3 週)
STOP_LOSS_MULT = 1.5     # 停損 = close − 1.5×ATR


def _enrich_with_targets(
    df: pd.DataFrame,
    target_date: str,
    atr_period: int = 14,
) -> pd.DataFrame:
    """對選股結果 DataFrame 每行加 5 個欄位:
      atr14, target_low, target_high, stop_loss, risk_reward

    risk_reward = (target_high - close) / (close - stop_loss)
                = 3 / 1.5 = 2.0(理想 > 1.5)

    資料不足算 ATR → 5 個欄位都填 None。空 DF 也保證 schema 齊全。
    """
    extra_cols = [
        "atr14", "target_low", "target_high", "stop_loss", "risk_reward",
    ]
    if df.empty:
        # 空 DF 補欄位讓下游 schema 一致
        for col in extra_cols:
            if col not in df.columns:
                df[col] = pd.Series(dtype=float)
        return df

    rows: list[dict] = []
    for _, row in df.iterrows():
        sid = row["stock_id"]
        close = float(row.get("close") or 0)
        new_row = row.to_dict()
        atr14 = _compute_atr_for_stock(sid, target_date, atr_period)
        if atr14 is not None and atr14 > 0 and close > 0:
            target_low = close + TARGET_LOW_MULT * atr14
            target_high = close + TARGET_HIGH_MULT * atr14
            stop_loss = close - STOP_LOSS_MULT * atr14
            risk = close - stop_loss
            reward = target_high - close
            risk_reward = reward / risk if risk > 0 else None
            new_row.update({
                "atr14": atr14,
                "target_low": target_low,
                "target_high": target_high,
                "stop_loss": stop_loss,
                "risk_reward": risk_reward,
            })
        else:
            for col in extra_cols:
                new_row[col] = None
        rows.append(new_row)
    return pd.DataFrame(rows)


def _compute_atr_for_stock(
    stock_id: str, target_date: str, period: int = 14,
) -> float | None:
    """從 SQLite 拉個股近 N 日,算 ATR(period) 取最後一筆。

    需要至少 period+1 筆才有 ATR;否則回 None。
    """
    lookback = max(period * 2, 30)  # 留足夠資料給 ATR Wilder 平滑收斂
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT date, high, low, close FROM daily_prices "
            "WHERE stock_id=? AND date<=? "
            "ORDER BY date DESC LIMIT ?",
            (stock_id, target_date, lookback),
        ).fetchall()
    if len(rows) < period + 1:
        return None
    df = (
        pd.DataFrame([dict(r) for r in rows])
        .iloc[::-1]
        .reset_index(drop=True)
    )
    series = ind.atr(df, period=period)
    last = series.iloc[-1]
    if pd.isna(last) or last <= 0:
        return None
    return float(last)


def compute_target_prices(
    stock_id: str,
    target_date: str | None = None,
) -> dict | None:
    """對單檔股票算 ATR 與目標價(對外 helper,給 watchlist / UI 共用)。

    流程:
      1. 從 SQLite 拉近 30 日 daily_prices
      2. 算 ATR(14)
      3. 取「截至 target_date 的最後一筆 close」
      4. 套常數算 target_low / target_high / stop_loss / risk_reward

    target_date 預設今日。資料不足回 None。

    回 {close, atr14, target_low, target_high, stop_loss, risk_reward}
    """
    from datetime import date as _date
    if target_date is None:
        target_date = _date.today().isoformat()

    atr14 = _compute_atr_for_stock(stock_id, target_date, period=14)
    if atr14 is None or atr14 <= 0:
        return None

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT close FROM daily_prices "
            "WHERE stock_id=? AND date<=? "
            "ORDER BY date DESC LIMIT 1",
            (stock_id, target_date),
        ).fetchone()
    if row is None or row["close"] is None:
        return None
    close = float(row["close"])
    if close <= 0:
        return None

    target_low = close + TARGET_LOW_MULT * atr14
    target_high = close + TARGET_HIGH_MULT * atr14
    stop_loss = close - STOP_LOSS_MULT * atr14
    risk = close - stop_loss
    reward = target_high - close
    risk_reward = reward / risk if risk > 0 else None

    return {
        "close": close,
        "atr14": atr14,
        "target_low": target_low,
        "target_high": target_high,
        "stop_loss": stop_loss,
        "risk_reward": risk_reward,
    }


# === 策略 2:均線多頭排列 ===

def screen_ma_alignment(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 2:MA5 > MA10 > MA20 > MA60 + 四條全上揚 + 收盤站上 MA5。

    意義:多頭強勢排列 = 趨勢明確、追隨買盤。
    """
    p = {**DEFAULT_MA_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "ma5", "ma10", "ma20", "ma60", "matched_at",
    ]
    raw = _evaluate_strategy(
        date, sids, cols,
        evaluate_fn=lambda df, name: _evaluate_ma_alignment(df, name, date),
        lookback_days=p["lookback_days"],
        min_required=65,
    )
    return _enrich_with_targets(raw, date)


def _evaluate_ma_alignment(
    df: pd.DataFrame, name: str, date: str,
) -> dict | None:
    if df["date"].iloc[-1] != date:
        return None
    ma5 = ind.sma(df, 5)
    ma10 = ind.sma(df, 10)
    ma20 = ind.sma(df, 20)
    ma60 = ind.sma(df, 60)
    if any(pd.isna(x.iloc[-1]) or pd.isna(x.iloc[-2])
           for x in (ma5, ma10, ma20, ma60)):
        return None
    cond_align = (
        ma5.iloc[-1] > ma10.iloc[-1]
        > ma20.iloc[-1] > ma60.iloc[-1]
    )
    cond_trend = all([
        ma5.iloc[-1] > ma5.iloc[-2],
        ma10.iloc[-1] > ma10.iloc[-2],
        ma20.iloc[-1] > ma20.iloc[-2],
        ma60.iloc[-1] > ma60.iloc[-2],
    ])
    cond_above = df["close"].iloc[-1] > ma5.iloc[-1]
    if not (cond_align and cond_trend and cond_above):
        return None
    return {
        "stock_id": str(df["stock_id"].iloc[-1]) if "stock_id" in df.columns else "",
        "name": name,
        "close": float(df["close"].iloc[-1]),
        "ma5": float(ma5.iloc[-1]),
        "ma10": float(ma10.iloc[-1]),
        "ma20": float(ma20.iloc[-1]),
        "ma60": float(ma60.iloc[-1]),
        "matched_at": date,
    }


# === 策略 3:乖離率收斂 ===

def screen_bias_convergence(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 3:20 日乖離率在 [-5%, +1%] 區間 + 量比 > 1.2。

    意義:接近 MA20 但未明顯偏離 + 量能放出 = 拉回支撐 + 動能歸來。
    """
    p = {**DEFAULT_BIAS_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "ma20", "bias_pct", "vol_ratio", "matched_at",
    ]
    raw = _evaluate_strategy(
        date, sids, cols,
        evaluate_fn=lambda df, name: _evaluate_bias(df, name, date, p),
        lookback_days=30,
        min_required=22,  # 20 MA + 5 量均 + 緩衝
    )
    return _enrich_with_targets(raw, date)


def _evaluate_bias(
    df: pd.DataFrame, name: str, date: str, p: dict,
) -> dict | None:
    if df["date"].iloc[-1] != date:
        return None
    ma20 = ind.sma(df, 20)
    if pd.isna(ma20.iloc[-1]) or ma20.iloc[-1] <= 0:
        return None
    close = float(df["close"].iloc[-1])
    bias_pct = (close - ma20.iloc[-1]) / ma20.iloc[-1] * 100
    today_vol = float(df["volume"].iloc[-1])
    prev_5_vol = df["volume"].iloc[-6:-1]
    if len(prev_5_vol) < 5:
        return None
    ma_vol = float(prev_5_vol.mean())
    vol_ratio = today_vol / ma_vol if ma_vol > 0 else 0.0
    if not (p["bias_low"] <= bias_pct <= p["bias_high"]):
        return None
    if vol_ratio <= p["vol_ratio_min"]:
        return None
    return {
        "stock_id": str(df["stock_id"].iloc[-1]) if "stock_id" in df.columns else "",
        "name": name,
        "close": close,
        "ma20": float(ma20.iloc[-1]),
        "bias_pct": float(bias_pct),
        "vol_ratio": float(vol_ratio),
        "matched_at": date,
    }


# === 共用:單檔評估骨架 ===

def _evaluate_strategy(
    date: str,
    sids: list[str],
    cols: list[str],
    evaluate_fn: Callable[[pd.DataFrame, str], dict | None],
    lookback_days: int,
    min_required: int,
) -> pd.DataFrame:
    """跑單一策略;**用單一 connection 包整個 loop**(profile 顯示 8000+ 次
    connect/close 是 hot path,改成單一 connection 省 ~70% 時間)。
    """
    if not sids:
        return pd.DataFrame(columns=cols)
    placeholders = ",".join(["?"] * len(sids))

    with db.get_conn() as conn:
        # 1. 一次查 stocks 表所有名字
        meta = conn.execute(
            f"SELECT stock_id, name FROM stocks WHERE stock_id IN ({placeholders})",
            sids,
        ).fetchall()
        name_map = {r["stock_id"]: r["name"] for r in meta}

        # 2. loop 個股 — 重用同一個 conn(不開新 connection)
        rows: list[dict] = []
        for sid in sids:
            try:
                price_rows = conn.execute(
                    "SELECT date, open, high, low, close, volume "
                    "FROM daily_prices "
                    "WHERE stock_id=? AND date<=? "
                    "ORDER BY date DESC LIMIT ?",
                    (sid, date, lookback_days),
                ).fetchall()
                if len(price_rows) < min_required:
                    continue
                df = pd.DataFrame([dict(r) for r in price_rows])
                df["stock_id"] = sid
                df = df.iloc[::-1].reset_index(drop=True)
                result = evaluate_fn(df, name_map.get(sid, sid))
                if result is not None:
                    result["stock_id"] = sid
                    rows.append(result)
            except Exception as e:  # noqa: BLE001
                logger.debug("[STRATEGY] %s 跳過: %s", sid, e)
                continue
    return pd.DataFrame(rows, columns=cols)


# === 多策略聚合入口 ===

ALL_STRATEGIES: dict[str, Callable] = {
    "volume_kd": screen_volume_kd,
    "ma_alignment": screen_ma_alignment,
    "bias_convergence": screen_bias_convergence,
}

STRATEGY_LABELS: dict[str, str] = {
    "volume_kd": "量價KD",
    "ma_alignment": "多頭排列",
    "bias_convergence": "乖離收斂",
}


def run_all_strategies(
    date: str,
    enabled: list[str] | None = None,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> dict[str, dict]:
    """跑多策略並聚合結果。

    enabled: 策略 key 清單(volume_kd / ma_alignment / bias_convergence);
             None = 全部
    回 {
        stock_id: {
            "name": str,
            "signals": [strategy_label, ...],   # 哪幾套策略命中
            "details": {strategy_key: row_dict},
        }
    }
    """
    enabled = enabled or list(ALL_STRATEGIES.keys())
    aggregated: dict[str, dict] = {}
    for key in enabled:
        if key not in ALL_STRATEGIES:
            continue
        df = ALL_STRATEGIES[key](date, params=params, stock_ids=stock_ids)
        if df.empty:
            continue
        for _, row in df.iterrows():
            sid = str(row["stock_id"])
            if sid not in aggregated:
                aggregated[sid] = {
                    "name": row.get("name", ""),
                    "signals": [],
                    "details": {},
                }
            aggregated[sid]["signals"].append(STRATEGY_LABELS[key])
            aggregated[sid]["details"][key] = row.to_dict()
    return aggregated


_AGGREGATED_COLUMNS = [
    "stock_id", "name", "信號數", "信號",
    "close", "target_low", "target_high", "stop_loss",
    "risk_reward", "atr14",
]


def aggregated_to_dataframe(agg: dict[str, dict]) -> pd.DataFrame:
    """把 run_all_strategies 結果攤平成 DataFrame 給 UI 顯示。

    欄位含目標價(target_low / target_high / stop_loss / risk_reward / atr14),
    從任一策略 details 取得(三個策略的 _enrich_with_targets 都會塞同樣值)。
    """
    if not agg:
        return pd.DataFrame(columns=_AGGREGATED_COLUMNS)
    rows = []
    for sid, info in agg.items():
        close = None
        target_low = target_high = stop_loss = risk_reward = atr14 = None
        for d in info["details"].values():
            if close is None and d.get("close"):
                close = d["close"]
            if target_low is None and d.get("target_low"):
                target_low = d.get("target_low")
                target_high = d.get("target_high")
                stop_loss = d.get("stop_loss")
                risk_reward = d.get("risk_reward")
                atr14 = d.get("atr14")
        rows.append({
            "stock_id": sid,
            "name": info["name"],
            "信號數": len(info["signals"]),
            "信號": " + ".join(info["signals"]),
            "close": close,
            "target_low": target_low,
            "target_high": target_high,
            "stop_loss": stop_loss,
            "risk_reward": risk_reward,
            "atr14": atr14,
        })
    df = pd.DataFrame(rows, columns=_AGGREGATED_COLUMNS)
    return df.sort_values(
        ["信號數", "stock_id"], ascending=[False, True],
    ).reset_index(drop=True)


__all__ = [
    "screen_volume_kd",
    "screen_ma_alignment",
    "screen_bias_convergence",
    "run_all_strategies",
    "aggregated_to_dataframe",
    "compute_target_prices",
    "ALL_STRATEGIES",
    "STRATEGY_LABELS",
    "DEFAULT_MA_PARAMS",
    "DEFAULT_BIAS_PARAMS",
    "TARGET_LOW_MULT",
    "TARGET_HIGH_MULT",
    "STOP_LOSS_MULT",
]
