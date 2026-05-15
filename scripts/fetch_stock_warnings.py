"""TWSE / TPEx 警示股紀錄抓取 CLI(2026-05-15 主公拍板加入,違約交割教訓)。

抓取的警示分類(寫入 stock_warnings.warning_type):
  - default_settlement (違約交割) — TWSE punish 公告
  - attention         (注意股)   — TWSE notice 公告 + TPEx trading_warning_information
  - disposition       (處置股)   — TWSE punish/disposition(fallback TWTBAU2)
                                  + TPEx disposal_information
  - full_cash         (全額交割) — TWSE 變更交易方法 - 全額交割
                                  + TPEx cmode 之 ManagedStock=Ｙ(管理股票)
  - method_changed    (變更交易方法 - 其他) — 同一公告分支
                                  + TPEx cmode 之 AlteredTrading=Ｙ(未列為管理)

主要資料來源:
  TWSE (上市):
    - 違約交割 https://www.twse.com.tw/zh/announcement/punish.html
    - 注意股   https://www.twse.com.tw/zh/announcement/notice.html
    - 處置股   https://www.twse.com.tw/zh/announcement/punish/disposition.html
                fallback https://www.twse.com.tw/zh/trading/exchange/TWTBAU2.html
    - 變更交易方法 https://www.twse.com.tw/zh/announcement/method.html
  TPEx (上櫃):
    - 注意股   https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information
    - 處置股   https://www.tpex.org.tw/openapi/v1/tpex_disposal_information
    - 變更交易方法 https://www.tpex.org.tw/openapi/v1/tpex_cmode
    - 違約交割 :TPEx OpenAPI v1 沒對應 endpoint(2026-05 已 web 確認);
                只有 SPA 表單頁 /web/bulletin/announcement/default.php,需
                browser automation,違反專案禁止 selenium 原則 → 暫不抓,
                每次跑會 log warning 提醒(別 silent skip,違約交割教訓)。

設計原則(對齊 fetch_shareholder_concentration.py):
  - TWSE 純 HTTP requests + bs4 解析 HTML
  - TPEx 走官方 OpenAPI v1 JSON(穩定,不用 bs4)
  - 不依賴 selenium / playwright
  - User-Agent 必填(TDCC / TWSE / TPEx 都會擋 python-requests UA)
  - retry 3 次(走 src._retry.with_retry)
  - parse 失敗 raise + 寫 log,讓 CI exit 1 觸發告警
  - upsert 進 stock_warnings,同 PK (stock_id, warning_type, announced_date) 覆寫
    (TWSE / TPEx 共用同表,PK 已含 sid,4 開頭上市 vs 4 開頭上櫃理論上互斥不會撞)

Exit code:
  0 = 成功(寫入 0 筆也算成功)
  1 = 抓取或解析失敗
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src._retry import with_retry  # noqa: E402

logger = logging.getLogger(__name__)


# === HTTP setup ===
_HTTP_TIMEOUT = 30
# TWSE 的 SSL 憑證鏈缺 SubjectKeyIdentifier,新版 OpenSSL 會擋 → 用同 src/financial_fetcher_free.py
# 處理 pattern,公開資料 read-only 無 MITM 風險
_VERIFY_SSL = False
# python-requests 預設 UA 會被 TWSE 擋進 redirect loop / 403,必填常見瀏覽器 UA
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


# === 各來源 URL(可被測試 monkeypatch)===

URL_PUNISH = "https://www.twse.com.tw/zh/announcement/punish.html"
URL_NOTICE = "https://www.twse.com.tw/zh/announcement/notice.html"
URL_DISPOSITION = "https://www.twse.com.tw/zh/announcement/punish/disposition.html"
URL_DISPOSITION_FALLBACK = (
    "https://www.twse.com.tw/zh/trading/exchange/TWTBAU2.html"
)
URL_METHOD_CHANGED = "https://www.twse.com.tw/zh/announcement/method.html"

# TPEx (上櫃) — 走官方 OpenAPI v1 JSON,結構穩定;欄位以 swagger.json 為準。
# 違約交割 endpoint 不存在 OpenAPI(只有 SPA 表單頁),先不抓,run() 會 log warning。
TPEX_URL_DISPOSITION = (
    "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"
)
TPEX_URL_ATTENTION = (
    "https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information"
)
TPEX_URL_CMODE = "https://www.tpex.org.tw/openapi/v1/tpex_cmode"


# === HTTP fetch ===

def _http_get(url: str) -> str:
    """單次 GET — 抽出來讓測試可 monkeypatch / mock。

    SSL verify=False:TWSE 政府公開資料服務,跟既有 fetcher 同 pattern,
    無 MITM 風險(read-only 公開資料)。
    """
    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = requests.get(
        url, timeout=_HTTP_TIMEOUT, verify=_VERIFY_SSL, headers=_HTTP_HEADERS,
    )
    resp.raise_for_status()
    # TWSE 通常回 UTF-8;requests 預設 charset 偵測夠用
    return resp.text


def fetch_url_with_retry(url: str, label: str) -> str:
    """打 URL 拿原始 HTML / JSON 字串,失敗 retry 3 次(指數退避 1s/2s/4s)。

    給上層 parser 餵原始字串。網路 / 5xx 連續失敗會 raise,讓 CLI exit 1。
    """
    return with_retry(
        lambda: _http_get(url),
        max_attempts=3,
        base_delay=1.0,
        label=label,
    )


# === 共用解析 ===

# TWSE 公告的「日期」常見格式:
#   "民國 114 年 05 月 12 日" / "114/05/12" / "2025/05/12" / "2025-05-12"
_ROC_PATTERN = re.compile(r"^(\d{2,3})[/\-年]\s*(\d{1,2})[/\-月]\s*(\d{1,2})")
_AD_PATTERN = re.compile(r"^(\d{4})[/\-年]\s*(\d{1,2})[/\-月]\s*(\d{1,2})")


def normalize_date(raw: str | None) -> str | None:
    """民國 / 西元日期 → ISO YYYY-MM-DD。

    支援:
      "民國 114 年 05 月 12 日" / "114/05/12" → "2025-05-12"
      "2025/05/12" / "2025-05-12" → "2025-05-12"
      空字串 / None / 解析失敗 → None
    """
    if not raw:
        return None
    s = str(raw).strip().replace(" ", "").replace("民國", "")
    if not s:
        return None
    # 西元(4 位數年)優先試
    m_ad = _AD_PATTERN.match(s)
    if m_ad:
        y, mo, d = m_ad.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    # 民國(2-3 位數年)
    m_roc = _ROC_PATTERN.match(s)
    if m_roc:
        y_roc, mo, d = m_roc.groups()
        try:
            year = int(y_roc) + 1911
            return f"{year:04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    return None


def _extract_stock_id(raw: str | None) -> str | None:
    """從 cell 文字抽出股票代號。常見格式:'2330'、'2330 台積電'、'(2330)'。

    回 4-6 碼數字字串(支援 ETF 6 碼如 00878)或 None。
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = re.search(r"\b(\d{4,6})\b", s)
    return m.group(1) if m else None


