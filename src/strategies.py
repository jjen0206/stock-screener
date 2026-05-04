"""
短線多策略並行入口(11 套):

- volume_kd            量價突破 + KD 黃金交叉 + 法人連 N 日買超(原 screen_short)
- ma_alignment         均線多頭排列(MA5>MA10>MA20>MA60 + 全部上揚 + 收盤站上 MA5)
- bias_convergence     20 日乖離率收斂(-5% ~ +1%)+ 量比 > 1.2
- macd_golden          MACD 黃金交叉 + DIF<0(初升段)+ 量比 ≥ 1.0
- ma_squeeze_breakout  MA5/MA20/MA60 5 日內糾結 ≤ 2% + 突破 + 量比 ≥ 1.2
- inst_consensus       外/投/自三家同時買超(net > 0)≥ N 個交易日連續
- bb_lower_rebound     5 日內觸 BB 下軌 + 今日收紅 K + 量比 ≥ 1.0(超賣反彈)
- rsi_recovery         14 日內 RSI < 30 + 今日 RSI > 50 + 從低點 monotonic 上升
- inst_silent_accum    5/10/20 日法人累計皆 > 0 + 今日平盤 ±1% + BB 位置 < 50%
- volume_breakout      今日量 ≥ 5 日均量 × 2.5 + close 突破 20 日新高
- gap_up               跳空 ≥ 1.5% + 量比 ≥ 1.5 + 收紅 K(事件驅動)

run_all_strategies() 把多策略結果聚合,輸出 {stock_id: {name, signals: [...], details}}
信號數越多 = 多套策略同時看好 = 信心越強。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

from src import database as db, indicators as ind
from src._bulk_load import bulk_load_institutional_totals, bulk_load_prices
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

DEFAULT_MACD_PARAMS: dict[str, Any] = {
    "vol_ratio_min": 1.0,  # 黃金交叉時量比下限(基本量)
}

DEFAULT_SQUEEZE_PARAMS: dict[str, Any] = {
    "squeeze_window_days": 5,    # 過去 N 日 MA 都要糾結
    "squeeze_pct_max": 2.0,      # MA spread % 上限(糾結門檻)
    "breakout_pct": 0.5,         # close 突破 max(MA) × (1 + N%)
    "vol_ratio_min": 1.2,
}

DEFAULT_INST_CONSENSUS_PARAMS: dict[str, Any] = {
    "consecutive_days": 3,  # 三家同時買超連續天數
}

# === commit 3 新加 5 個 strategies (策略 7-11) 的 default params ===

DEFAULT_BB_REBOUND_PARAMS: dict[str, Any] = {
    # 用獨立 key:跟 RSI 的 rsi_window_days 區分(各自獨立 lookback)
    "bb_touch_lookback": 5,    # 過去 N 日內任一日要碰到下軌
    "bb_vol_ratio_min": 1.0,   # 紅 K 那天的量比下限(獨立,跟 bias 1.2 不同)
    "bb_period": 20,
    "bb_num_std": 2.0,
}

DEFAULT_RSI_RECOVERY_PARAMS: dict[str, Any] = {
    "rsi_oversold": 30.0,      # 14 日內曾 < 此值
    "rsi_recovered": 50.0,     # 今日 RSI 須 > 此值
    "rsi_window_days": 14,     # 獨立 key 避免跟 BB 的 bb_touch_lookback 撞
}

DEFAULT_INST_SILENT_PARAMS: dict[str, Any] = {
    # 主力默默吸貨:5/10/20 日累計都 > 0(任一視窗都不能負)
    "pct_change_max": 1.0,     # 今日 |漲跌幅%| 上限(平盤定義)
    "bb_position_max": 50.0,   # close 在 BB 內位置 % 上限(0=下軌, 100=上軌)
    "bb_period": 20,
    "bb_num_std": 2.0,
}

DEFAULT_VOL_BREAKOUT_PARAMS: dict[str, Any] = {
    # 用獨立 key(非共用 vol_ratio_min)避免跟 bias/macd/squeeze 的 1.2 衝撞
    "vbo_vol_ratio_min": 2.5,    # 今日量 / 5 日均量
    "highest_lookback": 20,      # 近 N 日 max(close)
}

DEFAULT_GAP_UP_PARAMS: dict[str, Any] = {
    "gap_pct_min": 1.5,            # open 至少高於昨日 close 此 %
    "gap_vol_ratio_min": 1.5,      # 獨立 key 避免跟其他策略 vol_ratio_min 衝撞
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

    # **Bulk load**:對所有入選個股一次拉 ATR 期歷史(避免 N 次 SELECT)
    sids_for_atr = [str(s) for s in df["stock_id"].tolist()]
    with db.get_conn() as conn:
        atr_history = bulk_load_prices(
            conn, sids_for_atr, target_date,
            lookback_days=max(atr_period * 2, 30),
        )

    rows: list[dict] = []
    for _, row in df.iterrows():
        sid = str(row["stock_id"])
        close = float(row.get("close") or 0)
        new_row = row.to_dict()
        atr14 = _compute_atr_from_df(atr_history.get(sid), atr_period)
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


def _compute_atr_from_df(
    df: pd.DataFrame | None, period: int = 14,
) -> float | None:
    """從已載入的 DataFrame 算 ATR(period) 最後一筆;資料不足回 None。"""
    if df is None or len(df) < period + 1:
        return None
    series = ind.atr(df, period=period)
    last = series.iloc[-1]
    if pd.isna(last) or last <= 0:
        return None
    return float(last)


def _compute_atr_for_stock(
    stock_id: str, target_date: str, period: int = 14,
) -> float | None:
    """單檔 ATR(包裝 bulk_load_prices + _compute_atr_from_df)。

    給 compute_target_prices() 等對外 helper 用;選股流程已改走 bulk load。
    """
    lookback = max(period * 2, 30)
    with db.get_conn() as conn:
        history = bulk_load_prices(conn, [stock_id], target_date, lookback)
    return _compute_atr_from_df(history.get(stock_id), period)


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


# === 策略 4:MACD 黃金交叉(初升段) ===

def screen_macd_golden(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 4:MACD 黃金交叉 + DIF < 0(初升段)+ 量比 ≥ 1.0。

    意義:DIF 在 0 軸下方剛上穿 signal,代表趨勢從空翻多的早期階段。比突破
    0 軸更早進場,風險也更高。
    """
    p = {**DEFAULT_MACD_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "macd_dif", "macd_signal", "vol_ratio", "matched_at",
    ]
    raw = _evaluate_strategy(
        date, sids, cols,
        evaluate_fn=lambda df, name: _evaluate_macd_golden(df, name, date, p),
        lookback_days=60,  # MACD 12-26-9 需 35 天 + 緩衝
        min_required=35,
    )
    return _enrich_with_targets(raw, date)


