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
