"""src/strategies.py::screen_ex_dividend_swing 單元測試。

涵蓋:
- 殖利率 < 3% → 不入選
- 過去 3 年填權成功率 < 60% → 不入選(2/3 = 0.66 過、1/3 = 0.33 不過)
- 沒有上 1-3 日內 ex_date → 不入選
- 歷史不足(< 3 年)→ skip
- 殖利率符合 + 填權率符合 + revenue_yoy > 0 → 入選 + score 含 1.2x boost
- score 計算正確 = (yield_pct / 5.0) × fill_success_rate × boost
- _compute_fill_success 邊界:無 before / 無 after → None
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _td

import pytest

from src import config, database as db
from src import strategies as strat


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "ex_div.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def _seed_stocks(sids: list[str]) -> None:
    db.upsert_stocks([
        {"stock_id": s, "name": f"股{s}", "market": "TW"}
        for s in sids
    ])


def _seed_daily_price_series(
    sid: str,
    start: str,
    end: str,
    close_fn=None,
) -> None:
    """灌日 K 線:start..end 每個工作日塞一筆;close_fn(date_str) -> float 自訂。"""
    if close_fn is None:
        def close_fn(_d):
            return 100.0
    rows = []
    d = _date.fromisoformat(start)
    end_d = _date.fromisoformat(end)
    while d <= end_d:
        if d.weekday() < 5:  # 跳過週末(snapshot daily_prices 跟現實一致)
            ds = d.isoformat()
            c = float(close_fn(ds))
            rows.append({
                "stock_id": sid, "date": ds,
                "open": c, "high": c, "low": c, "close": c,
                "volume": 1000,
                "trading_money": None, "trading_turnover": None, "spread": None,
            })
        d += _td(days=1)
    db.upsert_daily_prices(rows)


def _seed_dividends(rows: list[tuple[str, int, float, str]]) -> None:
    """rows = [(sid, year, cash_dividend, ex_dividend_date), ...]"""
    db.upsert_dividend([
        {
            "stock_id": sid, "year": year,
            "cash_dividend": cash, "stock_dividend": 0.0,
            "ex_dividend_date": ex_date,
        }
        for sid, year, cash, ex_date in rows
    ])


def _seed_revenue_yoy(rows: list[tuple[str, str, float]]) -> None:
    """rows = [(sid, period, revenue_yoy), ...] — period_type='monthly_revenue'。"""
    db.upsert_financials([
        {
            "stock_id": sid, "period_type": "monthly_revenue",
            "period": period, "revenue_yoy": yoy,
            "revenue": None, "eps": None, "roe": None,
        }
        for sid, period, yoy in rows
    ])


# === case 1:符合所有條件 → 入選 + score 正確 ===

def test_ex_div_swing_full_match(tmp_db):
    """殖利率 4% + 3 年都填權 + YoY > 0 → 入選,score = (4/5) × 1.0 × 1.2 = 0.96。"""
    sid = "1234"
    _seed_stocks([sid])
    # close = 100 → 4 元 cash 殖利率 4%
    _seed_daily_price_series(sid, "2022-01-01", "2026-05-15")
    _seed_dividends([
        (sid, 2023, 4.0, "2023-07-10"),
        (sid, 2024, 4.0, "2024-07-10"),
        (sid, 2025, 4.0, "2025-07-10"),
        (sid, 2026, 4.0, "2026-05-20"),  # 上 3 天內就要除權
    ])
    _seed_revenue_yoy([(sid, "2026-04", 10.0)])

    df = strat.screen_ex_dividend_swing("2026-05-18", stock_ids=[sid])
    assert len(df) == 1, df
    row = df.iloc[0]
    assert row["stock_id"] == sid
    assert row["next_ex_date"] == "2026-05-20"
    # close 全程 100 → 過去 3 年填權 100%(close 已等於 pre_close)
    assert row["fill_success_rate"] == pytest.approx(1.0)
    assert row["yield_pct"] == pytest.approx(4.0)
    # score = (4/5) × 1.0 × 1.2 = 0.96
    assert row["score"] == pytest.approx(0.96)


# === case 2:殖利率不足 → 不入選 ===

def test_ex_div_swing_yield_below_threshold(tmp_db):
    """殖利率 2% < 3% → 不入選。"""
    sid = "2345"
    _seed_stocks([sid])
    _seed_daily_price_series(sid, "2022-01-01", "2026-05-15")
    _seed_dividends([
        (sid, 2023, 2.0, "2023-07-10"),
        (sid, 2024, 2.0, "2024-07-10"),
        (sid, 2025, 2.0, "2025-07-10"),
        (sid, 2026, 2.0, "2026-05-20"),
    ])
    df = strat.screen_ex_dividend_swing("2026-05-18", stock_ids=[sid])
    assert df.empty


# === case 3:填權成功率不足 → 不入選 ===

def test_ex_div_swing_fill_rate_below_threshold(tmp_db):
    """3 次只有 1 次填權(33%)< 60% → 不入選。"""
    sid = "3456"
    _seed_stocks([sid])

    # 3 個過去 ex_date:7-10。其中 2 個 ex_date 後 20 天的 close 一路掉,不填權
    failed_ex_dates = {"2023-07-10", "2024-07-10"}

    def close_fn(d: str) -> float:
        # 在 failed ex_date 後 20 個交易日內 close 拉低,讓無法填權
        for ex in failed_ex_dates:
            ex_d = _date.fromisoformat(ex)
            d_d = _date.fromisoformat(d)
            if 0 <= (d_d - ex_d).days <= 30:
                return 80.0  # 一路低於 pre-ex 100
        return 100.0

    _seed_daily_price_series(sid, "2022-01-01", "2026-05-15", close_fn=close_fn)
    _seed_dividends([
        (sid, 2023, 5.0, "2023-07-10"),
        (sid, 2024, 5.0, "2024-07-10"),
        (sid, 2025, 5.0, "2025-07-10"),
        (sid, 2026, 5.0, "2026-05-20"),
    ])
    df = strat.screen_ex_dividend_swing("2026-05-18", stock_ids=[sid])
    assert df.empty


# === case 4:沒有未來 lookahead 內的 ex_date → 不入選 ===

def test_ex_div_swing_no_upcoming_ex_date(tmp_db):
    """上 3 天內沒 ex_date → 即使其他條件符合也不入選。"""
    sid = "4567"
    _seed_stocks([sid])
    _seed_daily_price_series(sid, "2022-01-01", "2026-05-15")
    _seed_dividends([
        (sid, 2023, 4.0, "2023-07-10"),
        (sid, 2024, 4.0, "2024-07-10"),
        (sid, 2025, 4.0, "2025-07-10"),
        # 下次 ex_date 已過(或還沒到),不在 2026-05-18 + 3 天的窗口內
    ])
    df = strat.screen_ex_dividend_swing("2026-05-18", stock_ids=[sid])
    assert df.empty


# === case 5:歷史不足 3 年 → 不入選 ===

def test_ex_div_swing_insufficient_history(tmp_db):
    """只有 1 個過去 ex_date(歷史不足 lookback_years=3)→ skip。"""
    sid = "5678"
    _seed_stocks([sid])
    _seed_daily_price_series(sid, "2024-01-01", "2026-05-15")
    _seed_dividends([
        (sid, 2025, 4.0, "2025-07-10"),       # 只有 1 個過去
        (sid, 2026, 4.0, "2026-05-20"),       # 上來這個是當前要進場的
    ])
    df = strat.screen_ex_dividend_swing("2026-05-18", stock_ids=[sid])
    assert df.empty


# === case 6:YoY 為負 / None → 無 boost ===

def test_ex_div_swing_no_yoy_boost(tmp_db):
    """YoY <= 0 或缺資料 → score 不加 1.2 boost。"""
    sid = "6789"
    _seed_stocks([sid])
    _seed_daily_price_series(sid, "2022-01-01", "2026-05-15")
    _seed_dividends([
        (sid, 2023, 5.0, "2023-07-10"),
        (sid, 2024, 5.0, "2024-07-10"),
        (sid, 2025, 5.0, "2025-07-10"),
        (sid, 2026, 5.0, "2026-05-20"),
    ])
    _seed_revenue_yoy([(sid, "2026-04", -5.0)])  # YoY 為負

    df = strat.screen_ex_dividend_swing("2026-05-18", stock_ids=[sid])
    assert len(df) == 1
    row = df.iloc[0]
    # score = (5/5) × 1.0 × 1.0(無 boost)= 1.0
    assert row["score"] == pytest.approx(1.0)
    assert row["revenue_yoy"] == pytest.approx(-5.0)


# === case 7:純股票股利(無現金)→ 不入選(算不出殖利率)===

def test_ex_div_swing_stock_dividend_only(tmp_db):
    """cash_dividend = 0 → 殖利率無意義,直接 skip。"""
    sid = "7890"
    _seed_stocks([sid])
    _seed_daily_price_series(sid, "2022-01-01", "2026-05-15")
    _seed_dividends([
        (sid, 2023, 4.0, "2023-07-10"),
        (sid, 2024, 4.0, "2024-07-10"),
        (sid, 2025, 4.0, "2025-07-10"),
        (sid, 2026, 0.0, "2026-05-20"),  # 無現金
    ])
    df = strat.screen_ex_dividend_swing("2026-05-18", stock_ids=[sid])
    assert df.empty


# === case 8:_compute_fill_success 邊界 ===

def test_compute_fill_success_edge_cases():
    """單獨測 helper 的 None / True / False 三種回值。"""
    import pandas as pd

    # 空 df → None
    assert strat._compute_fill_success(
        pd.DataFrame(columns=["date", "close"]), "2025-07-10", 20
    ) is None

    # 全部都在 ex_date 前 → after 為空 → None
    df = pd.DataFrame([
        {"date": "2025-07-08", "close": 100.0},
        {"date": "2025-07-09", "close": 101.0},
    ])
    assert strat._compute_fill_success(df, "2025-07-10", 20) is None

    # 有 before 沒 after(ex_date 在最後一筆之後)→ None
    df = pd.DataFrame([
        {"date": "2025-07-08", "close": 100.0},
    ])
    assert strat._compute_fill_success(df, "2025-07-09", 20) is None

    # 填權成功:after max >= pre_close
    df = pd.DataFrame([
        {"date": "2025-07-09", "close": 100.0},  # pre-ex
        {"date": "2025-07-10", "close": 95.0},
        {"date": "2025-07-11", "close": 98.0},
        {"date": "2025-07-15", "close": 102.0},  # 填回 + 超過
    ])
    assert strat._compute_fill_success(df, "2025-07-10", 20) is True

    # 填權失敗:after max < pre_close
    df = pd.DataFrame([
        {"date": "2025-07-09", "close": 100.0},
        {"date": "2025-07-10", "close": 95.0},
        {"date": "2025-07-11", "close": 96.0},
        {"date": "2025-07-15", "close": 97.0},
    ])
    assert strat._compute_fill_success(df, "2025-07-10", 20) is False


# === case 9:registry wired ===

def test_ex_dividend_swing_in_registry():
    """確認 strategy 已 wire 進 ALL_STRATEGIES / LABELS / CATEGORY / RR_PARAMS。"""
    assert "ex_dividend_swing" in strat.ALL_STRATEGIES
    assert "ex_dividend_swing" in strat.STRATEGY_LABELS
    assert strat.STRATEGY_CATEGORY["ex_dividend_swing"] == "殖利率"
    target, stop, hold = strat.STRATEGY_RR_PARAMS["ex_dividend_swing"]
    assert target > 0 and stop > 0 and hold == 15  # spec: 強制 15 日出場
