"""src/strategies.py::screen_earnings_surprise_followthrough 單元測試 (PEAD)。

涵蓋:
- _next_trading_day / _trading_days_after 跨週末
- 無 announce_date 在窗口內 → 0 picks
- announce 後第 1-5 個交易日 + eps_yoy > 50 → 入選
- announce 後窗口內 + gap_open > 3% (eps_yoy None / 不足) → 入選
- 兩條件都不滿足 → 不入選
- announce 後超過 5 個交易日 → 不入選 (出窗口)
- announce 在週五,as_of 在下週一 (第 1 個交易日) → 入選
- score 計算正確: max(eps_yoy/100, gap_pct/5),clip 0-2
- registry 已 wire
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _td

import pytest

from src import config, database as db
from src import strategies as strat


# === 純 helper 測試(不需 DB) ===


def test_next_trading_day_basic():
    """簡單情境:announce 在週三,下一交易日就是下個有 price 的日子。"""
    trading_dates = ["2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07"]
    assert strat._next_trading_day("2026-05-04", trading_dates) == "2026-05-05"


def test_next_trading_day_skip_weekend():
    """announce 在週五(05/08),trading_dates 跳過週末 → 下週一(05/11)。"""
    trading_dates = [
        "2026-05-06", "2026-05-07", "2026-05-08",  # 三/四/五
        "2026-05-11", "2026-05-12",                 # 下週一/二
    ]
    assert strat._next_trading_day("2026-05-08", trading_dates) == "2026-05-11"


def test_next_trading_day_announce_on_weekend():
    """announce 標的 date 落在週六 → 下個交易日是週一。

    PEAD 場景:財報半夜公佈,FinMind 把 date 標成週六/週日是可能的。
    """
    trading_dates = ["2026-05-08", "2026-05-11", "2026-05-12"]
    # announce_date 2026-05-09 (週六) → next trading day = 2026-05-11
    assert strat._next_trading_day("2026-05-09", trading_dates) == "2026-05-11"


def test_next_trading_day_no_future():
    """trading_dates 最末日就是 announce_date → 沒有更後面的交易日,回 None。"""
    trading_dates = ["2026-05-04", "2026-05-05", "2026-05-08"]
    assert strat._next_trading_day("2026-05-08", trading_dates) is None


def test_trading_days_after_n5_with_weekend():
    """announce=週五 05/08,第 5 個交易日 = 下下週五 05/15。"""
    trading_dates = [
        "2026-05-08",  # 週五 (announce)
        "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15",
        "2026-05-18",
    ]
    assert (
        strat._trading_days_after("2026-05-08", 5, trading_dates)
        == "2026-05-15"
    )


def test_trading_days_after_insufficient():
    """資料不足 N 個交易日 → 回 None。"""
    trading_dates = ["2026-05-08", "2026-05-11", "2026-05-12"]
    # 公佈日後只有 2 個交易日 (11/12),要 5 個 → None
    assert strat._trading_days_after("2026-05-08", 5, trading_dates) is None


def test_trading_days_after_n_zero():
    """n <= 0 → None(防呆)。"""
    assert strat._trading_days_after("2026-05-08", 0, ["2026-05-09"]) is None
    assert strat._trading_days_after("2026-05-08", -1, ["2026-05-09"]) is None


# === DB 測試 fixture ===


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "pead.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def _seed_stocks(sids: list[str]) -> None:
    db.upsert_stocks([
        {"stock_id": s, "name": f"股{s}", "market": "TW"}
        for s in sids
    ])


def _seed_daily_prices(
    sid: str,
    start: str,
    end: str,
    close: float = 100.0,
    gap_open_on: dict[str, float] | None = None,
) -> None:
    """灌日 K 線(只工作日);特定日期可指定 open 創造 gap。

    gap_open_on = {"2026-05-11": 105.0}  → 該日 open=105(其他日 open=close)。
    """
    gap_open_on = gap_open_on or {}
    rows = []
    d = _date.fromisoformat(start)
    end_d = _date.fromisoformat(end)
    while d <= end_d:
        if d.weekday() < 5:
            ds = d.isoformat()
            o = float(gap_open_on.get(ds, close))
            rows.append({
                "stock_id": sid, "date": ds,
                "open": o, "high": max(o, close), "low": min(o, close),
                "close": close,
                "volume": 1_000_000,
                "trading_money": None, "trading_turnover": None, "spread": None,
            })
        d += _td(days=1)
    db.upsert_daily_prices(rows)


def _seed_quarterly_financials(
    rows: list[tuple[str, str, str, float | None]],
) -> None:
    """rows = [(sid, period, announce_date, eps_yoy), ...]"""
    db.upsert_financials([
        {
            "stock_id": sid, "period_type": "quarterly",
            "period": period, "announce_date": ann,
            "eps_yoy": eps_yoy,
            "revenue": None, "revenue_yoy": None, "eps": None, "roe": None,
        }
        for sid, period, ann, eps_yoy in rows
    ])


# === case 1:無 announce 在窗口 → 0 picks ===


def test_no_announce_in_window(tmp_db):
    """as_of=05/15,但 sid 最近一次 announce_date=04/10(超出 5 交易日窗口),
    且早於 announce_from 緩衝(15 - 17 = 04/28)→ 根本不會被查到。
    """
    sid = "1111"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    # announce 太早(離 as_of > 17 天),根本不會被 SQL 撈到
    _seed_quarterly_financials([(sid, "2025Q4", "2026-04-10", 80.0)])

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-15", stock_ids=[sid],
    )
    assert df.empty


def test_announce_future_no_pick(tmp_db):
    """as_of 在 announce_date 之前 → 還沒到進場時機,不入選。

    (SQL announce BETWEEN announce_from AND date 已過濾未來 announce,這裡只是
     再確認沒有 picks。)
    """
    sid = "1112"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    _seed_quarterly_financials([(sid, "2026Q1", "2026-05-20", 100.0)])

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-15", stock_ids=[sid],
    )
    assert df.empty


# === case 2:eps_yoy > 50 → 入選 ===


def test_eps_yoy_above_threshold_picks(tmp_db):
    """announce=05/08(週五)、as_of=05/12(週二,公佈後第 2 個交易日)、
    eps_yoy=80% > 50% → 入選。gap=0% (open=close)。
    """
    sid = "1113"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-04-01", "2026-05-15", close=100.0)
    _seed_quarterly_financials([(sid, "2026Q1", "2026-05-08", 80.0)])

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-12", stock_ids=[sid],
    )
    assert len(df) == 1
    row = df.iloc[0]
    assert row["stock_id"] == sid
    assert row["announce_date"] == "2026-05-08"
    assert row["eps_yoy"] == pytest.approx(80.0)
    assert row["gap_pct"] == pytest.approx(0.0)  # open=close → gap 0
    # score = max(80/100, 0/5) = 0.8
    assert row["score"] == pytest.approx(0.8)


# === case 3:gap > 3% → 入選(eps_yoy 不足或 None) ===


def test_gap_above_threshold_picks(tmp_db):
    """announce=05/08,public 後第 1 交易日(05/11 週一)open=105(前 close=100)→
    gap=5%。eps_yoy=20%(不足),但 gap > 3% → 入選。
    """
    sid = "1114"
    _seed_stocks([sid])
    _seed_daily_prices(
        sid, "2026-04-01", "2026-05-15", close=100.0,
        gap_open_on={"2026-05-11": 105.0},
    )
    _seed_quarterly_financials([(sid, "2026Q1", "2026-05-08", 20.0)])

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-12", stock_ids=[sid],
    )
    assert len(df) == 1
    row = df.iloc[0]
    assert row["gap_pct"] == pytest.approx(5.0)
    assert row["eps_yoy"] == pytest.approx(20.0)
    # score = max(20/100, 5/5) = max(0.2, 1.0) = 1.0
    assert row["score"] == pytest.approx(1.0)


def test_gap_above_threshold_eps_yoy_none(tmp_db):
    """eps_yoy=None(舊財報沒這欄)但 gap > 3% → 仍入選。"""
    sid = "1115"
    _seed_stocks([sid])
    _seed_daily_prices(
        sid, "2026-04-01", "2026-05-15", close=100.0,
        gap_open_on={"2026-05-11": 110.0},
    )
    _seed_quarterly_financials([(sid, "2026Q1", "2026-05-08", None)])

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-12", stock_ids=[sid],
    )
    assert len(df) == 1
    row = df.iloc[0]
    # eps_yoy 在 DB 是 None,讀進來也應是 None
    assert row["eps_yoy"] is None
    assert row["gap_pct"] == pytest.approx(10.0)
    # score = max(0.0, 10/5)=2.0 → clip 至 2.0
    assert row["score"] == pytest.approx(2.0)


# === case 4:兩條件都不滿足 → 不入選 ===


def test_neither_condition_passes(tmp_db):
    """eps_yoy=20% (< 50) AND gap=1% (< 3%) → 兩個都不過,不入選。"""
    sid = "1116"
    _seed_stocks([sid])
    _seed_daily_prices(
        sid, "2026-04-01", "2026-05-15", close=100.0,
        gap_open_on={"2026-05-11": 101.0},  # 1% gap
    )
    _seed_quarterly_financials([(sid, "2026Q1", "2026-05-08", 20.0)])

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-12", stock_ids=[sid],
    )
    assert df.empty


# === case 5:超出 5 個交易日窗口 → 不入選 ===


def test_outside_window_too_late(tmp_db):
    """announce=05/04(週一),as_of=05/12(週二)= 第 6 個交易日 → 超出 5 個窗口。

    交易日序列:05/05 (1st), 05/06 (2nd), 05/07 (3rd), 05/08 (4th), 05/11 (5th), 05/12 (6th)
    """
    sid = "1117"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-04-01", "2026-05-15", close=100.0)
    _seed_quarterly_financials([(sid, "2026Q1", "2026-05-04", 80.0)])

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-12", stock_ids=[sid],
    )
    assert df.empty


# === case 6:announce 跨週末 → entry_day=下週一 ===


def test_announce_friday_entry_monday(tmp_db):
    """announce=05/08(週五),as_of=05/11(週一,第 1 個交易日)→ 入選。"""
    sid = "1118"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-04-01", "2026-05-15", close=100.0)
    _seed_quarterly_financials([(sid, "2026Q1", "2026-05-08", 80.0)])

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-11", stock_ids=[sid],
    )
    assert len(df) == 1
    assert df.iloc[0]["stock_id"] == sid


# === case 7:score clip 0-2 ===


def test_score_clip_upper_bound(tmp_db):
    """eps_yoy=300% → eps_score=3.0,但 score 被 clip 至 score_clip_max=2.0。"""
    sid = "1119"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-04-01", "2026-05-15", close=100.0)
    _seed_quarterly_financials([(sid, "2026Q1", "2026-05-08", 300.0)])

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-12", stock_ids=[sid],
    )
    assert len(df) == 1
    assert df.iloc[0]["score"] == pytest.approx(2.0)


# === case 8:registry wired ===


def test_pead_in_registry():
    """確認 strategy 已 wire 進 5 個 dict。"""
    key = "earnings_surprise_followthrough"
    assert key in strat.ALL_STRATEGIES
    assert strat.STRATEGY_LABELS[key] == "財報後延續"
    assert strat.STRATEGY_CATEGORY[key] == "基本面"
    target, stop, hold = strat.STRATEGY_RR_PARAMS[key]
    assert target == pytest.approx(0.05)
    assert stop == pytest.approx(0.04)
    assert hold == 5

    # consensus.STRATEGY_NATURE 內也要分到 neutral(事件驅動,跟方向訊號正交)
    from src import consensus
    assert consensus.STRATEGY_NATURE[key] == "neutral"
