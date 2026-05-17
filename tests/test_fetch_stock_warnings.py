"""scripts/fetch_stock_warnings.py 單元測試。

2026-05-16 大改:從 bs4 HTML parser 改成 OpenAPI v1 JSON parser
(silent 0 rows 修復;違約交割教訓 round 2)。
2026-05-17 加 TWSE BFIGTU + TPEx breach fetcher / parser(主來源,違約交割
教訓 round 3 — 3105 4/27-4/28 silent miss bug)。

涵蓋:
  - schema 對齊 production(用 db.init_db() 建表,不自編 CREATE TABLE)
  - TWSE OpenAPI JSON fixture 餵 parser → 驗 sid / warning_type / dates
  - TPEx OpenAPI JSON fixture 同樣覆蓋(舊測試保留)
  - **TPEx breach 個股違約 fixture**:per-stock 細目 → stock_warnings
  - **TWSE BFIGTU + TPEx breach 每日彙總**:寫 default_settlement_daily 表
  - 重複 fetch idempotent(同 PK 不重複插)
  - User-Agent header 確實送出(TDCC 教訓)
  - normalize_date 民國 / 西元雙格式(連寫 + 分隔符)
  - HTTP error / JSON parse fail → raise(silent skip 是禁區)
  - baseline 偵測:TWSE punish + TWT85U 同時 0 rows → raise(endpoint 整體壞掉)
"""
from __future__ import annotations

import pytest

from scripts import fetch_stock_warnings as fetcher
from src import config, database as db


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """每個測試一份乾淨 DB(走 production schema,不自編 CREATE TABLE)。"""
    db_file = tmp_path / "warnings_test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    yield db_file
    db._reset_path_cache()  # type: ignore[attr-defined]


# ============================================================================
# normalize_date — 民國 / 西元 / 連寫 / 分隔符
# ============================================================================

def test_normalize_date_roc_compact():
    """民國連寫 YYYMMDD → ISO(TWSE OpenAPI punish.Date 用此格式)。"""
    assert fetcher.normalize_date("1150506") == "2026-05-06"
    assert fetcher.normalize_date("1140131") == "2025-01-31"


def test_normalize_date_ad_compact():
    """西元連寫 YYYYMMDD → ISO。"""
    assert fetcher.normalize_date("20260506") == "2026-05-06"


def test_normalize_date_roc_with_separator():
    assert fetcher.normalize_date("114/05/12") == "2025-05-12"
    assert fetcher.normalize_date("民國 114 年 05 月 12 日") == "2025-05-12"
    assert fetcher.normalize_date("114年5月3日") == "2025-05-03"


def test_normalize_date_western_with_separator():
    assert fetcher.normalize_date("2026-05-12") == "2026-05-12"
    assert fetcher.normalize_date("2026/05/12") == "2026-05-12"


def test_normalize_date_invalid_returns_none():
    assert fetcher.normalize_date("") is None
    assert fetcher.normalize_date(None) is None
    assert fetcher.normalize_date("not a date") is None


def test_normalize_tpex_date_alias():
    """normalize_tpex_date 是 normalize_date 的 alias,行為一致。"""
    assert fetcher.normalize_tpex_date("1150514") == "2026-05-14"
    assert fetcher.normalize_tpex_date("20250131") == "2025-01-31"
    assert fetcher.normalize_tpex_date("114/05/14") == "2025-05-14"
    assert fetcher.normalize_tpex_date(None) is None


def test_extract_stock_id_various_formats():
    assert fetcher._extract_stock_id("2330") == "2330"
    assert fetcher._extract_stock_id("2330 台積電") == "2330"
    assert fetcher._extract_stock_id("(2330) 台積電") == "2330"
    assert fetcher._extract_stock_id("00878") == "00878"
    assert fetcher._extract_stock_id("無代號文字") is None
    assert fetcher._extract_stock_id("") is None


def test_parse_period_handles_tilde_and_separator():
    """處置期 "1150515~1150528" / "115/05/07～115/05/20" 都要支援。"""
    eff_from, eff_to = fetcher._parse_period("1150515~1150528")
    assert eff_from == "2026-05-15"
    assert eff_to == "2026-05-28"
    eff_from, eff_to = fetcher._parse_period("115/05/07～115/05/20")
    assert eff_from == "2026-05-07"
    assert eff_to == "2026-05-20"


def test_parse_period_invalid():
    assert fetcher._parse_period(None) == (None, None)
    assert fetcher._parse_period("") == (None, None)


# ============================================================================
# TWSE OpenAPI JSON parsers
# ============================================================================

_TWSE_PUNISH_JSON = """[
  {
    "Number": "1",
    "Date": "1150506",
    "Code": "1597",
    "Name": "直得",
    "NumberOfAnnouncement": "1",
    "ReasonsOfDisposition": "連續三次",
    "DispositionPeriod": "115/05/07～115/05/20",
    "DispositionMeasures": "第一次處置",
    "Detail": "處置內容文字...",
    "LinkInformation": "備註"
  },
  {
    "Number": "2",
    "Date": "1150506",
    "Code": "2330",
    "Name": "台積電測試",
    "NumberOfAnnouncement": "1",
    "ReasonsOfDisposition": "連續六次",
    "DispositionPeriod": "115/05/07～115/05/20",
    "DispositionMeasures": "第二次處置",
    "Detail": "",
    "LinkInformation": ""
  }
]"""


