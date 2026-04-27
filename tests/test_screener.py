"""src/screener_short.py 與 src/screener_long.py 單元測試。

策略:
- tmp_path 建立獨立 SQLite,monkeypatch 改 config.DATABASE_PATH
- 用 helper 灌入 stocks / daily_prices / institutional / financials / dividend
- 不打網路、不依賴真實 cache
"""
from __future__ import annotations

import pytest

from src import config, database as db
from src.screener_long import DEFAULT_LONG_PARAMS, screen_long
from src.screener_short import DEFAULT_SHORT_PARAMS, screen_short


# === 共用 fixture ===

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "screener.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def _add_stock(stock_id: str, name: str, industry: str | None = None) -> None:
    db.upsert_stocks([
        {"stock_id": stock_id, "name": name, "industry": industry, "market": "TW"}
    ])


def _add_prices(stock_id: str, prices: list[dict]) -> None:
    db.upsert_daily_prices([{"stock_id": stock_id, **p} for p in prices])


def _add_inst(stock_id: str, rows: list[dict]) -> None:
    db.upsert_institutional([{"stock_id": stock_id, **r} for r in rows])


def _add_financials(stock_id: str, rows: list[dict]) -> None:
    db.upsert_financials([{"stock_id": stock_id, **r} for r in rows])


def _add_dividend(stock_id: str, rows: list[dict]) -> None:
    db.upsert_dividend([{"stock_id": stock_id, **r} for r in rows])


# === 短線 fixture builder ===

# 12 個交易日的範本日期(取週一到週五,跳過週末避免歧義)
_DATES = [
    "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05",
    "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11",
    "2024-01-12", "2024-01-15", "2024-01-16", "2024-01-17",
]


def _build_short_passing_fixture(stock_id: str = "TEST") -> str:
    """構造一個「全部三條件滿足」的個股資料,回最後一日的 date。

    KD 黃金交叉:
        close = [100]*9 + [80, 120], high = close+10, low = close-10
        index=9 K<D(死亡), index=10 K>D(交叉) 且 K>20
    量價突破:
        volume = [1000]*10 + [3000];最後一日 3000 > 5 日均(1000)*1.5
    法人連 3 日買超: 最後 3 日 total_buy_sell 全 > 0
    """
    _add_stock(stock_id, f"測試股_{stock_id}")
    closes = [100.0] * 9 + [80.0, 120.0]
    volumes = [1000] * 10 + [3000]
    dates = _DATES[: len(closes)]
    prices = []
    for d, c, v in zip(dates, closes, volumes):
        prices.append({
            "date": d, "open": c, "high": c + 10, "low": c - 10,
            "close": c, "volume": v, "trading_money": c * v,
            "trading_turnover": 100, "spread": 0.0,
        })
    _add_prices(stock_id, prices)
    _add_inst(stock_id, [
        {"date": dates[-3], "total_buy_sell": 100_000,
         "foreign_buy_sell": 100_000, "trust_buy_sell": 0, "dealer_buy_sell": 0},
        {"date": dates[-2], "total_buy_sell": 200_000,
         "foreign_buy_sell": 200_000, "trust_buy_sell": 0, "dealer_buy_sell": 0},
        {"date": dates[-1], "total_buy_sell": 300_000,
         "foreign_buy_sell": 300_000, "trust_buy_sell": 0, "dealer_buy_sell": 0},
    ])
    return dates[-1]


# === 短線測試 ===

def test_short_all_conditions_pass(tmp_db):
    last = _build_short_passing_fixture("2330")
    result = screen_short(last)
    assert len(result) == 1
    row = result.iloc[0]
    assert row["stock_id"] == "2330"
    assert row["close"] == 120.0
    assert row["volume"] == 3000
    assert row["k"] > row["d"]              # 確實是黃金交叉
    assert row["k"] > DEFAULT_SHORT_PARAMS["kd_threshold_low"]
    assert row["inst_total_3d"] == 600_000  # 100k + 200k + 300k
    assert row["matched_at"] == last


