"""src/financial_fetcher_free.py 單元測試。

策略:
- mock requests.get 回 TWSE OpenAPI 預設 JSON
- 每個測試前清空模組級 cache
- 不打真網路
"""
from __future__ import annotations

from unittest.mock import Mock

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


def test_fetch_metrics_empty_list_raises_hard(tmp_db, monkeypatch):
    """雲端常見的『200 但回空 list』要被視為失敗 → batch 函式回 error。"""
    def empty(url, timeout=30):
        return _mock_response([])  # 空 list = 雲端被擋的典型症狀

    monkeypatch.setattr(ff, "_twse_get", empty)
    result = ff.update_long_term_data_free(["2330"])
    assert result["success_metrics"] == []
    assert "2330" in result["failed"]
    # error 該被填(而不是 None,讓 UI 能顯示具體訊息)
    assert result["error"] is not None
    assert "格式異常" in str(result["error"])


def test_twse_get_falls_back_to_httpx(monkeypatch):
    """requests 失敗 → 自動試 httpx → 成功。"""
    fake_resp = _mock_response(_FAKE_BWIBBU)

    # 讓 requests session 失敗
    def failing_session():
        s = Mock()
        s.get.side_effect = requests.ConnectionError("requests boom")
        return s
    monkeypatch.setattr(ff, "_twse_session", failing_session)

    # 讓 httpx fallback 成功
    monkeypatch.setattr(ff, "_twse_get_via_httpx", lambda url, t=30: fake_resp)

    r = ff._twse_get(ff.TWSE_BWIBBU_URL)
    assert r is fake_resp


# === fetch_industry_classification ===

_FAKE_BASIC = [
    {"公司代號": "2330", "公司名稱": "台積電", "產業別": "24"},
    {"公司代號": "2317", "公司名稱": "鴻海", "產業別": "31"},
    {"公司代號": "2881", "公司名稱": "富邦金", "產業別": "17"},
    {"公司代號": "BAD", "公司名稱": "未知", "產業別": "99"},  # 未對應的代碼
    {"公司代號": "", "公司名稱": "空代號"},  # 該被跳過
]


def test_fetch_industry_classification_translates_codes(monkeypatch):
    def get(url, timeout=30):
        if "t187ap03" in url:
            return _mock_response(_FAKE_BASIC)
        return _mock_response([])
    monkeypatch.setattr(ff, "_twse_get", get)
    out = ff.fetch_industry_classification()
    assert out["2330"] == "半導體業"
    assert out["2317"] == "其他電子業"
    assert out["2881"] == "金融保險"
    # 未對應代碼 fallback "其他"
    assert out["BAD"] == "其他"
    # 空 stock_id 該被跳過
    assert "" not in out


def test_fetch_industry_classification_api_failure_returns_empty(monkeypatch):
    def boom(url, timeout=30):
        raise requests.ConnectionError("boom")
    monkeypatch.setattr(ff, "_twse_get", boom)
    out = ff.fetch_industry_classification()
    assert out == {}


def test_update_long_term_data_free_writes_industry(tmp_db, monkeypatch):
    """update_long_term_data_free 該把 industry 寫進 stocks 表。"""
    # 先 seed stocks 表(實際 app 在按按鈕時也會 upsert_stocks 一遍)
    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積電", "market": "TW"},
        {"stock_id": "2317", "name": "鴻海", "market": "TW"},
    ])

    def get(url, timeout=30):
        if "BWIBBU" in url:
            return _mock_response(_FAKE_BWIBBU)
        if "t187ap14" in url:
            return _mock_response(_FAKE_INC)
        if "t187ap03" in url:
            return _mock_response(_FAKE_BASIC)
        return _mock_response([])
    monkeypatch.setattr(ff, "_twse_get", get)

    ff.update_long_term_data_free(["2330", "2317"])

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id, industry FROM stocks WHERE stock_id IN ('2330','2317')"
        ).fetchall()
    by_id = {r["stock_id"]: r["industry"] for r in rows}
    assert by_id["2330"] == "半導體業"
    assert by_id["2317"] == "其他電子業"


def test_twse_get_both_clients_fail_raises(monkeypatch):
    """兩個 client 都失敗 → raise 第一個 exception(requests 的)。"""
    def failing_session():
        s = Mock()
        s.get.side_effect = requests.ConnectionError("requests boom")
        return s
    monkeypatch.setattr(ff, "_twse_session", failing_session)

    def httpx_boom(url, t=30):
        raise RuntimeError("httpx boom")
    monkeypatch.setattr(ff, "_twse_get_via_httpx", httpx_boom)

    with pytest.raises(requests.ConnectionError, match="requests boom"):
        ff._twse_get(ff.TWSE_BWIBBU_URL)
