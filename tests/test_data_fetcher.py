"""src/data_fetcher.py 單元測試。

策略:
- mock 掉 _api_call,讓測試不打真網路。
- 用 tmp_path 的 SQLite,避免污染 data/cache.db。
- 重點驗收:
    1. 第一次 fetch_xxx → _api_call 被呼叫
    2. 第二次同樣參數 → _api_call 不再被呼叫(快取命中)
    3. 擴展時間區間 → 只補頭/尾差額
    4. 三大法人 pivot 邏輯正確
    5. 季財報遇到 FinMindAPIError 會降級回空 DataFrame
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from src import config, data_fetcher as fetcher, database as db
from src.data_fetcher import FinMindAPIError


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """獨立 DB,自動初始化。"""
    db_file = tmp_path / "fetcher.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def _fake_price_rows(stock_id: str, dates: list[str]) -> list[dict]:
    """模擬 FinMind TaiwanStockPrice 回傳格式。"""
    return [
        {
            "date": d,
            "stock_id": stock_id,
            "Trading_Volume": 26841832,
            "Trading_money": 15808906000,
            "open": 593.0,
            "max": 595.0,
            "min": 587.0,
            "close": 593.0 + i,
            "spread": 4.0,
            "Trading_turnover": 30821,
        }
        for i, d in enumerate(dates)
    ]


# === fetch_daily_price: 快取核心測試 ===

def test_fetch_daily_price_first_call_hits_api(tmp_db):
    fake = _fake_price_rows("2330", ["2024-01-02", "2024-01-03"])
    with patch.object(fetcher, "_api_call", return_value=fake) as m:
        df = fetcher.fetch_daily_price("2330", "2024-01-01", "2024-01-05")
    assert m.call_count == 1
    assert len(df) == 2
    assert set(df["date"]) == {"2024-01-02", "2024-01-03"}


def test_fetch_daily_price_second_call_uses_cache(tmp_db):
    """同樣的請求第二次應該完全不打 API。"""
    fake = _fake_price_rows("2330", ["2024-01-02", "2024-01-03"])
    with patch.object(fetcher, "_api_call", return_value=fake) as m:
        fetcher.fetch_daily_price("2330", "2024-01-01", "2024-01-05")
        assert m.call_count == 1
        df2 = fetcher.fetch_daily_price("2330", "2024-01-01", "2024-01-05")
    assert m.call_count == 1, "第二次相同請求不該再打 API"
    assert len(df2) == 2


def test_fetch_daily_price_subset_uses_cache(tmp_db):
    """請求縮窄到已快取區間內,也不該打 API。"""
    fake = _fake_price_rows("2330", ["2024-01-02", "2024-01-03", "2024-01-04"])
    with patch.object(fetcher, "_api_call", return_value=fake) as m:
        fetcher.fetch_daily_price("2330", "2024-01-01", "2024-01-10")
        assert m.call_count == 1
        df2 = fetcher.fetch_daily_price("2330", "2024-01-02", "2024-01-04")
    assert m.call_count == 1
    assert len(df2) == 3


def test_fetch_daily_price_extending_end_only_fetches_tail(tmp_db):
    """擴展尾端時,只該對缺的部分打 API,而且只打一次(那段 missing range)。"""
    first = _fake_price_rows("2330", ["2024-01-02", "2024-01-03"])
    extra = _fake_price_rows("2330", ["2024-01-08", "2024-01-09"])

    def side_effect(dataset, **params):
        if params.get("start_date") == "2024-01-01":
            return first
        return extra

    with patch.object(fetcher, "_api_call", side_effect=side_effect) as m:
        fetcher.fetch_daily_price("2330", "2024-01-01", "2024-01-05")
        assert m.call_count == 1
        fetcher.fetch_daily_price("2330", "2024-01-01", "2024-01-10")

    assert m.call_count == 2
    second_call_kwargs = m.call_args_list[1].kwargs
    # 第二次只該補 [2024-01-06, 2024-01-10] (即 _next_day('2024-01-05') ~ '2024-01-10')
    assert second_call_kwargs["start_date"] == "2024-01-06"
    assert second_call_kwargs["end_date"] == "2024-01-10"


def test_fetch_daily_price_extending_head_only_fetches_head(tmp_db):
    """擴展頭端時,只該補前面缺的那段。"""
    first = _fake_price_rows("2330", ["2024-01-15"])
    extra = _fake_price_rows("2330", ["2024-01-05"])

    def side_effect(dataset, **params):
        if params.get("start_date") == "2024-01-10":
            return first
        return extra

    with patch.object(fetcher, "_api_call", side_effect=side_effect) as m:
        fetcher.fetch_daily_price("2330", "2024-01-10", "2024-01-20")
        assert m.call_count == 1
        fetcher.fetch_daily_price("2330", "2024-01-01", "2024-01-20")

    assert m.call_count == 2
    second_call_kwargs = m.call_args_list[1].kwargs
    # 應該補 [2024-01-01, 2024-01-09]
    assert second_call_kwargs["start_date"] == "2024-01-01"
    assert second_call_kwargs["end_date"] == "2024-01-09"


def test_fetch_daily_price_returns_dataframe_columns(tmp_db):
    fake = _fake_price_rows("2330", ["2024-01-02"])
    with patch.object(fetcher, "_api_call", return_value=fake):
        df = fetcher.fetch_daily_price("2330", "2024-01-01", "2024-01-05")
    for col in [
        "stock_id", "date", "open", "high", "low", "close",
        "volume", "trading_money", "trading_turnover", "spread",
    ]:
        assert col in df.columns


# === list_tw_stocks ===

def test_list_tw_stocks_caches_after_first_call(tmp_db):
    fake = [
        {"stock_id": "2330", "stock_name": "台積電",
         "industry_category": "半導體", "type": "twse"},
        {"stock_id": "2454", "stock_name": "聯發科",
         "industry_category": "半導體", "type": "twse"},
    ]
    with patch.object(fetcher, "_api_call", return_value=fake) as m:
        df = fetcher.list_tw_stocks()
        assert m.call_count == 1
        df2 = fetcher.list_tw_stocks()  # 第二次應該走 DB
    assert m.call_count == 1
    assert len(df) == 2 and len(df2) == 2
    assert set(df["stock_id"]) == {"2330", "2454"}


def test_list_tw_stocks_force_refresh_hits_api(tmp_db):
    fake = [{"stock_id": "2330", "stock_name": "台積電",
             "industry_category": "半導體", "type": "twse"}]
    with patch.object(fetcher, "_api_call", return_value=fake) as m:
        fetcher.list_tw_stocks()
        fetcher.list_tw_stocks(force_refresh=True)
    assert m.call_count == 2


# === fetch_institutional + pivot 邏輯 ===

def test_fetch_institutional_pivots_correctly(tmp_db):
    fake = [
        {"date": "2024-01-02", "stock_id": "2330",
         "name": "Foreign_Investor", "buy": 1_000_000, "sell": 200_000},
        {"date": "2024-01-02", "stock_id": "2330",
         "name": "Investment_Trust", "buy": 50_000, "sell": 30_000},
        {"date": "2024-01-02", "stock_id": "2330",
         "name": "Dealer_self", "buy": 10_000, "sell": 15_000},
        {"date": "2024-01-02", "stock_id": "2330",
         "name": "Dealer_Hedging", "buy": 8_000, "sell": 5_000},
    ]
    with patch.object(fetcher, "_api_call", return_value=fake) as m:
        df = fetcher.fetch_institutional("2330", "2024-01-01", "2024-01-05")
    assert m.call_count == 1
    assert len(df) == 1
    row = df.iloc[0]
    assert row["foreign_buy_sell"] == 800_000
    assert row["trust_buy_sell"] == 20_000
    assert row["dealer_buy_sell"] == -5_000 + 3_000  # -5000 + 3000 = -2000
    assert row["total_buy_sell"] == 800_000 + 20_000 + (-2000)


def test_fetch_institutional_cache(tmp_db):
    fake = [
        {"date": "2024-01-02", "stock_id": "2330",
         "name": "Foreign_Investor", "buy": 1_000_000, "sell": 200_000},
    ]
    with patch.object(fetcher, "_api_call", return_value=fake) as m:
        fetcher.fetch_institutional("2330", "2024-01-01", "2024-01-05")
        fetcher.fetch_institutional("2330", "2024-01-01", "2024-01-05")
    assert m.call_count == 1


# === fetch_monthly_revenue ===

def test_fetch_monthly_revenue_normalizes_period(tmp_db):
    fake = [
        {"date": "2024-02-10", "stock_id": "2330",
         "revenue_year": 2024, "revenue_month": 1,
         "revenue": 195_620_000_000, "revenue_year_growth": 0.123},
        {"date": "2024-03-10", "stock_id": "2330",
         "revenue_year": 2024, "revenue_month": 2,
         "revenue": 180_000_000_000, "revenue_year_growth": 0.05},
    ]
    with patch.object(fetcher, "_api_call", return_value=fake) as m:
        df = fetcher.fetch_monthly_revenue("2330", "2024-01-01", "2024-12-31")
    assert m.call_count == 1
    periods = set(df["period"])
    assert periods == {"2024-01", "2024-02"}


def test_fetch_monthly_revenue_cache(tmp_db):
    fake = [
        {"date": "2024-02-10", "stock_id": "2330",
         "revenue_year": 2024, "revenue_month": 1,
         "revenue": 1.9e11, "revenue_year_growth": 0.1},
    ]
    with patch.object(fetcher, "_api_call", return_value=fake) as m:
        fetcher.fetch_monthly_revenue("2330", "2024-01-01", "2024-03-31")
        fetcher.fetch_monthly_revenue("2330", "2024-01-01", "2024-03-31")
    assert m.call_count == 1


# === fetch_quarterly_financials: 降級行為 ===

def test_fetch_quarterly_financials_graceful_on_api_error(tmp_db):
    """無 token 模式下,FinMind 拒絕季財報 → 應回空 DataFrame,不該炸。"""
    with patch.object(
        fetcher, "_api_call",
        side_effect=FinMindAPIError("status=402, msg=token required")
    ):
        df = fetcher.fetch_quarterly_financials("2330", "2024-01-01", "2024-12-31")
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_fetch_quarterly_financials_parses_eps_roe(tmp_db):
    fake = [
        {"date": "2024-03-31", "stock_id": "2330", "type": "EPS", "value": 8.5},
        {"date": "2024-03-31", "stock_id": "2330", "type": "ROE", "value": 27.3},
        {"date": "2024-06-30", "stock_id": "2330", "type": "EPS", "value": 9.1},
    ]
    with patch.object(fetcher, "_api_call", return_value=fake):
        df = fetcher.fetch_quarterly_financials("2330", "2024-01-01", "2024-12-31")
    assert set(df["period"]) == {"2024-Q1", "2024-Q2"}
    q1 = df[df["period"] == "2024-Q1"].iloc[0]
    assert q1["eps"] == pytest.approx(8.5)
    assert q1["roe"] == pytest.approx(27.3)


# === fetch_dividend ===

def _fake_div_rows(stock_id: str = "2330") -> list[dict]:
    """模擬 FinMind TaiwanStockDividend 回傳(同年中間配 + 期末配兩筆)。"""
    return [
        {
            "date": "2024-07-15", "stock_id": stock_id, "year": "2024",
            "CashEarningsDistribution": 3.0,
            "StockEarningsDistribution": 0.0,
            "CashExDividendTradingDate": "2024-07-15",
        },
        {
            "date": "2024-12-15", "stock_id": stock_id, "year": "2024",
            "CashEarningsDistribution": 1.5,
            "StockEarningsDistribution": 0.0,
            "CashExDividendTradingDate": "2024-12-15",
        },
        {
            "date": "2023-08-15", "stock_id": stock_id, "year": "2023",
            "CashEarningsDistribution": 11.0,
            "StockEarningsDistribution": 0.5,
            "CashExDividendTradingDate": "2023-08-15",
        },
    ]


def test_fetch_dividend_first_call_hits_api(tmp_db):
    with patch.object(fetcher, "_api_call", return_value=_fake_div_rows()) as m:
        df = fetcher.fetch_dividend("2330")
    assert m.call_count == 1
    # 兩個年度,2024 加總 4.5,2023 是 11.0
    assert set(df["year"]) == {2023, 2024}
    y2024 = df[df["year"] == 2024].iloc[0]
    y2023 = df[df["year"] == 2023].iloc[0]
    assert y2024["cash_dividend"] == pytest.approx(4.5)
    assert y2023["cash_dividend"] == pytest.approx(11.0)
    assert y2023["stock_dividend"] == pytest.approx(0.5)


def test_fetch_dividend_second_call_uses_cache(tmp_db):
    with patch.object(fetcher, "_api_call", return_value=_fake_div_rows()) as m:
        fetcher.fetch_dividend("2330")
        assert m.call_count == 1
        df2 = fetcher.fetch_dividend("2330")
    assert m.call_count == 1, "第二次相同請求不該打 API"
    assert len(df2) == 2


def test_fetch_dividend_graceful_on_api_error(tmp_db):
    """FinMind 拒絕 dividend dataset → 回空 DataFrame,不拋例外。"""
    with patch.object(
        fetcher, "_api_call",
        side_effect=FinMindAPIError("status=402, msg=token required"),
    ):
        df = fetcher.fetch_dividend("2330")
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_fetch_dividend_year_fallback_from_date(tmp_db):
    """row 沒 year 欄位時,應從 date 推年份。"""
    fake = [
        {
            "date": "2025-08-01", "stock_id": "2330",
            "CashEarningsDistribution": 5.0,
            "StockEarningsDistribution": 0.0,
        },
    ]
    with patch.object(fetcher, "_api_call", return_value=fake):
        df = fetcher.fetch_dividend("2330")
    assert df.iloc[0]["year"] == 2025


def test_fetch_dividend_skips_rows_without_year(tmp_db):
    """連 date 都沒有的 row 應被跳過,不炸。"""
    fake = [
        {"stock_id": "2330", "CashEarningsDistribution": 5.0},  # 沒 year 沒 date
        {"date": "2024-08-01", "stock_id": "2330",
         "CashEarningsDistribution": 3.0, "StockEarningsDistribution": 0.0},
    ]
    with patch.object(fetcher, "_api_call", return_value=fake):
        df = fetcher.fetch_dividend("2330")
    assert len(df) == 1
    assert df.iloc[0]["year"] == 2024


# === fetch_long_term_data ===

def test_fetch_long_term_data_calls_both_datasets(tmp_db):
    """確認 fetch_long_term_data 對每檔都會呼叫財報 + 配息兩個 dataset。"""
    fin_fake = [
        {"date": "2024-03-31", "stock_id": "2330", "type": "EPS", "value": 8.5},
        {"date": "2024-03-31", "stock_id": "2330", "type": "ROE", "value": 27.3},
    ]
    div_fake = _fake_div_rows("2330")

    def side_effect(dataset, **params):
        if dataset == fetcher.DATASET_FINANCIAL:
            return fin_fake
        if dataset == fetcher.DATASET_DIVIDEND:
            return div_fake
        return []

    with patch.object(fetcher, "_api_call", side_effect=side_effect) as m:
        result = fetcher.fetch_long_term_data(["2330"])

    # 每檔該打 2 次(financial + dividend),共 1 檔 = 2 次
    assert m.call_count == 2
    assert "2330" in result["success_financials"]
    assert "2330" in result["success_dividend"]
    assert result["failed"] == []


def test_fetch_long_term_data_progress_callback(tmp_db):
    """on_progress callback 該被叫 N 次,參數正確。"""
    calls: list[tuple[int, int, str, object]] = []

    def cb(idx, total, sid, err):
        calls.append((idx, total, sid, err))

    with patch.object(fetcher, "_api_call", return_value=[]):
        fetcher.fetch_long_term_data(["2330", "2454"], on_progress=cb)

    assert len(calls) == 2
    assert calls[0][0] == 1 and calls[0][1] == 2 and calls[0][2] == "2330"
    assert calls[1][0] == 2 and calls[1][1] == 2 and calls[1][2] == "2454"


def test_fetch_long_term_data_continues_on_failure(tmp_db):
    """單檔財報抓 raise → 該檔記 failed,但其他檔仍處理。"""
    div_fake = _fake_div_rows("2454")

    def side_effect(dataset, data_id=None, **params):
        if data_id == "2330":
            raise FinMindAPIError("simulated network error")
        if data_id == "2454" and dataset == fetcher.DATASET_DIVIDEND:
            return div_fake
        return []

    result = None
    with patch.object(fetcher, "_api_call", side_effect=side_effect):
        result = fetcher.fetch_long_term_data(["2330", "2454"])

    assert "2330" in result["failed"]
    assert "2454" in result["success_dividend"]


def test_fetch_long_term_data_callback_error_does_not_stop_loop(tmp_db):
    """callback 自己 raise 不該影響主流程。"""
    def bad_cb(idx, total, sid, err):
        raise RuntimeError("callback boom")

    with patch.object(fetcher, "_api_call", return_value=[]):
        result = fetcher.fetch_long_term_data(["2330", "2454"], on_progress=bad_cb)

    # 兩檔都因為 _api_call 回空,記為 failed,但流程沒中斷
    assert len(result["failed"]) == 2


# === _missing_ranges 邏輯單元測試 ===

@pytest.mark.parametrize("synced, start, end, expected", [
    (None, "2024-01-01", "2024-01-31", [("2024-01-01", "2024-01-31")]),
    (("2024-01-01", "2024-01-31"), "2024-01-05", "2024-01-20", []),
    (("2024-01-01", "2024-01-31"), "2024-01-01", "2024-01-31", []),
    (("2024-01-10", "2024-01-20"), "2024-01-05", "2024-01-25",
     [("2024-01-05", "2024-01-09"), ("2024-01-21", "2024-01-25")]),
    (("2024-01-10", "2024-01-20"), "2024-01-15", "2024-01-25",
     [("2024-01-21", "2024-01-25")]),
    (("2024-01-10", "2024-01-20"), "2024-01-05", "2024-01-15",
     [("2024-01-05", "2024-01-09")]),
])
def test_missing_ranges(synced, start, end, expected):
    assert fetcher._missing_ranges(start, end, synced) == expected