def test_parse_twse_punish_extracts_rows():
    rows = fetcher.parse_twse_punish_json(
        _TWSE_PUNISH_JSON, source_url=fetcher.URL_PUNISH,
    )
    assert len(rows) == 2
    by_sid = {r["stock_id"]: r for r in rows}
    assert "1597" in by_sid
    r = by_sid["1597"]
    assert r["warning_type"] == "disposition"
    # 民國 115/05/06 → 西元 2026-05-06
    assert r["announced_date"] == "2026-05-06"
    assert r["effective_from"] == "2026-05-07"
    assert r["effective_to"] == "2026-05-20"
    assert "連續三次" in r["reason"]
    assert "第一次處置" in r["reason"]
    assert r["source_url"] == fetcher.URL_PUNISH


def test_parse_twse_punish_skips_empty_placeholder():
    """假日 / 沒事件 sentinel row(Number='0',Code/Name 全空)應被 skip。"""
    sentinel = """[{"Number":"0","Date":"","Code":"","Name":"",
                    "NumberOfAnnouncement":"0","ReasonsOfDisposition":"",
                    "DispositionPeriod":"","DispositionMeasures":"",
                    "Detail":"","LinkInformation":""}]"""
    assert fetcher.parse_twse_punish_json(
        sentinel, source_url=fetcher.URL_PUNISH,
    ) == []


_TWSE_NOTICE_JSON = """[
  {
    "Number": "1",
    "Code": "9999",
    "Name": "注意測試",
    "NumberOfAnnouncement": "1",
    "TradingInfoForAttention": "當日週轉率異常",
    "Date": "1150514",
    "ClosingPrice": "100.00",
    "PE": "25.5"
  }
]"""