def _parse_html_rows(html_text: str) -> list[dict[str, str]]:
    """通用 TWSE HTML 表格解析:把 <table> 內每個 <tr> 轉成 {header: cell_text}。

    TWSE 公告頁多用 <table> 配 <thead>/<tbody>,有些頁是 div table。這裡只處理
    傳統 <table>;若該源是 SPA 沒 <table>,parser 回 [],fetcher 該 raise 提示
    主公換 endpoint(別 silent skip,違約交割教訓)。
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError(
            "需要 beautifulsoup4 (pip install beautifulsoup4) 才能解析 TWSE HTML"
        ) from e

    soup = BeautifulSoup(html_text, "html.parser")
    out: list[dict[str, str]] = []
    for table in soup.find_all("table"):
        # 抽 header(thead 或第一列 tr)
        headers: list[str] = []
        thead = table.find("thead")
        if thead:
            head_row = thead.find("tr")
            if head_row:
                headers = [
                    th.get_text(strip=True)
                    for th in head_row.find_all(["th", "td"])
                ]
        if not headers:
            first_tr = table.find("tr")
            if first_tr:
                headers = [
                    c.get_text(strip=True)
                    for c in first_tr.find_all(["th", "td"])
                ]
        if not headers:
            continue

        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            # skip header row 自己
            if all(c.name == "th" for c in cells):
                continue
            values = [c.get_text(strip=True) for c in cells]
            if len(values) < 2:
                continue
            row: dict[str, str] = {}
            for i, v in enumerate(values):
                key = headers[i] if i < len(headers) else f"col_{i}"
                row[key] = v
            out.append(row)
    return out


# === Per-source 解析函式 ===

def _find_first_key(row: dict[str, str], candidates: list[str]) -> str | None:
    """在 row dict 內找第一個 match 的 key(支援 substring 包含)。

    TWSE 表頭常見變體:「處置股票」/「股票名稱」/「股票代號」等,寬鬆匹配。
    """
    for k in row.keys():
        for c in candidates:
            if c in k:
                return k
    return None


def parse_default_settlement_html(html_text: str, source_url: str) -> list[dict]:
    """違約交割公告 → list of stock_warnings rows(warning_type='default_settlement')。

    TWSE punish 公告欄位常見:公告日期 / 違約日期 / 證券代號 / 證券名稱 / 違約金額 / 說明
    違約交割沒有「解除日」概念(歷史污點),effective_to 一律 NULL。
    """
    rows = _parse_html_rows(html_text)
    out: list[dict] = []
    for r in rows:
        sid_key = _find_first_key(r, ["代號", "證券代號", "股票代號", "代碼"])
        date_key = _find_first_key(r, ["公告日期", "公告日", "日期"])
        reason_key = _find_first_key(r, ["說明", "事由", "違約", "原因"])
        sid = _extract_stock_id(r.get(sid_key) if sid_key else None)
        announced = normalize_date(r.get(date_key) if date_key else None)
        if not sid or not announced:
            continue
        reason = (r.get(reason_key) if reason_key else None) or "違約交割公告"
        out.append({
            "stock_id": sid,
            "warning_type": "default_settlement",
            "announced_date": announced,
            "effective_from": announced,
            "effective_to": None,
            "reason": str(reason)[:500],
            "source_url": source_url,
        })
    return out


def parse_attention_html(html_text: str, source_url: str) -> list[dict]:
    """注意股公告 → warning_type='attention'。

    注意股有「公告日 / 處置起 / 處置迄」三日期欄位;effective_to 取「處置迄」,
    缺值寫 NULL(視為仍生效)。
    """
    return _parse_typical_warning_html(
        html_text, source_url, warning_type="attention",
        default_reason="注意股公告",
    )


def parse_disposition_html(html_text: str, source_url: str) -> list[dict]:
    """處置股公告 → warning_type='disposition'。"""
    return _parse_typical_warning_html(
        html_text, source_url, warning_type="disposition",
        default_reason="處置股公告",
    )


def parse_method_changed_html(html_text: str, source_url: str) -> list[dict]:
    """變更交易方法公告 → warning_type='full_cash' / 'method_changed'。

    若公告 reason 含「全額交割」→ 歸類 full_cash(picks 硬擋之列);其餘
    歸類 method_changed(soft 降權)。
    """
    rows = _parse_html_rows(html_text)
    out: list[dict] = []
    for r in rows:
        sid_key = _find_first_key(r, ["代號", "證券代號", "股票代號", "代碼"])
        date_key = _find_first_key(r, ["公告日期", "公告日", "日期"])
        from_key = _find_first_key(r, ["生效日", "起始", "起日", "處置起"])
        to_key = _find_first_key(r, ["解除日", "迄日", "結束", "處置迄"])
        reason_key = _find_first_key(r, ["變更", "說明", "事由", "原因", "方法"])
        sid = _extract_stock_id(r.get(sid_key) if sid_key else None)
        announced = normalize_date(r.get(date_key) if date_key else None)
        if not sid or not announced:
            continue
        reason = (r.get(reason_key) if reason_key else None) or "變更交易方法"
        wt = (
            "full_cash" if "全額交割" in str(reason)
            else "method_changed"
        )
        out.append({
            "stock_id": sid,
            "warning_type": wt,
            "announced_date": announced,
            "effective_from": normalize_date(
                r.get(from_key) if from_key else None
            ),
            "effective_to": normalize_date(
                r.get(to_key) if to_key else None
            ),
            "reason": str(reason)[:500],
            "source_url": source_url,
        })
    return out


def _parse_typical_warning_html(
    html_text: str,
    source_url: str,
    warning_type: str,
    default_reason: str,
) -> list[dict]:
    """通用注意 / 處置股 parser(都是「公告日 + 處置起迄 + sid + 原因」結構)。"""
    rows = _parse_html_rows(html_text)
    out: list[dict] = []
    for r in rows:
        sid_key = _find_first_key(r, ["代號", "證券代號", "股票代號", "代碼"])
        date_key = _find_first_key(r, ["公告日期", "公告日", "日期"])
        from_key = _find_first_key(r, ["處置起", "起日", "生效日", "起始"])
        to_key = _find_first_key(r, ["處置迄", "迄日", "解除日", "結束"])
        reason_key = _find_first_key(r, ["原因", "事由", "說明"])
        sid = _extract_stock_id(r.get(sid_key) if sid_key else None)
        announced = normalize_date(r.get(date_key) if date_key else None)
        if not sid or not announced:
            continue
        reason = (r.get(reason_key) if reason_key else None) or default_reason
        out.append({
            "stock_id": sid,
            "warning_type": warning_type,
            "announced_date": announced,
            "effective_from": normalize_date(
                r.get(from_key) if from_key else None
            ),
            "effective_to": normalize_date(
                r.get(to_key) if to_key else None
            ),
            "reason": str(reason)[:500],
            "source_url": source_url,
        })
    return out


# === TPEx (上櫃) JSON parsers ===

# TPEx 日期欄位常見兩種:
#   ROC YYYMMDD 連寫 "1150514"(disposal / attention / cmode endpoint)
#   西元 YYYYMMDD 連寫 "20260514"(warning_note endpoint;為一致也支援)
_TPEX_ROC_COMPACT = re.compile(r"^(\d{3})(\d{2})(\d{2})$")
_TPEX_AD_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def normalize_tpex_date(raw: str | None) -> str | None:
    """TPEx 連寫日期 → ISO YYYY-MM-DD。

    支援:
      "1150514"  (民國 114/05/14) → "2025-05-14"
      "20260514" (西元 2026-05-14) → "2026-05-14"
      也接 normalize_date 支援的所有有分隔符格式(走 fallback)
      空字串 / None / 解析失敗 → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m_ad = _TPEX_AD_COMPACT.match(s)
    if m_ad:
        y, mo, d = m_ad.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    m_roc = _TPEX_ROC_COMPACT.match(s)
    if m_roc:
        y_roc, mo, d = m_roc.groups()
        try:
            year = int(y_roc) + 1911
            return f"{year:04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    # 有分隔符的格式走通用 normalizer
    return normalize_date(s)


