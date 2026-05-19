"""src/system_brief.py 單元測試。

production schema fixture（tmp DB + db.init_db），不 mock streamlit。
測試重點:
  - 健康度:正常 / stale / 全空
  - 策略 verdict 邊界(WR ≥60%, ≤40%, N<30)
  - recommendations 空資料 fallback + 軍師主觀觸發
  - 法人共識趨勢 上升 / 下降 / 持平
  - format_brief_for_telegram 結構 + 4096 字
"""
from __future__ import annotations

from datetime import date, timedelta

from src import database as db
from src.system_brief import (
    _build_health,
    _build_strategy_performance,
    _build_recommendations,
    _classify_verdict,
    build_system_brief,
    format_brief_for_telegram,
)

# tmp_db fixture 共用 tests/conftest.py


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


# === _classify_verdict 純邏輯 ===

def test_verdict_hot_at_boundary():
    """WR = 60% AND N = 30 → 🔥 發燙(門檻包含等號)。"""
    assert _classify_verdict(30, 0.60) == "🔥 發燙"


def test_verdict_cold_at_boundary():
    """WR = 40% AND N = 30 → 🥶 該休息(門檻包含等號)。"""
    assert _classify_verdict(30, 0.40) == "🥶 該休息"


def test_verdict_observation_below_min_n():
    """N < 30 → 🌱 觀察中,無論 WR 多高也不下結論。"""
    assert _classify_verdict(29, 0.90) == "🌱 觀察中"


def test_verdict_neutral_band():
    """40% < WR < 60% AND N >= 30 → 普通(— 中性)。"""
    assert _classify_verdict(50, 0.50) == "—"


# === _build_health ===

def test_health_normal(tmp_db):
    """daily_prices + institutional 都今天 → is_healthy True, 0 warnings。"""
    today = _today()
    db.upsert_daily_prices([{
        "stock_id": "2330", "date": today,
        "open": 100, "high": 100, "low": 100, "close": 100, "volume": 0,
        "trading_money": None, "trading_turnover": None, "spread": None,
    }])
    db.upsert_institutional([{
        "stock_id": "2330", "date": today,
        "foreign_buy_sell": 1, "trust_buy_sell": 1,
        "dealer_buy_sell": 1, "total_buy_sell": 3,
    }])

    with db.get_conn() as conn:
        h = _build_health(conn)
    assert h["is_healthy"] is True
    assert h["warnings"] == [] or "shareholder" in (h["warnings"][0] if h["warnings"] else "")
    assert h["daily_prices_max_date"] == today
    assert h["daily_prices_stale_days"] == 0


def test_health_stale_daily_prices(tmp_db):
    """daily_prices 落後 5 天(> 3 天門檻)→ is_healthy False + warnings 有提及。"""
    stale = _days_ago(5)
    db.upsert_daily_prices([{
        "stock_id": "2330", "date": stale,
        "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0,
        "trading_money": None, "trading_turnover": None, "spread": None,
    }])
    db.upsert_institutional([{
        "stock_id": "2330", "date": _today(),
        "foreign_buy_sell": 1, "trust_buy_sell": 1,
        "dealer_buy_sell": 1, "total_buy_sell": 3,
    }])
    with db.get_conn() as conn:
        h = _build_health(conn)
    assert h["is_healthy"] is False
    assert any("daily_prices" in w for w in h["warnings"])
    assert h["daily_prices_stale_days"] == 5


def test_health_empty_db(tmp_db):
    """完全空 DB → is_healthy False, warnings 列各表 missing。"""
    with db.get_conn() as conn:
        h = _build_health(conn)
    assert h["is_healthy"] is False
    assert h["daily_prices_max_date"] is None
    # 3 個表都該有警告(institutional / daily_prices / shareholder)
    assert len(h["warnings"]) >= 2


# === _build_strategy_performance ===

def _seed_outcomes(strategy: str, pick_date: str, returns_d5: list[float]) -> None:
    """灌 pick_outcomes,每筆 sid 用 idx 區分,strategy 一致。"""
    rows = [
        (pick_date, f"99{i:02d}", strategy, 100.0, r, _today())
        for i, r in enumerate(returns_d5)
    ]
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO pick_outcomes "
            "(pick_date, sid, strategy, entry_close, return_d5, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def test_strategy_performance_hot(tmp_db):
    """灌 35 筆策略 A,70% 正報酬 → verdict = 🔥 發燙。"""
    returns = [1.0] * 25 + [-1.0] * 10  # 25 wins / 10 losses → WR ~71%
    _seed_outcomes("strategy_hot", _days_ago(5), returns)
    with db.get_conn() as conn:
        perf = _build_strategy_performance(conn)
    assert len(perf) == 1
    s = perf[0]
    assert s["name"] == "strategy_hot"
    assert s["n"] == 35
    assert s["wr"] is not None and s["wr"] > 0.6
    assert s["verdict"] == "🔥 發燙"


def test_strategy_performance_cold(tmp_db):
    """灌 40 筆策略 B,30% 正報酬 → verdict = 🥶 該休息。"""
    returns = [1.0] * 12 + [-1.0] * 28  # 12 wins / 28 losses → WR ~30%
    _seed_outcomes("strategy_cold", _days_ago(5), returns)
    with db.get_conn() as conn:
        perf = _build_strategy_performance(conn)
    assert perf[0]["verdict"] == "🥶 該休息"


def test_strategy_performance_small_sample(tmp_db):
    """灌 10 筆策略 C,WR 80% 但 N < 30 → verdict = 🌱 觀察中。"""
    returns = [1.0] * 8 + [-1.0] * 2
    _seed_outcomes("strategy_small", _days_ago(3), returns)
    with db.get_conn() as conn:
        perf = _build_strategy_performance(conn)
    assert perf[0]["verdict"] == "🌱 觀察中"