def test_parse_twse_notice_extracts_rows():
    rows = fetcher.parse_twse_notice_json(
        _TWSE_NOTICE_JSON, source_url=fetcher.URL_NOTICE,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["stock_id"] == "9999"
    assert r["warning_type"] == "attention"
    assert r["announced_date"] == "2026-05-14"
    assert r["effective_from"] == "2026-05-14"
    assert r["effective_to"] is None
    assert "週轉率" in r["reason"]


def test_parse_twse_notice_holiday_sentinel_returns_empty():
    """notice 假日 / 沒事件回 1 筆 sentinel(Code='' Name='')→ 應 skip。"""
    sentinel = """[{"Number":"0","Code":"","Name":"","NumberOfAnnouncement":"0",
                    "TradingInfoForAttention":"","Date":"","ClosingPrice":"0","PE":"0"}]"""
    assert fetcher.parse_twse_notice_json(
        sentinel, source_url=fetcher.URL_NOTICE,
    ) == []


_TWSE_NOTETRANS_JSON = """[
  {
    "Code": "6449",
    "Name": "鈺邦",
    "RecentlyMetAttentionSecuritiesCriteria": "115年5月14日至115年5月15日連續二次"
  },
  {
    "Code": "3035",
    "Name": "智原",
    "RecentlyMetAttentionSecuritiesCriteria": "連續三次但找不到日期"
  }
]"""


def test_parse_twse_notetrans_extracts_dates_from_criteria_text():
    """notetrans 沒 Date 欄位,從 criteria 文字抽日期(取最後一個 = 最近一次達標)。"""
    rows = fetcher.parse_twse_notetrans_json(
        _TWSE_NOTETRANS_JSON, source_url=fetcher.URL_NOTETRANS,
        fallback_date="2026-05-16",
    )
    assert len(rows) == 2
    by_sid = {r["stock_id"]: r for r in rows}
    # criteria 內有 2 個日期,取最後一個 = "115年5月15日" → 2026-05-15
    assert by_sid["6449"]["announced_date"] == "2026-05-15"
    assert by_sid["6449"]["warning_type"] == "attention"
    # 找不到日期 → fallback
    assert by_sid["3035"]["announced_date"] == "2026-05-16"


_TWSE_TWT85U_JSON = """[
  {"Code": "1213", "Name": "大飲", "PeriodicCallAuctionTrading": "  "},
  {"Code": "2314", "Name": "台揚", "PeriodicCallAuctionTrading": "**"}
]"""


def test_parse_twse_method_changed_extracts_rows():
    rows = fetcher.parse_twse_method_changed_json(
        _TWSE_TWT85U_JSON, source_url=fetcher.URL_METHOD_CHANGED,
        fallback_date="2026-05-16",
    )
    assert len(rows) == 2
    by_sid = {r["stock_id"]: r for r in rows}
    # 全部歸 method_changed(TWT85U 欄位陽春,無法區分 full_cash)
    assert by_sid["1213"]["warning_type"] == "method_changed"
    assert by_sid["2314"]["warning_type"] == "method_changed"
    # fallback_date 套到 announced
    assert by_sid["1213"]["announced_date"] == "2026-05-16"
    # 分盤標記反映到 reason
    assert "分盤" in by_sid["2314"]["reason"]
    assert "分盤" not in by_sid["1213"]["reason"]


def test_parse_twse_method_changed_skips_empty_code():
    bad = """[{"Code": "", "Name": "", "PeriodicCallAuctionTrading": "**"}]"""
    assert fetcher.parse_twse_method_changed_json(
        bad, source_url=fetcher.URL_METHOD_CHANGED,
        fallback_date="2026-05-16",
    ) == []


def test_twse_parsers_raise_on_malformed_json():
    """parse 失敗應該 raise(讓 CI exit 1 — 違約交割教訓)。"""
    with pytest.raises(Exception):  # JSONDecodeError 子類
        fetcher.parse_twse_punish_json(
            "this is not json", source_url=fetcher.URL_PUNISH,
        )
    with pytest.raises(Exception):
        fetcher.parse_twse_notice_json(
            "{", source_url=fetcher.URL_NOTICE,
        )


def test_twse_parsers_empty_input_returns_empty():
    """空字串 / 空 array → 回 [],不 raise(合理的 holiday 結果)。"""
    for fn, url in [
        (fetcher.parse_twse_punish_json, fetcher.URL_PUNISH),
        (fetcher.parse_twse_notice_json, fetcher.URL_NOTICE),
    ]:
        assert fn("", source_url=url) == []
        assert fn("[]", source_url=url) == []


# ============================================================================
# TPEx (上櫃) — OpenAPI v1 JSON parsers(原測試保留)
# ============================================================================

_TPEX_DISPOSITION_JSON = """[
  {
    "Date": "1150514",
    "SecuritiesCompanyCode": "4966",
    "CompanyName": "譜瑞-KY",
    "DispositionPeriod": "1150515~1150528",
    "DispositionReasons": "因連續3個營業日達本中心作業要點第四條第一項第一款",
    "DisposalCondition": "詳細處置條件文字"
  },
  {
    "Date": "1150514",
    "SecuritiesCompanyCode": "3236",
    "CompanyName": "千如",
    "DispositionPeriod": "1150515~1150528",
    "DispositionReasons": "處置事由 B",
    "DisposalCondition": "處置條件 B"
  }
]"""


def test_parse_tpex_disposition_extracts_rows():
    rows = fetcher.parse_tpex_disposition_json(
        _TPEX_DISPOSITION_JSON, source_url=fetcher.TPEX_URL_DISPOSITION,
    )
    assert len(rows) == 2
    by_sid = {r["stock_id"]: r for r in rows}
    assert "4966" in by_sid
    r = by_sid["4966"]
    assert r["warning_type"] == "disposition"
    assert r["announced_date"] == "2026-05-14"
    assert r["effective_from"] == "2026-05-15"
    assert r["effective_to"] == "2026-05-28"
    assert "連續3個營業日" in r["reason"]
    assert r["source_url"] == fetcher.TPEX_URL_DISPOSITION


_TPEX_ATTENTION_JSON = """[
  {
    "Date": "1150514",
    "SecuritiesCompanyCode": "3236",
    "CompanyName": "千如",
    "TradingInformation": "最近六個營業日(含當日)之累積週轉率為88.28%",
    "ClosePrice": "40.20",
    "PriceEarningRatio": "125.63"
  }
]"""


def test_parse_tpex_attention_extracts_row():
    rows = fetcher.parse_tpex_attention_json(
        _TPEX_ATTENTION_JSON, source_url=fetcher.TPEX_URL_ATTENTION,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["stock_id"] == "3236"
    assert r["warning_type"] == "attention"
    assert r["announced_date"] == "2026-05-14"
    assert r["effective_from"] == "2026-05-14"
    assert r["effective_to"] is None
    assert "週轉率" in r["reason"]


_TPEX_CMODE_JSON = """[
  {
    "Date": "1150515",
    "SecuritiesCompanyCode": "3064",
    "CompanyName": "泰偉",
    "AlteredTrading": "Ｙ",
    "PeriodicTrading": "",
    "ManagedStock": "",
    "MatchingFrequency": "",
    "SuspensionOfTrading": "",
    "FinancialAnnouncements": "Ｙ"
  },
  {
    "Date": "1150515",
    "SecuritiesCompanyCode": "9999",
    "CompanyName": "管理股測試",
    "AlteredTrading": "",
    "PeriodicTrading": "",
    "ManagedStock": "Ｙ",
    "MatchingFrequency": "",
    "SuspensionOfTrading": "",
    "FinancialAnnouncements": ""
  },
  {
    "Date": "1150515",
    "SecuritiesCompanyCode": "8888",
    "CompanyName": "停止交易測試",
    "AlteredTrading": "",
    "PeriodicTrading": "",
    "ManagedStock": "",
    "MatchingFrequency": "",
    "SuspensionOfTrading": "Ｙ",
    "FinancialAnnouncements": ""
  },
  {
    "Date": "1150515",
    "SecuritiesCompanyCode": "7777",
    "CompanyName": "全空跳過",
    "AlteredTrading": "",
    "PeriodicTrading": "",
    "ManagedStock": "",
    "MatchingFrequency": "",
    "SuspensionOfTrading": "",
    "FinancialAnnouncements": ""
  }
]"""


def test_parse_tpex_cmode_classifies_full_cash_vs_method_changed():
    rows = fetcher.parse_tpex_cmode_json(
        _TPEX_CMODE_JSON, source_url=fetcher.TPEX_URL_CMODE,
    )
    by_sid = {r["stock_id"]: r for r in rows}
    assert "7777" not in by_sid
    assert set(by_sid.keys()) == {"3064", "9999", "8888"}
    assert by_sid["3064"]["warning_type"] == "method_changed"
    assert "變更交易方法" in by_sid["3064"]["reason"]
    assert "財務報告未申報" in by_sid["3064"]["reason"]
    assert by_sid["9999"]["warning_type"] == "full_cash"
    assert "管理股票" in by_sid["9999"]["reason"]
    assert by_sid["8888"]["warning_type"] == "full_cash"
    assert "停止交易" in by_sid["8888"]["reason"]


def test_parse_tpex_cmode_accepts_halfwidth_y():
    half_width = _TPEX_CMODE_JSON.replace("Ｙ", "Y")
    rows = fetcher.parse_tpex_cmode_json(
        half_width, source_url=fetcher.TPEX_URL_CMODE,
    )
    by_sid = {r["stock_id"]: r for r in rows}
    assert by_sid["9999"]["warning_type"] == "full_cash"


def test_parse_tpex_empty_json_returns_empty_list():
    assert fetcher.parse_tpex_disposition_json(
        "", source_url=fetcher.TPEX_URL_DISPOSITION,
    ) == []
    assert fetcher.parse_tpex_disposition_json(
        "[]", source_url=fetcher.TPEX_URL_DISPOSITION,
    ) == []


def test_parse_tpex_malformed_json_raises():
    with pytest.raises(Exception):
        fetcher.parse_tpex_disposition_json(
            "this is not json", source_url=fetcher.TPEX_URL_DISPOSITION,
        )


def test_parse_tpex_skips_rows_with_missing_required_fields():
    bad = """[
      {"Date": "", "SecuritiesCompanyCode": "1234"},
      {"Date": "1150514", "SecuritiesCompanyCode": ""}
    ]"""
    assert fetcher.parse_tpex_disposition_json(
        bad, source_url=fetcher.TPEX_URL_DISPOSITION,
    ) == []


# ============================================================================
# End-to-end via html_overrides — 跳過真實 HTTP
# ============================================================================

def _twse_only_overrides() -> dict[str, str]:
    """只給 TWSE fixture,TPEx + MOPS 餵空隔離(免打真實 API)。

    違約交割兩條 endpoint(TPEx breach + TWSE BFIGTU)用 base URL key
    餵空,跳過真實 HTTP(orchestration 內部會優先吃 base URL key)。
    """
    return {
        fetcher.URL_PUNISH: _TWSE_PUNISH_JSON,
        fetcher.URL_NOTICE: _TWSE_NOTICE_JSON,
        fetcher.URL_NOTETRANS: _TWSE_NOTETRANS_JSON,
        fetcher.URL_METHOD_CHANGED: _TWSE_TWT85U_JSON,
        fetcher.TPEX_URL_ATTENTION: "[]",
        fetcher.TPEX_URL_DISPOSITION: "[]",
        fetcher.TPEX_URL_CMODE: "[]",
        fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS: "",
        # 違約交割兩條 endpoint:餵空 JSON / 空 dict 跳過真實 HTTP
        fetcher.URL_TWSE_BFIGTU_BASE: "",
        fetcher.URL_TPEX_BREACH_BASE: "",
    }


def test_run_writes_twse_rows_to_db(tmp_db):
    summary = fetcher.run(html_overrides=_twse_only_overrides())
    # TWSE: punish 2 (disposition) + notice 1 (attention) + notetrans 2 (attention)
    #       + TWT85U 2 (method_changed) = 7
    assert summary["rows_parsed"] == 7
    assert summary["rows_written"] == 7
    assert summary["by_type"]["disposition"] == 2
    assert summary["by_type"]["attention"] == 3  # notice 1 + notetrans 2
    assert summary["by_type"]["method_changed"] == 2

    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM stock_warnings"
        ).fetchone()["c"]
    assert n == 7


def test_run_idempotent_same_pk_no_dup(tmp_db):
    """同 fixture 跑兩次 → PK 防重,DB 內仍只有原本 7 筆。"""
    fetcher.run(html_overrides=_twse_only_overrides())
    fetcher.run(html_overrides=_twse_only_overrides())

    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM stock_warnings"
        ).fetchone()["c"]
    assert n == 7, "PK 防重失敗,出現重複 row"