def _parse_tpex_period(raw: str | None) -> tuple[str | None, str | None]:
    """解析 TPEx DispositionPeriod 字串 "1150515~1150528" → (from, to) ISO 日期。

    處置 / 注意股的處置期常以 ~ 或 - 或 ～ 分隔起迄。缺值或解析失敗回 (None, None)。
    """
    if not raw:
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None
    # 接受 ASCII tilde / 全形波浪 / 短橫線當分隔符
    parts = re.split(r"[~～\-]", s, maxsplit=1)
    if len(parts) != 2:
        return normalize_tpex_date(s), None
    return normalize_tpex_date(parts[0]), normalize_tpex_date(parts[1])


def _decode_tpex_json(raw_text: str) -> list[dict]:
    """TPEx OpenAPI v1 一律回 JSON list[dict]。空字串 / 非 list 回 []。

    parse 失敗(JSONDecodeError)直接 raise,讓上層 fetcher exit 1。
    """
    import json
    s = (raw_text or "").strip()
    if not s:
        return []
    data = json.loads(s)
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def parse_tpex_disposition_json(raw_text: str, source_url: str) -> list[dict]:
    """TPEx 上櫃處置 JSON → list of stock_warnings rows(warning_type='disposition')。

    JSON 欄位:Date, SecuritiesCompanyCode, CompanyName, DispositionPeriod,
              DispositionReasons, DisposalCondition
    """
    rows = _decode_tpex_json(raw_text)
    out: list[dict] = []
    for r in rows:
        sid = _extract_stock_id(r.get("SecuritiesCompanyCode"))
        announced = normalize_tpex_date(r.get("Date"))
        if not sid or not announced:
            continue
        eff_from, eff_to = _parse_tpex_period(r.get("DispositionPeriod"))
        reason = (
            r.get("DispositionReasons")
            or r.get("DisposalCondition")
            or "上櫃處置股公告"
        )
        out.append({
            "stock_id": sid,
            "warning_type": "disposition",
            "announced_date": announced,
            "effective_from": eff_from,
            "effective_to": eff_to,
            "reason": str(reason)[:500],
            "source_url": source_url,
        })
    return out


