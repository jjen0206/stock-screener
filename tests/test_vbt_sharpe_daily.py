"""daily-aggregated Sharpe(2026-05-15 修 trade-level sqrt(N) 膨脹)單元測試。

問題:vectorbt 預設 trade-level Sharpe = mean/std × sqrt(N),N 動則 6000+
→ sqrt(6000) ≈ 77.5 把 Sharpe 放大近百倍,跨策略 / 不同 N 比較失真。

修法:`_compute_daily_sharpe` 把 trade returns 歸到 exit 當天,
reindex 到完整交易日序列(沒交易那日 = 0),用 daily 算 annualized Sharpe
(× sqrt(252))— 公平比較。

本檔守住:
1. 已知 daily return 序列 → 算出的 Sharpe 對得上手算
2. 空 trades / 空 index / 樣本不足 → 0.0(不爆)
3. 同一筆策略 N=10 vs N=6000:trade-level Sharpe 差幾十倍,daily 不會
4. edge case: 無交易日(0 trades 但有 index)、單日多筆、跨年度、std=0
5. records_readable 欄位名容錯(Return 為 fraction;Return [%] 已 *100)
6. _portfolio_stats 整合 — 回 dict 帶 sharpe_daily 欄
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import config, database as db
from src.vbt_backtest import (
    DAILY_SHARPE_PERIODS_PER_YEAR,
    _compute_daily_sharpe,
    _portfolio_stats,
)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "sharpe.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


# ============================================================================
# 算法正確性 — 已知 daily series 對得上手算
# ============================================================================

def test_compute_daily_sharpe_known_series():
    """每天 +1% return × 252 交易日 → Sharpe 該是 +∞ 但 std=0 → 退回 0.0。

    驗 std=0 邊界 case(完全沒波動 → 公式分母 0)。
    """
    idx = pd.date_range("2025-01-01", periods=252, freq="D")
    # 每天都 +1% return,每筆 trade 跨 0 天(exit_date == idx[i])
    records = pd.DataFrame({
        "Exit Timestamp": idx,
        "Return": [0.01] * 252,
    })
    s = _compute_daily_sharpe(records, idx)
    assert s == 0.0, "std=0(零波動)該回 0.0 不爆"


def test_compute_daily_sharpe_matches_manual_calc():
    """3 筆 trade,人工算 Sharpe = mean/std × sqrt(252)。

    daily returns: [0.02, -0.01, 0.03, 0, 0, ...] reindex 到 5 天
    mean = 0.04/5 = 0.008, std (ddof=1) sqrt(sum((x-mean)^2)/(n-1))
    """
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    records = pd.DataFrame({
        "Exit Timestamp": [idx[0], idx[1], idx[2]],
        "Return": [0.02, -0.01, 0.03],
    })
    s = _compute_daily_sharpe(records, idx, risk_free_rate=0.0)

    # 手算對拍
    daily = np.array([0.02, -0.01, 0.03, 0.0, 0.0])
    expected = (
        daily.mean()
        / daily.std(ddof=1)
        * np.sqrt(DAILY_SHARPE_PERIODS_PER_YEAR)
    )
    assert s == pytest.approx(expected, rel=1e-6)


def test_compute_daily_sharpe_same_day_sum_aggregation():
    """同一天多筆 trade → 預設 sum aggregation,加總當日 returns。"""
    idx = pd.date_range("2025-01-01", periods=4, freq="D")
    # idx[1] 有 2 筆 trade(0.01 + 0.02 = 0.03 sum)
    records = pd.DataFrame({
        "Exit Timestamp": [idx[1], idx[1], idx[2]],
        "Return": [0.01, 0.02, -0.005],
    })
    s = _compute_daily_sharpe(records, idx, aggregate="sum")

    # 手算:[0, 0.03, -0.005, 0]
    daily = np.array([0.0, 0.03, -0.005, 0.0])
    expected = (
        daily.mean()
        / daily.std(ddof=1)
        * np.sqrt(DAILY_SHARPE_PERIODS_PER_YEAR)
    )
    assert s == pytest.approx(expected, rel=1e-6)


def test_compute_daily_sharpe_same_day_mean_aggregation():
    """aggregate='mean' → 同日多筆取平均(不加總)。"""
    idx = pd.date_range("2025-01-01", periods=3, freq="D")
    records = pd.DataFrame({
        "Exit Timestamp": [idx[1], idx[1]],
        "Return": [0.01, 0.03],
    })
    s = _compute_daily_sharpe(records, idx, aggregate="mean")

    # mean → idx[1] = 0.02,daily = [0, 0.02, 0]
    daily = np.array([0.0, 0.02, 0.0])
    expected = (
        daily.mean()
        / daily.std(ddof=1)
        * np.sqrt(DAILY_SHARPE_PERIODS_PER_YEAR)
    )
    assert s == pytest.approx(expected, rel=1e-6)


def test_compute_daily_sharpe_return_pct_column():
    """'Return [%]' 欄位是百分比(已 *100),helper 應除回 fraction。"""
    idx = pd.date_range("2025-01-01", periods=3, freq="D")
    records_pct = pd.DataFrame({
        "Exit Timestamp": [idx[0], idx[1]],
        "Return [%]": [1.0, 2.0],  # 表 1% / 2%
    })
    records_frac = pd.DataFrame({
        "Exit Timestamp": [idx[0], idx[1]],
        "Return": [0.01, 0.02],
    })
    s_pct = _compute_daily_sharpe(records_pct, idx)
    s_frac = _compute_daily_sharpe(records_frac, idx)
    assert s_pct == pytest.approx(s_frac, rel=1e-9), (
        "Return [%] 該被除回 fraction,跟 Return 算出來一致"
    )


# ============================================================================
# Edge cases — 不爆
# ============================================================================

def test_compute_daily_sharpe_empty_trades_returns_zero():
    """空 trades_records → 0.0,不爆。"""
    idx = pd.date_range("2025-01-01", periods=10, freq="D")
    empty = pd.DataFrame({"Exit Timestamp": [], "Return": []})
    assert _compute_daily_sharpe(empty, idx) == 0.0
    assert _compute_daily_sharpe(None, idx) == 0.0


def test_compute_daily_sharpe_short_index_returns_zero():
    """trading_index 少於 2 點 → 0.0(算不出 std)。"""
    records = pd.DataFrame({
        "Exit Timestamp": [pd.Timestamp("2025-01-01")],
        "Return": [0.05],
    })
    assert _compute_daily_sharpe(records, pd.DatetimeIndex([])) == 0.0
    assert _compute_daily_sharpe(
        records, pd.DatetimeIndex(["2025-01-01"])
    ) == 0.0


def test_compute_daily_sharpe_missing_columns_returns_zero():
    """records 缺 Exit / Return 欄 → 0.0,不爆。"""
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    # 沒 Exit Timestamp
    no_exit = pd.DataFrame({"Return": [0.01, 0.02]})
    assert _compute_daily_sharpe(no_exit, idx) == 0.0
    # 沒 Return
    no_ret = pd.DataFrame({"Exit Timestamp": [idx[0], idx[1]]})
    assert _compute_daily_sharpe(no_ret, idx) == 0.0


def test_compute_daily_sharpe_pnl_amount_returns_zero():
    """records 只有 PnL(金額)不是 return rate → 0.0(沒法直接算 Sharpe)。"""
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    records = pd.DataFrame({
        "Exit Timestamp": [idx[0], idx[1]],
        "PnL": [1000.0, -500.0],
    })
    # 退回 0.0(金額不是 %,沒法 mean/std 跨筆比)
    assert _compute_daily_sharpe(records, idx) == 0.0


def test_compute_daily_sharpe_cross_year_boundary():
    """trade exit 橫跨年底 → 算對(不會因 year normalize 出錯)。"""
    idx = pd.date_range("2024-12-28", "2025-01-08", freq="D")
    records = pd.DataFrame({
        "Exit Timestamp": [
            pd.Timestamp("2024-12-30"),
            pd.Timestamp("2025-01-02"),
            pd.Timestamp("2025-01-06"),
        ],
        "Return": [0.01, -0.02, 0.015],
    })
    s = _compute_daily_sharpe(records, idx)
    # 對拍:idx 12 天,3 個非 0 報酬
    daily_map = {
        pd.Timestamp("2024-12-30"): 0.01,
        pd.Timestamp("2025-01-02"): -0.02,
        pd.Timestamp("2025-01-06"): 0.015,
    }
    daily_arr = np.array([daily_map.get(d, 0.0) for d in idx])
    expected = (
        daily_arr.mean()
        / daily_arr.std(ddof=1)
        * np.sqrt(DAILY_SHARPE_PERIODS_PER_YEAR)
    )
    assert s == pytest.approx(expected, rel=1e-6)


# ============================================================================
# 核心修法目的 — N 大不應放大 Sharpe
# ============================================================================

def test_compute_daily_sharpe_does_not_inflate_with_n():
    """N=10 vs N=6000 同分布的 trade returns → daily Sharpe 不會差幾十倍。

    這是修法的核心 — 證明 daily-aggregated 抗 N 膨脹。
    """
    rng = np.random.default_rng(42)
    n_days = 252  # 1 年交易日

    # Case A:N=10 trades 散落 252 天,每筆 ~N(0.001, 0.02)
    idx = pd.date_range("2025-01-01", periods=n_days, freq="B")
    exits_small = rng.choice(idx, size=10, replace=False)
    rets_small = rng.normal(0.001, 0.02, size=10)
    small = pd.DataFrame({"Exit Timestamp": exits_small, "Return": rets_small})

    # Case B:N=6000 trades 散落同 252 天(同 RNG seed 保持分布相似)
    exits_large = rng.choice(idx, size=6000, replace=True)
    rets_large = rng.normal(0.001, 0.02, size=6000)
    large = pd.DataFrame({"Exit Timestamp": exits_large, "Return": rets_large})

    s_small_daily = _compute_daily_sharpe(small, idx)
    s_large_daily = _compute_daily_sharpe(large, idx)

    # daily Sharpe:N 從 10 → 6000(600×)而 daily 兩者差異該在 10× 內
    # (不是 600× 等比例膨脹,因為 daily 樣本永遠是 252 而不隨 trade N 變)
    # 防呆下限 — 至少不會像 trade-level 那樣差 sqrt(600)≈24.5×
    if s_small_daily != 0 and s_large_daily != 0:
        ratio = abs(s_large_daily / s_small_daily)
        assert ratio < 10, (
            f"daily Sharpe N 膨脹防呆 — N=10 vs N=6000 比例不該 > 10× "
            f"(small={s_small_daily:.3f}, large={s_large_daily:.3f}, "
            f"ratio={ratio:.2f})"
        )


def test_compute_daily_sharpe_lower_than_trade_level_on_large_n():
    """N=2000 大樣本 → daily Sharpe 通常 < trade-level Sharpe(typically much less)。"""
    rng = np.random.default_rng(123)
    idx = pd.date_range("2025-01-01", periods=252, freq="B")
    n_trades = 2000
    exits = rng.choice(idx, size=n_trades, replace=True)
    rets = rng.normal(0.001, 0.02, size=n_trades)
    records = pd.DataFrame({"Exit Timestamp": exits, "Return": rets})

    # trade-level Sharpe(乘 sqrt(N))
    trade_sharpe = (
        rets.mean() / rets.std(ddof=1) * np.sqrt(n_trades)
    )
    daily_sharpe = _compute_daily_sharpe(records, idx)

    # trade-level 用 sqrt(2000) ≈ 44.7 放大;daily 用 sqrt(252) ≈ 15.9
    # 兩者比例應 ≈ sqrt(2000)/sqrt(252) ≈ 2.82×(理論上界,實際因
    # daily aggregation 內部互相 cancel 通常更小)
    assert abs(daily_sharpe) < abs(trade_sharpe) * 1.05, (
        f"daily Sharpe({daily_sharpe:.3f}) 在 N=2000 該明顯小於 trade-level"
        f"({trade_sharpe:.3f})"
    )


# ============================================================================
# _portfolio_stats 整合 — 回 dict 帶 sharpe_daily 欄
# ============================================================================

def test_portfolio_stats_dict_has_sharpe_daily_key():
    """_portfolio_stats 必回 sharpe_daily 欄,不論 trade 數。"""
    idx = pd.date_range("2025-01-01", periods=10, freq="D")
    close = pd.DataFrame(
        {"AAA": [100, 102, 104, 106, 108, 110, 112, 114, 116, 118]},
        index=idx,
    )
    entries = pd.DataFrame(False, index=idx, columns=["AAA"], dtype=bool)
    exits = pd.DataFrame(False, index=idx, columns=["AAA"], dtype=bool)
    entries.iloc[1, 0] = True
    exits.iloc[6, 0] = True

    stats = _portfolio_stats(close, entries, exits)
    assert "sharpe_daily" in stats, "_portfolio_stats 缺 sharpe_daily 欄"
    assert "sharpe" in stats, "_portfolio_stats 原 sharpe 欄該保留(deprecated 並行)"
    # 數值不爆
    assert np.isfinite(stats["sharpe_daily"])


def test_portfolio_stats_no_trades_sharpe_daily_zero():
    """沒進場 → sharpe_daily = 0.0,不爆。"""
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    close = pd.DataFrame({"AAA": [100, 101, 102, 103, 104]}, index=idx)
    entries = pd.DataFrame(False, index=idx, columns=["AAA"], dtype=bool)
    exits = pd.DataFrame(False, index=idx, columns=["AAA"], dtype=bool)
    stats = _portfolio_stats(close, entries, exits)
    assert stats["sharpe_daily"] == 0.0


# ============================================================================
# 持久化 — sharpe_daily 落到 vbt_grid_results 表
# ============================================================================

def test_vbt_grid_results_schema_has_sharpe_daily(tmp_db):
    """init_db 建表 / migrate 後,vbt_grid_results 須有 sharpe_daily 欄。"""
    with db.get_conn() as conn:
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(vbt_grid_results)").fetchall()
        }
    assert "sharpe_daily" in cols, "vbt_grid_results 缺 sharpe_daily 欄"
    assert "sharpe" in cols, "vbt_grid_results 原 sharpe 欄該保留"


def test_upsert_vbt_grid_results_persists_sharpe_daily(tmp_db):
    """upsert 帶 sharpe_daily → load 拿得到。"""
    rows = [{
        "strategy": "volume_breakout",
        "params_hash": "test01daily",
        "params_json": '{"x":1}',
        "period_start": "2025-01-01",
        "period_end": "2025-06-01",
        "n_trades": 100,
        "total_return": 5.0,
        "sharpe": 12.5,          # 舊 N-inflated 指標
        "sharpe_daily": 1.42,    # 新 daily annualized 指標
        "max_drawdown": 8.0,
        "win_rate": 55.0,
        "generated_at": "2026-05-15T00:00:00+00:00",
    }]
    assert db.upsert_vbt_grid_results(rows) == 1
    loaded = db.load_vbt_grid_results(strategy="volume_breakout")
    assert not loaded.empty
    assert "sharpe_daily" in loaded.columns
    assert float(loaded.iloc[0]["sharpe_daily"]) == pytest.approx(1.42)
    # 舊 sharpe 並存
    assert float(loaded.iloc[0]["sharpe"]) == pytest.approx(12.5)


def test_upsert_vbt_grid_results_backwards_compat_no_sharpe_daily(tmp_db):
    """舊 caller 沒帶 sharpe_daily → upsert 不爆,DB 寫 NULL,load 回 NaN。"""
    rows = [{
        "strategy": "macd_golden",
        "params_hash": "legacynull1",
        "params_json": "{}",
        "period_start": "2025-01-01",
        "period_end": "2025-06-01",
        "n_trades": 5,
        "total_return": 1.0,
        "sharpe": 0.8,
        # 沒 sharpe_daily 欄
        "max_drawdown": 2.0,
        "win_rate": 60.0,
        "generated_at": "2026-05-15T00:00:00+00:00",
    }]
    assert db.upsert_vbt_grid_results(rows) == 1
    loaded = db.load_vbt_grid_results(strategy="macd_golden")
    assert not loaded.empty
    # NULL → pandas NaN
    val = loaded.iloc[0]["sharpe_daily"]
    assert pd.isna(val), f"沒帶 sharpe_daily 該寫 NULL,拿到 {val}"