def test_run_writes_twse_and_tpex_together(tmp_db):
    """TWSE + TPEx 同時餵 fixture,確認兩家警示都進 stock_warnings(共表)。"""
    overrides = {
        # TWSE
        fetcher.URL_PUNISH: _TWSE_PUNISH_JSON,
        fetcher.URL_NOTICE: _TWSE_NOTICE_JSON,
        fetcher.URL_NOTETRANS: _TWSE_NOTETRANS_JSON,
        fetcher.URL_METHOD_CHANGED: _TWSE_TWT85U_JSON,
        # TPEx
        fetcher.TPEX_URL_ATTENTION: _TPEX_ATTENTION_JSON,
        fetcher.TPEX_URL_DISPOSITION: _TPEX_DISPOSITION_JSON,
        fetcher.TPEX_URL_CMODE: _TPEX_CMODE_JSON,
        # MOPS 違約交割(餵空,免打真實 API)
        fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS: "",
        # 違約交割兩條 endpoint(餵空,免打真實 API)
        fetcher.URL_TWSE_BFIGTU_BASE: "",
        fetcher.URL_TPEX_BREACH_BASE: "",
    }
    summary = fetcher.run(html_overrides=overrides)
    # TWSE 7 + TPEx attention 1 + disposition 2 + cmode 3(7777 跳過) = 13
    assert summary["rows_parsed"] == 13
    # by_type 合併計數
    assert summary["by_type"]["disposition"] == 2 + 2  # TWSE + TPEx
    assert summary["by_type"]["attention"] == 3 + 1    # TWSE 3 + TPEx 1
    assert summary["by_type"]["method_changed"] == 2 + 1  # TWSE 2 + TPEx 1
    assert summary["by_type"]["full_cash"] == 2        # TPEx 管理 + 停止


# ============================================================================
# Baseline 偵測 — 兩條源同時 0 rows 必 raise
# ============================================================================

def test_baseline_raises_when_punish_and_twt85u_both_empty(tmp_db):
    """TWSE punish + TWT85U 同時 0 rows → raise(endpoint 整體壞掉防呆)。"""
    overrides = {
        fetcher.URL_PUNISH: "[]",          # baseline 0
        fetcher.URL_METHOD_CHANGED: "[]",  # baseline 0
        fetcher.URL_NOTICE: _TWSE_NOTICE_JSON,
        fetcher.URL_NOTETRANS: _TWSE_NOTETRANS_JSON,
        fetcher.TPEX_URL_ATTENTION: "[]",
        fetcher.TPEX_URL_DISPOSITION: "[]",
        fetcher.TPEX_URL_CMODE: "[]",
        fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS: "",
        fetcher.URL_TWSE_BFIGTU_BASE: "",
        fetcher.URL_TPEX_BREACH_BASE: "",
    }
    with pytest.raises(RuntimeError, match="baseline"):
        fetcher.run(html_overrides=overrides)


