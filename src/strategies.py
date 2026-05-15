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
    # 量比上限:過去 1 年 diagnose 顯示 vol_ratio > 3x 那群反而 WR 44.8%
    # (低於 baseline 40.7% 只 +4pp,而 1.5-3x 那群 WR 50.3% — sweet spot 在這)
    # 假說:極端高量多是「主力出貨 / 利多出盡」一日量爆,後續 mean revert。
    # 詳見 docs/gap-up-decision-2026-05-15.md。3.0 是 diagnose bucket 邊界,
    # 不是 ad-hoc 拍腦袋。如要關閉上限 → 設 float("inf") / None。
    "gap_vol_ratio_max": 3.0,
}

# === Phase 1 新加 5 個策略 (12-16) 預設參數 ===

DEFAULT_EPS_ACCEL_PARAMS: dict[str, Any] = {
    # 季 EPS 加速:連 2 季 YoY 都正、且當季 YoY > 上季 YoY
    # 不設 min YoY % 門檻(過濾在「YoY 都正」就夠嚴),只比加速度
    "min_quarters": 5,             # 至少 5 個季資料才能算當季 + 上季 YoY
}

DEFAULT_HIGH_YIELD_PARAMS: dict[str, Any] = {
    "yield_min_pct": 6.0,          # 殖利率 > 6%(優於台股大盤平均 ~3.5%)
    "stable_quarters": 4,          # 近 4 季 EPS 都為正
}

DEFAULT_INST_REVERSAL_PARAMS: dict[str, Any] = {
    "down_days": 3,                # 連 N 日法人淨賣超
    # 反轉條件:今日轉買(total_buy_sell > 0),前 down_days 日都 < 0
}

DEFAULT_TAIEX_ALPHA_PARAMS: dict[str, Any] = {
    "stock_up_pct_min": 1.0,       # 個股當日漲 > 1%
    "taiex_down_pct_max": 0.0,     # TAIEX 當日 < 0%(任何負值)
}

DEFAULT_REV_ACCEL_PARAMS: dict[str, Any] = {
    "yoy_min_pct": 30.0,           # 月營收 YoY > 30%
    # 加速條件:當月 YoY > 上月 YoY(無下限,只需嚴格遞增)
}

