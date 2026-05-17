"""D 績效分析 — src/performance_analysis.py 單元測試。

涵蓋:
  - compute_user_pnl:含 long/short side、空 df、無 closed positions
  - compute_user_win_rate:30 日滾動勝率 + 平均報酬
  - compute_attribution:歸因到 daily_picks 策略、平均分配多策略命中、unknown bucket
  - compute_drawdown_curve:peak / drawdown / drawdown_pct
  - kill-switch PERFORMANCE_ENABLED=false → 全空
"""
from __future__ import annotations

import sys
from datetime import date as _date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from src import database as db  # noqa: E402
from src import performance_analysis as pa  # noqa: E402

# tmp_db fixture 從 tests/conftest.py 來


def _seed_position(
    sid: str, entry_date: str, entry_price: float,
    shares: int = 1000, side: str = "long",
    is_open: bool = False, exit_date: str | None = None,
    exit_price: float | None = None,
) -> int:
    """寫一筆 user_positions(可控 open/closed)。"""
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO user_positions ("
            "stock_id, entry_date, entry_price, shares, side, "
            "is_open, exit_date, exit_price, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-05-17T00:00:00Z', "
            "'2026-05-17T00:00:00Z')",
            (
                sid, entry_date, entry_price, shares, side,
                0 if not is_open else 1, exit_date, exit_price,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def _seed_daily_pick(
    sid: str, trade_date: str, strategy: str = "ma_alignment",
) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_picks ("
            "trade_date, universe, strategy, sid, params_hash, "
            "payload, computed_at) VALUES (?, 'top_50', ?, ?, "
            "'default_v1', '{}', '2026-05-17T00:00:00Z')",
            (trade_date, strategy, sid),
        )
        conn.commit()


def _seed_price(sid: str, date_iso: str, close: float) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_prices ("
            "stock_id, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, 1000)",
            (sid, date_iso, close, close, close, close),
        )
        conn.commit()


# === compute_user_pnl ===


def test_compute_user_pnl_empty(tmp_db):
    with db.get_conn() as conn:
        df = pa.compute_user_pnl(conn)
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_compute_user_pnl_long_winner(tmp_db):
    _seed_position(
        "2330", "2026-05-01", 600.0, shares=1000, side="long",
        exit_date="2026-05-10", exit_price=650.0,
    )
    with db.get_conn() as conn:
        df = pa.compute_user_pnl(conn)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["sid"] == "2330"
    # (650-600) * 1000 = 50000
    assert row["pnl"] == pytest.approx(50000.0)
    # 50/600 = 8.3333%
    assert row["pnl_pct"] == pytest.approx(8.3333, rel=1e-3)
    assert row["holding_days"] == 9


def test_compute_user_pnl_short_winner(tmp_db):
    _seed_position(
        "2330", "2026-05-01", 600.0, shares=1000, side="short",
        exit_date="2026-05-10", exit_price=550.0,
    )
    with db.get_conn() as conn:
        df = pa.compute_user_pnl(conn)
    assert len(df) == 1
    # short:價跌賺。(550-600) * 1000 * -1 = 50000
    assert df.iloc[0]["pnl"] == pytest.approx(50000.0)


def test_compute_user_pnl_excludes_open(tmp_db):
    _seed_position("2330", "2026-05-01", 600.0, is_open=True)
    _seed_position(
        "2454", "2026-04-01", 1000.0, exit_date="2026-04-15",
        exit_price=1100.0,
    )
    with db.get_conn() as conn:
        df = pa.compute_user_pnl(conn)
    assert len(df) == 1
    assert df.iloc[0]["sid"] == "2454"


def test_compute_user_pnl_date_range_filter(tmp_db):
    _seed_position("A", "2026-04-01", 100.0, exit_date="2026-04-15", exit_price=110.0)
    _seed_position("B", "2026-05-01", 100.0, exit_date="2026-05-10", exit_price=120.0)
    with db.get_conn() as conn:
        df = pa.compute_user_pnl(
            conn, start_date="2026-05-01", end_date="2026-05-31",
        )
    assert len(df) == 1
    assert df.iloc[0]["sid"] == "B"


def test_compute_user_pnl_kill_switch(tmp_db, monkeypatch):
    monkeypatch.setenv("PERFORMANCE_ENABLED", "false")
    _seed_position("2330", "2026-05-01", 600.0, exit_date="2026-05-10", exit_price=650.0)
    with db.get_conn() as conn:
        df = pa.compute_user_pnl(conn)
    assert df.empty


# === compute_user_win_rate ===


def test_win_rate_no_closed(tmp_db):
    with db.get_conn() as conn:
        stats = pa.compute_user_win_rate(conn)
    assert stats["n_trades"] == 0
    assert stats["win_rate"] is None


def test_win_rate_mixed(tmp_db):
    today = _date.today()
    # 3 wins 1 loss in last 30 days
    for i, (sid, entry, exit_px) in enumerate([
        ("A", 100.0, 110.0), ("B", 100.0, 120.0),
        ("C", 100.0, 90.0), ("D", 100.0, 105.0),
    ]):
        exit_date = (today - timedelta(days=i * 2)).isoformat()
        entry_date = (today - timedelta(days=i * 2 + 5)).isoformat()
        _seed_position(
            sid, entry_date, entry, exit_date=exit_date, exit_price=exit_px,
        )
    with db.get_conn() as conn:
        stats = pa.compute_user_win_rate(conn, window_days=30)
    assert stats["n_trades"] == 4
    assert stats["n_wins"] == 3
    assert stats["win_rate"] == 0.75


# === compute_attribution ===


def test_attribution_routes_to_strategy(tmp_db):
    # 部位 2330 進場 2026-05-01;daily_picks 命中 ma_alignment 2026-05-02
    # → 歸因到 ma_alignment
    _seed_position(
        "2330", "2026-05-01", 600.0, exit_date="2026-05-10", exit_price=660.0,
    )
    _seed_daily_pick("2330", "2026-05-02", strategy="ma_alignment")

    with db.get_conn() as conn:
        attr = pa.compute_attribution(conn)
    assert "ma_alignment" in attr
    assert attr["ma_alignment"]["count"] == 1
    assert attr["ma_alignment"]["total_pnl"] == pytest.approx(60000.0)
    assert attr["ma_alignment"]["win_rate"] == 1.0
    assert "_unknown" not in attr


def test_attribution_splits_across_multiple_strategies(tmp_db):
    """同 sid + 進場日 ± 5 命中兩策略 → P&L 平均分。"""
    _seed_position(
        "2330", "2026-05-01", 600.0, exit_date="2026-05-10", exit_price=660.0,
    )
    _seed_daily_pick("2330", "2026-05-01", strategy="ma_alignment")
    _seed_daily_pick("2330", "2026-05-03", strategy="volume_breakout")

    with db.get_conn() as conn:
        attr = pa.compute_attribution(conn)
    assert "ma_alignment" in attr
    assert "volume_breakout" in attr
    # 60000 / 2 = 30000 each
    assert attr["ma_alignment"]["total_pnl"] == pytest.approx(30000.0)
    assert attr["volume_breakout"]["total_pnl"] == pytest.approx(30000.0)


def test_attribution_unknown_bucket_when_no_match(tmp_db):
    _seed_position(
        "9999", "2026-05-01", 100.0, exit_date="2026-05-10", exit_price=110.0,
    )
    # 無對應 daily_picks
    with db.get_conn() as conn:
        attr = pa.compute_attribution(conn)
    assert "_unknown" in attr
    assert attr["_unknown"]["count"] == 1


def test_attribution_kill_switch(tmp_db, monkeypatch):
    monkeypatch.setenv("PERFORMANCE_ENABLED", "false")
    _seed_position("2330", "2026-05-01", 600.0, exit_date="2026-05-10", exit_price=660.0)
    _seed_daily_pick("2330", "2026-05-02")
    with db.get_conn() as conn:
        attr = pa.compute_attribution(conn)
    assert attr == {}


# === compute_drawdown_curve ===


def test_drawdown_curve_empty(tmp_db):
    with db.get_conn() as conn:
        df = pa.compute_drawdown_curve(conn)
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_drawdown_curve_basic(tmp_db):
    # 3 平倉:+50k, -30k, +20k → equity 50k, 20k, 40k;peak 50k, 50k, 50k
    # drawdown 0, -30k, -10k
    _seed_position("A", "2026-04-25", 100.0, shares=1000, exit_date="2026-05-01", exit_price=150.0)
    _seed_position("B", "2026-05-02", 100.0, shares=1000, exit_date="2026-05-05", exit_price=70.0)
    _seed_position("C", "2026-05-06", 100.0, shares=1000, exit_date="2026-05-10", exit_price=120.0)
    with db.get_conn() as conn:
        df = pa.compute_drawdown_curve(conn)
    assert len(df) == 3
    eq = df["equity"].tolist()
    assert eq == pytest.approx([50000.0, 20000.0, 40000.0])
    dd = df["drawdown"].tolist()
    assert dd == pytest.approx([0.0, -30000.0, -10000.0])


# === compute_summary_metrics ===


def test_summary_metrics_empty(tmp_db):
    with db.get_conn() as conn:
        s = pa.compute_summary_metrics(conn)
    assert s["n_trades"] == 0
    assert s["total_pnl"] == 0.0
    assert s["win_rate"] is None
    assert s["sharpe"] is None


def test_summary_metrics_basic(tmp_db):
    _seed_position("A", "2026-04-25", 100.0, exit_date="2026-05-01", exit_price=150.0)
    _seed_position("B", "2026-05-02", 100.0, exit_date="2026-05-05", exit_price=80.0)
    with db.get_conn() as conn:
        s = pa.compute_summary_metrics(conn)
    assert s["n_trades"] == 2
    assert s["win_rate"] == 0.5
    assert s["total_pnl"] == pytest.approx(30000.0)
    # Sharpe 應該算得出(有兩筆,std > 0)
    assert s["sharpe"] is not None


# === best_strategy_by_pnl ===


def test_best_strategy_excludes_unknown():
    attr = {
        "_unknown": {"total_pnl": 100000.0, "count": 1},
        "ma_alignment": {"total_pnl": 30000.0, "count": 1},
        "volume_breakout": {"total_pnl": 50000.0, "count": 2},
    }
    best = pa.best_strategy_by_pnl(attr)
    assert best is not None
    key, info = best
    assert key == "volume_breakout"


def test_best_strategy_returns_none_when_empty():
    assert pa.best_strategy_by_pnl({}) is None


def test_best_strategy_respects_min_count():
    attr = {
        "ma_alignment": {"total_pnl": 100000.0, "count": 1},
        "volume_breakout": {"total_pnl": 50000.0, "count": 5},
    }
    best = pa.best_strategy_by_pnl(attr, min_count=3)
    assert best is not None
    assert best[0] == "volume_breakout"


# === is_enabled ===


def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("PERFORMANCE_ENABLED", raising=False)
    assert pa.is_enabled() is True


def test_is_enabled_false(monkeypatch):
    monkeypatch.setenv("PERFORMANCE_ENABLED", "false")
    assert pa.is_enabled() is False


def test_is_enabled_truthy_variants(monkeypatch):
    for v in ("true", "1", "TRUE", "yes", "on"):
        monkeypatch.setenv("PERFORMANCE_ENABLED", v)
        assert pa.is_enabled() is True
