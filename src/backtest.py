"""歷史回測引擎 — 對每個 strategy 跑過去 N 個交易日,統計勝率 / 平均報酬。

設計重點:
- **目標報酬 / 停損百分比固定**(由 caller 傳;預設 +5% / -3%)— 跟個股
  ATR 無關,純 % 為單位。理由:跨策略 / 跨股可比性。
- **持有期 N 天固定**(預設 5 個交易日)。
- **勝負判定簡化**(沒分 neutral,簡化成 win / lose):
  * 持有期內 high 觸 target (1+target_pct)× entry → win, return = +target_pct
  * 持有期內 low  觸 stop   (1-stop_pct)  × entry → lose, return = -stop_pct
  * 同日兩個都觸到(intra-day path 不可知)→ **保守視為先觸停損**(lose)
  * N 天結束都沒觸 → close at D+N close;return > 0 → win;else lose
- **盤整退場簡化**:現實有平盤、停利停損,但簡化版只看「最終 D+N close vs entry」
  決定 win / lose,不分 neutral。

Output schema(餵 db.dump_strategy_backtest):
    {strategy, period_end, lookback_days, target_pct, stop_pct, hold_days,
     n_fires, n_wins, win_rate, avg_return, computed_at}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import pandas as pd

from src import database as db
from src._bulk_load import bulk_load_prices
from src.strategies import ALL_STRATEGIES

logger = logging.getLogger(__name__)


def simulate_outcome(
    ohlc_future: pd.DataFrame,
    entry_price: float,
    target_pct: float = 0.05,
    stop_pct: float = 0.03,
) -> tuple[str, float]:
    """模擬持有 N 天的結局 — 回 (outcome, return_pct)。

    Args:
        ohlc_future: D+1 ~ D+N 的 OHLC DataFrame(需含 high / low / close 欄)
        entry_price: 進場價(D 收盤)
        target_pct: 目標 % (default 0.05 = +5%)
        stop_pct: 停損 % (default 0.03 = -3%)

    Returns:
        outcome: 'win' | 'lose'
        return_pct: 報酬率(decimal,e.g. 0.05 = +5%)

    決策規則:
        1. 逐日掃 D+1 ~ D+N
        2. 該日 high >= entry × (1+target) → win, return = +target_pct, 結束
        3. 該日 low  <= entry × (1-stop)   → lose, return = -stop_pct, 結束
        4. 同日兩個都觸 → 保守視為先觸停損(lose)
        5. N 天結束都沒觸 → close at D+N close,(close-entry)/entry,
           > 0 → win,else → lose
    """
    if entry_price <= 0:
        # 邊角:沒 entry → 視為 lose 0(避免除零),caller 通常會先 filter
        return ("lose", 0.0)

    target_price = entry_price * (1 + target_pct)
    stop_price = entry_price * (1 - stop_pct)

    last_close = entry_price
    for _, row in ohlc_future.iterrows():
        high = float(row.get("high", 0) or 0)
        low = float(row.get("low", 0) or 0)
        close = float(row.get("close", 0) or 0)
        last_close = close
        hit_target = high >= target_price
        hit_stop = low <= stop_price
        # 同日兩邊都觸 → 保守視為先觸停損(intra-day path 不可知,壞情境)
        if hit_stop:
            return ("lose", -stop_pct)
        if hit_target:
            return ("win", target_pct)

    # N 天結束沒觸 → 看 D+N close vs entry
    if last_close <= 0:
        return ("lose", 0.0)
    final_return = (last_close - entry_price) / entry_price
    if final_return > 0:
        return ("win", final_return)
    return ("lose", final_return)


def _list_trading_dates(end_date: str, lookback_days: int) -> list[str]:
    """從 daily_prices 撈 end_date 之前 lookback_days 個交易日(含 end_date)。
    回升序日期 list(舊 → 新)。
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_prices "
            "WHERE date <= ? AND stock_id != 'TAIEX' "
            "ORDER BY date DESC LIMIT ?",
            (end_date, lookback_days),
        ).fetchall()
    dates = [r["date"] for r in rows]
    return sorted(dates)


