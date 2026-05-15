"""scripts/fetch_stock_warnings.py 單元測試。

涵蓋:
  - schema 對齊 production(用 db.init_db() 建表,不自編 CREATE TABLE)
  - 模擬 HTML fixture 餵 parser,驗 stock_id / warning_type / dates 解析正確
  - 重複 fetch idempotent(同 PK 不重複插)
  - User-Agent header 確實送出(TDCC 教訓)
  - normalize_date 民國 / 西元雙格式
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
# normalize_date
# ============================================================================

def test_normalize_date_roc_year():
    assert fetcher.normalize_date("114/05/12") == "2025-05-12"
    assert fetcher.normalize_date("民國 114 年 05 月 12 日") == "2025-05-12"
    assert fetcher.normalize_date("114年5月3日") == "2025-05-03"


def test_normalize_date_western_year():
    assert fetcher.normalize_date("2026-05-12") == "2026-05-12"
    assert fetcher.normalize_date("2026/05/12") == "2026-05-12"


def test_normalize_date_invalid_returns_none():
    assert fetcher.normalize_date("") is None
    assert fetcher.normalize_date(None) is None
    assert fetcher.normalize_date("not a date") is None


def test_extract_stock_id_various_formats():
    assert fetcher._extract_stock_id("2330") == "2330"
    assert fetcher._extract_stock_id("2330 台積電") == "2330"
    assert fetcher._extract_stock_id("(2330) 台積電") == "2330"
    assert fetcher._extract_stock_id("00878") == "00878"
    assert fetcher._extract_stock_id("無代號文字") is None
    assert fetcher._extract_stock_id("") is None


# ============================================================================
# parser fixtures
# ============================================================================

_DEFAULT_SETTLEMENT_HTML = """
<html><body>
<table>
  <thead>
    <tr><th>公告日期</th><th>證券代號</th><th>證券名稱</th><th>說明</th></tr>
  </thead>
  <tbody>
    <tr><td>114/05/12</td><td>9999</td><td>違約測試</td><td>違約交割金額 NT$1,000,000</td></tr>
    <tr><td>114/05/14</td><td>8888</td><td>另一檔</td><td>違約交割金額 NT$500,000</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_parse_default_settlement_extracts_rows():
    rows = fetcher.parse_default_settlement_html(
        _DEFAULT_SETTLEMENT_HTML, source_url=fetcher.URL_PUNISH,
    )
    assert len(rows) == 2
    by_sid = {r["stock_id"]: r for r in rows}
    assert "9999" in by_sid
    assert by_sid["9999"]["warning_type"] == "default_settlement"
    assert by_sid["9999"]["announced_date"] == "2025-05-12"
    # 違約交割沒有解除日 → effective_to 應為 None
    assert by_sid["9999"]["effective_to"] is None
    assert "違約" in by_sid["9999"]["reason"]
    assert by_sid["9999"]["source_url"] == fetcher.URL_PUNISH


