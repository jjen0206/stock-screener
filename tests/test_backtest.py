"""src/backtest.py 單元測試 — simulate_outcome + backtest_strategy + DB helpers。"""
from __future__ import annotations

import pandas as pd
import pytest

from src import config, database as db
from src.backtest import (
    backtest_all_strategies, backtest_strategy, simulate_outcome,
)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "backtest.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


# === simulate_outcome ===

def test_simulate_outcome_win_first():
    """high 觸 target 之前 low 沒觸 stop → win + return = +target_pct。"""
    df = pd.DataFrame([
        {"high": 101, "low": 99, "close": 100},
        {"high": 106, "low": 100, "close": 105},  # hit target 105 = 100×1.05
        {"high": 110, "low": 104, "close": 108},
    ])
    outcome, ret = simulate_outcome(df, entry_price=100.0)
    assert outcome == "win"
    assert ret == pytest.approx(0.05)


def test_simulate_outcome_lose_first():
    """low 觸 stop 之前 high 沒觸 target → lose + return = -stop_pct。"""
    df = pd.DataFrame([
        {"high": 102, "low": 96, "close": 97},  # low=96 ≤ 97 = 100×(1-0.03)
        {"high": 105, "low": 95, "close": 100},
    ])
    outcome, ret = simulate_outcome(df, entry_price=100.0)
    assert outcome == "lose"
    assert ret == pytest.approx(-0.03)


def test_simulate_outcome_same_day_both_hit_treats_as_lose():
    """同日 high 跟 low 都觸 → 保守視為先觸停損(intra-day path 不可知)。"""
    df = pd.DataFrame([
        {"high": 106, "low": 96, "close": 100},  # 同日兩邊都觸
    ])
    outcome, ret = simulate_outcome(df, entry_price=100.0)
    assert outcome == "lose"
    assert ret == pytest.approx(-0.03)


def test_simulate_outcome_neutral_close_above_returns_win():
    """N 天結束都沒觸 + close 高於 entry → win + return = (close-entry)/entry。"""
    df = pd.DataFrame([
        {"high": 102, "low": 99, "close": 101},
        {"high": 103, "low": 100, "close": 102},
    ])
    outcome, ret = simulate_outcome(df, entry_price=100.0)
    assert outcome == "win"
    assert ret == pytest.approx(0.02)


def test_simulate_outcome_neutral_close_below_returns_lose():
    """N 天結束都沒觸 + close 低於 entry → lose + return = 負 %。"""
    df = pd.DataFrame([
        {"high": 101, "low": 98, "close": 99},
        {"high": 100, "low": 98, "close": 98},
    ])
    outcome, ret = simulate_outcome(df, entry_price=100.0)
    assert outcome == "lose"
    assert ret == pytest.approx(-0.02)


def test_simulate_outcome_zero_entry_returns_lose_safely():
    """entry_price <= 0 → lose 0(避免除零;caller 通常會先 filter)。"""
    df = pd.DataFrame([{"high": 1, "low": 1, "close": 1}])
    outcome, ret = simulate_outcome(df, entry_price=0.0)
    assert outcome == "lose"
    assert ret == 0.0


def test_simulate_outcome_custom_target_stop_pct():
    """target_pct=0.07 / stop_pct=0.04 — 自訂閾值生效。"""
    df = pd.DataFrame([
        {"high": 108, "low": 99, "close": 107},  # 108 ≥ 107 = 100×1.07
    ])
    outcome, ret = simulate_outcome(df, 100.0, target_pct=0.07, stop_pct=0.04)
    assert outcome == "win"
    assert ret == pytest.approx(0.07)


# === DB helpers ===

def test_strategy_backtest_table_schema_exists(tmp_db):
    """init_db 後 strategy_backtest 表 + 欄位齊全 + index 存在。"""
    with db.get_conn() as conn:
        names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "strategy_backtest" in names
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(strategy_backtest)"
            ).fetchall()
        }
        assert cols == {
            "strategy", "period_end", "lookback_days", "target_pct", "stop_pct",
            "hold_days", "n_fires", "n_wins", "win_rate", "avg_return",
            "computed_at",
        }
        idx_names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_sb_period" in idx_names


