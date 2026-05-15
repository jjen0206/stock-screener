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