def test_short_volume_not_breakout(tmp_db):
    """條件 A 失敗:今日量沒有 > 5 日均量 × 1.5。"""
    last = _build_short_passing_fixture("2330")
    # 把今日成交量壓到只比均量稍高
    db.upsert_daily_prices([{
        "stock_id": "2330", "date": last,
        "open": 120.0, "high": 130.0, "low": 110.0, "close": 120.0,
        "volume": 1100,  # 1100 / 1000 = 1.1 < 1.5
        "trading_money": None, "trading_turnover": None, "spread": None,
    }])
    result = screen_short(last)
    assert result.empty


def test_short_inst_one_day_negative(tmp_db):
    """條件 C 失敗:法人 3 日中有 1 日賣超。"""
    last = _build_short_passing_fixture("2330")
    db.upsert_institutional([{
        "stock_id": "2330", "date": _DATES[-2],
        "total_buy_sell": -50_000,
        "foreign_buy_sell": -50_000, "trust_buy_sell": 0, "dealer_buy_sell": 0,
    }])
    result = screen_short(last)
    assert result.empty


def test_short_no_kd_cross(tmp_db):
    """條件 B 失敗:今日 K 沒有從 ≤ D 翻為 > D。"""
    _add_stock("2330", "台積電")
    # 全部平盤 → K = D = 50,沒有交叉(prev_k 不 ≤ prev_d 是相等;curr_k 不 > curr_d)
    closes = [100.0] * 12
    dates = _DATES
    prices = [
        {"date": d, "open": c, "high": c + 10, "low": c - 10,
         "close": c, "volume": 3000 if i == len(closes) - 1 else 1000,
         "trading_money": None, "trading_turnover": None, "spread": None}
        for i, (d, c) in enumerate(zip(dates, closes))
    ]
    _add_prices("2330", prices)
    _add_inst("2330", [
        {"date": dates[-3], "total_buy_sell": 100, "foreign_buy_sell": 100,
         "trust_buy_sell": 0, "dealer_buy_sell": 0},
        {"date": dates[-2], "total_buy_sell": 100, "foreign_buy_sell": 100,
         "trust_buy_sell": 0, "dealer_buy_sell": 0},
        {"date": dates[-1], "total_buy_sell": 100, "foreign_buy_sell": 100,
         "trust_buy_sell": 0, "dealer_buy_sell": 0},
    ])
    result = screen_short(dates[-1])
    assert result.empty


def test_short_kd_just_crosses_passes(tmp_db):
    """邊界:K 剛剛超過 D(差很小)就算交叉,應入選。"""
    last = _build_short_passing_fixture("2330")
    # 用 helper 構造的 fixture K-D 差約 5,本身就是「剛交叉」附近;
    # 確認入選即代表邊界邏輯正確(不會用 >= 漏掉)
    result = screen_short(last)
    assert not result.empty
    row = result.iloc[0]
    assert row["k"] - row["d"] > 0  # 嚴格大於,即使很小也算


def test_short_insufficient_data_skipped(tmp_db):
    """資料不足(< 6 日價格)→ 跳過該檔,不報錯。"""
    _add_stock("2330", "台積電")
    _add_prices("2330", [
        {"date": "2024-01-02", "open": 100, "high": 110, "low": 90,
         "close": 100, "volume": 1000, "trading_money": None,
         "trading_turnover": None, "spread": None}
    ])
    # 沒有法人資料 → 也是不足
    result = screen_short("2024-01-02")
    assert result.empty  # 沒入選,也沒拋例外


def test_short_target_date_no_trading(tmp_db):
    """target_date 不是交易日(資料表最後一筆不是 target_date)→ 跳過。"""
    last = _build_short_passing_fixture("2330")
    # 用一個比實際資料更晚的日期(假設今天沒交易)
    result = screen_short("2024-01-20")
    assert result.empty