def _evaluate_macd_golden(
    df: pd.DataFrame, name: str, date: str, p: dict,
) -> dict | None:
    if df["date"].iloc[-1] != date:
        return None
    macd_df = ind.macd(df, fast=12, slow=26, signal=9)
    if macd_df.empty or len(macd_df) < 2:
        return None
    dif_today = macd_df["DIF"].iloc[-1]
    dif_prev = macd_df["DIF"].iloc[-2]
    sig_today = macd_df["DEA"].iloc[-1]
    sig_prev = macd_df["DEA"].iloc[-2]
    if any(pd.isna(v) for v in [dif_today, dif_prev, sig_today, sig_prev]):
        return None
    # 黃金交叉:今日 DIF > sig,昨日 DIF ≤ sig
    if not (dif_today > sig_today and dif_prev <= sig_prev):
        return None
    # 初升段:DIF < 0(0 軸下方剛上穿)
    if dif_today >= 0:
        return None
    # 量比 ≥ 1.0(基本量)
    today_vol = float(df["volume"].iloc[-1])
    prev_5_vol = df["volume"].iloc[-6:-1]
    if len(prev_5_vol) < 5:
        return None
    ma_vol = float(prev_5_vol.mean())
    vol_ratio = today_vol / ma_vol if ma_vol > 0 else 0.0
    if vol_ratio < p["vol_ratio_min"]:
        return None
    return {
        "stock_id": str(df["stock_id"].iloc[-1]) if "stock_id" in df.columns else "",
        "name": name,
        "close": float(df["close"].iloc[-1]),
        "macd_dif": float(dif_today),
        "macd_signal": float(sig_today),
        "vol_ratio": float(vol_ratio),
        "matched_at": date,
    }