def _sample_backtest_row(strategy: str, period_end: str, win_rate: float) -> dict:
    return {
        "strategy": strategy,
        "period_end": period_end,
        "lookback_days": 126,
        "target_pct": 0.05,
        "stop_pct": 0.03,
        "hold_days": 5,
        "n_fires": 100,
        "n_wins": int(100 * win_rate),
        "win_rate": win_rate,
        "avg_return": 0.012,
        "computed_at": "2026-05-04T00:00:00+00:00",
    }


def test_dump_strategy_backtest_then_load_roundtrip(tmp_db):
    """dump 進 SQLite,load_latest_strategy_backtest 還原 {strategy: win_rate}。"""
    rows = [
        _sample_backtest_row("volume_kd", "2026-05-04", 0.62),
        _sample_backtest_row("ma_alignment", "2026-05-04", 0.55),
        _sample_backtest_row("macd_golden", "2026-05-04", 0.48),
    ]
    n = db.dump_strategy_backtest(rows)
    assert n == 3

    rates = db.load_latest_strategy_backtest()
    assert rates == {
        "volume_kd": 0.62,
        "ma_alignment": 0.55,
        "macd_golden": 0.48,
    }


def test_load_latest_takes_max_period_end_per_strategy(tmp_db):
    """同 strategy 多個 period_end → load 只回最新。"""
    db.dump_strategy_backtest([
        _sample_backtest_row("volume_kd", "2026-04-01", 0.50),  # 舊
        _sample_backtest_row("volume_kd", "2026-05-04", 0.65),  # 新
    ])
    rates = db.load_latest_strategy_backtest()
    assert rates["volume_kd"] == 0.65


def test_dump_strategy_backtest_on_conflict_replaces(tmp_db):
    """重 dump 同 (strategy, period_end) → 用新值覆蓋。"""
    db.dump_strategy_backtest([
        _sample_backtest_row("volume_kd", "2026-05-04", 0.50),
    ])
    db.dump_strategy_backtest([
        _sample_backtest_row("volume_kd", "2026-05-04", 0.70),
    ])
    rates = db.load_latest_strategy_backtest()
    assert rates["volume_kd"] == 0.70
    # 沒爆出兩倍 row
    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM strategy_backtest").fetchone()["c"]
    assert n == 1


def test_load_strategy_backtest_for_period_returns_dataframe(tmp_db):
    """load_strategy_backtest_for_period 撈某天全策略,sorted by win_rate desc。"""
    db.dump_strategy_backtest([
        _sample_backtest_row("low_wr", "2026-05-04", 0.40),
        _sample_backtest_row("high_wr", "2026-05-04", 0.70),
        _sample_backtest_row("mid_wr", "2026-05-04", 0.55),
    ])
    df = db.load_strategy_backtest_for_period("2026-05-04")
    assert len(df) == 3
    # sorted by win_rate desc
    assert list(df["strategy"]) == ["high_wr", "mid_wr", "low_wr"]


def test_load_latest_strategy_backtest_empty_returns_empty_dict(tmp_db):
    """空表 → 空 dict(讓 caller fallback)。"""
    assert db.load_latest_strategy_backtest() == {}


# === backtest_strategy(整合 — mock screener)===

