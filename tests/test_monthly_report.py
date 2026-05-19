"""scripts/monthly_strategy_report.py 單元測試。

聚焦純邏輯 + DB 查詢:
- _previous_month_range / _parse_month_arg(日期算術)
- _query_strategy_stats 走 pick_outcomes(seed → group → WR/avg/sharpe)
- _query_taiex_baseline 走 daily_prices TAIEX
- build_monthly_report 端到端 shape
- format_monthly_report_for_telegram 含關鍵 section + < 4096
"""
from __future__ import annotations

from datetime import date

from src import database as db

from scripts.monthly_strategy_report import (
    _parse_month_arg,
    _previous_month_range,
    _query_strategy_stats,
    _query_taiex_baseline,
    build_monthly_report,
    format_monthly_report_for_telegram,
)


# === 日期算術 ===

def test_previous_month_range_mid_year():
    s, e, label = _previous_month_range(today=date(2026, 5, 14))
    assert s == "2026-04-01"
    assert e == "2026-04-30"
    assert label == "2026-04"


def test_previous_month_range_january():
    """1 月 → 上個月跨年到去年 12 月。"""
    s, e, label = _previous_month_range(today=date(2026, 1, 15))
    assert s == "2025-12-01"
    assert e == "2025-12-31"
    assert label == "2025-12"


def test_parse_month_arg_december_end_day():
    """12 月 → end_iso 必須是 31 號,不能跑出去年。"""
    s, e, label = _parse_month_arg("2025-12")
    assert s == "2025-12-01"
    assert e == "2025-12-31"
    assert label == "2025-12"


def test_parse_month_arg_february_leap():
    """2024 閏年 2 月 → 29 號;2025 平年 → 28 號。"""
    _, e_leap, _ = _parse_month_arg("2024-02")
    _, e_norm, _ = _parse_month_arg("2025-02")
    assert e_leap == "2024-02-29"
    assert e_norm == "2025-02-28"


# === SQL 查詢 ===

def _seed_outcomes(strategy: str, pick_date: str, returns_d5: list[float]) -> None:
    """同 test_system_brief 的 helper。"""
    rows = [
        (pick_date, f"99{i:02d}", strategy, 100.0, r, "2026-05-19")
        for i, r in enumerate(returns_d5)
    ]
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO pick_outcomes "
            "(pick_date, sid, strategy, entry_close, return_d5, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def test_query_strategy_stats_basic(tmp_db):
    """灌策略 A 5 筆 4/15(全月份內)→ N=5, WR=0.6, avg/sharpe 算正確。"""
    _seed_outcomes("strategy_a", "2026-04-15", [2.0, 3.0, -1.0, 1.0, -2.0])
    # 4 月外的資料不該被算進來
    _seed_outcomes("strategy_a", "2026-05-02", [100.0])  # 5 月,排除
    _seed_outcomes("strategy_a", "2026-03-30", [100.0])  # 3 月,排除

    with db.get_conn() as conn:
        stats = _query_strategy_stats(conn, "2026-04-01", "2026-04-30")
    assert len(stats) == 1
    s = stats[0]
    assert s["name"] == "strategy_a"
    assert s["n"] == 5
    assert s["wr"] == 3 / 5
    assert abs(s["avg_d5"] - (2 + 3 - 1 + 1 - 2) / 5) < 1e-6
    assert s["std_d5"] > 0
    assert s["sharpe_d5"] is not None


def test_query_strategy_stats_empty_returns_empty_list(tmp_db):
    with db.get_conn() as conn:
        stats = _query_strategy_stats(conn, "2026-04-01", "2026-04-30")
    assert stats == []


def test_query_strategy_stats_single_sample_no_sharpe(tmp_db):
    """N=1 → std=0, sharpe=None(不下結論)。"""
    _seed_outcomes("solo", "2026-04-10", [5.0])
    with db.get_conn() as conn:
        stats = _query_strategy_stats(conn, "2026-04-01", "2026-04-30")
    assert stats[0]["n"] == 1
    assert stats[0]["sharpe_d5"] is None


# === TAIEX baseline ===