# === 策略 5:均線糾結突破 ===

def screen_ma_squeeze_breakout(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 5:過去 N 日 MA5/MA20/MA60 範圍 ≤ 2%(糾結)+ 今日突破 max(MA) +
    量比 ≥ 1.2。

    意義:多重均線收斂表示橫盤整理,突破往往伴隨強趨勢開始。
    """
    p = {**DEFAULT_SQUEEZE_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "ma5", "ma20", "ma60", "squeeze_pct", "vol_ratio", "matched_at",
    ]
    raw = _evaluate_strategy(
        date, sids, cols,
        evaluate_fn=lambda df, name: _evaluate_squeeze(df, name, date, p),
        lookback_days=80,  # MA60 + 5 天糾結 window
        min_required=65,
    )
    return _enrich_with_targets(raw, date)


def _evaluate_squeeze(
    df: pd.DataFrame, name: str, date: str, p: dict,
) -> dict | None:
    if df["date"].iloc[-1] != date:
        return None
    ma5 = ind.sma(df, 5)
    ma20 = ind.sma(df, 20)
    ma60 = ind.sma(df, 60)
    window = int(p["squeeze_window_days"])
    if len(ma60) < window:
        return None
    # 過去 window 天每天 MA5/MA20/MA60 spread % 都 ≤ squeeze_pct_max
    for i in range(-window, 0):
        v5 = ma5.iloc[i]
        v20 = ma20.iloc[i]
        v60 = ma60.iloc[i]
        if pd.isna(v5) or pd.isna(v20) or pd.isna(v60):
            return None
        avg = (v5 + v20 + v60) / 3
        if avg <= 0:
            return None
        spread = (max(v5, v20, v60) - min(v5, v20, v60)) / avg * 100
        if spread > p["squeeze_pct_max"]:
            return None
    # 今日 close 突破 max(MA) × (1 + breakout_pct/100)
    close = float(df["close"].iloc[-1])
    today_max = max(ma5.iloc[-1], ma20.iloc[-1], ma60.iloc[-1])
    breakout_threshold = today_max * (1 + p["breakout_pct"] / 100)
    if close <= breakout_threshold:
        return None
    # 量比
    today_vol = float(df["volume"].iloc[-1])
    prev_5_vol = df["volume"].iloc[-6:-1]
    if len(prev_5_vol) < 5:
        return None
    ma_vol = float(prev_5_vol.mean())
    vol_ratio = today_vol / ma_vol if ma_vol > 0 else 0.0
    if vol_ratio < p["vol_ratio_min"]:
        return None
    last_avg = (ma5.iloc[-1] + ma20.iloc[-1] + ma60.iloc[-1]) / 3
    last_spread = (
        (max(ma5.iloc[-1], ma20.iloc[-1], ma60.iloc[-1])
         - min(ma5.iloc[-1], ma20.iloc[-1], ma60.iloc[-1]))
        / last_avg * 100
    )
    return {
        "stock_id": str(df["stock_id"].iloc[-1]) if "stock_id" in df.columns else "",
        "name": name,
        "close": close,
        "ma5": float(ma5.iloc[-1]),
        "ma20": float(ma20.iloc[-1]),
        "ma60": float(ma60.iloc[-1]),
        "squeeze_pct": float(last_spread),
        "vol_ratio": float(vol_ratio),
        "matched_at": date,
    }


# === 策略 6:三大法人連買(共識買進) ===

def screen_inst_consensus(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 6:外資 + 投信 + 自營商**同時**買超(net > 0)≥ N 個交易日連續。

    意義:重「共識」而非單家絕對值。三家共識買超罕見,出現往往是強訊號。

    跟 _evaluate_strategy 共用 skeleton 不一樣 — 此策略需 institutional 三欄
    (外/投/自各 net),不只 total_buy_sell。寫獨立 inline SQL。
    """
    from datetime import date as _date, timedelta as _td

    p = {**DEFAULT_INST_CONSENSUS_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "inst_consensus_days", "matched_at",
    ]
    n_days = int(p["consecutive_days"])
    if not sids:
        return pd.DataFrame(columns=cols)

    with db.get_conn() as conn:
        # name_map
        if len(sids) <= 500:
            placeholders = ",".join(["?"] * len(sids))
            meta = conn.execute(
                f"SELECT stock_id, name FROM stocks "
                f"WHERE stock_id IN ({placeholders})",
                sids,
            ).fetchall()
        else:
            meta = conn.execute(
                "SELECT stock_id, name FROM stocks WHERE market='TW'"
            ).fetchall()
        name_map = {r["stock_id"]: r["name"] for r in meta}

        # institutional 三欄(外/投/自,各自 net),撈 N × 3 天緩衝(週末/假日)
        start_date = (
            _date.fromisoformat(date) - _td(days=n_days * 3)
        ).isoformat()
        if len(sids) <= 500:
            placeholders = ",".join(["?"] * len(sids))
            inst_rows = conn.execute(
                f"SELECT stock_id, date, foreign_buy_sell, "
                f"trust_buy_sell, dealer_buy_sell "
                f"FROM institutional "
                f"WHERE stock_id IN ({placeholders}) "
                f"AND date BETWEEN ? AND ? "
                f"ORDER BY stock_id, date DESC",
                (*sids, start_date, date),
            ).fetchall()
        else:
            inst_rows = conn.execute(
                "SELECT stock_id, date, foreign_buy_sell, "
                "trust_buy_sell, dealer_buy_sell "
                "FROM institutional "
                "WHERE date BETWEEN ? AND ? "
                "ORDER BY stock_id, date DESC",
                (start_date, date),
            ).fetchall()
        # 拿每檔最新 close(目標日)
        prices_by_sid = bulk_load_prices(conn, sids, date, 5)

    # group by sid(date desc)
    inst_by_sid: dict[str, list[dict]] = {}
    for r in inst_rows:
        inst_by_sid.setdefault(r["stock_id"], []).append({
            "f": r["foreign_buy_sell"] or 0,
            "t": r["trust_buy_sell"] or 0,
            "d": r["dealer_buy_sell"] or 0,
        })

    rows: list[dict] = []
    for sid in sids:
        recs = inst_by_sid.get(sid, [])
        if len(recs) < n_days:
            continue
        # 最近 N 天三家都 > 0
        latest_n = recs[:n_days]
        all_consensus = all(
            r["f"] > 0 and r["t"] > 0 and r["d"] > 0
            for r in latest_n
        )
        if not all_consensus:
            continue
        price_df = prices_by_sid.get(sid)
        # 最後一筆 close(若 price_df 沒對應日,用最後可得日,fallback 0)
        close = 0.0
        if price_df is not None and not price_df.empty:
            close = float(price_df["close"].iloc[-1])
        rows.append({
            "stock_id": sid,
            "name": name_map.get(sid, sid),
            "close": close,
            "inst_consensus_days": n_days,
            "matched_at": date,
        })

    raw = pd.DataFrame(rows, columns=cols)
    return _enrich_with_targets(raw, date)


# === 策略 7:布林下軌反彈(超賣後反彈) ===

def screen_bb_lower_rebound(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 7:過去 N 日內任一日 close 觸 BB 下軌 + 今日收紅 K + 量比 ≥ 1.0。

    意義:超賣後反彈訊號。先碰下軌(超賣),再反手收紅 K(止跌),代表
    賣壓消化、籌碼換手完成。
    """
    p = {**DEFAULT_BB_REBOUND_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close", "open",
        "bb_lower", "bb_mid", "vol_ratio", "matched_at",
    ]
    raw = _evaluate_strategy(
        date, sids, cols,
        evaluate_fn=lambda df, name: _evaluate_bb_rebound(df, name, date, p),
        lookback_days=40,  # BB 20 + 5 日 lookback + 緩衝
        min_required=22,
    )
    return _enrich_with_targets(raw, date)


def _evaluate_bb_rebound(
    df: pd.DataFrame, name: str, date: str, p: dict,
) -> dict | None:
    if df["date"].iloc[-1] != date:
        return None
    bb = ind.bollinger(df, period=int(p["bb_period"]), num_std=float(p["bb_num_std"]))
    if bb.empty:
        return None
    lookback = int(p["bb_touch_lookback"])
    # 過去 lookback 天內(含今日往前數 N 天)任一天 close ≤ lower
    window = df.tail(lookback)
    bb_window = bb.tail(lookback)
    if len(window) < lookback or bb_window["lower"].isna().all():
        return None
    touched_lower = (window["close"].to_numpy() <= bb_window["lower"].to_numpy()).any()
    if not touched_lower:
        return None
    # 今日收紅 K
    today_open = float(df["open"].iloc[-1])
    today_close = float(df["close"].iloc[-1])
    if today_close <= today_open:
        return None
    # 量比 ≥ bb_vol_ratio_min
    today_vol = float(df["volume"].iloc[-1])
    prev_5_vol = df["volume"].iloc[-6:-1]
    if len(prev_5_vol) < 5:
        return None
    ma_vol = float(prev_5_vol.mean())
    vol_ratio = today_vol / ma_vol if ma_vol > 0 else 0.0
    if vol_ratio < p["bb_vol_ratio_min"]:
        return None
    return {
        "stock_id": str(df["stock_id"].iloc[-1]) if "stock_id" in df.columns else "",
        "name": name,
        "close": today_close,
        "open": today_open,
        "bb_lower": float(bb["lower"].iloc[-1]) if not pd.isna(bb["lower"].iloc[-1]) else 0.0,
        "bb_mid": float(bb["mid"].iloc[-1]) if not pd.isna(bb["mid"].iloc[-1]) else 0.0,
        "vol_ratio": float(vol_ratio),
        "matched_at": date,
    }


# === 策略 8:RSI 回升(底部反轉確認) ===

def screen_rsi_recovery(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 8:14 日內 RSI(14) 曾 < 30 + 今日 RSI > 50 + 從最低 RSI 點到今日
    持續上升(monotonic)。

    意義:超賣 → 中線 → 還在攻,確認底部反轉。比單純看 RSI 回升 50 多一層
    過濾(中間沒回測 RSI 低點)。
    """
    p = {**DEFAULT_RSI_RECOVERY_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "rsi_today", "rsi_min_in_window", "matched_at",
    ]
    raw = _evaluate_strategy(
        date, sids, cols,
        evaluate_fn=lambda df, name: _evaluate_rsi_recovery(df, name, date, p),
        lookback_days=40,  # RSI 14 + 14 日 window + 緩衝
        min_required=29,
    )
    return _enrich_with_targets(raw, date)


def _evaluate_rsi_recovery(
    df: pd.DataFrame, name: str, date: str, p: dict,
) -> dict | None:
    if df["date"].iloc[-1] != date:
        return None
    rsi_series = ind.rsi(df, period=14)
    if rsi_series.isna().all():
        return None
    lookback = int(p["rsi_window_days"])
    rsi_window = rsi_series.tail(lookback)
    rsi_today = rsi_window.iloc[-1]
    if pd.isna(rsi_today):
        return None
    # 今日 RSI > recovered
    if rsi_today <= p["rsi_recovered"]:
        return None
    # window 內曾 < oversold
    valid_window = rsi_window.dropna()
    if valid_window.empty:
        return None
    rsi_min = float(valid_window.min())
    if rsi_min >= p["rsi_oversold"]:
        return None
    # 從 RSI 最低那天到今日 monotonic 上升
    min_idx = int(valid_window.idxmin())
    # idxmin 給原 series 的 index;從該 index 切到最後
    rsi_after_min = rsi_series.loc[min_idx:].dropna()
    if len(rsi_after_min) < 2:
        return None
    diffs = rsi_after_min.diff().dropna()
    if (diffs < 0).any():
        return None
    return {
        "stock_id": str(df["stock_id"].iloc[-1]) if "stock_id" in df.columns else "",
        "name": name,
        "close": float(df["close"].iloc[-1]),
        "rsi_today": float(rsi_today),
        "rsi_min_in_window": rsi_min,
        "matched_at": date,
    }


# === 策略 9:主力默默吸貨(平盤盤整 + 法人連續累積買超 + 低檔) ===

def screen_inst_silent_accum(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 9:5/10/20 日法人累計都 > 0 + 今日 |漲跌幅%| < 1.0 + close 在 BB
    下半部(< 50% 位置)。

    意義:主力悄悄佈局 — 量價沒明顯異常(平盤),法人卻連續多週期累積進貨,
    且還在低檔。比 inst_consensus 更被動,適合「翻冷股」。
    """
    p = {**DEFAULT_INST_SILENT_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "inst_5d", "inst_10d", "inst_20d",
        "pct_change", "bb_position_pct", "matched_at",
    ]
    if not sids:
        return pd.DataFrame(columns=cols)

    bb_period = int(p["bb_period"])
    bb_num_std = float(p["bb_num_std"])

    with db.get_conn() as conn:
        if len(sids) <= 500:
            placeholders = ",".join(["?"] * len(sids))
            meta = conn.execute(
                f"SELECT stock_id, name FROM stocks "
                f"WHERE stock_id IN ({placeholders})",
                sids,
            ).fetchall()
        else:
            meta = conn.execute(
                "SELECT stock_id, name FROM stocks WHERE market='TW'"
            ).fetchall()
        name_map = {r["stock_id"]: r["name"] for r in meta}

        # 30 calendar days ≈ 20+ 交易日(BB 20 + 安全緩衝);institutional 也撈同範圍
        prices_by_sid = bulk_load_prices(conn, sids, date, lookback_days=40)
        inst_by_sid = bulk_load_institutional_totals(
            conn, sids, date, lookback_days=40,
        )

    rows: list[dict] = []
    for sid in sids:
        df = prices_by_sid.get(sid)
        inst_list = inst_by_sid.get(sid, [])
        if df is None or len(df) < bb_period + 1:
            continue
        if df["date"].iloc[-1] != date:
            continue
        # 5/10/20 日法人累計都 > 0
        inst_sums = sorted(
            [(r["date"], r["total_buy_sell"] or 0) for r in inst_list],
            key=lambda x: x[0],
        )
        # 取最後 20 / 10 / 5 筆(已按 date 升序)
        if len(inst_sums) < 20:
            continue
        sum_5 = sum(v for _, v in inst_sums[-5:])
        sum_10 = sum(v for _, v in inst_sums[-10:])
        sum_20 = sum(v for _, v in inst_sums[-20:])
        if not (sum_5 > 0 and sum_10 > 0 and sum_20 > 0):
            continue
        # 平盤 ±1%
        if len(df) < 2:
            continue
        prev_close = float(df["close"].iloc[-2])
        today_close = float(df["close"].iloc[-1])
        if prev_close <= 0:
            continue
        pct_change = (today_close - prev_close) / prev_close * 100
        if abs(pct_change) >= p["pct_change_max"]:
            continue
        # BB 位置 < 50%
        bb = ind.bollinger(df, period=bb_period, num_std=bb_num_std)
        if bb.empty:
            continue
        upper = bb["upper"].iloc[-1]
        lower = bb["lower"].iloc[-1]
        if pd.isna(upper) or pd.isna(lower) or upper <= lower:
            continue
        bb_position = (today_close - lower) / (upper - lower) * 100
        if bb_position >= p["bb_position_max"]:
            continue
        rows.append({
            "stock_id": sid,
            "name": name_map.get(sid, sid),
            "close": today_close,
            "inst_5d": float(sum_5),
            "inst_10d": float(sum_10),
            "inst_20d": float(sum_20),
            "pct_change": float(pct_change),
            "bb_position_pct": float(bb_position),
            "matched_at": date,
        })

    raw = pd.DataFrame(rows, columns=cols)
    return _enrich_with_targets(raw, date)


# === 策略 10:量爆突破(強勢股啟動) ===

def screen_volume_breakout(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 10:今日量 ≥ 5 日均量 × 2.5(爆量)+ close 突破近 20 日 max(close)。

    意義:強勢股啟動的經典訊號 — 突破創新高同時量能放大,代表多方共識
    一致跑進場,趨勢開始。
    """
    p = {**DEFAULT_VOL_BREAKOUT_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "high_20d", "vol_ratio", "matched_at",
    ]
    raw = _evaluate_strategy(
        date, sids, cols,
        evaluate_fn=lambda df, name: _evaluate_volume_breakout(df, name, date, p),
        lookback_days=35,  # 20 日高 + 5 日量均 + 緩衝
        min_required=21,
    )
    return _enrich_with_targets(raw, date)


def _evaluate_volume_breakout(
    df: pd.DataFrame, name: str, date: str, p: dict,
) -> dict | None:
    if df["date"].iloc[-1] != date:
        return None
    today_vol = float(df["volume"].iloc[-1])
    prev_5_vol = df["volume"].iloc[-6:-1]
    if len(prev_5_vol) < 5:
        return None
    ma_vol = float(prev_5_vol.mean())
    vol_ratio = today_vol / ma_vol if ma_vol > 0 else 0.0
    if vol_ratio < p["vbo_vol_ratio_min"]:
        return None
    # close 突破近 N 日 max(close)(不含今日)
    n = int(p["highest_lookback"])
    prev_n_close = df["close"].iloc[-(n + 1):-1]
    if len(prev_n_close) < n:
        return None
    high_n = float(prev_n_close.max())
    today_close = float(df["close"].iloc[-1])
    if today_close <= high_n:
        return None
    return {
        "stock_id": str(df["stock_id"].iloc[-1]) if "stock_id" in df.columns else "",
        "name": name,
        "close": today_close,
        "high_20d": high_n,
        "vol_ratio": float(vol_ratio),
        "matched_at": date,
    }


# === 策略 11:跳空缺口(事件驅動短線) ===

def screen_gap_up(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 11:今日 open > 昨日 close × 1.015 + 量比 ≥ 1.5 + 收紅 K。

    意義:事件驅動(法說、營收、利多)導致的跳空 + 量增 + 收紅,
    短線追擊適合。風險高(回補缺口)所以停損要嚴。
    """
    p = {**DEFAULT_GAP_UP_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close", "open",
        "gap_pct", "vol_ratio", "matched_at",
    ]
    raw = _evaluate_strategy(
        date, sids, cols,
        evaluate_fn=lambda df, name: _evaluate_gap_up(df, name, date, p),
        lookback_days=15,  # 只需昨日 close + 5 日量均 + 緩衝
        min_required=6,
    )
    return _enrich_with_targets(raw, date)


def _evaluate_gap_up(
    df: pd.DataFrame, name: str, date: str, p: dict,
) -> dict | None:
    if df["date"].iloc[-1] != date:
        return None
    if len(df) < 2:
        return None
    today_open = float(df["open"].iloc[-1])
    today_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    if prev_close <= 0:
        return None
    gap_pct = (today_open - prev_close) / prev_close * 100
    if gap_pct < p["gap_pct_min"]:
        return None
    # 收紅
    if today_close <= today_open:
        return None
    # 量比 ≥ 1.5
    today_vol = float(df["volume"].iloc[-1])
    prev_5_vol = df["volume"].iloc[-6:-1]
    if len(prev_5_vol) < 5:
        return None
    ma_vol = float(prev_5_vol.mean())
    vol_ratio = today_vol / ma_vol if ma_vol > 0 else 0.0
    if vol_ratio < p["gap_vol_ratio_min"]:
        return None
    return {
        "stock_id": str(df["stock_id"].iloc[-1]) if "stock_id" in df.columns else "",
        "name": name,
        "close": today_close,
        "open": today_open,
        "gap_pct": float(gap_pct),
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
    """跑單一策略;**Bulk SQL load**:一次拉全 universe 歷史,避免 N 次 SELECT。

    Profile:per-stock SELECT × 2360 = 4720 次 SQL,雲端 SQLite IO 是主要瓶頸;
    改 bulk load 後本機 0.22s → 0.04s,雲端再快數倍。
    """
    if not sids:
        return pd.DataFrame(columns=cols)

    with db.get_conn() as conn:
        # 1. stocks 名稱(若 sids 太大則直接撈 TW 全表,避免 IN clause 變數爆量)
        if len(sids) <= 500:
            placeholders = ",".join(["?"] * len(sids))
            meta = conn.execute(
                f"SELECT stock_id, name FROM stocks "
                f"WHERE stock_id IN ({placeholders})",
                sids,
            ).fetchall()
        else:
            meta = conn.execute(
                "SELECT stock_id, name FROM stocks WHERE market='TW'"
            ).fetchall()
        name_map = {r["stock_id"]: r["name"] for r in meta}

        # 2. **Bulk load** 全 universe 的 lookback_days 歷史
        prices_by_sid = bulk_load_prices(conn, sids, date, lookback_days)

    # 3. loop 個股 — 從 dict 拿,不再進 DB
    rows: list[dict] = []
    for sid in sids:
        df = prices_by_sid.get(sid)
        if df is None or len(df) < min_required:
            continue
        try:
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
    # commit 1 新加 3 個 strategies
    "macd_golden": screen_macd_golden,
    "ma_squeeze_breakout": screen_ma_squeeze_breakout,
    "inst_consensus": screen_inst_consensus,
    # commit 3 新加 5 個 strategies
    "bb_lower_rebound": screen_bb_lower_rebound,
    "rsi_recovery": screen_rsi_recovery,
    "inst_silent_accum": screen_inst_silent_accum,
    "volume_breakout": screen_volume_breakout,
    "gap_up": screen_gap_up,
}

STRATEGY_LABELS: dict[str, str] = {
    "volume_kd": "量價KD",
    "ma_alignment": "多頭排列",
    "bias_convergence": "乖離收斂",
    "macd_golden": "MACD 黃金交叉",
    "ma_squeeze_breakout": "均線糾結突破",
    "inst_consensus": "三大法人連買",
    "bb_lower_rebound": "布林下軌反彈",
    "rsi_recovery": "RSI 回升",
    "inst_silent_accum": "主力默默吸貨",
    "volume_breakout": "量爆突破",
    "gap_up": "跳空缺口",
}


# === Per-strategy ML 過濾門檻(Stage 2B 校準後落地) ===
# 來源:scripts/audit/calibrate_ml_thresholds.py --use-per-strategy-models 跑
# 30-day grid search 結果(Stage 2B 6 個 trained per-strategy models)。
#
# Stage 2A(通用 ML model)只有 ma_alignment 過 winner 條件;Stage 2B(per-
# strategy retrain)後 5 個 strategies 額外過 winner — 通用模型跟其他策略 alpha
# 信號重疊的問題確認被 per-strategy 重訓化解。
#
# Threshold 對照(per-strategy 校準 30d):
#   bias_convergence    0.65 → 100% WR (97 fires)
#   macd_golden         0.60 → 100% WR (30 fires)
#   bb_lower_rebound    0.50 → 75.8% WR (33 fires)
#   volume_breakout     0.65 → 100% WR (74 fires)
#   gap_up              0.60 → 100% WR (108 fires)
#   ma_alignment        保留 0.60(Stage 2A 60d 確認;30d sample 太小 (<30 fires)
#                       calibrator 退 baseline,但 60d 已驗證有效)
#
# 沒過 winner / 沒跑(sample 太小)的策略不放 dict 內(.get → None,不過濾):
#   rsi_recovery / volume_kd / ma_squeeze_breakout / inst_consensus /
#   inst_silent_accum
STRATEGY_ML_THRESHOLDS: dict[str, float] = {
    "ma_alignment": 0.60,
    "bias_convergence": 0.65,
    "macd_golden": 0.60,
    "bb_lower_rebound": 0.50,
    "volume_breakout": 0.65,
    "gap_up": 0.60,
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
    "screen_macd_golden",
    "screen_ma_squeeze_breakout",
    "screen_inst_consensus",
    "screen_bb_lower_rebound",
    "screen_rsi_recovery",
    "screen_inst_silent_accum",
    "screen_volume_breakout",
    "screen_gap_up",
    "run_all_strategies",
    "aggregated_to_dataframe",
    "compute_target_prices",
    "ALL_STRATEGIES",
    "STRATEGY_LABELS",
    "STRATEGY_ML_THRESHOLDS",
    "DEFAULT_MA_PARAMS",
    "DEFAULT_MACD_PARAMS",
    "DEFAULT_SQUEEZE_PARAMS",
    "DEFAULT_INST_CONSENSUS_PARAMS",
    "DEFAULT_BIAS_PARAMS",
    "DEFAULT_BB_REBOUND_PARAMS",
    "DEFAULT_RSI_RECOVERY_PARAMS",
    "DEFAULT_INST_SILENT_PARAMS",
    "DEFAULT_VOL_BREAKOUT_PARAMS",
    "DEFAULT_GAP_UP_PARAMS",
    "TARGET_LOW_MULT",
    "TARGET_HIGH_MULT",
    "STOP_LOSS_MULT",
]