def test_short_empty_stocks_table(tmp_db):
    """stocks 表為空 → 回空 DataFrame,不拋例外。"""
    result = screen_short("2024-01-17")
    assert result.empty
    # 欄位仍在
    from src.screener_short import OUTPUT_COLUMNS
    assert list(result.columns) == OUTPUT_COLUMNS


# === 長線 fixture builder ===

def _build_long_passing_fixture(stock_id: str = "2330", industry: str = "半導體") -> None:
    """全條件滿足的長線標的:
    - 12 季 ROE 都 18%(平均 18 > 15)
    - 4 季 EPS 都 10 → TTM 40
    - close = 200 → PE = 5 < 20 ✓
    - 5 年 cash_dividend 都 10 → 連續配息 5 年 ✓
    - 殖利率 = 10/200 * 100 = 5% > 4% ✓
    """
    _add_stock(stock_id, f"長線_{stock_id}", industry=industry)
    _add_prices(stock_id, [
        {"date": "2026-04-25", "open": 200, "high": 200, "low": 200,
         "close": 200, "volume": 1000, "trading_money": None,
         "trading_turnover": None, "spread": None}
    ])
    fin_rows = []
    for year in (2024, 2023, 2022):
        for q in (1, 2, 3, 4):
            fin_rows.append({
                "period_type": "quarterly",
                "period": f"{year}-Q{q}",
                "roe": 18.0,
                "eps": 10.0 if year == 2024 else None,
                "revenue": None, "revenue_yoy": None,
            })
    _add_financials(stock_id, fin_rows)
    _add_dividend(stock_id, [
        {"year": y, "cash_dividend": 10.0, "stock_dividend": 0.0,
         "ex_dividend_date": None}
        for y in (2025, 2024, 2023, 2022, 2021)
    ])


# === 長線測試 ===

def test_long_all_conditions_pass(tmp_db):
    _build_long_passing_fixture("2330")
    result = screen_long()
    assert len(result) == 1
    row = result.iloc[0]
    assert row["stock_id"] == "2330"
    assert row["avg_roe"] == pytest.approx(18.0)
    assert row["pe"] == pytest.approx(5.0)
    assert row["consecutive_dividend_years"] == 5
    assert row["dividend_yield"] == pytest.approx(5.0)


def test_long_low_roe_excluded(tmp_db):
    _build_long_passing_fixture("2330")
    # 全部 ROE 改 10
    with db.get_conn() as conn:
        conn.execute("UPDATE financials SET roe=10.0 WHERE stock_id='2330'")
    result = screen_long()
    assert result.empty


def test_long_dividend_break_excluded(tmp_db):
    _build_long_passing_fixture("2330")
    # 把 2023 年配息改 0(中斷連續)
    db.upsert_dividend([{
        "stock_id": "2330", "year": 2023, "cash_dividend": 0.0,
        "stock_dividend": 0.0, "ex_dividend_date": None,
    }])
    result = screen_long()
    assert result.empty


def test_long_high_pe_excluded(tmp_db):
    """PE >= pe_max 且無產業同業 → 不入選。"""
    _build_long_passing_fixture("2330")
    # 把 EPS 改小,讓 TTM EPS = 4 → PE = 200/4 = 50 > 20
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE financials SET eps=1.0 WHERE stock_id='2330' AND period_type='quarterly'"
        )
    result = screen_long()
    assert result.empty


def test_long_low_yield_excluded(tmp_db):
    """殖利率 ≤ 4% → 不入選。"""
    _build_long_passing_fixture("2330")
    # cash_dividend 改 5 → yield = 5/200*100 = 2.5%
    db.upsert_dividend([
        {"stock_id": "2330", "year": y, "cash_dividend": 5.0,
         "stock_dividend": 0.0, "ex_dividend_date": None}
        for y in (2025, 2024, 2023, 2022, 2021)
    ])
    result = screen_long()
    assert result.empty