def _seed_taiex(rows: list[tuple[str, float]]) -> None:
    """rows: [(date_iso, close), ...]"""
    payload = [
        {
            "stock_id": "TAIEX", "date": d,
            "open": c, "high": c, "low": c, "close": c, "volume": 0,
            "trading_money": None, "trading_turnover": None, "spread": None,
        }
        for d, c in rows
    ]
    db.upsert_daily_prices(payload)


def test_query_taiex_baseline_basic(tmp_db):
    """月初前 1 交易日 20000 / 月底 21000 → +5%。"""
    _seed_taiex([
        ("2026-03-31", 20000.0),
        ("2026-04-01", 20100.0),
        ("2026-04-15", 20500.0),
        ("2026-04-30", 21000.0),
    ])
    with db.get_conn() as conn:
        ret = _query_taiex_baseline(conn, "2026-04-01", "2026-04-30")
    assert ret is not None
    assert abs(ret - 5.0) < 0.001


def test_query_taiex_baseline_missing_returns_none(tmp_db):
    """沒 TAIEX 資料 → None。"""
    with db.get_conn() as conn:
        ret = _query_taiex_baseline(conn, "2026-04-01", "2026-04-30")
    assert ret is None


# === build_monthly_report 端到端 ===

def test_build_monthly_report_shape(tmp_db):
    """空 DB 也要回完整 shape,不 raise。"""
    with db.get_conn() as conn:
        report = build_monthly_report(conn, month="2026-04")
    assert report["month"] == "2026-04"
    assert report["start_date"] == "2026-04-01"
    assert report["end_date"] == "2026-04-30"
    assert report["stats"] == []
    assert report["taiex_return_pct"] is None
    assert "generated_at" in report


def test_build_monthly_report_with_data(tmp_db):
    _seed_outcomes("hot", "2026-04-10", [3.0] * 6 + [-1.0] * 2)
    _seed_outcomes("cold", "2026-04-12", [-2.0] * 5 + [1.0] * 1)
    _seed_taiex([("2026-03-31", 20000.0), ("2026-04-30", 21000.0)])
    with db.get_conn() as conn:
        report = build_monthly_report(conn, month="2026-04")
    assert len(report["stats"]) == 2
    # 應排序:avg_d5 高 → 低
    names = [s["name"] for s in report["stats"]]
    assert names == ["hot", "cold"]
    assert report["taiex_return_pct"] is not None


# === Telegram format ===

def test_format_monthly_report_empty(tmp_db):
    """空資料時也回有效字串 + 提及 baseline。"""
    report = {
        "month": "2026-04", "stats": [], "taiex_return_pct": 2.5,
        "start_date": "2026-04-01", "end_date": "2026-04-30",
    }
    text = format_monthly_report_for_telegram(report)
    assert "2026-04" in text
    assert "TAIEX" in text
    assert "+2.50%" in text
    assert len(text) < 4096


def test_format_monthly_report_strong_weak(tmp_db):
    report = {
        "month": "2026-04",
        "start_date": "2026-04-01", "end_date": "2026-04-30",
        "stats": [
            {"name": "hot", "label": "強A", "n": 10, "wr": 0.7,
             "avg_d5": 3.5, "std_d5": 1.0, "sharpe_d5": 3.5},
            {"name": "mid", "label": "中B", "n": 8, "wr": 0.5,
             "avg_d5": 0.5, "std_d5": 1.0, "sharpe_d5": 0.5},
            {"name": "cold", "label": "弱C", "n": 6, "wr": 0.2,
             "avg_d5": -2.0, "std_d5": 1.0, "sharpe_d5": -2.0},
        ],
        "taiex_return_pct": 1.0,
    }
    text = format_monthly_report_for_telegram(report)
    assert "強策略" in text
    assert "弱策略" in text
    assert "強A" in text
    assert "弱C" in text
    assert "軍師建議" in text
    assert len(text) < 4096


def test_format_monthly_report_alpha_recommendation(tmp_db):
    """系統整體 > TAIEX + 1% → 出 alpha 訊息。"""
    report = {
        "month": "2026-04",
        "start_date": "2026-04-01", "end_date": "2026-04-30",
        "stats": [
            {"name": "x", "label": "X", "n": 10, "wr": 0.6,
             "avg_d5": 5.0, "std_d5": 1.0, "sharpe_d5": 5.0},
        ],
        "taiex_return_pct": 1.0,
    }
    text = format_monthly_report_for_telegram(report)
    assert "alpha" in text