def test_baseline_passes_when_only_one_zero(tmp_db):
    """只有一條 baseline 0,另一條有資料 → 不 raise(可能假日某條沒事件)。"""
    overrides = {
        fetcher.URL_PUNISH: "[]",                       # baseline 0
        fetcher.URL_METHOD_CHANGED: _TWSE_TWT85U_JSON,  # baseline 仍有
        fetcher.URL_NOTICE: "[]",
        fetcher.URL_NOTETRANS: "[]",
        fetcher.TPEX_URL_ATTENTION: "[]",
        fetcher.TPEX_URL_DISPOSITION: "[]",
        fetcher.TPEX_URL_CMODE: "[]",
        fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS: "",
        fetcher.URL_TWSE_BFIGTU_BASE: "",
        fetcher.URL_TPEX_BREACH_BASE: "",
    }
    summary = fetcher.run(html_overrides=overrides)
    assert summary["rows_parsed"] == 2  # 來自 TWT85U


def test_http_error_raises(tmp_db, monkeypatch):
    """任一 source HTTP 失敗 → retry 後仍失敗 → raise(silent skip 是禁區)。"""
    def fake_http_get(url: str) -> str:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(fetcher, "_http_get", fake_http_get)
    with pytest.raises(Exception):
        fetcher.run()


# ============================================================================
# 其他保留測試
# ============================================================================

def test_user_agent_is_sent(monkeypatch):
    """確認 _http_get 帶 User-Agent header(TDCC 教訓:沒帶會被擋進 redirect loop)。"""
    captured: dict = {}

    class _FakeResp:
        text = "[]"

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return _FakeResp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    fetcher._http_get(fetcher.URL_PUNISH)
    assert captured["headers"] is not None
    ua = captured["headers"].get("User-Agent", "")
    assert "Mozilla" in ua, f"User-Agent 必填且應像瀏覽器 UA,實際:{ua}"


def test_db_schema_aligned_with_production(tmp_db):
    """fixture 用 db.init_db() 建表(不自編 CREATE TABLE),
    確認 stock_warnings 表 columns 跟 SCHEMA 一致(Lessons-Learned 條款)。
    """
    with db.get_conn() as conn:
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(stock_warnings)"
            ).fetchall()
        }
    expected = {
        "stock_id", "warning_type", "announced_date",
        "effective_from", "effective_to", "reason", "source_url", "fetched_at",
    }
    assert expected.issubset(cols), (
        f"stock_warnings schema 缺欄位,實有 {cols}"
    )


def test_sources_list_covers_twse_and_tpex():
    """確認 _SOURCES 表同時含 TWSE 與 TPEx 條目(防有人 commit 漏掉一邊)。"""
    markets = {market for _, _, _, market in fetcher._SOURCES}
    assert "TWSE" in markets
    assert "TPEx" in markets
    twse_count = sum(1 for _, _, _, m in fetcher._SOURCES if m == "TWSE")
    tpex_count = sum(1 for _, _, _, m in fetcher._SOURCES if m == "TPEx")
    # TWSE 至少 4 source(punish/notice/notetrans/twt85u)
    assert twse_count >= 4, f"TWSE source 數 < 4,實有 {twse_count}"
    assert tpex_count >= 3, f"TPEx source 數 < 3,實有 {tpex_count}"


def test_sources_all_use_openapi():
    """所有 source URL 都應該指向 OpenAPI v1(2026-05-16 bs4 → JSON 重構不再回退)。"""
    for label, url, parser, market in fetcher._SOURCES:
        assert "openapi" in url.lower() or "tpex.org.tw/openapi" in url.lower(), (
            f"{market} {label} URL 非 OpenAPI:{url} — "
            "bs4 HTML 已禁用(silent 0 rows 教訓)"
        )


# ============================================================================
# MOPS 違約交割 RSS — parser + 整合 (2026-05-16 加入)
# ============================================================================

# RSS XML fixture(模擬 mopsrss201001.xml 的最小子集)。
# 真實 RSS encoding='big5',測試用 fixture 解碼後字串(Python str)即可。
_MOPS_RSS_HAS_DEFAULT_SETTLEMENT = """<?xml version='1.0' encoding='big5'?>
<rss version='2.0'>
<channel>
<title>公開資訊觀測站重大訊息</title>
<item>
    <title>(1234)違約股-重大訊息</title>
    <link> <![CDATA[https://mopsov.twse.com.tw/mops/web/t05st02?co_id=1234&seq=1]]></link>
    <description> <![CDATA[公告本公司股票於民國115年5月15日發生違約交割事件詳如說明]]></description>
    <pubDate>Fri, 15 May 2026 14:30:00 +0800</pubDate>
</item>
<item>
    <title>(2330)台積電-重大訊息</title>
    <link> <![CDATA[https://mopsov.twse.com.tw/mops/web/t05st02?co_id=2330&seq=1]]></link>
    <description> <![CDATA[公告本公司董事會決議發放現金股利]]></description>
    <pubDate>Fri, 15 May 2026 10:00:00 +0800</pubDate>
</item>
<item>
    <title>(5678)違約測試二-重大訊息</title>
    <link> <![CDATA[https://mopsov.twse.com.tw/mops/web/t05st02?co_id=5678&seq=1]]></link>
    <description> <![CDATA[本公司澄清近期市場關於違約交割之相關報導]]></description>
    <pubDate>Thu, 14 May 2026 09:15:00 +0800</pubDate>
</item>
</channel>
</rss>"""