def parse_tpex_attention_json(raw_text: str, source_url: str) -> list[dict]:
    """TPEx 上櫃注意股 JSON → warning_type='attention'。

    JSON 欄位:Date, SecuritiesCompanyCode, CompanyName, TradingInformation,
              ClosePrice, PriceEarningRatio
    注意:此 endpoint 沒有處置期欄位,effective_from/to 全 NULL(視同公告日當天)。
    """
    rows = _decode_tpex_json(raw_text)
    out: list[dict] = []
    for r in rows:
        sid = _extract_stock_id(r.get("SecuritiesCompanyCode"))
        announced = normalize_tpex_date(r.get("Date"))
        if not sid or not announced:
            continue
        reason = r.get("TradingInformation") or "上櫃注意股公告"
        out.append({
            "stock_id": sid,
            "warning_type": "attention",
            "announced_date": announced,
            "effective_from": announced,
            "effective_to": None,
            "reason": str(reason)[:500],
            "source_url": source_url,
        })
    return out


def parse_tpex_cmode_json(raw_text: str, source_url: str) -> list[dict]:
    """TPEx 上櫃變更交易方法 JSON → 'full_cash' 或 'method_changed'。

    JSON 欄位:Date, SecuritiesCompanyCode, CompanyName, AlteredTrading,
              PeriodicTrading, ManagedStock, MatchingFrequency,
              SuspensionOfTrading, FinancialAnnouncements
    Y 旗標常以全形 'Ｙ' 出現,也接半形 'Y'。

    分類規則(picks pipeline 嚴重度,對齊 TWSE full_cash 概念):
      ManagedStock=Ｙ        → full_cash(管理股票,等同 TWSE 全額交割,硬擋)
      SuspensionOfTrading=Ｙ → full_cash(已停止交易,picks 一律不該推)
      AlteredTrading=Ｙ      → method_changed(soft 降權)
      其他單純 PeriodicTrading 或 FinancialAnnouncements → method_changed
      全為空 → skip(不寫入)
    """
    rows = _decode_tpex_json(raw_text)
    out: list[dict] = []
    for r in rows:
        sid = _extract_stock_id(r.get("SecuritiesCompanyCode"))
        announced = normalize_tpex_date(r.get("Date"))
        if not sid or not announced:
            continue

        def _is_y(v) -> bool:
            return str(v or "").strip() in ("Ｙ", "Y", "ｙ", "y")

        flags: list[str] = []
        if _is_y(r.get("AlteredTrading")):
            flags.append("變更交易方法")
        if _is_y(r.get("PeriodicTrading")):
            flags.append("分盤交易")
        if _is_y(r.get("ManagedStock")):
            flags.append("管理股票")
        if _is_y(r.get("SuspensionOfTrading")):
            flags.append("停止交易")
        if _is_y(r.get("FinancialAnnouncements")):
            flags.append("財務報告未申報")
        if not flags:
            continue

        if (
            _is_y(r.get("ManagedStock"))
            or _is_y(r.get("SuspensionOfTrading"))
        ):
            wt = "full_cash"
        else:
            wt = "method_changed"

        out.append({
            "stock_id": sid,
            "warning_type": wt,
            "announced_date": announced,
            "effective_from": announced,
            "effective_to": None,
            "reason": " / ".join(flags)[:500],
            "source_url": source_url,
        })
    return out