_ATTENTION_HTML = """
<html><body>
<table>
  <thead>
    <tr><th>公告日期</th><th>處置起</th><th>處置迄</th><th>證券代號</th><th>證券名稱</th><th>事由</th></tr>
  </thead>
  <tbody>
    <tr><td>114/05/01</td><td>114/05/02</td><td>114/05/12</td><td>7777</td><td>注意股</td><td>當日週轉率異常</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_parse_attention_with_effective_dates():
    rows = fetcher.parse_attention_html(
        _ATTENTION_HTML, source_url=fetcher.URL_NOTICE,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["stock_id"] == "7777"
    assert r["warning_type"] == "attention"
    assert r["announced_date"] == "2025-05-01"
    assert r["effective_from"] == "2025-05-02"
    assert r["effective_to"] == "2025-05-12"


_DISPOSITION_HTML = """
<html><body>
<table>
  <thead>
    <tr><th>公告日</th><th>處置起</th><th>處置迄</th><th>股票代號</th><th>原因</th></tr>
  </thead>
  <tbody>
    <tr><td>2026/05/13</td><td>2026/05/14</td><td>2026/05/24</td><td>6666</td><td>連續 5 日達公布注意交易資訊</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_parse_disposition_western_dates():
    rows = fetcher.parse_disposition_html(
        _DISPOSITION_HTML, source_url=fetcher.URL_DISPOSITION,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["stock_id"] == "6666"
    assert r["warning_type"] == "disposition"
    assert r["announced_date"] == "2026-05-13"
    assert r["effective_to"] == "2026-05-24"


_METHOD_HTML = """
<html><body>
<table>
  <thead>
    <tr><th>公告日期</th><th>生效日</th><th>解除日</th><th>證券代號</th><th>說明</th></tr>
  </thead>
  <tbody>
    <tr><td>114/05/10</td><td>114/05/11</td><td></td><td>5555</td><td>變更交易方法為全額交割</td></tr>
    <tr><td>114/05/10</td><td>114/05/11</td><td>114/06/10</td><td>4444</td><td>變更交易方法 - 限制信用交易</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_parse_method_changed_classifies_full_cash_vs_other():
    rows = fetcher.parse_method_changed_html(
        _METHOD_HTML, source_url=fetcher.URL_METHOD_CHANGED,
    )
    by_sid = {r["stock_id"]: r for r in rows}
    # 「全額交割」關鍵字 → full_cash(picks 硬擋)
    assert by_sid["5555"]["warning_type"] == "full_cash"
    # 沒有「全額交割」關鍵字 → method_changed(soft 降權)
    assert by_sid["4444"]["warning_type"] == "method_changed"
    # full_cash 沒解除日 → effective_to None(視為仍生效)
    assert by_sid["5555"]["effective_to"] is None
    # method_changed 有解除日
    assert by_sid["4444"]["effective_to"] == "2025-06-10"


def test_parser_skips_rows_with_missing_required_fields():
    bad_html = """<html><body><table>
      <thead><tr><th>公告日期</th><th>說明</th></tr></thead>
      <tbody><tr><td>114/05/12</td><td>沒代號的列</td></tr></tbody>
    </table></body></html>"""
    rows = fetcher.parse_default_settlement_html(
        bad_html, source_url=fetcher.URL_PUNISH,
    )
    assert rows == []


def test_parser_handles_empty_html():
    assert fetcher.parse_default_settlement_html(
        "", source_url=fetcher.URL_PUNISH,
    ) == []
    assert fetcher.parse_default_settlement_html(
        "<html><body><p>No tables</p></body></html>",
        source_url=fetcher.URL_PUNISH,
    ) == []


# ============================================================================
# run() 端到端 — 用 html_overrides 注入 fixture,跳過真實 HTTP
# ============================================================================

def test_run_writes_rows_to_db(tmp_db):
    overrides = {
        fetcher.URL_PUNISH: _DEFAULT_SETTLEMENT_HTML,
        fetcher.URL_NOTICE: _ATTENTION_HTML,
        fetcher.URL_DISPOSITION: _DISPOSITION_HTML,
        fetcher.URL_METHOD_CHANGED: _METHOD_HTML,
        # TPEx 沒給 fixture 就會打真實 API,測試會 flaky → 餵空 list 隔離
        fetcher.TPEX_URL_ATTENTION: "[]",
        fetcher.TPEX_URL_DISPOSITION: "[]",
        fetcher.TPEX_URL_CMODE: "[]",
    }
    summary = fetcher.run(html_overrides=overrides)
    # default 2 + attention 1 + disposition 1 + full_cash 1 + method_changed 1 = 6
    assert summary["rows_parsed"] == 6
    assert summary["rows_written"] == 6
    assert summary["by_type"]["default_settlement"] == 2
    assert summary["by_type"]["attention"] == 1
    assert summary["by_type"]["disposition"] == 1
    assert summary["by_type"]["full_cash"] == 1
    assert summary["by_type"]["method_changed"] == 1

    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM stock_warnings").fetchone()["c"]
    assert n == 6


def test_run_idempotent_same_pk_no_dup(tmp_db):
    """同 fixture 跑兩次 → PK (stock_id, warning_type, announced_date) 防重,
    DB 內仍只有原本 6 筆(覆蓋更新而非新增)。
    """
    overrides = {
        fetcher.URL_PUNISH: _DEFAULT_SETTLEMENT_HTML,
        fetcher.URL_NOTICE: _ATTENTION_HTML,
        fetcher.URL_DISPOSITION: _DISPOSITION_HTML,
        fetcher.URL_METHOD_CHANGED: _METHOD_HTML,
        fetcher.TPEX_URL_ATTENTION: "[]",
        fetcher.TPEX_URL_DISPOSITION: "[]",
        fetcher.TPEX_URL_CMODE: "[]",
    }
    fetcher.run(html_overrides=overrides)
    fetcher.run(html_overrides=overrides)  # 第二次跑

    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM stock_warnings").fetchone()["c"]
    assert n == 6, "PK 防重失敗,出現重複 row"


def test_disposition_fallback_when_main_url_missing(tmp_db, monkeypatch):
    """主 disposition URL 抓不到 → fallback URL_DISPOSITION_FALLBACK 應該被使用。

    用 monkeypatch 模擬主 URL 抓 fail、fallback 成功。
    """
    call_log: list[str] = []

    def fake_http_get(url: str) -> str:
        call_log.append(url)
        if url == fetcher.URL_DISPOSITION:
            raise RuntimeError("主端點 404")
        if url == fetcher.URL_DISPOSITION_FALLBACK:
            return _DISPOSITION_HTML
        return ""  # 其他 URL 回空 HTML(不會解出 row)

    monkeypatch.setattr(fetcher, "_http_get", fake_http_get)
    summary = fetcher.run()
    # disposition 應該被解析到(來自 fallback)
    assert summary["by_type"].get("disposition") == 1
    # call log 確認兩個 URL 都被叫到
    assert fetcher.URL_DISPOSITION in call_log
    assert fetcher.URL_DISPOSITION_FALLBACK in call_log


def test_user_agent_is_sent(monkeypatch):
    """確認 _http_get 帶 User-Agent header(TDCC 教訓:沒帶會被擋進 redirect loop)。"""
    captured: dict = {}

    class _FakeResp:
        text = "<html></html>"

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return _FakeResp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    fetcher._http_get("https://www.twse.com.tw/zh/announcement/punish.html")
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


# ============================================================================
# TPEx (上櫃) — OpenAPI v1 JSON parsers
# ============================================================================

def test_normalize_tpex_date_roc_compact():
    """民國連寫 YYYMMDD → ISO。TPEx disposal / attention / cmode 用此格式。"""
    assert fetcher.normalize_tpex_date("1150514") == "2026-05-14"
    assert fetcher.normalize_tpex_date("1140131") == "2025-01-31"


def test_normalize_tpex_date_ad_compact():
    """西元連寫 YYYYMMDD → ISO。TPEx warning_note 用此格式。"""
    assert fetcher.normalize_tpex_date("20260514") == "2026-05-14"
    assert fetcher.normalize_tpex_date("20250131") == "2025-01-31"


def test_normalize_tpex_date_falls_back_to_normalize_date():
    """有分隔符格式走 fallback,跟 TWSE 通用 normalizer 結果一致。"""
    assert fetcher.normalize_tpex_date("114/05/14") == "2025-05-14"
    assert fetcher.normalize_tpex_date("2026-05-14") == "2026-05-14"


def test_normalize_tpex_date_invalid():
    assert fetcher.normalize_tpex_date(None) is None
    assert fetcher.normalize_tpex_date("") is None
    assert fetcher.normalize_tpex_date("not a date") is None


def test_parse_tpex_period_tilde():
    """處置期 "1150515~1150528" → (起, 迄)。"""
    eff_from, eff_to = fetcher._parse_tpex_period("1150515~1150528")
    assert eff_from == "2026-05-15"
    assert eff_to == "2026-05-28"


def test_parse_tpex_period_invalid():
    assert fetcher._parse_tpex_period(None) == (None, None)
    assert fetcher._parse_tpex_period("") == (None, None)


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
    # 民國 114/05/14 → 西元 2025-05-14(不是 2026 — 重要 normalize 檢查)
    assert r["announced_date"] == "2026-05-14", (
        "民國 115 = 西元 2026 — normalize_tpex_date 要正確 +1911"
    )
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
    # attention endpoint 沒處置期 → effective_from 補公告日,effective_to None
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
    # 4 筆中 1 筆全空 → 跳過,只剩 3 筆
    assert "7777" not in by_sid, "全空 flag 不該寫入"
    assert set(by_sid.keys()) == {"3064", "9999", "8888"}

    # AlteredTrading=Ｙ + FinancialAnnouncements=Ｙ → method_changed(soft)
    assert by_sid["3064"]["warning_type"] == "method_changed"
    assert "變更交易方法" in by_sid["3064"]["reason"]
    assert "財務報告未申報" in by_sid["3064"]["reason"]

    # ManagedStock=Ｙ → full_cash(hard block,管理股票 = TWSE 全額交割等價)
    assert by_sid["9999"]["warning_type"] == "full_cash"
    assert "管理股票" in by_sid["9999"]["reason"]

    # SuspensionOfTrading=Ｙ → full_cash(picks 不該推已停交易股)
    assert by_sid["8888"]["warning_type"] == "full_cash"
    assert "停止交易" in by_sid["8888"]["reason"]


def test_parse_tpex_cmode_accepts_halfwidth_y():
    """半形 Y 也應該被認為是旗標(防 TPEx 改字)。"""
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
    assert fetcher.parse_tpex_attention_json(
        "[]", source_url=fetcher.TPEX_URL_ATTENTION,
    ) == []
    assert fetcher.parse_tpex_cmode_json(
        "[]", source_url=fetcher.TPEX_URL_CMODE,
    ) == []


def test_parse_tpex_malformed_json_raises():
    """parse 失敗應該 raise(讓 CI exit 1,別 silent skip — 違約交割教訓)。"""
    with pytest.raises(Exception):  # JSONDecodeError 子類
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
    assert fetcher.parse_tpex_attention_json(
        bad, source_url=fetcher.TPEX_URL_ATTENTION,
    ) == []


# ============================================================================
# 端到端:TWSE + TPEx 同時跑
# ============================================================================

def test_run_writes_tpex_rows_to_db(tmp_db):
    """TWSE + TPEx 一起餵 fixture,確認兩家警示都進 stock_warnings(共表)。"""
    overrides = {
        # TWSE — 沿用上面 TWSE fixture
        fetcher.URL_PUNISH: _DEFAULT_SETTLEMENT_HTML,
        fetcher.URL_NOTICE: _ATTENTION_HTML,
        fetcher.URL_DISPOSITION: _DISPOSITION_HTML,
        fetcher.URL_METHOD_CHANGED: _METHOD_HTML,
        # TPEx
        fetcher.TPEX_URL_ATTENTION: _TPEX_ATTENTION_JSON,
        fetcher.TPEX_URL_DISPOSITION: _TPEX_DISPOSITION_JSON,
        fetcher.TPEX_URL_CMODE: _TPEX_CMODE_JSON,
    }
    summary = fetcher.run(html_overrides=overrides)
    # TWSE 6 (見上) + TPEx attention 1 + disposition 2 + cmode (3 非空) = 12
    assert summary["rows_parsed"] == 12
    assert summary["rows_written"] == 12
    # by_type 合併計數
    assert summary["by_type"]["default_settlement"] == 2  # TWSE only
    assert summary["by_type"]["attention"] == 1 + 1       # TWSE + TPEx
    assert summary["by_type"]["disposition"] == 1 + 2     # TWSE + TPEx
    # TPEx full_cash 2(管理 + 停止)+ TWSE full_cash 1 = 3
    assert summary["by_type"]["full_cash"] == 3
    # TPEx method_changed 1 + TWSE method_changed 1 = 2
    assert summary["by_type"]["method_changed"] == 2

    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM stock_warnings"
        ).fetchone()["c"]
    assert n == 12


def test_run_idempotent_with_tpex(tmp_db):
    """同 fixture 跑兩次 → TWSE + TPEx 都 PK 防重(共表測試)。"""
    overrides = {
        fetcher.URL_PUNISH: _DEFAULT_SETTLEMENT_HTML,
        fetcher.URL_NOTICE: _ATTENTION_HTML,
        fetcher.URL_DISPOSITION: _DISPOSITION_HTML,
        fetcher.URL_METHOD_CHANGED: _METHOD_HTML,
        fetcher.TPEX_URL_ATTENTION: _TPEX_ATTENTION_JSON,
        fetcher.TPEX_URL_DISPOSITION: _TPEX_DISPOSITION_JSON,
        fetcher.TPEX_URL_CMODE: _TPEX_CMODE_JSON,
    }
    fetcher.run(html_overrides=overrides)
    fetcher.run(html_overrides=overrides)
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM stock_warnings"
        ).fetchone()["c"]
    assert n == 12, "PK 防重失敗,TWSE/TPEx 共表出現重複"


def test_sources_list_covers_twse_and_tpex():
    """確認 _SOURCES 表同時含 TWSE 與 TPEx 條目(防有人 commit 漏掉一邊)。"""
    markets = {market for _, _, _, market in fetcher._SOURCES}
    assert "TWSE" in markets
    assert "TPEx" in markets
    # TPEx 至少要有 3 source(attention / disposition / cmode)
    tpex_count = sum(1 for _, _, _, m in fetcher._SOURCES if m == "TPEx")
    assert tpex_count >= 3, (
        f"TPEx source 數 < 3,實有 {tpex_count}(可能漏 endpoint)"
    )