def test_long_empty_financials_returns_empty_with_warning(tmp_db, capsys):
    """financials 空表 → 回空 DataFrame + stderr warning。"""
    # 只灌 dividend 不灌 financials
    _add_stock("2330", "台積電")
    _add_dividend("2330", [
        {"year": y, "cash_dividend": 10.0, "stock_dividend": 0.0,
         "ex_dividend_date": None}
        for y in (2025, 2024, 2023, 2022, 2021)
    ])
    result = screen_long()
    assert result.empty
    captured = capsys.readouterr()
    assert "缺財報" in captured.err
    assert "升級 FinMind token" in captured.err


def test_long_empty_dividend_returns_empty_with_warning(tmp_db, capsys):
    """dividend 空表 → 回空 DataFrame + stderr warning。"""
    _add_stock("2330", "台積電")
    db.upsert_financials([{
        "stock_id": "2330", "period_type": "quarterly",
        "period": "2024-Q1", "roe": 18.0, "eps": 10.0,
        "revenue": None, "revenue_yoy": None,
    }])
    result = screen_long()
    assert result.empty
    captured = capsys.readouterr()
    assert "缺財報" in captured.err


def test_long_industry_avg_pe_used_when_available(tmp_db):
    """當 PE > pe_max 但 < 產業平均 PE 時應該入選。"""
    # close=200, TTM EPS = 4 季 × 單季 EPS
    # A: 單季 EPS=2.0 → TTM=8.0 → PE=25 (> pe_max 20, 但 < 產業均)
    # B/C: 單季 EPS=1.0 → TTM=4.0 → PE=50 (墊高產業均)
    # industry_avg = (25+50+50)/3 ≈ 41.67
    _add_stock("A", "甲", industry="半導體")
    _add_stock("B", "乙", industry="半導體")
    _add_stock("C", "丙", industry="半導體")
    base_close = 200
    for sid, q_eps in [("A", 2.0), ("B", 1.0), ("C", 1.0)]:
        _add_prices(sid, [{
            "date": "2026-04-25", "open": base_close, "high": base_close,
            "low": base_close, "close": base_close, "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        }])
        for year in (2024, 2023, 2022):
            for q in (1, 2, 3, 4):
                _add_financials(sid, [{
                    "period_type": "quarterly",
                    "period": f"{year}-Q{q}",
                    "roe": 18.0,
                    "eps": q_eps if year == 2024 else None,
                    "revenue": None, "revenue_yoy": None,
                }])
        _add_dividend(sid, [
            {"year": y, "cash_dividend": 10.0, "stock_dividend": 0.0,
             "ex_dividend_date": None}
            for y in (2025, 2024, 2023, 2022, 2021)
        ])

    result = screen_long()
    selected_ids = set(result["stock_id"])
    # A: PE=25, < industry avg(~41.67) → 入選
    assert "A" in selected_ids
    # B/C: PE=50, 不 < pe_max 20 也不 < industry avg ≈ 41.67 → 不入選
    assert "B" not in selected_ids
    assert "C" not in selected_ids


def test_long_param_override(tmp_db):
    """傳 params 應該覆蓋預設值。"""
    _build_long_passing_fixture("2330")
    # 把 ROE 門檻調到 25(原本 18 過不了)
    result = screen_long(params={"roe_threshold": 25.0})
    assert result.empty
    # 調回 10 應該入選
    result = screen_long(params={"roe_threshold": 10.0})
    assert len(result) == 1


def test_long_empty_columns_when_no_data(tmp_db, capsys):
    """缺資料時回空 DF 但欄位齊全。"""
    result = screen_long()
    capsys.readouterr()  # 吃掉 warning
    from src.screener_long import OUTPUT_COLUMNS
    assert list(result.columns) == OUTPUT_COLUMNS