# 策略 17:千張戶進場(big_holder_inflow)
# Phase 2(滾動 4 週 mean + 1σ 突破):對每檔 sid 撈最近 5 週 holders_delta_w,
#   取前 4 週算 mean μ + std σ,本週 > μ + 1σ 命中。
# 歷史不足 fallback(< 4 週前期歷史)→ Phase 1 邏輯(Top 20% absolute P80)。
DEFAULT_BIG_HOLDER_PARAMS: dict[str, Any] = {
    "percentile": 0.80,            # Phase 1 fallback:P80(holders_delta_w Top 20%)
    "rolling_weeks": 4,            # Phase 2:前 4 週滾動視窗算 mean / std
    "sigma_threshold": 1.0,        # Phase 2:命中門檻 = mean + sigma_threshold × std
    "min_delta_floor": 5,          # Phase 2 絕對下限:本週 delta 必須 ≥ 此值,過濾小型股雜訊
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
    # 量比 ≥ 1.5 且 < 3.0(2026-05-15 加上限 — diagnose 顯示 >3x 反而 mean revert)
    today_vol = float(df["volume"].iloc[-1])
    prev_5_vol = df["volume"].iloc[-6:-1]
    if len(prev_5_vol) < 5:
        return None
    ma_vol = float(prev_5_vol.mean())
    vol_ratio = today_vol / ma_vol if ma_vol > 0 else 0.0
    if vol_ratio < p["gap_vol_ratio_min"]:
        return None
    vol_ratio_max = p.get("gap_vol_ratio_max")
    if vol_ratio_max is not None and vol_ratio >= vol_ratio_max:
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


# === Phase 1 新加策略 12:eps_acceleration(季 EPS 加速) ===

def _quarter_sort_key(period: str) -> tuple:
    """quarterly period 排序 key。FinMind period 多為 "YYYY-MM-DD"(季末日)
    或 "YYYY-Q[1-4]";直接字串比較剛好遞增。回 tuple 讓 sorted() 用。
    """
    return (period,)


def screen_eps_acceleration(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 12:連 2 季 EPS YoY > 0 且當季 YoY > 上季 YoY(加速)。

    意義:獲利動能加速 — 不只成長,還在「成長率本身」上升。
    資料來源:financials.period_type='quarterly';需要至少 5 季歷史
    (當季 + 上季 + 各對應的 4 季前)。
    """
    p = {**DEFAULT_EPS_ACCEL_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "curr_yoy", "prev_yoy", "matched_at",
    ]
    if not sids:
        return pd.DataFrame(columns=cols)

    min_q = int(p["min_quarters"])

    with db.get_conn() as conn:
        if len(sids) <= 500:
            placeholders = ",".join(["?"] * len(sids))
            meta = conn.execute(
                f"SELECT stock_id, name FROM stocks "
                f"WHERE stock_id IN ({placeholders})",
                sids,
            ).fetchall()
            fin_rows = conn.execute(
                f"SELECT stock_id, period, eps FROM financials "
                f"WHERE stock_id IN ({placeholders}) "
                f"AND period_type='quarterly' AND eps IS NOT NULL "
                f"ORDER BY stock_id, period DESC",
                sids,
            ).fetchall()
        else:
            meta = conn.execute(
                "SELECT stock_id, name FROM stocks WHERE market='TW'"
            ).fetchall()
            fin_rows = conn.execute(
                "SELECT stock_id, period, eps FROM financials "
                "WHERE period_type='quarterly' AND eps IS NOT NULL "
                "ORDER BY stock_id, period DESC"
            ).fetchall()
        name_map = {r["stock_id"]: r["name"] for r in meta}
        prices_by_sid = bulk_load_prices(conn, sids, date, 5)

    fin_by_sid: dict[str, list[dict]] = {}
    for r in fin_rows:
        fin_by_sid.setdefault(r["stock_id"], []).append({
            "period": r["period"],
            "eps": float(r["eps"]) if r["eps"] is not None else None,
        })

    rows: list[dict] = []
    for sid in sids:
        recs = fin_by_sid.get(sid, [])
        # 已經 ORDER BY period DESC,但保險再排一次
        recs = sorted(recs, key=lambda x: x["period"], reverse=True)
        if len(recs) < min_q:
            continue
        # idx: 0=當季 / 1=上季 / 4=當季-1y / 5=上季-1y
        try:
            curr_eps = recs[0]["eps"]
            prev_eps = recs[1]["eps"]
            curr_yoy_base = recs[4]["eps"] if len(recs) > 4 else None
            prev_yoy_base = recs[5]["eps"] if len(recs) > 5 else None
        except (IndexError, KeyError):
            continue
        if any(v is None or v == 0 for v in
               (curr_eps, prev_eps, curr_yoy_base, prev_yoy_base)):
            continue
        curr_yoy = (curr_eps - curr_yoy_base) / abs(curr_yoy_base) * 100
        prev_yoy = (prev_eps - prev_yoy_base) / abs(prev_yoy_base) * 100
        if curr_yoy <= 0 or prev_yoy <= 0:
            continue
        if curr_yoy <= prev_yoy:
            continue
        # close
        price_df = prices_by_sid.get(sid)
        close = 0.0
        if price_df is not None and not price_df.empty:
            close = float(price_df["close"].iloc[-1])
        rows.append({
            "stock_id": sid,
            "name": name_map.get(sid, sid),
            "close": close,
            "curr_yoy": float(curr_yoy),
            "prev_yoy": float(prev_yoy),
            "matched_at": date,
        })

    raw = pd.DataFrame(rows, columns=cols)
    return _enrich_with_targets(raw, date)


# === Phase 1 新加策略 13:high_yield_stable(殖利率異常高 + 獲利穩定) ===

def screen_high_yield_stable(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 13:當前殖利率 > 6% 且近 4 季 EPS 都為正(高殖利率 + 獲利穩定)。

    意義:存股族常用 — 高殖利率不是地雷(有持續獲利支撐)。
    資料來源:daily_metrics.dividend_yield + financials.eps quarterly。
    """
    p = {**DEFAULT_HIGH_YIELD_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "dividend_yield", "stable_quarters", "matched_at",
    ]
    if not sids:
        return pd.DataFrame(columns=cols)

    yield_min = float(p["yield_min_pct"])
    stable_q = int(p["stable_quarters"])

    with db.get_conn() as conn:
        if len(sids) <= 500:
            placeholders = ",".join(["?"] * len(sids))
            meta = conn.execute(
                f"SELECT stock_id, name FROM stocks "
                f"WHERE stock_id IN ({placeholders})",
                sids,
            ).fetchall()
            # 殖利率取最近一筆(<= date)— daily_metrics 不一定每天有
            yield_rows = conn.execute(
                f"SELECT stock_id, MAX(date) AS d, dividend_yield "
                f"FROM daily_metrics "
                f"WHERE stock_id IN ({placeholders}) "
                f"AND date <= ? AND dividend_yield IS NOT NULL "
                f"GROUP BY stock_id",
                (*sids, date),
            ).fetchall()
            fin_rows = conn.execute(
                f"SELECT stock_id, period, eps FROM financials "
                f"WHERE stock_id IN ({placeholders}) "
                f"AND period_type='quarterly' AND eps IS NOT NULL "
                f"ORDER BY stock_id, period DESC",
                sids,
            ).fetchall()
        else:
            meta = conn.execute(
                "SELECT stock_id, name FROM stocks WHERE market='TW'"
            ).fetchall()
            yield_rows = conn.execute(
                "SELECT stock_id, MAX(date) AS d, dividend_yield "
                "FROM daily_metrics "
                "WHERE date <= ? AND dividend_yield IS NOT NULL "
                "GROUP BY stock_id",
                (date,),
            ).fetchall()
            fin_rows = conn.execute(
                "SELECT stock_id, period, eps FROM financials "
                "WHERE period_type='quarterly' AND eps IS NOT NULL "
                "ORDER BY stock_id, period DESC"
            ).fetchall()
        name_map = {r["stock_id"]: r["name"] for r in meta}
        prices_by_sid = bulk_load_prices(conn, sids, date, 5)

    yield_by_sid = {
        r["stock_id"]: float(r["dividend_yield"])
        for r in yield_rows if r["dividend_yield"] is not None
    }
    eps_by_sid: dict[str, list[float]] = {}
    for r in fin_rows:
        eps_by_sid.setdefault(r["stock_id"], []).append(
            float(r["eps"]) if r["eps"] is not None else 0.0
        )

    rows: list[dict] = []
    for sid in sids:
        y = yield_by_sid.get(sid)
        if y is None or y < yield_min:
            continue
        eps_list = eps_by_sid.get(sid, [])
        if len(eps_list) < stable_q:
            continue
        if not all(e > 0 for e in eps_list[:stable_q]):
            continue
        price_df = prices_by_sid.get(sid)
        close = 0.0
        if price_df is not None and not price_df.empty:
            close = float(price_df["close"].iloc[-1])
        rows.append({
            "stock_id": sid,
            "name": name_map.get(sid, sid),
            "close": close,
            "dividend_yield": float(y),
            "stable_quarters": stable_q,
            "matched_at": date,
        })

    raw = pd.DataFrame(rows, columns=cols)
    return _enrich_with_targets(raw, date)


# === Phase 1 新加策略 14:inst_oversold_reversal(法人減碼後反轉) ===

def screen_inst_oversold_reversal(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 14:三大法人連 N 日淨賣超後當日轉買。

    意義:法人短期殺低後反手承接,常見大戶調節結束訊號。
    資料來源:institutional.total_buy_sell。覆蓋率:TOP_50 + watchlist。
    """
    from datetime import date as _date, timedelta as _td

    p = {**DEFAULT_INST_REVERSAL_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "down_days", "matched_at",
    ]
    n_down = int(p["down_days"])
    if not sids:
        return pd.DataFrame(columns=cols)

    with db.get_conn() as conn:
        if len(sids) <= 500:
            placeholders = ",".join(["?"] * len(sids))
            meta = conn.execute(
                f"SELECT stock_id, name FROM stocks "
                f"WHERE stock_id IN ({placeholders})",
                sids,
            ).fetchall()
            start_date = (
                _date.fromisoformat(date) - _td(days=(n_down + 1) * 3)
            ).isoformat()
            inst_rows = conn.execute(
                f"SELECT stock_id, date, total_buy_sell "
                f"FROM institutional "
                f"WHERE stock_id IN ({placeholders}) "
                f"AND date BETWEEN ? AND ? "
                f"ORDER BY stock_id, date DESC",
                (*sids, start_date, date),
            ).fetchall()
        else:
            meta = conn.execute(
                "SELECT stock_id, name FROM stocks WHERE market='TW'"
            ).fetchall()
            start_date = (
                _date.fromisoformat(date) - _td(days=(n_down + 1) * 3)
            ).isoformat()
            inst_rows = conn.execute(
                "SELECT stock_id, date, total_buy_sell "
                "FROM institutional "
                "WHERE date BETWEEN ? AND ? "
                "ORDER BY stock_id, date DESC",
                (start_date, date),
            ).fetchall()
        name_map = {r["stock_id"]: r["name"] for r in meta}
        prices_by_sid = bulk_load_prices(conn, sids, date, 5)

    inst_by_sid: dict[str, list[int]] = {}
    for r in inst_rows:
        inst_by_sid.setdefault(r["stock_id"], []).append(
            int(r["total_buy_sell"] or 0)
        )

    rows: list[dict] = []
    for sid in sids:
        recs = inst_by_sid.get(sid, [])
        if len(recs) < n_down + 1:
            continue
        # recs[0] = 最新一日(目標日);recs[1..n_down] = 前 N 日
        if recs[0] <= 0:
            continue
        if not all(v < 0 for v in recs[1:n_down + 1]):
            continue
        price_df = prices_by_sid.get(sid)
        close = 0.0
        if price_df is not None and not price_df.empty:
            close = float(price_df["close"].iloc[-1])
        rows.append({
            "stock_id": sid,
            "name": name_map.get(sid, sid),
            "close": close,
            "down_days": n_down,
            "matched_at": date,
        })

    raw = pd.DataFrame(rows, columns=cols)
    return _enrich_with_targets(raw, date)


# === Phase 1 新加策略 15:taiex_alpha(個股獨立行情) ===

def _load_taiex_pct_change(conn, date: str) -> float | None:
    """撈 TAIEX 當日相對昨日 close 漲跌 %。沒資料回 None。"""
    rows = conn.execute(
        "SELECT date, close FROM daily_prices "
        "WHERE stock_id='TAIEX' AND date <= ? "
        "ORDER BY date DESC LIMIT 2",
        (date,),
    ).fetchall()
    if len(rows) < 2 or rows[0]["date"] != date:
        return None
    prev = float(rows[1]["close"]) if rows[1]["close"] else 0.0
    curr = float(rows[0]["close"]) if rows[0]["close"] else 0.0
    if prev <= 0:
        return None
    return (curr - prev) / prev * 100


def screen_taiex_alpha(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 15:TAIEX 當日 < 0,個股當日漲 > 1%(個股逆大盤強勢)。

    意義:大盤跌而個股仍漲,通常代表獨立利多 / 強勢主力護盤。
    """
    p = {**DEFAULT_TAIEX_ALPHA_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "stock_pct", "taiex_pct", "matched_at",
    ]
    if not sids:
        return pd.DataFrame(columns=cols)

    with db.get_conn() as conn:
        taiex_pct = _load_taiex_pct_change(conn, date)
        if taiex_pct is None or taiex_pct >= float(p["taiex_down_pct_max"]):
            # 大盤沒資料 or 沒跌 → 整批回空
            return pd.DataFrame(columns=cols)

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
        prices_by_sid = bulk_load_prices(conn, sids, date, 5)

    rows: list[dict] = []
    up_min = float(p["stock_up_pct_min"])
    for sid in sids:
        df = prices_by_sid.get(sid)
        if df is None or len(df) < 2:
            continue
        if df["date"].iloc[-1] != date:
            continue
        curr_raw = df["close"].iloc[-1]
        prev_raw = df["close"].iloc[-2]
        if curr_raw is None or prev_raw is None or pd.isna(curr_raw) or pd.isna(prev_raw):
            continue
        curr = float(curr_raw)
        prev = float(prev_raw)
        if prev <= 0:
            continue
        stock_pct = (curr - prev) / prev * 100
        if stock_pct < up_min:
            continue
        rows.append({
            "stock_id": sid,
            "name": name_map.get(sid, sid),
            "close": curr,
            "stock_pct": float(stock_pct),
            "taiex_pct": float(taiex_pct),
            "matched_at": date,
        })

    raw = pd.DataFrame(rows, columns=cols)
    return _enrich_with_targets(raw, date)


# === Phase 1 新加策略 16:revenue_acceleration(月營收年增加速) ===

def screen_revenue_acceleration(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 16:月營收 YoY > 30% 且當月 YoY > 上月 YoY(加速)。

    意義:營收動能加速 — 月營收是台股最快公布的基本面數字(每月 10 號前)。
    資料來源:financials.period_type='monthly_revenue',revenue_yoy 直接用。
    """
    p = {**DEFAULT_REV_ACCEL_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "curr_yoy", "prev_yoy", "matched_at",
    ]
    if not sids:
        return pd.DataFrame(columns=cols)

    yoy_min = float(p["yoy_min_pct"])

    with db.get_conn() as conn:
        if len(sids) <= 500:
            placeholders = ",".join(["?"] * len(sids))
            meta = conn.execute(
                f"SELECT stock_id, name FROM stocks "
                f"WHERE stock_id IN ({placeholders})",
                sids,
            ).fetchall()
            rev_rows = conn.execute(
                f"SELECT stock_id, period, revenue_yoy FROM financials "
                f"WHERE stock_id IN ({placeholders}) "
                f"AND period_type='monthly_revenue' "
                f"AND revenue_yoy IS NOT NULL "
                f"ORDER BY stock_id, period DESC",
                sids,
            ).fetchall()
        else:
            meta = conn.execute(
                "SELECT stock_id, name FROM stocks WHERE market='TW'"
            ).fetchall()
            rev_rows = conn.execute(
                "SELECT stock_id, period, revenue_yoy FROM financials "
                "WHERE period_type='monthly_revenue' "
                "AND revenue_yoy IS NOT NULL "
                "ORDER BY stock_id, period DESC"
            ).fetchall()
        name_map = {r["stock_id"]: r["name"] for r in meta}
        prices_by_sid = bulk_load_prices(conn, sids, date, 5)

    rev_by_sid: dict[str, list[float]] = {}
    for r in rev_rows:
        rev_by_sid.setdefault(r["stock_id"], []).append(
            float(r["revenue_yoy"])
        )

    rows: list[dict] = []
    for sid in sids:
        recs = rev_by_sid.get(sid, [])
        if len(recs) < 2:
            continue
        curr_yoy = recs[0]
        prev_yoy = recs[1]
        if curr_yoy < yoy_min:
            continue
        if curr_yoy <= prev_yoy:
            continue
        price_df = prices_by_sid.get(sid)
        close = 0.0
        if price_df is not None and not price_df.empty:
            close = float(price_df["close"].iloc[-1])
        rows.append({
            "stock_id": sid,
            "name": name_map.get(sid, sid),
            "close": close,
            "curr_yoy": float(curr_yoy),
            "prev_yoy": float(prev_yoy),
            "matched_at": date,
        })

    raw = pd.DataFrame(rows, columns=cols)
    return _enrich_with_targets(raw, date)


# === 策略 17:千張戶進場(big_holder_inflow) ===

def screen_big_holder_inflow(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 17:千張戶(持股 ≥ 1000 張)本週進場 — Phase 2 滾動 mean+sigma 突破。

    意義:大戶進場 — 千張戶人數週增是「籌碼集中往大戶手裡跑」的訊號,
    通常領先股價 1-2 週(TDCC 週五公佈,週六凌晨入庫)。

    Phase 2 邏輯(對每檔 sid):
      1. 撈最近 rolling_weeks+1 週(預設 5 週)的 holders_delta_w
      2. 取前 rolling_weeks 週算 mean μ + std σ(sample std,ddof=1)
      3. 命中:本週 holders_delta_w > μ + sigma_threshold × σ
        (顯著高於近期常態 → 突破訊號)

    Fallback(歷史前期資料 < rolling_weeks):
      回 Phase 1 邏輯 — 在這批不足歷史的 sid 內取本週 delta_w > 0,P80(percentile)
      切 Top 20%。讓資料少的 universe 仍可運作。

    NULL / 缺資料處理:
    - shareholder_concentration 完全沒資料 → return []
    - 個別 sid 任一前期週 delta_w NULL → 視為歷史不足 → 進 fallback 候選
    - 個別 sid 本週 delta_w NULL → 不算命中
    - 沒 shareholder 資料的 sid → 不命中(不算空訊號)

    參數:
      percentile (float): Phase 1 fallback 用的 quantile,預設 0.80
      rolling_weeks (int): Phase 2 滾動視窗(前期週數),預設 4
      sigma_threshold (float): Phase 2 σ 倍數,預設 1.0
      min_delta_floor (int): Phase 2 絕對下限,本週 delta 必須 ≥ 此值,預設 5
        (只作用於 Phase 2 命中分支;Phase 1 fallback 不套用)

    date 在此策略單純是傳給 _enrich_with_targets 的 ATR 計算基準日。
    資料來源:shareholder_concentration 表最近 N 週(以 week_end DESC 取)。
    """
    p = {**DEFAULT_BIG_HOLDER_PARAMS, **(params or {})}
    sids = stock_ids or [s for s, _ in TW_TOP_50]
    cols = [
        "stock_id", "name", "close",
        "holders_delta_w", "holders_pct", "week_end", "matched_at",
    ]
    if not sids:
        return pd.DataFrame(columns=cols)

    percentile = float(p["percentile"])
    rolling_weeks = int(p["rolling_weeks"])
    sigma_threshold = float(p["sigma_threshold"])
    min_delta_floor = int(p["min_delta_floor"])
    needed_weeks = rolling_weeks + 1  # 前期 N 週 + 本週

    import sqlite3
    from collections import defaultdict

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

        # 撈最近 needed_weeks 個 distinct week_end(DESC)— 全 universe 的時間軸基準
        try:
            week_rows = conn.execute(
                "SELECT DISTINCT week_end FROM shareholder_concentration "
                "ORDER BY week_end DESC LIMIT ?",
                (needed_weeks,),
            ).fetchall()
        except sqlite3.OperationalError:
            return pd.DataFrame(columns=cols)
        if not week_rows:
            return pd.DataFrame(columns=cols)

        last_weeks = [r["week_end"] for r in week_rows]  # DESC,index 0 為 latest
        latest_week = last_weeks[0]

        # 撈這幾週內所有目標 sid 的 delta_w / pct
        wk_placeholders = ",".join(["?"] * len(last_weeks))
        if len(sids) <= 500:
            sid_placeholders = ",".join(["?"] * len(sids))
            sc_rows = conn.execute(
                f"SELECT sid, week_end, holders_delta_w, holders_pct "
                f"FROM shareholder_concentration "
                f"WHERE week_end IN ({wk_placeholders}) "
                f"  AND sid IN ({sid_placeholders})",
                (*last_weeks, *sids),
            ).fetchall()
        else:
            sc_rows = conn.execute(
                f"SELECT sid, week_end, holders_delta_w, holders_pct "
                f"FROM shareholder_concentration "
                f"WHERE week_end IN ({wk_placeholders})",
                tuple(last_weeks),
            ).fetchall()

        prices_by_sid = bulk_load_prices(conn, sids, date, 5)

    # 依 sid 分組 → 依 week_end ASC 排序
    per_sid: dict[str, list[dict]] = defaultdict(list)
    for r in sc_rows:
        per_sid[r["sid"]].append({
            "week_end": r["week_end"],
            "delta": r["holders_delta_w"],
            "pct": r["holders_pct"],
        })
    for sid in per_sid:
        per_sid[sid].sort(key=lambda x: x["week_end"])

    # 分流:Phase 2 命中 / Phase 1 fallback 候選
    sids_set = set(sids)
    phase2_hits: list[dict] = []
    fallback_candidates: list[dict] = []

    for sid in sids_set:
        rows_for_sid = per_sid.get(sid, [])
        if not rows_for_sid:
            continue  # 完全沒資料 → skip

        latest_row = next(
            (r for r in rows_for_sid if r["week_end"] == latest_week),
            None,
        )
        if latest_row is None or latest_row["delta"] is None:
            continue  # 本週 NULL / 無資料 → 不算命中
        latest_delta = int(latest_row["delta"])
        latest_pct = (
            float(latest_row["pct"]) if latest_row["pct"] is not None else None
        )

        # 取前期(latest 以外)非 NULL 的 delta
        prior_deltas = [
            r["delta"] for r in rows_for_sid
            if r["week_end"] != latest_week and r["delta"] is not None
        ]

        if len(prior_deltas) >= rolling_weeks:
            # Phase 2:取最近 rolling_weeks 個(ASC 排序後尾部)算 mean / sigma
            window = prior_deltas[-rolling_weeks:]
            s = pd.Series(window, dtype=float)
            mu = float(s.mean())
            sigma = float(s.std(ddof=1))
            if pd.isna(sigma):  # 理論上 n>=4 不會發生,保險
                sigma = 0.0
            threshold = mu + sigma_threshold * sigma
            if latest_delta > threshold and latest_delta >= min_delta_floor:
                phase2_hits.append({
                    "sid": sid,
                    "delta": latest_delta,
                    "pct": latest_pct,
                })
        else:
            # 歷史不足 → 進 Phase 1 fallback(留正向 delta 即可,P80 之後算)
            if latest_delta > 0:
                fallback_candidates.append({
                    "sid": sid,
                    "delta": latest_delta,
                    "pct": latest_pct,
                })

    # Phase 1 fallback:在 fallback_candidates 內取 P80 切 Top 20%
    fallback_hits: list[dict] = []
    if fallback_candidates:
        deltas_series = pd.Series([c["delta"] for c in fallback_candidates])
        fb_threshold = float(deltas_series.quantile(percentile))
        for c in fallback_candidates:
            if c["delta"] >= fb_threshold:
                fallback_hits.append(c)

    # 合併命中 → enrich close + targets
    rows: list[dict] = []
    for hit in (*phase2_hits, *fallback_hits):
        sid = hit["sid"]
        if sid not in sids_set:
            continue
        price_df = prices_by_sid.get(sid)
        close = 0.0
        if price_df is not None and not price_df.empty:
            close = float(price_df["close"].iloc[-1])
        rows.append({
            "stock_id": sid,
            "name": name_map.get(sid, sid),
            "close": close,
            "holders_delta_w": hit["delta"],
            "holders_pct": hit["pct"],
            "week_end": latest_week,
            "matched_at": date,
        })

    raw = pd.DataFrame(rows, columns=cols)
    return _enrich_with_targets(raw, date)


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
    # Phase 1 新加 5 個 strategies(基本面 / 殖利率 / 籌碼反轉 / 大盤 alpha / 營收)
    "eps_acceleration": screen_eps_acceleration,
    "high_yield_stable": screen_high_yield_stable,
    "inst_oversold_reversal": screen_inst_oversold_reversal,
    "taiex_alpha": screen_taiex_alpha,
    "revenue_acceleration": screen_revenue_acceleration,
    # 籌碼:千張戶進場(TDCC 千張大戶週快照)
    "big_holder_inflow": screen_big_holder_inflow,
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
    # Phase 1
    "eps_acceleration": "EPS 加速",
    "high_yield_stable": "高殖利率穩健",
    "inst_oversold_reversal": "法人反轉",
    "taiex_alpha": "獨立行情",
    "revenue_acceleration": "營收加速",
    # 籌碼:千張戶進場
    "big_holder_inflow": "千張戶進場",
}


# === Per-strategy ML 過濾門檻(Stage 2B 校準後落地) ===
# 來源:scripts/audit/calibrate_ml_thresholds.py --use-per-strategy-models 跑
# 30-day grid search 結果(Stage 2B 6 個 trained per-strategy models)。
#
# Stage 2A(通用 ML model)只有 ma_alignment 過 winner 條件;Stage 2B(per-
# strategy retrain)後 5 個 strategies 額外過 winner — 通用模型跟其他策略 alpha
# 信號重疊的問題確認被 per-strategy 重訓化解。
#
# Threshold 對照(per-strategy 校準 30d / 60d / 126d):
#   bias_convergence    0.65 → 100% WR (30d: 97 fires)
#   macd_golden         0.60 → 100% WR (30d: 30 fires)
#   bb_lower_rebound    0.50 → 75.8% WR (30d: 33 fires)
#   volume_breakout     0.65 → 100% WR (30d: 74 fires)
#   ma_alignment        0.55 → 100% WR (126d: 45 fires;60d: 26 fires < 30 邊緣)
#                       — Stage 2B 重校準把 0.60 → 0.55:同 100% WR 但 fires 多
#                       8 個(126d 37→45),60d 多 5 個(21→26)。60d 仍不過嚴格
#                       30-fire bar,但 126d 穩定 winner,保留入 dict。
#
# 沒過 winner / 沒跑(sample 太小)的策略不放 dict 內(.get → None,不過濾):
#   rsi_recovery(38 samples 結構性 sparse,200/365/560-day 全試過皆 38)/
#   volume_kd / ma_squeeze_breakout / inst_consensus / inst_silent_accum
#
# 2026-05-15 gap_up 下架 ML 過濾(走路 B):scripts/diagnose_gap_up.py 顯示
# 整體 WR 48.0% vs baseline 40.7%(+7.26pp edge),但 467-sample WF ROC=0.4926
# 接近 random — ML 從 16 個 v3 features 學不到「哪些 gap_up follow-through」。
# Diagnose 找到的真正 sub-edge 在 vol_ratio sweet spot(1.5-3x),用 rule-based
# 過濾(DEFAULT_GAP_UP_PARAMS.gap_vol_ratio_max=3.0)就能取得,不需要 ML。
# 詳見 docs/gap-up-decision-2026-05-15.md。
STRATEGY_ML_THRESHOLDS: dict[str, float] = {
    "ma_alignment": 0.55,
    "bias_convergence": 0.65,
    "macd_golden": 0.60,
    "bb_lower_rebound": 0.50,
    "volume_breakout": 0.65,
}


# === Per-strategy R:R 參數(Plan G Part 1 期望值優化校準) ===
# 來源:scripts/optimize_strategy_rr.py 60-day grid search 結果。
# Format:(target_pct, stop_pct, hold_days)。對 ML threshold 過濾後的 picks
# 跑 64 combos sweep,取 fires ≥ 10 中 EV(avg_return)最高者當 winner。
#
# Winner 結果(60-day, fires ≥ 10):
#   ma_alignment       (0.15, 0.03, 10) → EV +4.28% / WR 71.4% / 21 fires
#   bias_convergence   (0.15, 0.04,  5) → EV +5.90% / WR 92.7% / 41 fires
#   macd_golden        (0.10, 0.02, 10) → EV +6.62% / WR 75.0% / 16 fires
#   inst_consensus     (0.10, 0.03, 10) → EV +1.89% / WR 62.1% / 29 fires
#   bb_lower_rebound   (0.15, 0.04,  3) → EV +6.26% / WR 87.5% / 16 fires
#   rsi_recovery       (0.15, 0.03, 10) → EV -0.00% / WR 24.7% / 154 fires ⚠ EV ~0
#   inst_silent_accum  (0.15, 0.05, 10) → EV +1.22% / WR 65.2% / 46 fires
#   volume_breakout    (0.15, 0.04,  3) → EV +8.42% / WR 100.0% / 13 fires
#   gap_up             (0.15, 0.03,  5) → EV +6.80% / WR 65.5% / 29 fires
#
# 沒過 min_fires 的策略走 DEFAULT_RR_PARAMS(picks 太少不調):
#   volume_kd(2 picks),ma_squeeze_breakout(1 pick)
#
# rsi_recovery EV ≈ 0 caveat — 雖然技術上「winner」(fires ≥ 10 中最高 EV),但
# 該 EV 0.00% 表示就算照此 R:R 跑也賺不到錢。Plan G Part 4 對比驗證會
# 顯示出來,如要拿掉就改回 DEFAULT_RR_PARAMS 即可。
STRATEGY_RR_PARAMS: dict[str, tuple[float, float, int]] = {
    "ma_alignment": (0.15, 0.03, 10),
    "bias_convergence": (0.15, 0.04, 5),
    "macd_golden": (0.10, 0.02, 10),
    "inst_consensus": (0.10, 0.03, 10),
    "bb_lower_rebound": (0.15, 0.04, 3),
    "rsi_recovery": (0.15, 0.03, 10),
    "inst_silent_accum": (0.15, 0.05, 10),
    "volume_breakout": (0.15, 0.04, 3),
    "gap_up": (0.15, 0.03, 5),
}

# Default 給沒在 STRATEGY_RR_PARAMS 內的策略用(volume_kd / ma_squeeze_breakout
# 60-day picks 太少,沒法可信地校準)。
DEFAULT_RR_PARAMS: tuple[float, float, int] = (0.05, 0.03, 5)


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
    "industry", "industry_heat",
]


def _get_industries_map(sids: list[str]) -> dict[str, str]:
    """SELECT stock_id, industry FROM stocks IN (sids) → {sid: industry}。

    industry IS NULL / '' → 不入 dict(caller .map 拿 NaN)。給
    enrich_with_industry_heat 用。
    """
    if not sids:
        return {}
    placeholders = ",".join("?" * len(sids))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT stock_id, industry FROM stocks "
            f"WHERE stock_id IN ({placeholders}) "
            f"AND industry IS NOT NULL AND industry != ''",
            sids,
        ).fetchall()
    return {r["stock_id"]: r["industry"] for r in rows}


def enrich_with_analyst_target(df: pd.DataFrame) -> pd.DataFrame:
    """加 analyst_target_mean / analyst_num / analyst_target_prev_mean 欄
    (從 SQLite analyst_targets join)。

    df 缺 stock_id 欄 → 直接回(no-op);analyst_targets 表沒資料 → 三欄填 NaN。
    優先 yfinance source(get_analyst_targets_for_sids 已處理優先序)。
    `analyst_target_prev_mean` 用於 picks 推播 Δ 標示(主公 2026-05-08 拍板)。
    """
    if df is None or df.empty:
        if df is not None:
            df = df.copy()
            for col in (
                "analyst_target_mean", "analyst_num",
                "analyst_target_prev_mean",
            ):
                if col not in df.columns:
                    df[col] = pd.Series(dtype=float)
        return df
    if "stock_id" not in df.columns:
        return df

    from src.analyst_targets import get_analyst_targets_for_sids
    sids = df["stock_id"].astype(str).tolist()
    targets_map = get_analyst_targets_for_sids(sids)

    df = df.copy()
    df["analyst_target_mean"] = df["stock_id"].astype(str).map(
        lambda s: (targets_map.get(s) or {}).get("target_mean")
    )
    df["analyst_num"] = df["stock_id"].astype(str).map(
        lambda s: (targets_map.get(s) or {}).get("num_analysts")
    )
    df["analyst_target_prev_mean"] = df["stock_id"].astype(str).map(
        lambda s: (targets_map.get(s) or {}).get("previous_target_mean")
    )
    return df


def enrich_with_industry_heat(df: pd.DataFrame) -> pd.DataFrame:
    """加 industry + industry_heat 欄。industry_heat = 同 df 內同產業 fire 數。

    產業熱度高(同類股被多策略集中命中)→ 通常代表類股輪動 / 法人關注,
    UI 用此排序加分;同分時熱門產業排前面。
    industry 缺 / IS NULL → industry_heat = 0(不加分)。
    """
    if df is None or df.empty:
        if df is not None:
            df = df.copy()
            for col in ("industry", "industry_heat"):
                if col not in df.columns:
                    df[col] = pd.Series(dtype=object if col == "industry" else int)
        return df
    if "stock_id" not in df.columns:
        return df

    sids = df["stock_id"].astype(str).tolist()
    industries = _get_industries_map(sids)

    df = df.copy()
    df["industry"] = df["stock_id"].astype(str).map(industries)
    counts = df["industry"].dropna().value_counts().to_dict()
    df["industry_heat"] = df["industry"].map(counts).fillna(0).astype(int)
    return df


def aggregated_to_dataframe(agg: dict[str, dict]) -> pd.DataFrame:
    """把 run_all_strategies 結果攤平成 DataFrame 給 UI 顯示。

    欄位含目標價(target_low / target_high / stop_loss / risk_reward / atr14)
    + 產業 (industry / industry_heat)— 從任一策略 details 取得目標價,
    industry 從 stocks 表 JOIN。

    排序:信號數 desc → industry_heat desc(同類股集中加分)→ stock_id asc。
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
    df = pd.DataFrame(rows)
    df = enrich_with_industry_heat(df)
    # reindex 確保 schema 完整(空欄補 NaN)
    df = df.reindex(columns=_AGGREGATED_COLUMNS)
    return df.sort_values(
        ["信號數", "industry_heat", "stock_id"],
        ascending=[False, False, True],
    ).reset_index(drop=True)


# === 大盤環境感知:策略 → 類別 → regime 過濾 ===
# 這份是 strategies.py 的「真實來源」(app.py 的 _STRATEGY_CATEGORY 為 UI tab 顏色用,
# 一致;但這份對 src/* 模組可見,讓 market_regime.filter_strategies_by_regime 用)。
STRATEGY_CATEGORY: dict[str, str] = {
    "volume_kd": "動能",
    "ma_alignment": "趨勢",
    "bias_convergence": "反轉",
    "macd_golden": "趨勢",
    "ma_squeeze_breakout": "趨勢",
    "inst_consensus": "籌碼",
    "bb_lower_rebound": "反轉",
    "rsi_recovery": "反轉",
    "inst_silent_accum": "籌碼",
    "volume_breakout": "動能",
    "gap_up": "動能",
    # Phase 1 加 5 個策略的分類
    "eps_acceleration": "基本面",
    "high_yield_stable": "殖利率",
    "inst_oversold_reversal": "籌碼",
    "taiex_alpha": "大盤",
    "revenue_acceleration": "基本面",
    # 籌碼:千張戶進場
    "big_holder_inflow": "籌碼",
}

# regime → 哪些 category 該開。bull = 全開(value=None);其他 regime 只留某些 cat。
# 設計依據:
#   bull:大盤多頭趨勢,所有策略都吃得到順風,無過濾
#   weak_bull:大盤弱勢但未跌破 MA60,趨勢策略風險高,留反轉/基本面/殖利率/動能
#   sideways:盤整 chopping,趨勢策略最容易雙吐,留反轉/籌碼選個股 alpha
#   bear:空頭環境,只剩防禦性的籌碼/殖利率/獨立行情(逆勢個股)能搏
STRATEGY_REGIME_FILTER: dict[str, set[str] | None] = {
    "bull":      None,
    "weak_bull": {"反轉", "基本面", "殖利率", "動能"},
    "sideways":  {"反轉", "籌碼"},
    "bear":      {"籌碼", "殖利率", "大盤"},
}


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
    "screen_big_holder_inflow",
    "run_all_strategies",
    "aggregated_to_dataframe",
    "enrich_with_industry_heat",
    "enrich_with_analyst_target",
    "compute_target_prices",
    "ALL_STRATEGIES",
    "STRATEGY_LABELS",
    "STRATEGY_CATEGORY",
    "STRATEGY_REGIME_FILTER",
    "STRATEGY_ML_THRESHOLDS",
    "STRATEGY_RR_PARAMS",
    "DEFAULT_RR_PARAMS",
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
    "DEFAULT_BIG_HOLDER_PARAMS",
    "TARGET_LOW_MULT",
    "TARGET_HIGH_MULT",
    "STOP_LOSS_MULT",
]
