"""src/vbt_backtest.py 單元測試 — wrapper / signals matrix / stats / persist。

策略本身已有 test_strategies / test_backtest 覆蓋,這邊只測 vbt 整合層:
- params hash 穩定
- params grid 展開
- 對 1 個策略跑出非空結果
- _portfolio_stats 對 fixture entries 算對基本指標
- persist UPSERT 寫表
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import config, database as db
from src.vbt_backtest import (
    _build_signals_matrix,
    _clean_close_matrix,
    _hash_params,
    _make_exits_after_hold,
    _portfolio_stats,
    backtest_strategy_with_params,
    expand_params_grid,
    persist_grid_results,
)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "vbt.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


# === pure helpers (不需 DB) ===

def test_hash_params_stable():
    """同樣的 dict 不論 key 順序 → 同樣 hash。"""
    h1 = _hash_params({"a": 1, "b": 2.0})
    h2 = _hash_params({"b": 2.0, "a": 1})
    assert h1 == h2
    assert len(h1) == 12


def test_hash_params_distinct():
    """不同 params → 不同 hash。"""
    h1 = _hash_params({"a": 1})
    h2 = _hash_params({"a": 2})
    assert h1 != h2


def test_expand_params_grid_cartesian():
    """{a:[1,2], b:[0.1, 0.2]} → 4 組合。"""
    grid = {"a": [1, 2], "b": [0.1, 0.2]}
    combos = expand_params_grid(grid)
    assert len(combos) == 4
    assert {"a": 1, "b": 0.1} in combos
    assert {"a": 2, "b": 0.2} in combos


def test_expand_params_grid_empty_returns_default_singleton():
    """空 grid → 回 [{}] (代表跑一次 default)。"""
    assert expand_params_grid({}) == [{}]


def test_make_exits_after_hold_basic():
    """entries 在 index 0 → exits 在 index 3(hold_days=3)。"""
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    entries = pd.DataFrame(False, index=idx, columns=["AAA"], dtype=bool)
    entries.iloc[0, 0] = True
    exits = _make_exits_after_hold(entries, hold_days=3)
    assert exits.iloc[3, 0] == True  # noqa: E712
    assert exits.iloc[0, 0] == False  # noqa: E712


def test_make_exits_after_hold_clamps_to_last_bar():
    """entries 太靠近尾端 → exits 落最後一根。"""
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    entries = pd.DataFrame(False, index=idx, columns=["AAA"], dtype=bool)
    entries.iloc[4, 0] = True  # 最後一根 entry
    exits = _make_exits_after_hold(entries, hold_days=3)
    # 3 天後超出 → 落在 index 4(clamp)
    assert exits.iloc[4, 0] == True  # noqa: E712


# === _clean_close_matrix regression(macd_golden 全 0 trades 修法) ===

def test_clean_close_matrix_preserves_col_with_leading_nan():
    """leading NaN(資料起點之前)應被 bfill 補,column 不被丟。

    舊版只 ffill + 丟任意-NaN-欄 → 6 個月 universe 大量 sid 被丟 → close.empty
    → grid 寫 0 trades(macd_golden 全 0 trades 的成因)。
    """
    idx = pd.date_range("2025-01-01", periods=10, freq="D")
    # AAA 前 3 天 NaN(上市前),BBB 全程都有
    close = pd.DataFrame(
        {
            "AAA": [np.nan, np.nan, np.nan, 50.0, 51.0, 52.0, 53.0, 54.0, 55.0, 56.0],
            "BBB": [100.0] * 10,
        },
        index=idx,
    )
    cleaned = _clean_close_matrix(close)
    assert "AAA" in cleaned.columns, "leading-NaN 不應導致整欄被丟"
    assert "BBB" in cleaned.columns
    # leading NaN 被 bfill 成第一個有效價 50.0
    assert cleaned.loc[idx[0], "AAA"] == 50.0
    assert cleaned.loc[idx[2], "AAA"] == 50.0
    # 原本有資料的不受影響
    assert cleaned.loc[idx[3], "AAA"] == 50.0


def test_clean_close_matrix_treats_zero_as_bad_and_ffills():
    """close <= 0 當壞資料(權證沒成交日寫 0),ffill 補上一個有效價。

    vbt.Portfolio.from_signals 對 price <= 0 會 raise
    'order.price must be finite and greater than 0',
    所以一定要在進 vbt 前處理掉。
    """
    idx = pd.date_range("2025-01-01", periods=6, freq="D")
    # AAA 中間有 1 天 close=0(沒成交)
    close = pd.DataFrame(
        {"AAA": [10.0, 11.0, 0.0, 12.0, 13.0, 14.0]},
        index=idx,
    )
    cleaned = _clean_close_matrix(close)
    assert "AAA" in cleaned.columns
    # 0.0 被當 NaN ffill → 11.0
    assert cleaned.loc[idx[2], "AAA"] == 11.0
    # 沒有 0 / 負 / NaN 殘留
    assert (cleaned > 0).all().all()


def test_clean_close_matrix_drops_all_nan_col():
    """整欄都沒資料的 sid 還是要丟掉。"""
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    close = pd.DataFrame(
        {
            "AAA": [10.0, 11.0, 12.0, 13.0, 14.0],
            "BBB": [np.nan] * 5,
        },
        index=idx,
    )
    cleaned = _clean_close_matrix(close)
    assert "AAA" in cleaned.columns
    assert "BBB" not in cleaned.columns


# === _portfolio_stats fixture(不打 DB) ===

def test_portfolio_stats_winning_signal_gives_positive_return():
    """單 column 漲價序列 + 進出 → total_return 正。"""
    idx = pd.date_range("2025-01-01", periods=10, freq="D")
    close = pd.DataFrame(
        {"AAA": [100, 102, 104, 106, 108, 110, 112, 114, 116, 118]},
        index=idx,
    )
    entries = pd.DataFrame(False, index=idx, columns=["AAA"], dtype=bool)
    exits = pd.DataFrame(False, index=idx, columns=["AAA"], dtype=bool)
    entries.iloc[1, 0] = True   # buy at 102
    exits.iloc[6, 0] = True     # sell at 112

    stats = _portfolio_stats(close, entries, exits)
    assert stats["total_return"] > 0
    assert stats["n_trades"] >= 1


def test_portfolio_stats_no_signals_returns_zero_trades():
    """沒進場 → 0 trades + 0 報酬。"""
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    close = pd.DataFrame({"AAA": [100, 101, 102, 103, 104]}, index=idx)
    entries = pd.DataFrame(False, index=idx, columns=["AAA"], dtype=bool)
    exits = pd.DataFrame(False, index=idx, columns=["AAA"], dtype=bool)
    stats = _portfolio_stats(close, entries, exits)
    assert stats["n_trades"] == 0
    assert stats["total_return"] == 0.0


# === backtest_strategy_with_params 整合(用 fake prices 灌入 DB)===

def _seed_synthetic_prices(stock_ids: list[str], dates: list[str], base: float = 100.0):
    """灌入幾檔 sid 的合成價格序列(緩慢上漲 + 量波動)— 給策略命中。"""
    rng = np.random.default_rng(42)
    rows: list[dict] = []
    for sid in stock_ids:
        price = base + (hash(sid) % 50)
        for i, d in enumerate(dates):
            # 緩慢上漲 + 偶爾爆量
            drift = i * 0.5
            noise = float(rng.normal(0, 0.5))
            o = price + drift + noise
            c = o + float(rng.normal(0, 1))
            h = max(o, c) + abs(float(rng.normal(0, 0.3)))
            lo = min(o, c) - abs(float(rng.normal(0, 0.3)))
            # 第 25 天人為製造爆量突破訊號:量大 + 收盤大幅高
            if i == 25:
                c = max(o, c) + 8
                h = c + 1
                vol = 50_000_000  # 爆量
            else:
                vol = int(5_000_000 + rng.normal(0, 500_000))
            rows.append({
                "stock_id": sid,
                "date": d,
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(lo, 2),
                "close": round(c, 2),
                "volume": vol,
                "trading_money": vol * c,
                "trading_turnover": int(vol / 1000),
                "spread": 0.0,
            })
    db.upsert_daily_prices(rows)


def test_backtest_strategy_with_params_empty_signals_returns_zero_row(tmp_db):
    """universe 為空 → 回空 DataFrame(沒交易日 / 沒 picks)。"""
    df = backtest_strategy_with_params(
        "volume_breakout",
        params_grid={"vbo_vol_ratio_min": [2.5]},
        start_date="2025-01-01",
        end_date="2025-01-10",
        universe=[],
    )
    assert df.empty


def test_backtest_strategy_with_params_returns_row_per_combo(tmp_db):
    """灌假資料 + 1 個策略 × 2 組 params → 回 2 row,欄位齊。"""
    sids = ["1101", "2330", "2454"]
    # 35 個交易日(足夠跑 volume_breakout 的 lookback=35)
    dates = [f"2025-{m:02d}-{d:02d}" for m in [1, 2] for d in range(1, 20)][:35]
    _seed_synthetic_prices(sids, dates)

    df = backtest_strategy_with_params(
        "volume_breakout",
        params_grid={"vbo_vol_ratio_min": [2.0, 3.0]},
        start_date=dates[0],
        end_date=dates[-1],
        universe=sids,
    )
    assert len(df) == 2
    expected_cols = {
        "strategy", "params_hash", "params_json",
        "n_trades", "total_return", "sharpe", "max_drawdown", "win_rate",
    }
    assert expected_cols.issubset(set(df.columns))
    # 兩組 params 應有不同 hash
    assert df["params_hash"].nunique() == 2


def test_persist_grid_results_upsert(tmp_db):
    """persist_grid_results UPSERT — 同 (strategy, params_hash) 重跑會覆蓋。"""
    results = pd.DataFrame([
        {
            "strategy": "volume_breakout",
            "params_hash": "abc123",
            "params_json": '{"x":1}',
            "params_dict": {"x": 1},
            "n_trades": 5,
            "total_return": 10.0,
            "sharpe": 1.5,
            "max_drawdown": 3.0,
            "win_rate": 60.0,
        },
    ])
    n = persist_grid_results(
        results, period_start="2025-01-01", period_end="2025-06-01",
    )
    assert n == 1

    loaded = db.load_vbt_grid_results(strategy="volume_breakout")
    assert not loaded.empty
    assert float(loaded.iloc[0]["sharpe"]) == pytest.approx(1.5)

    # 重跑覆蓋:同 hash 改 sharpe
    results.iloc[0, results.columns.get_loc("sharpe")] = 2.5
    persist_grid_results(
        results, period_start="2025-01-01", period_end="2025-06-01",
    )
    loaded2 = db.load_vbt_grid_results(strategy="volume_breakout")
    assert len(loaded2) == 1  # 還是 1 row
    assert float(loaded2.iloc[0]["sharpe"]) == pytest.approx(2.5)


def test_load_vbt_grid_results_top_n_ordering(tmp_db):
    """load_vbt_grid_results 按 sharpe DESC + LIMIT top_n。"""
    results = pd.DataFrame([
        {
            "strategy": "volume_breakout", "params_hash": f"h{i:03d}",
            "params_json": f'{{"x":{i}}}', "params_dict": {"x": i},
            "n_trades": 5, "total_return": 1.0 * i,
            "sharpe": 0.1 * i, "max_drawdown": 5.0, "win_rate": 50.0,
        }
        for i in range(1, 6)
    ])
    persist_grid_results(
        results, period_start="2025-01-01", period_end="2025-06-01",
    )

    top3 = db.load_vbt_grid_results(strategy="volume_breakout", top_n=3)
    assert len(top3) == 3
    sharpes = list(top3["sharpe"])
    assert sharpes == sorted(sharpes, reverse=True)
    assert float(sharpes[0]) == pytest.approx(0.5)


def test_unknown_strategy_raises(tmp_db):
    with pytest.raises(ValueError, match="未知 strategy"):
        backtest_strategy_with_params(
            "does_not_exist",
            params_grid={},
            start_date="2025-01-01",
            end_date="2025-01-10",
            universe=["1101"],
        )
