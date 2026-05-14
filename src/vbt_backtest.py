"""vectorbt 回測 wrapper — 對既有 17 個短線策略做策略級 grid search。

設計重點:
- 不取代既有 src/backtest.py(逐 pick simulate_outcome,目標 / 停損 路徑模擬),
  vectorbt 是「策略級多參數 grid search」工具,主要回答「哪組參數最佳」,
  不取代「這張 pick 模擬持有 N 天會怎樣」。
- 流程:
    1. 對每組 params,逐個交易日 D 跑 ALL_STRATEGIES[name](D, params) 拿 picks
    2. 把這些 (D, sid) 對轉成 vbt entries 矩陣(columns=sid, index=date,bool)
    3. exits 用「固定持有 hold_days 後出場」(vectorbt 內建 sl_stop / tp_stop
       可選,但首版用簡化的 hold_days 出場以對齊既有 pick_outcomes 思維)
    4. vbt.Portfolio.from_signals → stats → 整理回單一 row
- 不 mutate 既有 strategy / ml / fetcher 邏輯。
- params_hash 用 sha1(json.dumps(params, sort_keys=True))[:12] 做主鍵。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src import database as db
from src._bulk_load import bulk_load_prices
from src.strategies import ALL_STRATEGIES

logger = logging.getLogger(__name__)


# 預設交易成本:台股手續費 0.1425% / 雙邊,證交稅 0.3% 賣方
# vectorbt fees 是每次交易單邊,所以買進賣出加總大概 0.1425%×2 + 0.3% = 0.585%
# 簡化:fees=0.001425(進場手續費)+ slippage=0.001(滑點 0.1%)
DEFAULT_FEES = 0.001425
DEFAULT_SLIPPAGE = 0.001
DEFAULT_INIT_CASH = 1_000_000.0  # 100 萬本金
DEFAULT_HOLD_DAYS = 5


def _hash_params(params: dict[str, Any]) -> str:
    """sha1(json.dumps(params, sort_keys=True))[:12] — 給 vbt_grid_results PK。"""
    payload = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _list_trading_dates_in_range(start_date: str, end_date: str) -> list[str]:
    """從 daily_prices 撈 [start_date, end_date] 區間的交易日,升序回。

    跟 src/backtest._list_trading_dates 不同 — 走區間 BETWEEN,不是 lookback N 天。
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_prices "
            "WHERE date BETWEEN ? AND ? AND stock_id != 'TAIEX' "
            "ORDER BY date ASC",
            (start_date, end_date),
        ).fetchall()
    return [r["date"] for r in rows]


def expand_params_grid(grid: dict[str, Iterable]) -> list[dict[str, Any]]:
    """把 {param_name: [v1, v2, ...]} 展成 list[dict],各 param 的 cartesian product。

    e.g. {"a": [1, 2], "b": [0.1, 0.2]} → [{"a":1,"b":0.1}, {"a":1,"b":0.2},
                                             {"a":2,"b":0.1}, {"a":2,"b":0.2}]
    空 grid → 回 [{}](代表跑一次 default)。
    """
    if not grid:
        return [{}]
    keys = list(grid.keys())
    values = [list(grid[k]) for k in keys]
    return [
        dict(zip(keys, combo, strict=False))
        for combo in itertools.product(*values)
    ]


def _clean_close_matrix(close: pd.DataFrame) -> pd.DataFrame:
    """把 close DataFrame 清成 vbt 可用的格式(全 finite & > 0)。

    步驟:
    1. close <= 0 統一當 NaN(壞資料,e.g. 上櫃權證在沒成交日寫 0)
    2. ffill 把「停牌 / 資料殘缺」中間的洞補成上一個有效價
    3. bfill 把「上市前 / 資料起點之前」的 leading NaN 補成第一個有效價
    4. 還是全 NaN 的欄位丟掉(整檔都沒資料)

    leading bfill 是安全的:screen_* 在資料不足的早期日子,lookback 達不到
    `min_required` 會直接跳過該 sid,entries 不會在 bfill 出來的假價上觸發
    → trade-level PnL 不受影響。

    沒這層處理時(舊版只 ffill + 丟任意-NaN-欄),6 個月以上 universe 大量
    sid 因 leading NaN 整欄被丟掉 → close.empty → grid 寫 0 trades
    (即 macd_golden 全 0 trades 的成因)。
    """
    if close.empty:
        return close
    close = close.mask(close <= 0)
    close = close.ffill().bfill()
    valid_cols = close.columns[~close.isna().any()]
    return close[valid_cols]


