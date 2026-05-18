"""src/strategies.py::screen_ex_dividend_swing 單元測試。

覆蓋:
- 未來 N 日內有 ex_date + yield >= 門檻 + fill_rate >= 門檻 → 命中
- yield < 門檻 → 不命中
- fill_rate < 門檻 → 不命中
- 沒歷史(fill_history_n=0) + require_history=True → 不命中
- 沒歷史 + require_history=False → 命中(score 用中性 0.5)
- ex_date 已過(< date) → 不命中
- 候選窗外(ex_date > date + lead_days) → 不命中
- score 計算正確
- enrich_with_targets 補齊欄位(target_low/high/stop_loss/risk_reward/atr14)
"""
from __future__ import annotations

import pytest

from src import strategies as strat


def _seed_stocks(sids: list[str]) -> None:
    from src import database as db
    db.upsert_stocks([
        {"stock_id": s, "name": f"股{s}", "market": "TW"}
        for s in sids
    ])


def _seed_price_window(
    sid: str,
    start: str,
    days: int,
    close: float = 100.0,
    step: float = 0.0,
) -> None:
    """連續灌 N 天 daily_prices,close 從 `close` 起每日 +step。"""
    from datetime import date as _date, timedelta as _td
    from src import database as db
    rows = []
    d = _date.fromisoformat(start)
    for i in range(days):
        c = close + step * i
        rows.append({
            "stock_id": sid,
            "date": d.isoformat(),
            "open": c, "high": c * 1.01, "low": c * 0.99,
            "close": c, "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
        d += _td(days=1)
    db.upsert_daily_prices(rows)


def _seed_dividend(
    sid: str,
    year: int,
    cash_dividend: float,
    ex_dividend_date: str,
) -> None:
    from src import database as db
    db.upsert_dividend([{
        "stock_id": sid, "year": year,
        "cash_dividend": cash_dividend,
        "stock_dividend": 0.0,
        "ex_dividend_date": ex_dividend_date,
    }])


# ---------- happy path: 命中 ----------

def test_ex_dividend_swing_happy_path(tmp_db):
    """yield 5% + 過去 3 次填權 2 次成功(67%) → 命中,score = 5 × 0.667 ≈ 3.33。"""
    sid = "1001"
    _seed_stocks([sid])
    # 過去 3 年的歷史 ex_dates + close 資料,讓 fill_rate 算得出來
    # 2023-07-10 ex,prev close 100,後 5 天 ≥ 100 → 填權成功
    _seed_price_window(sid, "2023-07-07", 14, close=100.0, step=0.5)
    # 2022-07-10 ex,prev close 100,後 5 天皆 < 100 → 沒填
    _seed_price_window(sid, "2022-07-07", 14, close=100.0, step=-0.5)
    # 2021-07-10 ex,後 5 天 ≥ → 填權成功
    _seed_price_window(sid, "2021-07-07", 14, close=100.0, step=0.5)
    # entry close 視窗(date=2024-07-15,候選 ex=2024-07-20)
    _seed_price_window(sid, "2024-07-10", 14, close=100.0, step=0.0)

    # 灌 dividend 表(過去 + 未來)
    _seed_dividend(sid, 2021, cash_dividend=5.0, ex_dividend_date="2021-07-10")
    _seed_dividend(sid, 2022, cash_dividend=5.0, ex_dividend_date="2022-07-10")
    _seed_dividend(sid, 2023, cash_dividend=5.0, ex_dividend_date="2023-07-10")
    _seed_dividend(sid, 2024, cash_dividend=5.0, ex_dividend_date="2024-07-20")

    df = strat.screen_ex_dividend_swing("2024-07-15", stock_ids=[sid])
    assert len(df) == 1
    row = df.iloc[0]
    assert row["stock_id"] == sid
    assert row["ex_dividend_date"] == "2024-07-20"
    # forward yield = 5 / 100 × 100 = 5.0
    assert row["dividend_yield"] == pytest.approx(5.0, abs=0.01)
    # fill_rate = 2/3 ≈ 0.667
    assert row["fill_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert row["fill_history_n"] == 3
    # days_to_ex = 2024-07-20 − 2024-07-15 = 5
    assert row["days_to_ex"] == 5
    # score = 5 × 0.667 = 3.33
    assert row["score"] == pytest.approx(5 * 2 / 3, abs=0.01)


# ---------- yield 過濾 ----------

def test_ex_dividend_swing_yield_below_threshold(tmp_db):
    """forward yield 2% < 預設 3% → 不命中。"""
    sid = "1002"
    _seed_stocks([sid])
    _seed_price_window(sid, "2024-07-10", 14, close=100.0)
    # cash_dividend 2.0 / close 100 = 2% < 3%
    _seed_dividend(sid, 2024, cash_dividend=2.0, ex_dividend_date="2024-07-20")

    df = strat.screen_ex_dividend_swing("2024-07-15", stock_ids=[sid])
    assert df.empty


def test_ex_dividend_swing_yield_above_threshold(tmp_db):
    """yield 邊界 3.5% > 預設 3%:配 require_history=False → 命中(沒歷史走中性 0.5)。"""
    sid = "1003"
    _seed_stocks([sid])
    _seed_price_window(sid, "2024-07-10", 14, close=100.0)
    _seed_dividend(sid, 2024, cash_dividend=3.5, ex_dividend_date="2024-07-20")

    df = strat.screen_ex_dividend_swing("2024-07-15", stock_ids=[sid])
    assert len(df) == 1
    # score = 3.5 × 0.5(中性) = 1.75
    assert df.iloc[0]["score"] == pytest.approx(3.5 * 0.5, abs=0.01)
    assert df.iloc[0]["fill_history_n"] == 0
    assert df.iloc[0]["fill_rate"] is None or (
        df.iloc[0]["fill_rate"] != df.iloc[0]["fill_rate"]  # NaN
    )


# ---------- fill_rate 過濾 ----------

def test_ex_dividend_swing_fill_rate_below_threshold(tmp_db):
    """過去 3 次全沒填(fill_rate=0) < 0.6 → 不命中。"""
    sid = "1004"
    _seed_stocks([sid])
    _seed_price_window(sid, "2023-07-07", 14, close=100.0, step=-0.5)
    _seed_price_window(sid, "2022-07-07", 14, close=100.0, step=-0.5)
    _seed_price_window(sid, "2021-07-07", 14, close=100.0, step=-0.5)
    _seed_price_window(sid, "2024-07-10", 14, close=100.0)

    _seed_dividend(sid, 2021, cash_dividend=5.0, ex_dividend_date="2021-07-10")
    _seed_dividend(sid, 2022, cash_dividend=5.0, ex_dividend_date="2022-07-10")
    _seed_dividend(sid, 2023, cash_dividend=5.0, ex_dividend_date="2023-07-10")
    _seed_dividend(sid, 2024, cash_dividend=5.0, ex_dividend_date="2024-07-20")

    df = strat.screen_ex_dividend_swing("2024-07-15", stock_ids=[sid])
    assert df.empty


# ---------- require_history ----------

def test_ex_dividend_swing_require_history_strict(tmp_db):
    """require_history=True + 沒任何歷史 → 不命中。"""
    sid = "1005"
    _seed_stocks([sid])
    _seed_price_window(sid, "2024-07-10", 14, close=100.0)
    _seed_dividend(sid, 2024, cash_dividend=5.0, ex_dividend_date="2024-07-20")

    df = strat.screen_ex_dividend_swing(
        "2024-07-15",
        stock_ids=[sid],
        params={"require_history": True},
    )
    assert df.empty


def test_ex_dividend_swing_require_history_loose(tmp_db):
    """require_history=False(預設) + 沒歷史 → 命中,score 用中性 0.5。"""
    sid = "1006"
    _seed_stocks([sid])
    _seed_price_window(sid, "2024-07-10", 14, close=100.0)
    _seed_dividend(sid, 2024, cash_dividend=5.0, ex_dividend_date="2024-07-20")

    df = strat.screen_ex_dividend_swing("2024-07-15", stock_ids=[sid])
    assert len(df) == 1
    assert df.iloc[0]["fill_history_n"] == 0
    assert df.iloc[0]["score"] == pytest.approx(5.0 * 0.5, abs=0.01)


# ---------- 候選窗 ----------

def test_ex_dividend_swing_ex_date_already_passed(tmp_db):
    """ex_date < date → 不在候選窗(過去事件)。"""
    sid = "1007"
    _seed_stocks([sid])
    _seed_price_window(sid, "2024-07-10", 14, close=100.0)
    _seed_dividend(sid, 2024, cash_dividend=5.0, ex_dividend_date="2024-07-10")

    df = strat.screen_ex_dividend_swing("2024-07-15", stock_ids=[sid])
    assert df.empty


def test_ex_dividend_swing_ex_date_beyond_lead_window(tmp_db):
    """ex_date > date + lead_days → 太遠,不在候選窗。"""
    sid = "1008"
    _seed_stocks([sid])
    _seed_price_window(sid, "2024-07-10", 14, close=100.0)
    # lead_days 預設 7 → 2024-07-15 + 7 = 2024-07-22。ex_date 2024-08-01 太遠
    _seed_dividend(sid, 2024, cash_dividend=5.0, ex_dividend_date="2024-08-01")

    df = strat.screen_ex_dividend_swing("2024-07-15", stock_ids=[sid])
    assert df.empty


def test_ex_dividend_swing_custom_lead_window(tmp_db):
    """放寬 entry_lead_calendar_days=30 → 抓到 ex_date 兩週後的股。"""
    sid = "1009"
    _seed_stocks([sid])
    _seed_price_window(sid, "2024-07-10", 25, close=100.0)
    _seed_dividend(sid, 2024, cash_dividend=5.0, ex_dividend_date="2024-08-01")

    df = strat.screen_ex_dividend_swing(
        "2024-07-15",
        stock_ids=[sid],
        params={"entry_lead_calendar_days": 30},
    )
    assert len(df) == 1
    assert df.iloc[0]["days_to_ex"] == 17  # 2024-08-01 − 2024-07-15


# ---------- enrich + schema ----------

def test_ex_dividend_swing_enrich_columns_present(tmp_db):
    """命中行需有 close + atr14 + target_* + risk_reward 等 enrich 欄位。"""
    sid = "1010"
    _seed_stocks([sid])
    # 灌足夠 atr 算的天數 (>= 30)
    _seed_price_window(sid, "2024-06-01", 50, close=100.0, step=0.1)
    _seed_dividend(sid, 2024, cash_dividend=5.0, ex_dividend_date="2024-07-20")

    df = strat.screen_ex_dividend_swing("2024-07-15", stock_ids=[sid])
    assert len(df) == 1
    row = df.iloc[0]
    for col in (
        "stock_id", "name", "close", "ex_dividend_date", "cash_dividend",
        "dividend_yield", "fill_rate", "fill_history_n", "days_to_ex",
        "score", "matched_at",
        # enrich
        "atr14", "target_low", "target_high", "stop_loss", "risk_reward",
    ):
        assert col in df.columns, f"missing column: {col}"
    assert row["matched_at"] == "2024-07-15"
    assert row["close"] > 0


def test_ex_dividend_swing_empty_input(tmp_db):
    """空 stock_ids → 空 DataFrame 但 schema 完整。"""
    df = strat.screen_ex_dividend_swing("2024-07-15", stock_ids=[])
    assert df.empty
    for col in (
        "stock_id", "ex_dividend_date", "dividend_yield",
        "fill_rate", "score",
    ):
        assert col in df.columns


def test_ex_dividend_swing_registry_wired(tmp_db):
    """ALL_STRATEGIES / STRATEGY_LABELS / STRATEGY_CATEGORY / STRATEGY_RR_PARAMS 都有 key。"""
    assert "ex_dividend_swing" in strat.ALL_STRATEGIES
    assert strat.STRATEGY_LABELS["ex_dividend_swing"] == "填權息"
    assert strat.STRATEGY_CATEGORY["ex_dividend_swing"] == "殖利率"
    assert "ex_dividend_swing" in strat.STRATEGY_RR_PARAMS
    target_pct, stop_pct, hold = strat.STRATEGY_RR_PARAMS["ex_dividend_swing"]
    # stop_pct 要 >= 殖利率上限(避免 mechanical drop 一進場就觸發停損)
    assert stop_pct >= 0.05, "stop_pct 太緊,mechanical drop 會立刻觸發"