def test_parse_mops_default_settlement_filters_by_keyword():
    """只抓含「違約」關鍵字的 item,其他重大訊息 skip。"""
    rows = fetcher.parse_mops_default_settlement_rss(
        _MOPS_RSS_HAS_DEFAULT_SETTLEMENT,
        source_url=fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS,
    )
    assert len(rows) == 2
    sids = {r["stock_id"] for r in rows}
    assert sids == {"1234", "5678"}
    # 2330 不含違約關鍵字 → skip
    assert "2330" not in sids


def test_parse_mops_default_settlement_extracts_metadata():
    """檢查 warning_type / dates / source_url 都正確填入。"""
    rows = fetcher.parse_mops_default_settlement_rss(
        _MOPS_RSS_HAS_DEFAULT_SETTLEMENT,
        source_url=fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS,
    )
    by_sid = {r["stock_id"]: r for r in rows}
    r = by_sid["1234"]
    assert r["warning_type"] == "default_settlement"
    # pubDate "Fri, 15 May 2026 14:30:00 +0800" → date 2026-05-15
    assert r["announced_date"] == "2026-05-15"
    assert r["effective_from"] == "2026-05-15"
    assert r["effective_to"] is None
    assert "違約" in r["reason"]
    # source_url 應為 item 的 <link>(指向 MOPS 詳情頁)
    assert "co_id=1234" in r["source_url"]


def test_parse_mops_default_settlement_empty_xml_returns_empty():
    """空字串 / 無 item → 回 [](MOPS RSS 偶爾 502 / 空頁,不該 raise)。"""
    assert fetcher.parse_mops_default_settlement_rss(
        "", source_url=fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS,
    ) == []
    assert fetcher.parse_mops_default_settlement_rss(
        "<rss><channel></channel></rss>",
        source_url=fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS,
    ) == []


def test_parse_mops_default_settlement_skips_no_keyword():
    """item 無「違約」關鍵字 → skip,不誤抓其他重大訊息。"""
    xml = """<rss><channel>
    <item>
        <title>(1101)台泥-重大訊息</title>
        <link><![CDATA[https://example.com/x]]></link>
        <description><![CDATA[公告本公司115年第1季合併財務報告]]></description>
        <pubDate>Fri, 15 May 2026 10:00:00 +0800</pubDate>
    </item>
    </channel></rss>"""
    assert fetcher.parse_mops_default_settlement_rss(
        xml, source_url=fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS,
    ) == []


def test_parse_mops_default_settlement_fallback_date_when_pubdate_missing():
    """item 無 pubDate → 用 fallback_date(預設今天 UTC)。"""
    xml = """<rss><channel>
    <item>
        <title>(9999)違約測試-重大訊息</title>
        <link><![CDATA[https://example.com/x]]></link>
        <description><![CDATA[公告違約交割情事]]></description>
    </item>
    </channel></rss>"""
    rows = fetcher.parse_mops_default_settlement_rss(
        xml, source_url=fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS,
        fallback_date="2026-05-16",
    )
    assert len(rows) == 1
    assert rows[0]["announced_date"] == "2026-05-16"


def test_run_writes_mops_default_settlement_rows(tmp_db):
    """整合測試:MOPS RSS fixture 進 stock_warnings 表,warning_type='default_settlement'。"""
    overrides = {
        # TWSE baseline 給最小資料避免 baseline raise
        fetcher.URL_PUNISH: _TWSE_PUNISH_JSON,
        fetcher.URL_NOTICE: "[]",
        fetcher.URL_NOTETRANS: "[]",
        fetcher.URL_METHOD_CHANGED: _TWSE_TWT85U_JSON,
        fetcher.TPEX_URL_ATTENTION: "[]",
        fetcher.TPEX_URL_DISPOSITION: "[]",
        fetcher.TPEX_URL_CMODE: "[]",
        # MOPS — 含 2 筆違約 + 1 筆 unrelated
        fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS: _MOPS_RSS_HAS_DEFAULT_SETTLEMENT,
        # 違約交割主來源餵空(只測 MOPS 輔助路徑)
        fetcher.URL_TWSE_BFIGTU_BASE: "",
        fetcher.URL_TPEX_BREACH_BASE: "",
    }
    summary = fetcher.run(html_overrides=overrides)
    assert summary["by_type"].get("default_settlement") == 2

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id FROM stock_warnings "
            "WHERE warning_type='default_settlement' ORDER BY stock_id"
        ).fetchall()
    assert [r["stock_id"] for r in rows] == ["1234", "5678"]


# ============================================================================
# TWSE BFIGTU + TPEx breach — 違約交割主來源(2026-05-17 加,3105 教訓)
# ============================================================================

# TWSE BFIGTU 真實 response 結構(僅每日彙總,無個股細目)。
_TWSE_BFIGTU_JSON = """{
  "stat": "OK",
  "flag": 104,
  "hints": "單位:元",
  "tables": [{
    "title": "115年04月20日至115年05月05日 證券商申報投資人違約金額",
    "fields": ["申報日期", "買進、賣出合計總金額", "買進、賣出相抵後金額"],
    "data": [
      ["115/04/27", "162,397,590", "12,448,690"],
      ["115/04/28", "63,008,045", "12,635,777"]
    ]
  }]
}"""