def _build_signals_matrix(
    strategy_name: str,
    params: dict[str, Any],
    trading_dates: list[str],
    universe: list[str],
) -> dict[str, pd.DataFrame] | None:
    """對每個交易日 D 跑 strategy(D, params) 收 picks,組成 vbt 用矩陣。

    回 dict {
        "close": pd.DataFrame[date, sid] = 收盤,
        "entries": pd.DataFrame[date, sid] = bool(True 代表那天該 sid 命中策略),
        "n_entries": int 命中總次數,
    }
    全空 → 回 None。
    """
    if strategy_name not in ALL_STRATEGIES:
        raise ValueError(f"未知 strategy: {strategy_name}")
    screen_fn = ALL_STRATEGIES[strategy_name]

    # 收集 entries:set[(date, sid)]
    entry_pairs: set[tuple[str, str]] = set()
    for D in trading_dates:
        try:
            df = screen_fn(D, params=params, stock_ids=universe)
        except Exception as e:  # noqa: BLE001
            logger.debug("[VBT] %s @ %s screener error: %s", strategy_name, D, e)
            continue
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            sid = str(row["stock_id"])
            if sid in universe:
                entry_pairs.add((D, sid))

    if not entry_pairs:
        return None

    # 為了 vbt:需要 close + entries DataFrame(同 index, 同 columns)
    # 只收 universe 內、且有 entries 出現過的 sid,降低 matrix 維度
    active_sids = sorted({sid for _, sid in entry_pairs})

    end_date = trading_dates[-1]
    lookback = len(trading_dates) + DEFAULT_HOLD_DAYS + 5
    with db.get_conn() as conn:
        prices_by_sid = bulk_load_prices(conn, active_sids, end_date, lookback)

    # 收集所有交易日(把 hold_days 緩衝也加進去,讓 exits 不落在矩陣外)
    all_dates = set(trading_dates)
    for sid_df in prices_by_sid.values():
        if sid_df is None or sid_df.empty:
            continue
        for d in sid_df["date"]:
            all_dates.add(d)
    full_dates = sorted(all_dates)

    close_data: dict[str, list[float]] = {}
    for sid in active_sids:
        sid_df = prices_by_sid.get(sid)
        if sid_df is None or sid_df.empty:
            continue
        # 重新索引到 full_dates
        sid_map = dict(zip(sid_df["date"], sid_df["close"], strict=False))
        close_data[sid] = [
            float(sid_map[d]) if d in sid_map and sid_map[d] is not None else np.nan
            for d in full_dates
        ]

    if not close_data:
        return None

    close = pd.DataFrame(close_data, index=pd.to_datetime(full_dates))
    close = _clean_close_matrix(close)
    if close.empty:
        return None

    entries = pd.DataFrame(
        False, index=close.index, columns=close.columns, dtype=bool,
    )
    for D, sid in entry_pairs:
        if sid not in entries.columns:
            continue
        ts = pd.Timestamp(D)
        if ts in entries.index:
            entries.loc[ts, sid] = True

    return {"close": close, "entries": entries, "n_entries": len(entry_pairs)}


def _make_exits_after_hold(entries: pd.DataFrame, hold_days: int) -> pd.DataFrame:
    """對每個 entries=True,在 hold_days 個交易日後放 exits=True。

    在 index 範圍外的 → 落在最後一根 bar(避免被丟）。
    """
    exits = pd.DataFrame(
        False, index=entries.index, columns=entries.columns, dtype=bool,
    )
    n = len(entries.index)
    for col in entries.columns:
        positions = np.where(entries[col].to_numpy())[0]
        for pos in positions:
            exit_pos = min(pos + hold_days, n - 1)
            exits.iat[exit_pos, exits.columns.get_loc(col)] = True
    return exits


def _portfolio_stats(
    close: pd.DataFrame,
    entries: pd.DataFrame,
    exits: pd.DataFrame,
    *,
    fees: float = DEFAULT_FEES,
    slippage: float = DEFAULT_SLIPPAGE,
    init_cash: float = DEFAULT_INIT_CASH,
) -> dict[str, float]:
    """vbt.Portfolio.from_signals → 整理回 dict 報酬指標。"""
    import vectorbt as vbt

    pf = vbt.Portfolio.from_signals(
        close,
        entries=entries,
        exits=exits,
        fees=fees,
        slippage=slippage,
        init_cash=init_cash,
    )

    # 多 column portfolio:每 column 多半只 1-2 trades(策略稀疏命中),per-column
    # sharpe_ratio() 對沒 trades 的 column 是 NaN,mean 把全集稀釋掉。改用 trade-level
    # PnL pct 計算策略整體指標:
    #   - total_return = mean of trade returns × 100(每筆獨立部位的平均報酬 %)
    #   - sharpe       = mean / std × sqrt(N)(annualized over the trade sample)
    #   - max_drawdown = trade-level worst loss(% 正值)
    #   - win_rate     = # winning trades / N × 100
    n_trades = 0
    total_return = 0.0
    sharpe = 0.0
    max_dd = 0.0
    win_rate = 0.0
    try:
        trades = pf.trades
        records = trades.records_readable
        if records is not None and not records.empty:
            ret_col = None
            for c in ("Return", "Return [%]", "PnL", "Profit"):
                if c in records.columns:
                    ret_col = c
                    break
            if ret_col is not None:
                returns = pd.to_numeric(records[ret_col], errors="coerce").dropna()
                # Return 在 vbt 1.0 為 fraction(0.05 = +5%);% 欄位已 *100
                if ret_col != "Return [%]":
                    returns_pct = returns * 100
                else:
                    returns_pct = returns
                n_trades = int(len(returns_pct))
                if n_trades > 0:
                    total_return = float(returns_pct.mean())
                    win_rate = float((returns_pct > 0).sum() / n_trades * 100)
                    max_dd = float(abs(returns_pct.min()))
                    if n_trades >= 2 and float(returns_pct.std(ddof=1)) > 0:
                        sharpe = float(
                            returns_pct.mean()
                            / returns_pct.std(ddof=1)
                            * np.sqrt(n_trades)
                        )
                    if np.isnan(sharpe) or np.isinf(sharpe):
                        sharpe = 0.0
    except Exception as e:  # noqa: BLE001
        logger.debug("[VBT] portfolio stats compute failed: %s", e)

    return {
        "total_return": total_return,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "win_rate": win_rate,
        "n_trades": n_trades,
    }