def test_backtest_strategy_integrates_screener_and_simulate(
    tmp_db, monkeypatch,
):
    """整合測試 — mock screen function 回固定 picks,驗 backtest 流程跑通,
    win_rate / avg_return 計算正確。
    """
    from src import backtest as bt

    # 灌 daily_prices(讓 _list_trading_dates 找得到 + bulk_load_prices 拿
    # 得到 OHLC)
    dates_full = [
        f"2026-04-{d:02d}" for d in range(1, 16)
    ] + [
        f"2026-05-{d:02d}" for d in range(1, 16)
    ]  # 30 個交易日
    rows = []
    for sid in ("2330", "2317"):
        for i, d in enumerate(dates_full):
            # 線性走勢:每天 +0.5(一定觸 target +5%)
            close = 100.0 + i * 0.5
            rows.append({
                "stock_id": sid, "date": d,
                "open": close, "high": close + 6, "low": close - 0.5,
                "close": close, "volume": 1000,
                "trading_money": None, "trading_turnover": None, "spread": None,
            })
    db.upsert_daily_prices(rows)

    # mock screener 在每個 D 都 fire 2330 + 2317
    def _fake_screener(date, params=None, stock_ids=None):
        return pd.DataFrame([
            {"stock_id": "2330", "name": "台積電",
             "close": 100.0 + dates_full.index(date) * 0.5},
            {"stock_id": "2317", "name": "鴻海",
             "close": 100.0 + dates_full.index(date) * 0.5},
        ])

    monkeypatch.setitem(
        bt.ALL_STRATEGIES, "fake_strat", _fake_screener,
    )

    # 跑回測:lookback 30 天,hold 5 天 → 25 天可 pickable, ×2 sids = 50 fires
    # 線性走勢 +0.5/day,5 天後 +2.5(漲 2.5%)— 但 high=close+6 → 第一天就觸
    # target 5%(close=100, target=105, high=106)→ 全 win
    stats = bt.backtest_strategy(
        "fake_strat",
        universe=["2330", "2317"],
        period_end="2026-05-15",
        lookback_days=30,
        target_pct=0.05,
        stop_pct=0.03,
        hold_days=5,
    )

    # 25 個 D × 2 sids = 50 fires
    assert stats["n_fires"] == 50
    # 全 win(line=close+6 第 1 天就觸 +5%)
    assert stats["n_wins"] == 50
    assert stats["win_rate"] == 1.0
    assert stats["avg_return"] == pytest.approx(0.05)


def test_backtest_strategy_unknown_name_raises(tmp_db):
    """跑不存在的 strategy → ValueError。"""
    with pytest.raises(ValueError, match="未知 strategy"):
        backtest_strategy(
            "nope_strategy",
            universe=["2330"],
            period_end="2026-05-04",
        )


def test_backtest_strategy_insufficient_history_returns_zero(tmp_db, monkeypatch):
    """daily_prices 歷史 < hold_days+1 → 直接回 0(不炸)。"""
    from src import backtest as bt
    db.upsert_daily_prices([{
        "stock_id": "2330", "date": "2026-05-01",
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    }])
    monkeypatch.setitem(
        bt.ALL_STRATEGIES, "fake", lambda date, **kw: pd.DataFrame(),
    )
    stats = backtest_strategy(
        "fake", universe=["2330"],
        period_end="2026-05-01", lookback_days=10, hold_days=5,
    )
    assert stats == {"n_fires": 0, "n_wins": 0, "win_rate": 0.0, "avg_return": 0.0}


def test_backtest_all_strategies_returns_rows_per_strategy(tmp_db, monkeypatch):
    """backtest_all_strategies 回 list[dict] 每個 strategy 一筆,schema 對齊
    db.dump_strategy_backtest 期望。"""
    from src import backtest as bt
    monkeypatch.setattr(
        bt, "backtest_strategy",
        lambda *a, **kw: {
            "n_fires": 10, "n_wins": 6, "win_rate": 0.6, "avg_return": 0.01,
        },
    )

    results = backtest_all_strategies(
        universe=["2330"], period_end="2026-05-04",
        strategies=["volume_kd", "ma_alignment"],
    )
    assert len(results) == 2
    assert {r["strategy"] for r in results} == {"volume_kd", "ma_alignment"}
    for r in results:
        assert r["win_rate"] == 0.6
        assert r["n_fires"] == 10
        assert r["period_end"] == "2026-05-04"
        # schema 完整(可餵 db.dump_strategy_backtest)
        assert {
            "strategy", "period_end", "lookback_days", "target_pct", "stop_pct",
            "hold_days", "n_fires", "n_wins", "win_rate", "avg_return",
            "computed_at",
        }.issubset(r.keys())
