"""src/intraday.py 單元測試。

is_market_hours / format_intraday_line 純邏輯,不打網路。
get_intraday_quote 用 mock yfinance.Ticker 驗 dict shape。
"""
from __future__ import annotations

from datetime import datetime, time as _time
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from src import intraday as intra


_TPE = ZoneInfo("Asia/Taipei")


# === is_market_hours ===

def test_is_market_hours_weekday_open():
    """週一上午 10:00 台北 → 盤中。"""
    # 2026-05-04 (Mon) 10:00 台北
    now = datetime(2026, 5, 4, 10, 0, tzinfo=_TPE)
    assert intra.is_market_hours(now) is True


def test_is_market_hours_weekday_before_open():
    """週一 08:30 → 盤前(False)。"""
    now = datetime(2026, 5, 4, 8, 30, tzinfo=_TPE)
    assert intra.is_market_hours(now) is False


def test_is_market_hours_weekday_after_close():
    """週一 14:00 → 盤後(False;13:35 之後)。"""
    now = datetime(2026, 5, 4, 14, 0, tzinfo=_TPE)
    assert intra.is_market_hours(now) is False


def test_is_market_hours_saturday():
    """週六 任何時間 → 非交易日(False)。"""
    now = datetime(2026, 5, 9, 10, 0, tzinfo=_TPE)  # Saturday
    assert intra.is_market_hours(now) is False


def test_is_market_hours_naive_datetime_treated_as_taipei():
    """naive datetime 視為 Asia/Taipei → 跟 tz-aware 同結果。"""
    naive = datetime(2026, 5, 4, 10, 0)  # 沒 tz
    assert intra.is_market_hours(naive) is True


# === format_intraday_line ===

def test_format_intraday_line_up():
    quote = {"current": 600.0, "change_pct": 1.5, "prev_close": 591.13}
    line = intra.format_intraday_line(quote)
    assert "📡 600.00" in line
    assert "↑1.5%" in line


def test_format_intraday_line_down():
    quote = {"current": 580.0, "change_pct": -1.88, "prev_close": 591.0}
    line = intra.format_intraday_line(quote)
    assert "↓1.9%" in line


def test_format_intraday_line_flat():
    quote = {"current": 100.0, "change_pct": 0.0, "prev_close": 100.0}
    line = intra.format_intraday_line(quote)
    assert "→0.0%" in line


def test_format_intraday_line_none_returns_empty():
    assert intra.format_intraday_line(None) == ""
    assert intra.format_intraday_line({"current": None}) == ""


# === get_intraday_quote (mock yfinance) ===

def test_get_intraday_quote_parses_yfinance_info():
    """yfinance Ticker.info 該 normalise 成統一 dict shape。"""
    fake_info = {
        "regularMarketPrice": 605.0,
        "regularMarketPreviousClose": 600.0,
        "regularMarketVolume": 12345678,
        "regularMarketTime": 1735000000,
    }
    fake_ticker = MagicMock()
    fake_ticker.info = fake_info
    with patch("yfinance.Ticker", return_value=fake_ticker):
        out = intra.get_intraday_quote(["2330"])
    assert out["2330"]["current"] == 605.0
    assert out["2330"]["prev_close"] == 600.0
    assert out["2330"]["change_pct"] == pytest_approx(5.0 / 600.0 * 100)
    assert out["2330"]["volume"] == 12345678


def test_get_intraday_quote_returns_none_when_yfinance_fails():
    """yfinance Ticker.info 失敗(回 {} 或 raise)→ 該 sid 該為 None。"""
    fake_ticker = MagicMock()
    fake_ticker.info = {}  # 沒 price 欄位
    with patch("yfinance.Ticker", return_value=fake_ticker):
        out = intra.get_intraday_quote(["BAD"])
    assert out["BAD"] is None


def test_get_intraday_quote_handles_yfinance_exception():
    """yfinance.Ticker() raise → 該 sid 該為 None,不影響其他 sids。"""
    def fake_ticker_factory(sym):
        if sym == "BAD.TW":
            raise RuntimeError("rate limit")
        m = MagicMock()
        m.info = {
            "regularMarketPrice": 100.0,
            "regularMarketPreviousClose": 99.0,
        }
        return m
    with patch("yfinance.Ticker", side_effect=fake_ticker_factory):
        out = intra.get_intraday_quote(["GOOD", "BAD"])
    assert out["GOOD"]["current"] == 100.0
    assert out["BAD"] is None


# helper: pytest.approx 不 import 一行
def pytest_approx(value):
    import pytest as _p
    return _p.approx(value)