# === Source orchestration ===

# Source 表:每筆 (warning_type_label, url, parser, market)
# parser 簽名:(text: str, source_url: str) -> list[dict rows]
# market: "TWSE" / "TPEx" — 只用於 log prefix 區分
_SOURCES: list[tuple[str, str, callable, str]] = [
    # --- TWSE 上市 (HTML 解析) ---
    ("default_settlement", URL_PUNISH, parse_default_settlement_html, "TWSE"),
    ("attention", URL_NOTICE, parse_attention_html, "TWSE"),
    ("disposition", URL_DISPOSITION, parse_disposition_html, "TWSE"),
    (
        "method_changed_or_full_cash",
        URL_METHOD_CHANGED,
        parse_method_changed_html,
        "TWSE",
    ),
    # --- TPEx 上櫃 (OpenAPI v1 JSON) ---
    # 違約交割 endpoint 不存在 OpenAPI;run() 會 log warning 提醒。
    ("attention", TPEX_URL_ATTENTION, parse_tpex_attention_json, "TPEx"),
    ("disposition", TPEX_URL_DISPOSITION, parse_tpex_disposition_json, "TPEx"),
    (
        "method_changed_or_full_cash",
        TPEX_URL_CMODE,
        parse_tpex_cmode_json,
        "TPEx",
    ),
]


