"""Phase 2 Q4 — weekly brief 60d/90d 視窗 + 多視窗趨勢對比。

驗 _build_trend_windows 三個視窗 N 計算正確 + format 出現 section。
"""
from __future__ import annotations

from datetime import date, timedelta

from src import database as db
from src.system_brief import (
    _build_strategy_window_summary,
    _build_trend_windows,
    build_system_brief,
    format_brief_for_telegram,
)


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _seed(strategy: str, pick_date: str, returns_d5: list[float]) -> None:
    rows = [
        (pick_date, f"99{i:02d}", strategy, 100.0, r, date.today().isoformat())
        for i, r in enumerate(returns_d5)
    ]
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO pick_outcomes "
            "(pick_date, sid, strategy, entry_close, return_d5, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def test_window_summary_30d_excludes_older(tmp_db):
    """40 天前的資料 30d 窗口看不到,60d 看得到。"""
    _seed("foo", _days_ago(10), [1.0, 2.0])    # 在 30d 內
    _seed("foo", _days_ago(40), [3.0, 4.0])    # 30d 外,60d 內

    with db.get_conn() as conn:
        w30 = _build_strategy_window_summary(conn, 30)
        w60 = _build_strategy_window_summary(conn, 60)
    assert w30["n"] == 2
    assert w60["n"] == 4
    assert w30["days"] == 30
    assert w60["days"] == 60


def test_window_summary_empty_db(tmp_db):
    with db.get_conn() as conn:
        w = _build_strategy_window_summary(conn, 30)
    assert w == {"days": 30, "n": 0, "wr": None, "avg_d5": None}


def test_build_trend_windows_three_buckets(tmp_db):
    """30 / 60 / 90 各一筆。"""
    _seed("a", _days_ago(5), [1.0])
    _seed("a", _days_ago(45), [2.0])
    _seed("a", _days_ago(75), [3.0])
    # 100 天前(90d 外)應該不被算進來
    _seed("a", _days_ago(100), [99.0])

    with db.get_conn() as conn:
        windows = _build_trend_windows(conn)
    assert len(windows) == 3
    days = [w["days"] for w in windows]
    assert days == [30, 60, 90]
    # N 計數應為累積 1 → 2 → 3
    n = {w["days"]: w["n"] for w in windows}
    assert n == {30: 1, 60: 2, 90: 3}


def test_build_system_brief_includes_trend_windows(tmp_db):
    """端到端:build_system_brief 回 dict 應含 trend_windows key。"""
    with db.get_conn() as conn:
        brief = build_system_brief(conn)
    assert "trend_windows" in brief
    assert isinstance(brief["trend_windows"], list)
    assert len(brief["trend_windows"]) == 3


def test_format_brief_includes_trend_section(tmp_db):
    """有資料時 format 應出現「📈 趨勢對比」section。"""
    brief = {
        "generated_at": "2026-05-19 10:00:00",
        "health": {"is_healthy": True, "warnings": [],
                   "daily_prices_stale_days": 0,
                   "institutional_stale_days": 0},
        "strategy_performance": [],
        "trend_windows": [
            {"days": 30, "n": 50, "wr": 0.55, "avg_d5": 1.2},
            {"days": 60, "n": 100, "wr": 0.52, "avg_d5": 0.8},
            {"days": 90, "n": 150, "wr": 0.50, "avg_d5": 0.5},
        ],
        "multiplier_attribution": [],
        "ml_performance": {"calibration_7d": None, "calibration_sample_n": 0},
        "market_state": {
            "regime": "bull", "regime_label": "多頭", "regime_emoji": "📈",
            "inst_consensus_count_today": 0,
            "inst_consensus_count_7d_ago": 0,
            "inst_consensus_trend_7d": "持平",
            "shareholder_movers_count": 0,
            "premium_picks_count": 0,
        },
        "watchlist_today": [],
        "recommendations": ["test"],
    }
    text = format_brief_for_telegram(brief)
    assert "趨勢對比" in text
    assert "30d" in text
    assert "60d" in text
    assert "90d" in text
    assert "N=50" in text
    assert "N=100" in text
    assert "N=150" in text


def test_format_brief_omits_trend_section_when_empty(tmp_db):
    """所有視窗 N=0 → section 不出現(避免空 noise)。"""
    brief = {
        "generated_at": "2026-05-19 10:00:00",
        "health": {"is_healthy": True, "warnings": [],
                   "daily_prices_stale_days": 0,
                   "institutional_stale_days": 0},
        "strategy_performance": [],
        "trend_windows": [
            {"days": 30, "n": 0, "wr": None, "avg_d5": None},
            {"days": 60, "n": 0, "wr": None, "avg_d5": None},
            {"days": 90, "n": 0, "wr": None, "avg_d5": None},
        ],
        "multiplier_attribution": [],
        "ml_performance": {"calibration_7d": None, "calibration_sample_n": 0},
        "market_state": {
            "regime": "bull", "regime_label": "多頭", "regime_emoji": "📈",
            "inst_consensus_count_today": 0,
            "inst_consensus_count_7d_ago": 0,
            "inst_consensus_trend_7d": "持平",
            "shareholder_movers_count": 0,
            "premium_picks_count": 0,
        },
        "watchlist_today": [],
        "recommendations": ["test"],
    }
    text = format_brief_for_telegram(brief)
    assert "趨勢對比" not in text
