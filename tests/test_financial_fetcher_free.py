"""src/financial_fetcher_free.py 單元測試。

策略:
- mock requests.get 回 TWSE OpenAPI 預設 JSON
- 每個測試前清空模組級 cache
- 不打真網路
"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pandas as pd
import pytest
import requests

from src import config, database as db
from src import financial_fetcher_free as ff


@pytest.fixture(autouse=True)
def reset_caches():
    """每個測試前清空模組級記憶體 cache,確保測試獨立。"""
    ff._reset_caches()
    yield
    ff._reset_caches()


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "free.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


# === BWIBBU 假資料 ===
_FAKE_BWIBBU = [
    {
        "Date": "20260424", "Code": "2330", "Name": "台積電",
        "ClosePrice": "2185.00", "DividendYield": "1.01",
        "DividendYear": "114", "PEratio": "32.99", "PBratio": "10.46",
        "FiscalYearQuarter": "2025Q4",
    },
    {
        "Date": "20260424", "Code": "2454", "Name": "聯發科",
        "ClosePrice": "1200.00", "DividendYield": "5.00",
        "DividendYear": "114", "PEratio": "15.00", "PBratio": "3.00",
        "FiscalYearQuarter": "2025Q4",
    },
    {
        "Date": "20260424", "Code": "1101", "Name": "台泥",
        "ClosePrice": "24.45", "DividendYield": "3.27",
        "DividendYear": "114", "PEratio": "",  # 空 PE
        "PBratio": "0.81", "FiscalYearQuarter": "2025Q4",
    },
]

_FAKE_INC = [
    {
        "出表日期": "1150427", "年度": "114", "季別": "4",
        "公司代號": "2330", "公司名稱": "台積電",
        "基本每股盈餘(元)": "66.26",
    },
    {
        "出表日期": "1150427", "年度": "114", "季別": "4",
        "公司代號": "2454", "公司名稱": "聯發科",
        "基本每股盈餘(元)": "80.00",
    },
]


def _mock_response(payload):
    r = Mock()
    r.status_code = 200
    r.json.return_value = payload
    r.raise_for_status = Mock()
    return r


def _bwibbu_or_inc(url, timeout=30):
    """模擬 _twse_get:依 URL 回對應的 fake JSON。"""
    if "BWIBBU" in url:
        return _mock_response(_FAKE_BWIBBU)
    if "t187ap14" in url:
        return _mock_response(_FAKE_INC)
    return _mock_response([])


# === fetch_daily_metrics ===

def test_fetch_daily_metrics_2330(monkeypatch):
    monkeypatch.setattr(ff, "_twse_get", _bwibbu_or_inc)
    m = ff.fetch_daily_metrics("2330")
    assert m["stock_id"] == "2330"
    assert m["close"] == 2185.0
    assert m["pe"] == 32.99
    assert m["pb"] == 10.46
    assert m["dividend_yield"] == 1.01
    assert m["date"] == "2026-04-24"  # 民國 → 西元
    assert m["_fiscal_quarter"] == "2025Q4"


def test_fetch_daily_metrics_unknown_stock(monkeypatch):
    monkeypatch.setattr(ff, "_twse_get", _bwibbu_or_inc)
    m = ff.fetch_daily_metrics("9999")
    assert m is None


def test_fetch_daily_metrics_api_failure_returns_none(monkeypatch):
    """API 整體失敗 → 回 None,不拋例外。"""
    def boom(url, timeout=30):
        raise requests.ConnectionError("boom")
    monkeypatch.setattr(ff, "_twse_get", boom)
    m = ff.fetch_daily_metrics("2330")
    assert m is None


def test_fetch_daily_metrics_uses_cache(monkeypatch):
    """同一個 process 內第二次呼叫不該再打 API。"""
    calls = {"n": 0}
    def counted(url, timeout=30):
        calls["n"] += 1
        return _bwibbu_or_inc(url, timeout)
    monkeypatch.setattr(ff, "_twse_get", counted)
    ff.fetch_daily_metrics("2330")
    ff.fetch_daily_metrics("2454")
    ff.fetch_daily_metrics("2330")
    # 全市場資料 cache 共用,只該打 1 次 BWIBBU
    assert calls["n"] == 1


# === fetch_quarterly_eps ===

def test_fetch_quarterly_eps_2330(monkeypatch):
    monkeypatch.setattr(ff, "_twse_get", _bwibbu_or_inc)
    df = ff.fetch_quarterly_eps("2330")
    assert len(df) == 1
    row = df.iloc[0]
    assert row["year"] == 2025  # 民國 114 → 2025
    assert row["quarter"] == 4
    assert row["eps_quarterly"] == pytest.approx(66.26)


def test_fetch_quarterly_eps_unknown_returns_empty(monkeypatch):
    monkeypatch.setattr(ff, "_twse_get", _bwibbu_or_inc)
    df = ff.fetch_quarterly_eps("9999")
    assert df.empty


# === compute_roe ===

def test_compute_roe_2330_about_31_pct(monkeypatch):
    """ROE = PB / PE × 100 = 10.46 / 32.99 × 100 ≈ 31.71%。"""
    monkeypatch.setattr(ff, "_twse_get", _bwibbu_or_inc)
    roe = ff.compute_roe("2330")
    assert roe == pytest.approx(31.71, abs=0.05)


def test_compute_roe_handles_zero_pe(monkeypatch):
    """PE 為空字串(台泥)→ 回 None,不除零炸。"""
    monkeypatch.setattr(ff, "_twse_get", _bwibbu_or_inc)
    roe = ff.compute_roe("1101")
    assert roe is None


def test_compute_roe_handles_unknown(monkeypatch):
    monkeypatch.setattr(ff, "_twse_get", _bwibbu_or_inc)
    roe = ff.compute_roe("9999")
    assert roe is None


def test_compute_roe_known_math(monkeypatch):
    """確認公式正確:PE=10, PB=2 → ROE = 2/10 × 100 = 20%。"""
    fake = [{
        "Date": "20260424", "Code": "TEST", "Name": "測試",
        "ClosePrice": "100", "PEratio": "10", "PBratio": "2",
        "DividendYield": "0",
    }]
    def custom(url, timeout=30):
        return _mock_response(fake)
    monkeypatch.setattr(ff, "_twse_get", custom)
    roe = ff.compute_roe("TEST")
    # EPS_TTM = 100/10 = 10; BVPS = 100/2 = 50; ROE = 10/50 = 0.2 = 20%
    assert roe == pytest.approx(20.0)


# === update_long_term_data_free ===

def test_update_long_term_data_free_writes_db(tmp_db, monkeypatch):
    """跑兩檔,確認 daily_metrics + financials 表都寫入。"""
    monkeypatch.setattr(ff, "_twse_get", _bwibbu_or_inc)
    result = ff.update_long_term_data_free(["2330", "2454"])

    assert "2330" in result["success_metrics"]
    assert "2454" in result["success_metrics"]
    assert result["failed"] == []
    assert result["error"] is None

    # 確認 daily_metrics 表
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_metrics ORDER BY stock_id"
        ).fetchall()
    assert len(rows) == 2
    by_id = {r["stock_id"]: r for r in rows}
    assert by_id["2330"]["pe"] == pytest.approx(32.99)
    assert by_id["2330"]["pb"] == pytest.approx(10.46)
    assert by_id["2330"]["dividend_yield"] == pytest.approx(1.01)

    # 確認 financials 表的 ROE 是 PB 反推
    with db.get_conn() as conn:
        fin = conn.execute(
            "SELECT * FROM financials WHERE stock_id='2330' "
            "AND period_type='quarterly'"
        ).fetchone()
    assert fin["period"] == "2025-Q4"
    assert fin["roe"] == pytest.approx(31.71, abs=0.05)
    assert fin["eps"] is not None and fin["eps"] > 0


def test_update_long_term_data_free_continues_on_failure(tmp_db, monkeypatch):
    """BWIBBU 失敗 → 全部 failed + error 帶具體 exception。"""
    def boom(url, timeout=30):
        raise requests.ConnectionError("boom")
    monkeypatch.setattr(ff, "_twse_get", boom)
    result = ff.update_long_term_data_free(["2330", "2454"])
    assert result["success_metrics"] == []
    assert "2330" in result["failed"]
    assert "2454" in result["failed"]
    # error 該帶具體 exception 給 UI 顯示
    assert isinstance(result["error"], requests.ConnectionError)


def test_update_long_term_data_free_progress_callback(tmp_db, monkeypatch):
    monkeypatch.setattr(ff, "_twse_get", _bwibbu_or_inc)
    calls = []

    def cb(idx, total, sid, err):
        calls.append((idx, total, sid))

    ff.update_long_term_data_free(["2330", "2454"], on_progress=cb)
    assert len(calls) == 2
    assert calls[0] == (1, 2, "2330")
    assert calls[1] == (2, 2, "2454")


# === robust HTTP helpers ===

def test_legacy_ssl_adapter_can_be_constructed():
    """確認 _LegacySSLAdapter 不會在 import / 建構時炸。"""
    adapter = ff._LegacySSLAdapter()
    assert adapter is not None


def test_twse_session_singleton():
    """_twse_session 該回 singleton(避免每次新建)。"""
    s1 = ff._twse_session()
    s2 = ff._twse_session()
    assert s1 is s2
    # UA 應該被設成偽裝瀏覽器
    assert "Mozilla" in s1.headers.get("User-Agent", "")
