"""src/strategies.py::screen_revenue_announce_anticipation 單元測試。

涵蓋:
- as_of 不在每月 window(1-4 號)→ 0 picks
- as_of 在窗口 + 所有條件符合 → 1 pick
- 最近一筆 YoY < 30% → 不入選
- 法人 5 日累計 <= 0 → 不入選
- 法人買超天數 < 3 → 不入選
- 60 日均量 < 1000 張 → 不入選
- score 計算正確(yoy_factor * z_factor,單檔池 z=1.0 → z_factor=0.5)
- registry 已 wire
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _td

import pytest

from src import config, database as db
from src import strategies as strat


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "rev_announce.db"
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
    volume: int = 2_000_000,  # 預設 2000 張(> 1000 張門檻)
    close: float = 100.0,
) -> None:
    """灌日 K 線(工作日):volume 用「股」單位(1 張 = 1000 股)。"""
    rows = []
    d = _date.fromisoformat(start)
    end_d = _date.fromisoformat(end)
    while d <= end_d:
        if d.weekday() < 5:
            ds = d.isoformat()
            rows.append({
                "stock_id": sid, "date": ds,
                "open": close, "high": close, "low": close, "close": close,
                "volume": int(volume),
                "trading_money": None, "trading_turnover": None, "spread": None,
            })
        d += _td(days=1)
    db.upsert_daily_prices(rows)


def _seed_revenue_yoy(sid: str, period: str, yoy: float) -> None:
    db.upsert_financials([{
        "stock_id": sid, "period_type": "monthly_revenue",
        "period": period, "revenue_yoy": yoy,
        "revenue": None, "eps": None, "roe": None,
    }])


def _seed_institutional(
    sid: str,
    dates_and_nets: list[tuple[str, int, int]],
) -> None:
    """rows = [(date, foreign_net, trust_net), ...]"""
    db.upsert_institutional([
        {
            "stock_id": sid, "date": d,
            "foreign_buy_sell": f, "trust_buy_sell": t,
            "dealer_buy_sell": 0, "total_buy_sell": f + t,
        }
        for d, f, t in dates_and_nets
    ])


def _last_5_weekdays(as_of: str) -> list[str]:
    """as_of 之前(含)的最近 5 個工作日(週末跳過)。"""
    out: list[str] = []
    d = _date.fromisoformat(as_of)
    while len(out) < 5:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= _td(days=1)
    return out


# === case 1:窗口外(月中)→ 0 picks ===

def test_revenue_announce_outside_window(tmp_db):
    """as_of 落在 5/10(超出 1-4 號窗口)→ 不掃。"""
    sid = "1101"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    _seed_revenue_yoy(sid, "2026-03", 50.0)
    days = _last_5_weekdays("2026-05-10")
    _seed_institutional(sid, [(d, 100_000, 50_000) for d in days])

    df = strat.screen_revenue_announce_anticipation(
        "2026-05-10", stock_ids=[sid]
    )
    assert df.empty


# === case 2:窗口內 + 全條件符合 → 入選 + score 正確 ===

def test_revenue_announce_full_match(tmp_db):
    """as_of 5/04 在窗口、YoY 50%、法人 5d 全買、60d 均量 2000 張 → 入選。

    單檔候選池 → z_factor = 1.0(中性);yoy_factor = min(50/50, 1) = 1.0;
    score = 1.0 × 1.0 = 1.0。
    """
    sid = "1102"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-04", volume=2_000_000)
    _seed_revenue_yoy(sid, "2026-03", 50.0)  # 最近一筆 YoY 50%
    days = _last_5_weekdays("2026-05-04")
    _seed_institutional(sid, [(d, 100_000, 50_000) for d in days])

    df = strat.screen_revenue_announce_anticipation(
        "2026-05-04", stock_ids=[sid]
    )
    assert len(df) == 1, df
    row = df.iloc[0]
    assert row["stock_id"] == sid
    assert row["revenue_yoy"] == pytest.approx(50.0)
    assert row["inst_buy_days"] == 5
    assert row["inst_5d_buy_sum"] == 5 * (100_000 + 50_000)
    assert row["avg_volume_lots"] == pytest.approx(2000.0)
    assert row["score"] == pytest.approx(1.0)


# === case 3:YoY 不足 → 不入選 ===

def test_revenue_announce_yoy_below_threshold(tmp_db):
    """YoY 20% < 30% threshold → 不入選。"""
    sid = "1103"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-04")
    _seed_revenue_yoy(sid, "2026-03", 20.0)
    days = _last_5_weekdays("2026-05-04")
    _seed_institutional(sid, [(d, 100_000, 50_000) for d in days])

    df = strat.screen_revenue_announce_anticipation(
        "2026-05-04", stock_ids=[sid]
    )
    assert df.empty


# === case 4:法人 5d 累計 <= 0 → 不入選 ===

def test_revenue_announce_inst_sum_nonpositive(tmp_db):
    """法人 5 日累計 = 0(進出抵銷)→ 不入選(spec:必須 > 0)。"""
    sid = "1104"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-04")
    _seed_revenue_yoy(sid, "2026-03", 50.0)
    days = _last_5_weekdays("2026-05-04")
    # 3 天買、2 天賣,但賣量大到讓 sum=0
    nets = [
        (days[0], 100_000, 0),
        (days[1], 100_000, 0),
        (days[2], 100_000, 0),
        (days[3], -150_000, 0),
        (days[4], -150_000, 0),
    ]
    _seed_institutional(sid, nets)

    df = strat.screen_revenue_announce_anticipation(
        "2026-05-04", stock_ids=[sid]
    )
    assert df.empty


# === case 5:買超天數不足 → 不入選 ===

def test_revenue_announce_buy_days_insufficient(tmp_db):
    """5 日內只有 2 天 (f+t) > 0,雖然 sum > 0 也不入選(min_buy_days=3)。"""
    sid = "1105"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-04")
    _seed_revenue_yoy(sid, "2026-03", 50.0)
    days = _last_5_weekdays("2026-05-04")
    # 2 天大買、3 天小賣 → sum > 0 但買超天數 = 2
    nets = [
        (days[0], 500_000, 0),
        (days[1], 500_000, 0),
        (days[2], -10_000, 0),
        (days[3], -10_000, 0),
        (days[4], -10_000, 0),
    ]
    _seed_institutional(sid, nets)

    df = strat.screen_revenue_announce_anticipation(
        "2026-05-04", stock_ids=[sid]
    )
    assert df.empty


# === case 6:流動性不足 → 不入選 ===

def test_revenue_announce_low_liquidity(tmp_db):
    """60 日均量 500 張 < 1000 張 → 不入選。"""
    sid = "1106"
    _seed_stocks([sid])
    # volume = 500_000 股 = 500 張
    _seed_daily_prices(sid, "2026-01-01", "2026-05-04", volume=500_000)
    _seed_revenue_yoy(sid, "2026-03", 50.0)
    days = _last_5_weekdays("2026-05-04")
    _seed_institutional(sid, [(d, 100_000, 50_000) for d in days])

    df = strat.screen_revenue_announce_anticipation(
        "2026-05-04", stock_ids=[sid]
    )
    assert df.empty


# === case 7:score 多檔 z-score ===

def test_revenue_announce_score_zscore_multi(tmp_db):
    """2 檔候選池,inst_sum 一大一小 → z-score 算出來大者 z>0、小者 z<0。

    大者:inst_sum=750k(5 天 × 150k),YoY 50% → yoy_factor=1.0
    小者:inst_sum=125k(5 天 × 25k),YoY 50% → yoy_factor=1.0
    mu = (750k + 125k) / 2 = 437.5k, sigma = std(ddof=1)
    大者 z > 0 → z_factor > 0 → score > 0
    小者 z < 0 → max(z, 0) = 0 → z_factor = 0 → score = 0
    但因為 score=0,小者仍會出現在結果中(不是 hard filter),只是排在後面。
    """
    sid_big, sid_small = "1107", "1108"
    _seed_stocks([sid_big, sid_small])
    _seed_daily_prices(sid_big, "2026-01-01", "2026-05-04")
    _seed_daily_prices(sid_small, "2026-01-01", "2026-05-04")
    _seed_revenue_yoy(sid_big, "2026-03", 50.0)
    _seed_revenue_yoy(sid_small, "2026-03", 50.0)
    days = _last_5_weekdays("2026-05-04")
    _seed_institutional(sid_big, [(d, 100_000, 50_000) for d in days])
    _seed_institutional(sid_small, [(d, 20_000, 5_000) for d in days])

    df = strat.screen_revenue_announce_anticipation(
        "2026-05-04", stock_ids=[sid_big, sid_small]
    )
    assert len(df) == 2
    # 大者排第一(score 高)
    assert df.iloc[0]["stock_id"] == sid_big
    assert df.iloc[1]["stock_id"] == sid_small
    assert df.iloc[0]["score"] > df.iloc[1]["score"]
    # 小者 z 為負 → z_factor=0 → score=0
    assert df.iloc[1]["score"] == pytest.approx(0.0)


# === case 8:registry wired ===

def test_revenue_announce_in_registry():
    """確認 strategy 已 wire 進 ALL_STRATEGIES / LABELS / CATEGORY / RR_PARAMS。"""
    key = "revenue_announce_anticipation"
    assert key in strat.ALL_STRATEGIES
    assert strat.STRATEGY_LABELS[key] == "營收公佈前佈局"
    assert strat.STRATEGY_CATEGORY[key] == "基本面"
    target, stop, hold = strat.STRATEGY_RR_PARAMS[key]
    assert target > 0 and stop > 0
    assert hold == 10  # spec:hold 10 個交易日強制出場