def backtest_strategy_with_params(
    strategy_name: str,
    params_grid: dict[str, Iterable],
    start_date: str,
    end_date: str,
    universe: list[str],
    *,
    hold_days: int = DEFAULT_HOLD_DAYS,
    fees: float = DEFAULT_FEES,
    slippage: float = DEFAULT_SLIPPAGE,
    init_cash: float = DEFAULT_INIT_CASH,
) -> pd.DataFrame:
    """對單一策略跑 params_grid 內所有組合,回 stats DataFrame。

    Args:
        strategy_name: ALL_STRATEGIES 內的 key
        params_grid: e.g. {"vbo_vol_ratio_min": [1.5, 2.0, 2.5], "highest_lookback": [5, 10, 20]}
        start_date / end_date: 'YYYY-MM-DD' 區間(自動撈所有交易日)
        universe: list[str] of sids
        hold_days: 持有期(預設 5 個交易日後出場)
        fees / slippage / init_cash: vbt.Portfolio.from_signals 參數

    Returns:
        DataFrame columns:
            strategy, params_hash, params_json, params_dict,
            n_trades, total_return, sharpe, max_drawdown, win_rate
        按 sharpe DESC 排。
    """
    if strategy_name not in ALL_STRATEGIES:
        raise ValueError(f"未知 strategy: {strategy_name}")

    trading_dates = _list_trading_dates_in_range(start_date, end_date)
    if len(trading_dates) < hold_days + 2:
        logger.warning(
            "[VBT] %s: 交易日不足(%d < %d)— 回空 DataFrame",
            strategy_name, len(trading_dates), hold_days + 2,
        )
        return pd.DataFrame()

    combos = expand_params_grid(params_grid)
    logger.info(
        "[VBT] %s: %d 組合 × %d 交易日",
        strategy_name, len(combos), len(trading_dates),
    )

    rows: list[dict] = []
    for params in combos:
        signals = _build_signals_matrix(strategy_name, params, trading_dates, universe)
        if signals is None:
            rows.append({
                "strategy": strategy_name,
                "params_hash": _hash_params(params),
                "params_json": json.dumps(params, sort_keys=True),
                "params_dict": params,
                "n_trades": 0,
                "total_return": 0.0,
                "sharpe": 0.0,
                "max_drawdown": 0.0,
                "win_rate": 0.0,
            })
            continue
        exits = _make_exits_after_hold(signals["entries"], hold_days)
        stats = _portfolio_stats(
            signals["close"], signals["entries"], exits,
            fees=fees, slippage=slippage, init_cash=init_cash,
        )
        rows.append({
            "strategy": strategy_name,
            "params_hash": _hash_params(params),
            "params_json": json.dumps(params, sort_keys=True),
            "params_dict": params,
            **stats,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    return df.sort_values("sharpe", ascending=False).reset_index(drop=True)


def persist_grid_results(
    results_df: pd.DataFrame,
    *,
    period_start: str,
    period_end: str,
    db_path=None,
) -> int:
    """把 backtest_strategy_with_params 的結果 UPSERT 進 vbt_grid_results。

    跳過 params_dict 欄(database 只存 params_json)。
    """
    if results_df is None or results_df.empty:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    for _, r in results_df.iterrows():
        rows.append({
            "strategy": str(r["strategy"]),
            "params_hash": str(r["params_hash"]),
            "params_json": str(r["params_json"]),
            "period_start": period_start,
            "period_end": period_end,
            "n_trades": int(r["n_trades"]),
            "total_return": float(r["total_return"]),
            "sharpe": float(r["sharpe"]),
            "max_drawdown": float(r["max_drawdown"]),
            "win_rate": float(r["win_rate"]),
            "generated_at": now,
        })
    return db.upsert_vbt_grid_results(rows, db_path=db_path)


__all__ = [
    "DEFAULT_FEES",
    "DEFAULT_SLIPPAGE",
    "DEFAULT_INIT_CASH",
    "DEFAULT_HOLD_DAYS",
    "_clean_close_matrix",
    "expand_params_grid",
    "backtest_strategy_with_params",
    "persist_grid_results",
]
