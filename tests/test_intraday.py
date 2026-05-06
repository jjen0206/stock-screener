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


# === 守門 tests:確保 intraday 注入不覆蓋 close / change_pct(主公報的 bug) ===

def test_inject_intraday_does_not_overwrite_row_close():
    """Guard:_inject_intraday_quotes 該只塞 row['intraday_quote'],
    不該動 row['close'] 或 row['change_pct']。回歸測試。
    """
    import sys
    sys.modules.pop("app", None)
    import app as _app
    from unittest.mock import patch

    rows = [
        {"stock_id": "2484", "close": 42.55, "change_pct": -1.62},
        {"stock_id": "2330", "close": 600.0, "change_pct": 0.5},
    ]
    fake_quotes = {
        "2484": {"current": 39.55, "change_pct": -7.05,
                 "prev_close": 42.55, "volume": 1000, "timestamp": 1},
        "2330": {"current": 595.0, "change_pct": -0.83,
                 "prev_close": 600.0, "volume": 2000, "timestamp": 2},
    }

    # 讓 is_market_hours 永遠 True 跟 _get_intraday_cached mock 回 fake_quotes
    with patch("src.intraday.is_market_hours", return_value=True), \
         patch.object(_app, "_get_intraday_cached", return_value=fake_quotes):
        out = _app._inject_intraday_quotes(rows, ["2484", "2330"])

    assert out is rows  # in-place mutation
    # 關鍵守門:close / change_pct 仍是 daily 值,沒被 intraday 蓋
    assert out[0]["close"] == 42.55, "row[0] close 不該被 intraday 覆蓋"
    assert out[0]["change_pct"] == -1.62, "row[0] change_pct 不該被 intraday 覆蓋"
    assert out[1]["close"] == 600.0
    assert out[1]["change_pct"] == 0.5
    # intraday_quote 該注入到獨立 key
    assert out[0]["intraday_quote"]["current"] == 39.55
    assert out[1]["intraday_quote"]["current"] == 595.0


def test_inject_intraday_skip_outside_market_hours():
    """非交易時段 → 不注入 intraday_quote(節省 yfinance quota)。"""
    import sys
    sys.modules.pop("app", None)
    import app as _app
    from unittest.mock import patch

    rows = [{"stock_id": "2484", "close": 42.55, "change_pct": -1.62}]

    with patch("src.intraday.is_market_hours", return_value=False):
        out = _app._inject_intraday_quotes(rows, ["2484"])

    assert "intraday_quote" not in out[0], "非交易時段不該注入"
    assert out[0]["close"] == 42.55  # 仍不該動


def test_render_pick_card_keeps_close_and_adds_intraday_line():
    """Guard:render_pick_card 同時 render 昨收(in card HTML)和 📡 盤中行(獨立)。

    主公曾報「股價欄被改成盤中價」— 這個 test 鎖定 close 跟 intraday 兩數字
    都該出現在 markdown call 內,不互相覆蓋。
    """
    from unittest.mock import patch, MagicMock
    from src import ui_cards

    # 抓所有 st.markdown 呼叫的內容
    captured: list[str] = []

    fake_st = MagicMock()
    fake_st.markdown = lambda content, **kwargs: captured.append(str(content))
    fake_st.button = lambda *a, **kw: False
    fake_st.caption = lambda *a, **kw: None
    fake_st.session_state = {}
    fake_container = MagicMock()
    fake_container.__enter__ = MagicMock(return_value=fake_container)
    fake_container.__exit__ = MagicMock(return_value=False)
    fake_st.container = MagicMock(return_value=fake_container)

    fake_col = MagicMock()
    fake_col.__enter__ = MagicMock(return_value=fake_col)
    fake_col.__exit__ = MagicMock(return_value=False)
    fake_st.columns = MagicMock(return_value=[fake_col, fake_col, fake_col])

    row = {
        "stock_id": "2484",
        "name": "希華",
        "close": 42.55,
        "change_pct": -1.62,
        "intraday_quote": {"current": 39.55, "change_pct": -7.05,
                           "prev_close": 42.55, "volume": 1000, "timestamp": 1},
    }

    with patch.object(ui_cards, "st", fake_st):
        ui_cards.render_pick_card(
            row, show_change=True, show_targets=False, show_signal=False,
        )

    all_md = "\n".join(captured)
    # 關鍵守門:42.55(昨收)跟 39.55(盤中)該同時出現
    assert "42.55" in all_md, f"昨收 42.55 該在 card HTML 裡,實際: {all_md}"
    assert "39.55" in all_md, f"盤中 39.55 該在 📡 line 裡,實際: {all_md}"
    assert "📡" in all_md, "📡 emoji 該在 intraday 行"