def test_parse_twse_bfigtu_daily_aggregate():
    """TWSE BFIGTU 只回每日彙總(無個股),寫 default_settlement_daily。"""
    rows = fetcher.parse_twse_bfigtu_json(
        _TWSE_BFIGTU_JSON, source_url=fetcher.URL_TWSE_BFIGTU_BASE,
    )
    assert len(rows) == 2
    by_date = {r["report_date"]: r for r in rows}
    assert "2026-04-27" in by_date
    r = by_date["2026-04-27"]
    assert r["market"] == "TWSE"
    assert r["gross_amount"] == 162_397_590
    assert r["net_amount"] == 12_448_690
    assert r["source_url"] == fetcher.URL_TWSE_BFIGTU_BASE


def test_parse_twse_bfigtu_empty_returns_empty():
    """空字串 / 沒資料 → 回 [](不 raise)。"""
    assert fetcher.parse_twse_bfigtu_json(
        "", source_url=fetcher.URL_TWSE_BFIGTU_BASE,
    ) == []
    assert fetcher.parse_twse_bfigtu_json(
        '{"stat":"查無資料","tables":[]}',
        source_url=fetcher.URL_TWSE_BFIGTU_BASE,
    ) == []


def test_parse_twse_bfigtu_malformed_raises():
    with pytest.raises(Exception):
        fetcher.parse_twse_bfigtu_json(
            "this is not json", source_url=fetcher.URL_TWSE_BFIGTU_BASE,
        )


# TPEx breach 真實 response(包含 4/27 3105 穩懋的歷史 case,本次 silent miss 教訓)
_TPEX_BREACH_JSON = """{
  "date": "20260420~20260510",
  "tables": [
    {
      "title": "證券商申報投資人違約金額",
      "date": "115/04/20~115/05/10",
      "totalCount": 6,
      "fields": ["申報日期", "類別", "買進、賣出合計總金額", "買進、賣出相抵後金額"],
      "data": [
        ["115/04/27", "上櫃", "5,000,000", "1,000,000"],
        ["115/04/27", "興櫃", "0", "0"],
        ["115/04/28", "上櫃", "3,500,000", "500,000"],
        ["115/04/28", "興櫃", "0", "0"]
      ]
    },
    {
      "title": "個股達違約資訊揭露標準(註1)之證券資訊",
      "totalCount": 3,
      "fields": ["申報日期", "證券名稱", "證券代號", "證券商名稱", "個股違約總金額(註1)"],
      "data": [
        ["115/04/27", "穩懋", "3105", "新光桃園<br>國泰敦南<br>群益永和<br>永豐羅東<br>永豐天母", "96,696,250"],
        ["115/04/27", "信驊", "5274", "玉山景美", "32,835,000"],
        ["115/04/28", "穩懋", "3105", "群益永和", "86,697,000"]
      ]
    }
  ]
}"""


def test_parse_tpex_breach_extracts_per_stock_default_settlement():
    """TPEx breach 第 2 表個股細目 → stock_warnings(default_settlement)。

    這條測試直接驗 3105 4/27-4/28 違約 case(silent miss bug 教訓 root cause)。
    """
    per_stock, daily = fetcher.parse_tpex_breach_json(
        _TPEX_BREACH_JSON, source_url=fetcher.URL_TPEX_BREACH_BASE,
    )
    assert len(per_stock) == 3
    sids_dates = {(r["stock_id"], r["announced_date"]) for r in per_stock}
    # 3105 必中(4/27 + 4/28)
    assert ("3105", "2026-04-27") in sids_dates
    assert ("3105", "2026-04-28") in sids_dates
    assert ("5274", "2026-04-27") in sids_dates

    r_3105_427 = next(
        r for r in per_stock
        if r["stock_id"] == "3105" and r["announced_date"] == "2026-04-27"
    )
    assert r_3105_427["warning_type"] == "default_settlement"
    assert r_3105_427["effective_from"] == "2026-04-27"
    # 金額換算 0.97 億 → reason 含「億」
    assert "0.97 億" in r_3105_427["reason"] or "9," in r_3105_427["reason"]
    # 多家證券商 <br> 應替換成 ' / ' 而非保留 HTML
    assert "<br>" not in r_3105_427["reason"]


def test_parse_tpex_breach_extracts_daily_aggregate():
    """TPEx breach 第 1 表每日彙總 → default_settlement_daily(market 對應)。"""
    _, daily = fetcher.parse_tpex_breach_json(
        _TPEX_BREACH_JSON, source_url=fetcher.URL_TPEX_BREACH_BASE,
    )
    assert len(daily) == 4  # 上櫃 x 2 日 + 興櫃 x 2 日
    by_key = {(r["market"], r["report_date"]): r for r in daily}
    assert ("TPEX_LISTED", "2026-04-27") in by_key
    assert ("TPEX_EMERGING", "2026-04-27") in by_key
    r_listed = by_key[("TPEX_LISTED", "2026-04-27")]
    assert r_listed["gross_amount"] == 5_000_000
    assert r_listed["net_amount"] == 1_000_000


def test_parse_tpex_breach_empty_returns_empty_tuples():
    per_stock, daily = fetcher.parse_tpex_breach_json(
        "", source_url=fetcher.URL_TPEX_BREACH_BASE,
    )
    assert per_stock == [] and daily == []


def test_parse_tpex_breach_malformed_raises():
    with pytest.raises(Exception):
        fetcher.parse_tpex_breach_json(
            "this is not json", source_url=fetcher.URL_TPEX_BREACH_BASE,
        )


