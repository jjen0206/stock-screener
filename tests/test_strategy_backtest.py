"""D 績效分析 — src/strategy_backtest.py 單元測試。

涵蓋:
  - backtest_combination:union / intersect mode、holding_days、entry/exit 價算 P&L
  - compute_strategy_correlation:Jaccard 0~1 + 對角線 1.0
  - kill-switch PERFORMANCE_ENABLED=false → 空 result
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from src import database as db  # noqa: E402
from src import strategy_backtest as sb  # noqa: E402

# tmp_db fixture 從 tests/conftest.py 來


def _seed_daily_pick(sid: str, trade_date: str, strategy: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_picks ("
            "trade_date, universe, strategy, sid, params_hash, payload, "
            "computed_at) VALUES (?, 'top_50', ?, ?, 'default_v1', '{}', "
            "'2026-05-17T00:00:00Z')",
            (trade_date, strategy, sid),
        )
        conn.commit()


def _seed_price_series(sid: str, start_date_iso: str, prices: list[float]) -> None:
    """從 start_date 往後連續 N 天寫 daily_prices。"""
    from datetime import date as _date, timedelta
    d = _date.fromisoformat(start_date_iso)
    with db.get_conn() as conn:
        for px in prices:
            conn.execute(
                "INSERT OR REPLACE INTO daily_prices ("
                "stock_id, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, 1000)",
                (sid, d.isoformat(), px, px, px, px),
            )
            d += timedelta(days=1)
        conn.commit()


# === backtest_combination ===


def test_backtest_combination_union_basic(tmp_db):
    # 2330 @ 2026-05-01 入,持有 3 天 → close[3] / close[0] - 1
    _seed_daily_pick("2330", "2026-05-01", "ma_alignment")
    _seed_price_series("2330", "2026-05-01", [100.0, 102.0, 105.0, 110.0])

    with db.get_conn() as conn:
        r = sb.backtest_combination(
            conn,
            strategies=["ma_alignment"],
            start_date="2026-05-01",
            end_date="2026-05-31",
            holding_days=3,
            mode="union",
            apply_costs=False,
        )
    assert r["n_trades"] == 1
    # 110 / 100 - 1 = 10%
    assert r["avg_return_pct"] == pytest.approx(10.0, rel=1e-3)
    assert r["win_rate"] == 1.0


def test_backtest_combination_intersect_requires_both(tmp_db):
    # 2330 同日命中 ma_alignment + volume_breakout
    _seed_daily_pick("2330", "2026-05-01", "ma_alignment")
    _seed_daily_pick("2330", "2026-05-01", "volume_breakout")
    _seed_price_series("2330", "2026-05-01", [100.0, 102.0, 105.0, 110.0])
    # 2454 只命中 ma_alignment
    _seed_daily_pick("2454", "2026-05-01", "ma_alignment")
    _seed_price_series("2454", "2026-05-01", [200.0, 200.0, 200.0, 200.0])

    with db.get_conn() as conn:
        r = sb.backtest_combination(
            conn,
            strategies=["ma_alignment", "volume_breakout"],
            start_date="2026-05-01",
            end_date="2026-05-31",
            holding_days=3,
            mode="intersect",
        )
    # intersect → 只剩 2330
    assert r["n_trades"] == 1
    assert r["trades"][0]["sid"] == "2330"


def test_backtest_combination_union_picks_either(tmp_db):
    _seed_daily_pick("A", "2026-05-01", "ma_alignment")
    _seed_daily_pick("B", "2026-05-01", "volume_breakout")
    _seed_price_series("A", "2026-05-01", [100.0, 100.0, 100.0, 110.0])
    _seed_price_series("B", "2026-05-01", [50.0, 50.0, 50.0, 60.0])

    with db.get_conn() as conn:
        r = sb.backtest_combination(
            conn,
            strategies=["ma_alignment", "volume_breakout"],
            start_date="2026-05-01",
            end_date="2026-05-31",
            holding_days=3,
            mode="union",
        )
    assert r["n_trades"] == 2


def test_backtest_combination_kill_switch(tmp_db, monkeypatch):
    monkeypatch.setenv("PERFORMANCE_ENABLED", "false")
    _seed_daily_pick("2330", "2026-05-01", "ma_alignment")
    _seed_price_series("2330", "2026-05-01", [100.0, 110.0])
    with db.get_conn() as conn:
        r = sb.backtest_combination(
            conn, strategies=["ma_alignment"],
            start_date="2026-05-01", end_date="2026-05-31",
            holding_days=1, mode="union",
        )
    assert r["n_trades"] == 0


def test_backtest_combination_no_strategies(tmp_db):
    with db.get_conn() as conn:
        r = sb.backtest_combination(
            conn, strategies=[],
            start_date="2026-05-01", end_date="2026-05-31",
        )
    assert r["n_trades"] == 0


def test_backtest_combination_invalid_mode(tmp_db):
    with db.get_conn() as conn:
        with pytest.raises(ValueError, match="mode"):
            sb.backtest_combination(
                conn, strategies=["ma_alignment"],
                start_date="2026-05-01", end_date="2026-05-31",
                mode="invalid_mode",
            )


def test_backtest_combination_invalid_holding_days(tmp_db):
    with db.get_conn() as conn:
        with pytest.raises(ValueError, match="holding_days"):
            sb.backtest_combination(
                conn, strategies=["ma_alignment"],
                start_date="2026-05-01", end_date="2026-05-31",
                holding_days=0,
            )


def test_backtest_combination_missing_exit_price_skipped(tmp_db):
    # 只有 entry 價,缺 exit → skip
    _seed_daily_pick("A", "2026-05-01", "ma_alignment")
    _seed_price_series("A", "2026-05-01", [100.0])  # 只一筆
    with db.get_conn() as conn:
        r = sb.backtest_combination(
            conn, strategies=["ma_alignment"],
            start_date="2026-05-01", end_date="2026-05-31",
            holding_days=5, mode="union",
        )
    assert r["n_trades"] == 0


def test_backtest_combination_sharpe_for_multiple(tmp_db):
    # 多筆 → sharpe 算得出
    _seed_daily_pick("A", "2026-05-01", "ma_alignment")
    _seed_daily_pick("B", "2026-05-02", "ma_alignment")
    _seed_price_series("A", "2026-05-01", [100.0, 105.0, 110.0])
    _seed_price_series("B", "2026-05-02", [200.0, 210.0, 215.0])
    with db.get_conn() as conn:
        r = sb.backtest_combination(
            conn, strategies=["ma_alignment"],
            start_date="2026-05-01", end_date="2026-05-31",
            holding_days=2, mode="union",
        )
    assert r["n_trades"] == 2
    # 不一定 > 0(看分佈),但要能算出來不是 None
    # 兩筆都 ~7~10% 報酬,差異不大,sharpe 應 > 0
    assert r["sharpe"] is not None


# === compute_strategy_correlation ===


def test_correlation_empty(tmp_db):
    with db.get_conn() as conn:
        df = sb.compute_strategy_correlation(conn, days=30)
    # 無 daily_picks 資料 → 空 df
    assert df.empty


def test_correlation_diagonal_is_one(tmp_db):
    from datetime import date as _date
    today = _date.today().isoformat()
    _seed_daily_pick("A", today, "ma_alignment")
    _seed_daily_pick("B", today, "volume_breakout")
    with db.get_conn() as conn:
        df = sb.compute_strategy_correlation(
            conn,
            strategies=["ma_alignment", "volume_breakout"],
            days=30,
        )
    assert df.shape == (2, 2)
    assert df.loc["ma_alignment", "ma_alignment"] == 1.0
    assert df.loc["volume_breakout", "volume_breakout"] == 1.0


def test_correlation_two_disjoint_strategies(tmp_db):
    from datetime import date as _date
    today = _date.today().isoformat()
    _seed_daily_pick("A", today, "ma_alignment")
    _seed_daily_pick("B", today, "volume_breakout")
    with db.get_conn() as conn:
        df = sb.compute_strategy_correlation(
            conn,
            strategies=["ma_alignment", "volume_breakout"],
            days=30,
        )
    # disjoint → off-diagonal = 0
    assert df.loc["ma_alignment", "volume_breakout"] == 0.0


def test_correlation_full_overlap(tmp_db):
    from datetime import date as _date
    today = _date.today().isoformat()
    _seed_daily_pick("A", today, "ma_alignment")
    _seed_daily_pick("A", today, "volume_breakout")
    with db.get_conn() as conn:
        df = sb.compute_strategy_correlation(
            conn,
            strategies=["ma_alignment", "volume_breakout"],
            days=30,
        )
    # 完全重疊 → off-diagonal = 1.0
    assert df.loc["ma_alignment", "volume_breakout"] == 1.0


def test_correlation_kill_switch(tmp_db, monkeypatch):
    monkeypatch.setenv("PERFORMANCE_ENABLED", "false")
    from datetime import date as _date
    today = _date.today().isoformat()
    _seed_daily_pick("A", today, "ma_alignment")
    with db.get_conn() as conn:
        df = sb.compute_strategy_correlation(conn, days=30)
    assert df.empty
