"""src/market_sentiment.py 單元測試。

策略:mock _api_call / fetch_daily_price / yfinance,確保不打網路。
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from src import market_sentiment as ms


@pytest.fixture(autouse=True)
def reset_cache():
    ms._reset_cache()
    yield
    ms._reset_cache()


def test_fetch_taiex_returns_df():
    fake = pd.DataFrame([
        {"date": "2026-04-25", "close": 17500.5, "stock_id": "TAIEX"},
        {"date": "2026-04-26", "close": 17600.3, "stock_id": "TAIEX"},
    ])
    with patch("src.market_sentiment.fetch_daily_price", return_value=fake):
        df = ms.fetch_taiex(days=30)
    assert len(df) == 2
    assert df["close"].iloc[-1] == pytest.approx(17600.3)


def test_fetch_taiex_returns_empty_on_failure():
    with patch(
        "src.market_sentiment.fetch_daily_price",
        side_effect=RuntimeError("boom"),
    ):
        df = ms.fetch_taiex(days=30)
    assert df.empty


def test_fetch_institutional_total_calls_correct_dataset():
    fake = [
        {"date": "2026-04-25", "name": "Foreign", "buy": 1e9, "sell": 5e8},
        {"date": "2026-04-25", "name": "Trust", "buy": 5e8, "sell": 3e8},
    ]
    with patch("src.market_sentiment._api_call", return_value=fake) as m:
        df = ms.fetch_institutional_total(days=30)
    assert m.call_count == 1
    args, kwargs = m.call_args
    assert args[0] == "TaiwanStockTotalInstitutionalInvestors"
    assert "start_date" in kwargs
    assert len(df) == 2


def test_fetch_institutional_total_failure_returns_empty():
    from src.data_fetcher import FinMindAPIError
    with patch(
        "src.market_sentiment._api_call",
        side_effect=FinMindAPIError("status=402"),
    ):
        df = ms.fetch_institutional_total(days=30)
    assert df.empty


def test_fetch_margin_balance_calls_correct_dataset():
    fake = [{"date": "2026-04-25", "MarginPurchaseTodayBalance": 1.5e11}]
    with patch("src.market_sentiment._api_call", return_value=fake) as m:
        df = ms.fetch_margin_balance(days=30)
    assert m.call_count == 1
    assert m.call_args.args[0] == "TaiwanStockTotalMarginPurchaseShortSale"
    assert len(df) == 1


def test_fetch_vix_uses_yfinance():
    """VIX 用 yfinance,mock Ticker.history。"""
    fake = pd.DataFrame({"Close": [18.5, 19.2, 17.8]})
    fake.index.name = "Date"

    class FakeTicker:
        def __init__(self, sym):
            self.sym = sym
        def history(self, period):
            return fake

    with patch("yfinance.Ticker", FakeTicker):
        df = ms.fetch_vix(days=30)
    assert not df.empty
    assert "Close" in df.columns
    assert df["Close"].iloc[-1] == pytest.approx(17.8)


def test_fetch_vix_failure_returns_empty():
    """yfinance 失敗 → 回空 DF。"""
    with patch("yfinance.Ticker", side_effect=RuntimeError("yf boom")):
        df = ms.fetch_vix(days=30)
    assert df.empty


def test_cache_avoids_double_fetch():
    """60 秒內第二次呼叫該走 cache。"""
    fake = pd.DataFrame([{"date": "2026-04-25", "close": 100, "stock_id": "TAIEX"}])
    with patch(
        "src.market_sentiment.fetch_daily_price", return_value=fake,
    ) as m:
        ms.fetch_taiex(days=30)
        ms.fetch_taiex(days=30)
    assert m.call_count == 1


# === compute_total_net_per_day(三大法人合計修法) ===

def test_compute_total_net_uses_total_row_only():
    """raw 一日 6 筆(5 法人 + 1 'total'),該只取 'total' 那筆,
    避免 sum 全 6 筆 = 2× 真值的舊 bug。"""
    raw = pd.DataFrame([
        # 2026-04-20 一日 6 筆
        {"date": "2026-04-20", "name": "Dealer_self",
         "buy": 6_000_000_000, "sell": 9_000_000_000},
        {"date": "2026-04-20", "name": "Foreign_Dealer_Self",
         "buy": 0, "sell": 0},
        {"date": "2026-04-20", "name": "Dealer_Hedging",
         "buy": 25_000_000_000, "sell": 35_000_000_000},
        {"date": "2026-04-20", "name": "Investment_Trust",
         "buy": 10_000_000_000, "sell": 7_000_000_000},
        {"date": "2026-04-20", "name": "Foreign_Investor",
         "buy": 227_000_000_000, "sell": 291_000_000_000},
        {"date": "2026-04-20", "name": "total",
         "buy": 268_000_000_000, "sell": 342_000_000_000},
    ])
    out = ms.compute_total_net_per_day(raw)
    assert len(out) == 1
    # total: (268e9 - 342e9) / 1e8 = -740 億
    assert out["net"].iloc[0] == pytest.approx(-740.0, abs=0.5)


def test_compute_total_net_fallback_when_no_total_row():
    """FinMind 漏給 'total' → fallback SUM 5 個分項法人。"""
    raw = pd.DataFrame([
        {"date": "2026-04-20", "name": "Foreign_Investor",
         "buy": 200_000_000_000, "sell": 250_000_000_000},
        {"date": "2026-04-20", "name": "Investment_Trust",
         "buy": 10_000_000_000, "sell": 5_000_000_000},
    ])
    out = ms.compute_total_net_per_day(raw)
    assert len(out) == 1
    # SUM:(210e9 - 255e9) / 1e8 = -450 億
    assert out["net"].iloc[0] == pytest.approx(-450.0, abs=0.5)


def test_compute_total_net_empty_input():
    out = ms.compute_total_net_per_day(pd.DataFrame())
    assert out.empty
    assert list(out.columns) == ["date", "net"]


def test_compute_total_net_multiple_days_sorted():
    """多天該按日期排序回傳。"""
    raw = pd.DataFrame([
        {"date": "2026-04-22", "name": "total",
         "buy": 100_000_000_000, "sell": 80_000_000_000},
        {"date": "2026-04-20", "name": "total",
         "buy": 268_000_000_000, "sell": 342_000_000_000},
        {"date": "2026-04-21", "name": "total",
         "buy": 150_000_000_000, "sell": 130_000_000_000},
    ])
    out = ms.compute_total_net_per_day(raw)
    assert list(out["date"]) == ["2026-04-20", "2026-04-21", "2026-04-22"]
    # 第 1 天賣超、第 2 天買超 200 億、第 3 天買超 200 億
    assert out["net"].iloc[0] == pytest.approx(-740.0, abs=0.5)
    assert out["net"].iloc[1] == pytest.approx(200.0, abs=0.5)
