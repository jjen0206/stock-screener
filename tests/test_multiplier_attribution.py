"""P2-7 consensus_multiplier 歸因測試 — _build_multiplier_attribution。

驗 paper_trades 按 multiplier 三 bucket(1.0/1.25/1.5)group,WR 算對。
format 應出現「🎯 共識倍率 vs 勝率」+ 各段 N/WR。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from src import database as db
from src.system_brief import (
    _bucket_label_for,
    _build_multiplier_attribution,
    format_brief_for_telegram,
)


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _add_paper_trade(
    sid: str, entry_date: str, status: str,
    consensus_multiplier: float | None,
    return_pct: float | None,
) -> None:
    """寫 paper_trades 一筆 closed trade。同 (sid, entry_date) UNIQUE。"""
    now = "2026-05-19T10:00:00+00:00"
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades
                (sid, name, entry_date, entry_price, target_price, stop_price,
                 status, return_pct, consensus_multiplier, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, sid, entry_date, 100.0, 105.0, 97.0,
             status, return_pct, consensus_multiplier, now),
        )


# === _bucket_label_for 純邏輯 ===

def test_bucket_label_exact_match():
    assert _bucket_label_for(1.0) == "1.0× solo"
    assert _bucket_label_for(1.25) == "1.25× mid"
    assert _bucket_label_for(1.5) == "1.5× strong"


def test_bucket_label_within_tolerance():
    """±0.04 內 → match,±0.05 外 → None。"""
    assert _bucket_label_for(1.02) == "1.0× solo"
    assert _bucket_label_for(1.27) == "1.25× mid"
    assert _bucket_label_for(1.48) == "1.5× strong"


def test_bucket_label_none_and_far_away():
    assert _bucket_label_for(None) is None
    assert _bucket_label_for(1.1) is None       # 介於 1.0 / 1.25 不歸誰
    assert _bucket_label_for(2.0) is None       # 超出範圍


# === _build_multiplier_attribution(走真 SQL)===

def test_multiplier_attribution_three_buckets(tmp_db):
    """灌三段資料:1.0 3 筆全輸 / 1.25 4 筆 2 勝 / 1.5 3 筆全勝 → WR 遞增。"""
    # 1.0× solo:3 lose
    _add_paper_trade("1001", _days_ago(5), "lose", 1.0, -3.0)
    _add_paper_trade("1002", _days_ago(5), "lose", 1.0, -3.0)
    _add_paper_trade("1003", _days_ago(5), "timeout_lose", 1.0, -1.5)
    # 1.25× mid:2 win + 2 lose
    _add_paper_trade("2001", _days_ago(5), "win", 1.25, 5.0)
    _add_paper_trade("2002", _days_ago(5), "timeout_win", 1.25, 2.0)
    _add_paper_trade("2003", _days_ago(5), "lose", 1.25, -3.0)
    _add_paper_trade("2004", _days_ago(5), "timeout_lose", 1.25, -1.0)
    # 1.5× strong:3 win
    _add_paper_trade("3001", _days_ago(5), "win", 1.5, 5.0)
    _add_paper_trade("3002", _days_ago(5), "win", 1.5, 5.0)
    _add_paper_trade("3003", _days_ago(5), "timeout_win", 1.5, 3.0)

    with db.get_conn() as conn:
        attr = _build_multiplier_attribution(conn)

    labels = [a["label"] for a in attr]
    assert labels == ["1.0× solo", "1.25× mid", "1.5× strong"]

    by_label = {a["label"]: a for a in attr}
    assert by_label["1.0× solo"]["n"] == 3
    assert by_label["1.0× solo"]["wr"] == 0.0
    assert by_label["1.25× mid"]["n"] == 4
    assert by_label["1.25× mid"]["wr"] == 0.5
    assert by_label["1.5× strong"]["n"] == 3
    assert by_label["1.5× strong"]["wr"] == 1.0

    # WR 應該遞增(P2-7 衝突檢測有效)
    wrs = [by_label[l]["wr"] for l in labels]
    assert wrs[0] < wrs[1] < wrs[2]


def test_multiplier_attribution_excludes_active_status(tmp_db):
    """status='active'(尚未平倉)不該被計入歸因。"""
    _add_paper_trade("9001", _days_ago(2), "active", 1.5, None)
    with db.get_conn() as conn:
        attr = _build_multiplier_attribution(conn)
    n_total = sum(a["n"] for a in attr)
    assert n_total == 0


def test_multiplier_attribution_excludes_null_multiplier(tmp_db):
    """consensus_multiplier IS NULL(P2-7 上線前舊資料)不被歸因。"""
    _add_paper_trade("8001", _days_ago(2), "win", None, 5.0)
    with db.get_conn() as conn:
        attr = _build_multiplier_attribution(conn)
    n_total = sum(a["n"] for a in attr)
    assert n_total == 0


def test_multiplier_attribution_time_window(tmp_db):
    """超出時間窗的 trade(預設 30d)不被算進來。"""
    _add_paper_trade("7001", _days_ago(2), "win", 1.5, 5.0)     # 在窗內
    _add_paper_trade("7002", _days_ago(60), "win", 1.5, 5.0)    # 30d 外
    with db.get_conn() as conn:
        attr = _build_multiplier_attribution(conn, days=30)
    by_label = {a["label"]: a for a in attr}
    assert by_label["1.5× strong"]["n"] == 1


def test_multiplier_attribution_empty_db(tmp_db):
    """完全沒資料 → 仍回 3 buckets(全 N=0),不 raise。"""
    with db.get_conn() as conn:
        attr = _build_multiplier_attribution(conn)
    assert len(attr) == 3
    assert all(a["n"] == 0 for a in attr)
    assert all(a["wr"] is None for a in attr)


# === Telegram format ===

def test_format_brief_includes_multiplier_section():
    brief: dict[str, Any] = {
        "generated_at": "2026-05-19 10:00:00",
        "health": {"is_healthy": True, "warnings": [],
                   "daily_prices_stale_days": 0,
                   "institutional_stale_days": 0},
        "strategy_performance": [],
        "trend_windows": [],
        "multiplier_attribution": [
            {"label": "1.0× solo", "mult": 1.0, "n": 3, "wr": 0.0,
             "avg_return_pct": -2.0},
            {"label": "1.25× mid", "mult": 1.25, "n": 4, "wr": 0.5,
             "avg_return_pct": 0.75},
            {"label": "1.5× strong", "mult": 1.5, "n": 3, "wr": 1.0,
             "avg_return_pct": 4.33},
        ],
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
    assert "共識倍率" in text
    assert "1.0× solo" in text
    assert "1.5× strong" in text
    assert "N=3" in text


def test_format_brief_omits_multiplier_when_all_zero():
    brief: dict[str, Any] = {
        "generated_at": "2026-05-19 10:00:00",
        "health": {"is_healthy": True, "warnings": [],
                   "daily_prices_stale_days": 0,
                   "institutional_stale_days": 0},
        "strategy_performance": [],
        "trend_windows": [],
        "multiplier_attribution": [
            {"label": "1.0× solo", "mult": 1.0, "n": 0, "wr": None,
             "avg_return_pct": None},
            {"label": "1.25× mid", "mult": 1.25, "n": 0, "wr": None,
             "avg_return_pct": None},
            {"label": "1.5× strong", "mult": 1.5, "n": 0, "wr": None,
             "avg_return_pct": None},
        ],
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
    assert "共識倍率" not in text