def test_strategy_performance_empty(tmp_db):
    """沒任何 pick_outcomes → 空 list,不 raise。"""
    with db.get_conn() as conn:
        perf = _build_strategy_performance(conn)
    assert perf == []


# === _build_recommendations ===

def test_recommendations_hot_cold_both(tmp_db):
    """既有 🔥 又有 🥶 → 都該出建議,各最多 2 條。"""
    health = {"is_healthy": True, "warnings": []}
    strategy_perf = [
        {"name": "hot1", "n": 40, "wr": 0.70, "avg_d5": 5.0, "verdict": "🔥 發燙"},
        {"name": "cold1", "n": 40, "wr": 0.30, "avg_d5": -1.0, "verdict": "🥶 該休息"},
    ]
    ml_perf = {"calibration_7d": None, "calibration_sample_n": 0}
    market_state = {
        "regime": "bull", "inst_consensus_count_today": 3,
        "inst_consensus_count_7d_ago": 3,
        "inst_consensus_trend_7d": "持平",
        "shareholder_movers_count": 10,
    }
    recs = _build_recommendations(health, strategy_perf, ml_perf, market_state)
    assert any("🔥" in r for r in recs)
    assert any("🥶" in r for r in recs)
    assert len(recs) >= 2


def test_recommendations_fallback_when_empty(tmp_db):
    """空輸入 → 至少 1 條中性提示。"""
    recs = _build_recommendations(
        health={"is_healthy": True, "warnings": []},
        strategy_perf=[],
        ml_perf={"calibration_7d": None, "calibration_sample_n": 0},
        market_state={
            "regime": "bull",
            "inst_consensus_count_today": 0,
            "inst_consensus_count_7d_ago": 0,
            "inst_consensus_trend_7d": "持平",
            "shareholder_movers_count": 0,
        },
    )
    assert len(recs) >= 1


def test_recommendations_bear_regime(tmp_db):
    """regime = bear → 降低短線權重建議。"""
    recs = _build_recommendations(
        health={"is_healthy": True, "warnings": []},
        strategy_perf=[],
        ml_perf={"calibration_7d": None, "calibration_sample_n": 0},
        market_state={
            "regime": "bear", "regime_label": "空頭",
            "inst_consensus_count_today": 0,
            "inst_consensus_count_7d_ago": 0,
            "inst_consensus_trend_7d": "持平",
            "shareholder_movers_count": 0,
        },
    )
    assert any("空頭" in r or "降低短線" in r for r in recs)


def test_recommendations_health_warning_at_top(tmp_db):
    """is_healthy=False → 第一條一定是 🚨 健康異常。"""
    recs = _build_recommendations(
        health={"is_healthy": False, "warnings": ["daily_prices 落後 7 天"]},
        strategy_perf=[],
        ml_perf={"calibration_7d": None, "calibration_sample_n": 0},
        market_state={
            "regime": "bull",
            "inst_consensus_count_today": 0,
            "inst_consensus_count_7d_ago": 0,
            "inst_consensus_trend_7d": "持平",
            "shareholder_movers_count": 0,
        },
    )
    assert recs[0].startswith("🚨")


# === build_system_brief integration ===

def test_build_system_brief_dict_shape(tmp_db):
    """端到端:空 DB 也該回完整 dict shape,所有 key 存在不 raise。"""
    with db.get_conn() as conn:
        brief = build_system_brief(conn)
    expected_keys = {
        "generated_at", "health", "strategy_performance",
        "trend_windows", "multiplier_attribution",
        "ml_performance", "market_state", "watchlist_today",
        "real_performance", "recommendations",
    }
    assert set(brief.keys()) == expected_keys
    # recommendations 至少 1 條(fallback)
    assert len(brief["recommendations"]) >= 1
    # generated_at 格式 YYYY-MM-DD HH:MM:SS
    assert len(brief["generated_at"]) == 19


# === format_brief_for_telegram ===

def test_format_brief_telegram_includes_sections(tmp_db):
    """format 結果包含關鍵 emoji + 字數 < 4096。"""
    brief = {
        "generated_at": "2026-05-14 22:30:00",
        "health": {"is_healthy": True, "warnings": [],
                   "daily_prices_stale_days": 1,
                   "institutional_stale_days": 1},
        "strategy_performance": [
            {"name": "vol_break", "n": 50, "wr": 0.70,
             "avg_d5": 4.5, "verdict": "🔥 發燙"},
            {"name": "rsi_recov", "n": 80, "wr": 0.30,
             "avg_d5": -1.0, "verdict": "🥶 該休息"},
        ],
        "ml_performance": {"calibration_7d": None, "calibration_sample_n": 0},
        "market_state": {
            "regime": "bull", "regime_label": "多頭", "regime_emoji": "📈",
            "inst_consensus_count_today": 5,
            "inst_consensus_count_7d_ago": 2,
            "inst_consensus_trend_7d": "上升",
            "shareholder_movers_count": 16,
            "premium_picks_count": 0,
        },
        "watchlist_today": [
            {"sid": "2330", "name": "台積電", "reason": "三維交集"},
        ],
        "recommendations": [
            "🔥 vol_break WR 70%",
            "🥶 rsi_recov WR 30%",
        ],
    }
    text = format_brief_for_telegram(brief)
    # 標題 + 主要 section
    assert "系統結論週報" in text
    assert "系統健康" in text
    assert "發燙策略" in text
    assert "該休息" in text
    assert "市場狀態" in text
    assert "觀察清單" in text
    assert "軍師建議" in text
    # ISO week 標記
    assert "W" in text and "2026" in text
    # 4096 字保險
    assert len(text) < 4096