def test_run_writes_tpex_breach_3105_to_stock_warnings(tmp_db):
    """整合測試 — TPEx breach fixture 含 3105 → stock_warnings 真的寫入。

    這是 silent miss bug 的 regression guard:跑完 run() 後 DB 內必有 3105
    違約紀錄(announced_date=2026-04-27/28)。
    """
    overrides = {
        # 保底 baseline
        fetcher.URL_PUNISH: _TWSE_PUNISH_JSON,
        fetcher.URL_NOTICE: "[]",
        fetcher.URL_NOTETRANS: "[]",
        fetcher.URL_METHOD_CHANGED: _TWSE_TWT85U_JSON,
        fetcher.TPEX_URL_ATTENTION: "[]",
        fetcher.TPEX_URL_DISPOSITION: "[]",
        fetcher.TPEX_URL_CMODE: "[]",
        fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS: "",
        # 違約主來源
        fetcher.URL_TWSE_BFIGTU_BASE: _TWSE_BFIGTU_JSON,
        fetcher.URL_TPEX_BREACH_BASE: _TPEX_BREACH_JSON,
    }
    summary = fetcher.run(html_overrides=overrides)
    # 至少 3 筆個股違約(3105 x2 + 5274 x1)
    assert summary["by_type"].get("default_settlement", 0) >= 3
    # default_settlement_daily 寫了 6 筆(TWSE 2 + TPEX_LISTED 2 + TPEX_EMERGING 2)
    assert summary["daily_rows_written"] == 6

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id, announced_date FROM stock_warnings "
            "WHERE stock_id='3105' AND warning_type='default_settlement' "
            "ORDER BY announced_date"
        ).fetchall()
    dates = [r["announced_date"] for r in rows]
    assert dates == ["2026-04-27", "2026-04-28"], (
        f"3105 違約交割 silent miss bug regression — DB 內未找到預期日期:{dates}"
    )

    # 驗 default_settlement_daily 表也有 3 個 market 的 4/27 row
    with db.get_conn() as conn:
        daily_rows = conn.execute(
            "SELECT market, gross_amount FROM default_settlement_daily "
            "WHERE report_date='2026-04-27' ORDER BY market"
        ).fetchall()
    markets = {r["market"] for r in daily_rows}
    assert {"TWSE", "TPEX_LISTED", "TPEX_EMERGING"} == markets


def test_run_default_settlement_idempotent_no_dup(tmp_db):
    """同 fixture 跑兩次 → stock_warnings 與 default_settlement_daily 都不重複插。"""
    overrides = {
        fetcher.URL_PUNISH: _TWSE_PUNISH_JSON,
        fetcher.URL_NOTICE: "[]",
        fetcher.URL_NOTETRANS: "[]",
        fetcher.URL_METHOD_CHANGED: _TWSE_TWT85U_JSON,
        fetcher.TPEX_URL_ATTENTION: "[]",
        fetcher.TPEX_URL_DISPOSITION: "[]",
        fetcher.TPEX_URL_CMODE: "[]",
        fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS: "",
        fetcher.URL_TWSE_BFIGTU_BASE: _TWSE_BFIGTU_JSON,
        fetcher.URL_TPEX_BREACH_BASE: _TPEX_BREACH_JSON,
    }
    fetcher.run(html_overrides=overrides)
    fetcher.run(html_overrides=overrides)

    with db.get_conn() as conn:
        n_warn = conn.execute(
            "SELECT COUNT(*) AS c FROM stock_warnings "
            "WHERE warning_type='default_settlement'"
        ).fetchone()["c"]
        n_daily = conn.execute(
            "SELECT COUNT(*) AS c FROM default_settlement_daily"
        ).fetchone()["c"]
    assert n_warn == 3, f"default_settlement PK 防重失敗,有 {n_warn} 筆"
    assert n_daily == 6, f"default_settlement_daily PK 防重失敗,有 {n_daily} 筆"


def test_twse_bfigtu_url_format():
    """URL 組裝:YYYYMMDD 連寫(無分隔)、含 response=json。"""
    url = fetcher._twse_bfigtu_url("2026-04-01", "2026-05-17")
    assert "startDate=20260401" in url
    assert "endDate=20260517" in url
    assert "response=json" in url


def test_tpex_breach_url_format():
    """URL 組裝:YYYY/MM/DD 斜線、含 response=json。"""
    url = fetcher._tpex_breach_url("2026-04-01", "2026-05-17")
    assert "startDate=2026/04/01" in url
    assert "endDate=2026/05/17" in url
    assert "response=json" in url


def test_default_settlement_db_schema_has_required_columns(tmp_db):
    """default_settlement_daily schema 對齊 production(防有人 commit 漏 migration)。"""
    with db.get_conn() as conn:
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(default_settlement_daily)"
            ).fetchall()
        }
    expected = {
        "market", "report_date", "gross_amount", "net_amount",
        "source_url", "fetched_at",
    }
    assert expected.issubset(cols), (
        f"default_settlement_daily schema 缺欄位,實有 {cols}"
    )


def test_mops_baseline_zero_does_not_raise(tmp_db):
    """MOPS 0 rows 屬正常(違約事件年數筆),不該觸發 baseline raise。"""
    overrides = {
        fetcher.URL_PUNISH: _TWSE_PUNISH_JSON,
        fetcher.URL_NOTICE: "[]",
        fetcher.URL_NOTETRANS: "[]",
        fetcher.URL_METHOD_CHANGED: _TWSE_TWT85U_JSON,
        fetcher.TPEX_URL_ATTENTION: "[]",
        fetcher.TPEX_URL_DISPOSITION: "[]",
        fetcher.TPEX_URL_CMODE: "[]",
        fetcher.URL_MOPS_DEFAULT_SETTLEMENT_RSS: "",  # 0 rows
        fetcher.URL_TWSE_BFIGTU_BASE: "",
        fetcher.URL_TPEX_BREACH_BASE: "",
    }
    # 不該 raise(MOPS 不在 _BASELINE_URLS 內)
    summary = fetcher.run(html_overrides=overrides)
    assert summary["by_type"].get("default_settlement", 0) == 0
