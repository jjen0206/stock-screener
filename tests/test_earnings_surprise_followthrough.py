"""src/strategies.py::screen_earnings_surprise_followthrough 單元測試。

策略:PEAD(Post-Earnings Announcement Drift)— 季報後第 1-5 個交易日,
eps_yoy > 50 OR gap_open_pct > 3 入場。

涵蓋:
- 同日 / 公佈當日(n_after=0)→ 不入選
- announce_date 在窗口內(t+1 ~ t+5) + eps_yoy 路徑命中 → 入選
- 純 gap_open_pct 路徑命中(eps_yoy 不夠 / 缺)→ 入選
- 公佈太久(n_after > 5)→ 不入選
- 兩條件都沒過 → 不入選
- 流動性不足 → 不入選
- announce_date 落假日(週末)→ helper 用 daily_prices 反推不會誤算
- score clip 上限
- helpers _next_trading_day / _trading_days_after 邊界
- registry 已 wire(ALL_STRATEGIES / LABELS / CATEGORY / RR_PARAMS / NATURE)
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _td

import pandas as pd
import pytest

from src import config, database as db
from src import consensus, strategies as strat


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
    *,
    volume: int = 2_000_000,
    close: float = 100.0,
    open_: float | None = None,
    override_open: dict[str, float] | None = None,
) -> None:
    """灌日 K 線(工作日)。

    override_open: {iso_date: open_value} — 對特定日期覆寫 open(算 gap 用)。
    """
    open_default = close if open_ is None else open_
    override_open = override_open or {}
    rows: list[dict] = []
    d = _date.fromisoformat(start)
    end_d = _date.fromisoformat(end)
    while d <= end_d:
        if d.weekday() < 5:
            ds = d.isoformat()
            o = override_open.get(ds, open_default)
            rows.append({
                "stock_id": sid, "date": ds,
                "open": o, "high": max(o, close),
                "low": min(o, close), "close": close,
                "volume": int(volume),
                "trading_money": None, "trading_turnover": None, "spread": None,
            })
        d += _td(days=1)
    db.upsert_daily_prices(rows)


def _seed_quarterly(
    sid: str,
    period: str,
    announce_date: str,
    *,
    eps_yoy: float | None = None,
    eps: float | None = None,
) -> None:
    db.upsert_financials([{
        "stock_id": sid, "period_type": "quarterly",
        "period": period,
        "announce_date": announce_date,
        "eps": eps, "eps_yoy": eps_yoy,
        "revenue": None, "revenue_yoy": None, "roe": None,
    }])


def _trading_day_offset(start: str, offset: int) -> str:
    """從 start (含)往後算第 offset 個工作日(只跳週末,不考慮假日)。"""
    d = _date.fromisoformat(start)
    count = 0
    while True:
        if d.weekday() < 5:
            if count == offset:
                return d.isoformat()
            count += 1
        d += _td(days=1)


# === case 1:announce_date 跟 date 同日(n_after=0)→ 不入選 ===

def test_pead_same_day_announce_not_in_window(tmp_db):
    sid = "1101"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    announce = "2026-05-15"
    _seed_quarterly(sid, "2026-Q1", announce, eps_yoy=80.0)

    df = strat.screen_earnings_surprise_followthrough(
        announce, stock_ids=[sid]
    )
    assert df.empty


# === case 2:eps_yoy 路徑命中(window 內 + eps_yoy > 50)→ 入選 ===

def test_pead_eps_yoy_path_match(tmp_db):
    """announce=2026-05-12,date=2026-05-15 → n_after=3 (Wed→Fri 中間 Thu/Fri)。

    eps_yoy=80% > 50 → 命中;gap=0 (no override) → 不貢獻 score。
    score = max(80/100, 0/5) = 0.80,clip [0, 2] → 0.80。
    """
    sid = "1102"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    announce = "2026-05-12"  # Tue
    _seed_quarterly(sid, "2026-Q1", announce, eps_yoy=80.0)

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-15", stock_ids=[sid]
    )
    assert len(df) == 1
    row = df.iloc[0]
    assert row["stock_id"] == sid
    assert row["announce_date"] == announce
    assert row["eps_yoy"] == pytest.approx(80.0)
    # 5/12 Tue → 5/13(1) 5/14(2) 5/15(3)
    assert row["days_after_announce"] == 3
    assert row["score"] == pytest.approx(0.80)


# === case 3:gap_open_pct 路徑命中(eps_yoy 缺 / 不夠 but gap > 3)→ 入選 ===

def test_pead_gap_path_match(tmp_db):
    """announce=2026-05-11 (Mon),5/12 open 高於 5/11 close 5% → gap=5%。

    eps_yoy=10(< 50)不過 eps thr,但 gap > 3 過 → 命中。
    score = max(10/100, 5/5) = max(0.10, 1.0) = 1.0。
    """
    sid = "1103"
    _seed_stocks([sid])
    # 5/11 close=100,5/12 open=105 → gap=5%
    _seed_daily_prices(
        sid, "2026-01-01", "2026-05-15",
        close=100.0, open_=100.0,
        override_open={"2026-05-12": 105.0},
    )
    announce = "2026-05-11"  # Mon
    _seed_quarterly(sid, "2026-Q1", announce, eps_yoy=10.0)

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-13", stock_ids=[sid]
    )
    assert len(df) == 1
    row = df.iloc[0]
    assert row["gap_open_pct"] == pytest.approx(5.0)
    assert row["eps_yoy"] == pytest.approx(10.0)
    # 5/11 Mon → 5/12(1) 5/13(2)
    assert row["days_after_announce"] == 2
    assert row["score"] == pytest.approx(1.0)


# === case 4:公佈太久(n_after > 5)→ 不入選 ===

def test_pead_too_late_outside_window(tmp_db):
    """announce=2026-05-01,date=2026-05-15 → n_after ≈ 10 個交易日,> 5。"""
    sid = "1104"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    _seed_quarterly(sid, "2026-Q1", "2026-05-01", eps_yoy=80.0)

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-15", stock_ids=[sid]
    )
    assert df.empty


# === case 5:兩條件都沒過(eps_yoy < 50 AND gap < 3)→ 不入選 ===

def test_pead_neither_condition_passes(tmp_db):
    """eps_yoy=20、gap=2% → 兩條 OR 路徑都不過 → 不入選。"""
    sid = "1105"
    _seed_stocks([sid])
    _seed_daily_prices(
        sid, "2026-01-01", "2026-05-15",
        close=100.0, open_=100.0,
        override_open={"2026-05-13": 102.0},  # gap = 2%
    )
    _seed_quarterly(sid, "2026-Q1", "2026-05-12", eps_yoy=20.0)

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-14", stock_ids=[sid]
    )
    assert df.empty


# === case 6:流動性不足 → 不入選 ===

def test_pead_low_liquidity(tmp_db):
    sid = "1106"
    _seed_stocks([sid])
    # 500 張(< 1000 張門檻)
    _seed_daily_prices(
        sid, "2026-01-01", "2026-05-15", volume=500_000,
    )
    _seed_quarterly(sid, "2026-Q1", "2026-05-12", eps_yoy=80.0)

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-14", stock_ids=[sid]
    )
    assert df.empty


# === case 7:announce_date 落週末 → helper 用 daily_prices 反推不會誤算 ===

def test_pead_announce_on_weekend(tmp_db):
    """announce=2026-05-09(週六)— 不是交易日。

    pre_close 應取 5/8 Fri,post_open 應取 5/11 Mon。
    n_after for date=5/12 Tue → 5/11(1) 5/12(2) = 2(在 1-5 窗口內)。
    """
    sid = "1107"
    _seed_stocks([sid])
    _seed_daily_prices(
        sid, "2026-01-01", "2026-05-15",
        close=100.0, open_=100.0,
        override_open={"2026-05-11": 104.0},  # 5/11 Mon open=104 vs 5/8 close=100
    )
    _seed_quarterly(sid, "2026-Q1", "2026-05-09", eps_yoy=10.0)  # Sat

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-12", stock_ids=[sid]
    )
    assert len(df) == 1
    row = df.iloc[0]
    assert row["announce_date"] == "2026-05-09"
    assert row["gap_open_pct"] == pytest.approx(4.0)
    assert row["days_after_announce"] == 2


# === case 8:score clip 上限(極端 eps_yoy 不會跑掉)===

def test_pead_score_clipped_at_max(tmp_db):
    """eps_yoy=500% → eps_score=5.0,經 clip 限在 2.0(score_clip_max)。"""
    sid = "1108"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    _seed_quarterly(sid, "2026-Q1", "2026-05-12", eps_yoy=500.0)

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-14", stock_ids=[sid]
    )
    assert len(df) == 1
    assert df.iloc[0]["score"] == pytest.approx(2.0)


# === case 9:announce_date NULL → 該股不入選(P2-4a backfill 未完保護)===

def test_pead_announce_date_null_skipped(tmp_db):
    """財報有 row 但 announce_date 為 NULL(舊資料 / backfill 未完)→ skip。"""
    sid = "1109"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    # eps_yoy=80 但 announce_date=None
    db.upsert_financials([{
        "stock_id": sid, "period_type": "quarterly",
        "period": "2026-Q1",
        "announce_date": None,
        "eps": 1.0, "eps_yoy": 80.0,
        "revenue": None, "revenue_yoy": None, "roe": None,
    }])

    df = strat.screen_earnings_surprise_followthrough(
        "2026-05-14", stock_ids=[sid]
    )
    assert df.empty


# === case 10:helper _trading_days_after / _next_trading_day 邊界 ===

def test_helper_next_trading_day():
    df = pd.DataFrame([
        {"date": "2026-05-08", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"date": "2026-05-11", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"date": "2026-05-12", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ])
    # 落週末:5/9 / 5/10 → 下一交易日 5/11
    assert strat._next_trading_day(df, "2026-05-09") == "2026-05-11"
    assert strat._next_trading_day(df, "2026-05-10") == "2026-05-11"
    # 落交易日:5/11 → 下一個 5/12
    assert strat._next_trading_day(df, "2026-05-11") == "2026-05-12"
    # 沒有後續 → None
    assert strat._next_trading_day(df, "2026-05-12") is None
    # None / empty
    assert strat._next_trading_day(None, "2026-05-10") is None
    assert strat._next_trading_day(pd.DataFrame({"date": []}), "2026-05-10") is None


def test_helper_trading_days_after():
    df = pd.DataFrame([
        {"date": "2026-05-08", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"date": "2026-05-11", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"date": "2026-05-12", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"date": "2026-05-13", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ])
    # 5/8 Fri → 5/13 Wed:含 5/11(1) 5/12(2) 5/13(3)
    assert strat._trading_days_after(df, "2026-05-08", "2026-05-13") == 3
    # 5/11 → 5/12:1 個
    assert strat._trading_days_after(df, "2026-05-11", "2026-05-12") == 1
    # 同日 → None
    assert strat._trading_days_after(df, "2026-05-12", "2026-05-12") is None
    # target < ref → None
    assert strat._trading_days_after(df, "2026-05-13", "2026-05-12") is None
    # None / empty
    assert strat._trading_days_after(None, "2026-05-11", "2026-05-13") is None


# === case 11:registry wired(5 dict + NATURE)===

def test_pead_in_registry():
    key = "earnings_surprise_followthrough"
    assert key in strat.ALL_STRATEGIES
    assert strat.STRATEGY_LABELS[key] == "財報後延續"
    assert strat.STRATEGY_CATEGORY[key] == "基本面"
    target, stop, hold = strat.STRATEGY_RR_PARAMS[key]
    assert target == pytest.approx(0.05)
    assert stop == pytest.approx(0.04)
    assert hold == 5
    assert consensus.STRATEGY_NATURE[key] == "neutral"