def backtest_strategy(
    strategy_name: str,
    universe: list[str],
    period_end: str,
    lookback_days: int = 126,
    target_pct: float = 0.05,
    stop_pct: float = 0.03,
    hold_days: int = 5,
    params: dict | None = None,
    ml_filter: float | None = None,
    ml_model=None,
) -> dict:
    """跑單一 strategy 在過去 lookback_days 日的回測。

    流程:
    1. 列出過去 lookback_days 個交易日(從 daily_prices 撈)
    2. 對每個交易日 D(除了最後 hold_days 天 — 沒足夠未來資料模擬):
       a. 跑 ALL_STRATEGIES[strategy_name](D, universe, params)
       b. 對每張 pick:
          - 撈 D+1 ~ D+hold_days 的 OHLC(從 daily_prices)
          - simulate_outcome → (outcome, return_pct)
          - 累計 fires / wins / total_return
    3. 回 dict {n_fires, n_wins, win_rate, avg_return}

    Args:
        strategy_name: ALL_STRATEGIES 內的 key
        universe: list[str] of sids
        period_end: 'YYYY-MM-DD'
        lookback_days: 取過去多少交易日
        target_pct/stop_pct/hold_days: simulate_outcome 參數
        params: 自訂 strategy params(None = 走各 screener DEFAULT_*_PARAMS)

    Returns:
        {n_fires, n_wins, win_rate, avg_return}
    """
    if strategy_name not in ALL_STRATEGIES:
        raise ValueError(
            f"未知 strategy: {strategy_name}. "
            f"可選: {list(ALL_STRATEGIES.keys())}"
        )
    screen_fn = ALL_STRATEGIES[strategy_name]

    all_dates = _list_trading_dates(period_end, lookback_days)
    if len(all_dates) < hold_days + 1:
        logger.warning(
            "[BACKTEST] %s: 歷史不足 — 只 %d 天 < %d hold_days",
            strategy_name, len(all_dates), hold_days + 1,
        )
        return {"n_fires": 0, "n_wins": 0, "win_rate": 0.0, "avg_return": 0.0}

    # 排除最後 hold_days 天(沒足夠未來資料模擬)
    pickable_dates = all_dates[: -hold_days]

    n_fires = 0
    n_wins = 0
    total_return = 0.0

    # 一次撈整段 OHLC 進記憶體 — 避免 N×M 次 SQL(N 天 × M 檔 picks)
    # bulk_load_prices 接 (sids, target_date, lookback_days) → dict[sid] -> df
    end_for_bulk = all_dates[-1]
    bulk_lookback = lookback_days + hold_days + 5  # 緩衝
    with db.get_conn() as conn:
        prices_by_sid = bulk_load_prices(
            conn, universe, end_for_bulk, lookback_days=bulk_lookback,
        )

    for D in pickable_dates:
        # 跑 strategy 拿 picks
        try:
            df = screen_fn(D, params=params, stock_ids=universe)
        except Exception as e:  # noqa: BLE001
            logger.debug("[BACKTEST] %s @ %s screener error: %s", strategy_name, D, e)
            continue
        if df is None or df.empty:
            continue

        # ML 過濾(如果指定):對該 D 的所有 picks 一次 batch predict_proba
        ml_probs_for_d: dict[str, float | None] = {}
        if ml_filter is not None and ml_model is not None:
            from src.ml_predictor import predict_batch
            sids_for_pred = [str(r["stock_id"]) for _, r in df.iterrows()]
            ml_probs_for_d = predict_batch(ml_model, sids_for_pred, D)

        for _, pick_row in df.iterrows():
            sid = str(pick_row["stock_id"])
            entry_price = float(pick_row.get("close", 0) or 0)
            if entry_price <= 0:
                continue

            # ML filter:prob >= filter 才算 fire(None 視為過濾掉)
            if ml_filter is not None:
                prob = ml_probs_for_d.get(sid)
                if prob is None or prob < ml_filter:
                    continue

            sid_df = prices_by_sid.get(sid)
            if sid_df is None or sid_df.empty:
                continue

            # 找 D+1 ~ D+hold_days 的 OHLC(在 sid_df 內)
            # sid_df 已 sorted asc by date(bulk_load_prices 的 contract)
            future = sid_df[sid_df["date"] > D].head(hold_days)
            if len(future) < hold_days:
                # 接近 period_end 沒 hold_days 天未來 — 跳過(該被 pickable_dates
                # filter 掉,但保險)
                continue

            outcome, ret = simulate_outcome(
                future, entry_price,
                target_pct=target_pct, stop_pct=stop_pct,
            )
            n_fires += 1
            total_return += ret
            if outcome == "win":
                n_wins += 1

    win_rate = (n_wins / n_fires) if n_fires > 0 else 0.0
    avg_return = (total_return / n_fires) if n_fires > 0 else 0.0
    return {
        "n_fires": n_fires,
        "n_wins": n_wins,
        "win_rate": win_rate,
        "avg_return": avg_return,
    }


def backtest_all_strategies(
    universe: list[str],
    period_end: str,
    lookback_days: int = 126,
    target_pct: float = 0.05,
    stop_pct: float = 0.03,
    hold_days: int = 5,
    params: dict | None = None,
    strategies: Iterable[str] | None = None,
    ml_filter: float | None = None,
) -> list[dict]:
    """跑全部(或指定子集)strategies — 回 list[dict] 餵 db.dump_strategy_backtest。

    Args:
        strategies: None = 跑 ALL_STRATEGIES 全 11 套;否則 subset
        ml_filter: 若 != None,只 count 那些 ML prob >= 該值的 picks 進 fires
            (給 with-ML 對比 baseline 驗證用)。Model 在 caller 之前載入並傳
            給每 strategy(避免每 strategy 重 load 一次)。
    """
    keys = list(strategies) if strategies else list(ALL_STRATEGIES.keys())
    computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results: list[dict] = []

    # 如果要 ML filter,先 load model 一次(後面每 strategy 共用)
    ml_model = None
    if ml_filter is not None:
        try:
            from src import config as _config
            from src.ml_predictor import load_model
            from pathlib import Path as _Path
            model_path = _Path(_config.PROJECT_ROOT) / "models" / "short_pick.pkl"
            if model_path.exists():
                ml_model = load_model(model_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("[BACKTEST] ML model load 失敗: %s", e)

    for name in keys:
        stats = backtest_strategy(
            name, universe, period_end,
            lookback_days=lookback_days,
            target_pct=target_pct, stop_pct=stop_pct,
            hold_days=hold_days, params=params,
            ml_filter=ml_filter, ml_model=ml_model,
        )
        results.append({
            "strategy": name,
            "period_end": period_end,
            "lookback_days": lookback_days,
            "target_pct": target_pct,
            "stop_pct": stop_pct,
            "hold_days": hold_days,
            "n_fires": stats["n_fires"],
            "n_wins": stats["n_wins"],
            "win_rate": stats["win_rate"],
            "avg_return": stats["avg_return"],
            "computed_at": computed_at,
        })
    return results


__all__ = [
    "simulate_outcome",
    "backtest_strategy",
    "backtest_all_strategies",
]