def _try_disposition_with_fallback(label: str) -> tuple[str, str]:
    """處置股主端點 404 / 抓不到 → fallback TWTBAU2。回 (html_text, used_url)。"""
    try:
        html = fetch_url_with_retry(URL_DISPOSITION, label=f"TWSE {label}")
        return html, URL_DISPOSITION
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[WARNINGS] %s 主端點失敗,fallback TWTBAU2: %s", label, e,
        )
        html = fetch_url_with_retry(
            URL_DISPOSITION_FALLBACK, label=f"TWSE {label} fallback",
        )
        return html, URL_DISPOSITION_FALLBACK


def fetch_and_parse_all(
    html_overrides: dict[str, str] | None = None,
) -> list[dict]:
    """打所有 source、parse、合併成單一 rows list(不寫 DB)。

    Args:
        html_overrides: {url: html_or_json_text} — 測試用,跳過真實 HTTP。
            key 應對應 _SOURCES 內的 URL_* constant 或 fallback URL。
            (鍵 "html_overrides" 是歷史名稱,實際同時接 TWSE HTML 和 TPEx JSON 字串)

    任何 source 抓 / parse 失敗 → raise(讓 CI exit 1 觸發告警),
    不要 silent skip(違約交割教訓:沒抓到等於沒擋,使用者繼續被坑)。

    額外行為:TPEx 違約交割無 OpenAPI v1 endpoint(2026-05 確認),每跑一次
    log warning,讓主公知道這條線目前還沒覆蓋(別 silent miss)。
    """
    html_overrides = html_overrides or {}
    all_rows: list[dict] = []
    logger.warning(
        "[TPEx] default_settlement: OpenAPI v1 無對應 endpoint,本次 run 不抓 "
        "上櫃違約;TWSE default_settlement 仍正常運作"
    )
    for label, url, parser, market in _SOURCES:
        # TWSE disposition 有 fallback URL 鏈;TPEx 沒有
        if market == "TWSE" and label == "disposition":
            if url in html_overrides:
                text, used_url = html_overrides[url], url
            elif URL_DISPOSITION_FALLBACK in html_overrides:
                text = html_overrides[URL_DISPOSITION_FALLBACK]
                used_url = URL_DISPOSITION_FALLBACK
            else:
                text, used_url = _try_disposition_with_fallback(label)
        else:
            if url in html_overrides:
                text, used_url = html_overrides[url], url
            else:
                text = fetch_url_with_retry(url, label=f"{market} {label}")
                used_url = url

        try:
            rows = parser(text, used_url)
        except Exception as ex:
            raise RuntimeError(
                f"[WARNINGS] parse {market} {label} 失敗:"
                f"{type(ex).__name__}: {ex}"
            ) from ex
        print(
            f"[WARNINGS] [{market}] {label:<28s} {len(rows)} rows",
            flush=True,
        )
        all_rows.extend(rows)
    return all_rows


def run(
    html_overrides: dict[str, str] | None = None,
    db_path: str | Path | None = None,
) -> dict:
    """主流程:抓 + parse 全部來源 → upsert stock_warnings。

    Returns summary dict {rows_parsed, rows_written, by_type, elapsed_secs}.
    """
    t0 = time.time()
    db.init_db(db_path)
    rows = fetch_and_parse_all(html_overrides=html_overrides)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in rows:
        r.setdefault("fetched_at", fetched_at)
    n_written = db.upsert_stock_warnings(rows, db_path=db_path)
    by_type: dict[str, int] = {}
    for r in rows:
        wt = r["warning_type"]
        by_type[wt] = by_type.get(wt, 0) + 1
    elapsed = round(time.time() - t0, 2)
    print(
        f"[WARNINGS] DONE rows_parsed={len(rows)} rows_written={n_written} "
        f"by_type={by_type} elapsed={elapsed}s",
        flush=True,
    )
    return {
        "rows_parsed": len(rows),
        "rows_written": n_written,
        "by_type": by_type,
        "elapsed_secs": elapsed,
    }


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="TWSE / TPEx 警示股紀錄抓取 + upsert(stock_warnings)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只 fetch + parse,不寫 DB(smoke 用)",
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    try:
        if args.dry_run:
            rows = fetch_and_parse_all()
            print(f"[WARNINGS] DRY RUN parsed {len(rows)} rows", flush=True)
            return 0
        summary = run()
    except Exception as ex:  # noqa: BLE001
        print(
            f"[WARNINGS] FATAL: {type(ex).__name__}: {ex}", file=sys.stderr,
        )
        return 1
    print("=" * 60, flush=True)
    print("[WARNINGS SUMMARY]", flush=True)
    for k, v in summary.items():
        print(f"  {k:<16s} {v}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
