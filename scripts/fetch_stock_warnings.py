"""TWSE / TPEx 警示股紀錄抓取 CLI(2026-05-15 主公拍板加入,違約交割教訓)。

抓取的警示分類(寫入 stock_warnings.warning_type):
  - default_settlement (違約交割) — **TWSE/TPEx OpenAPI 皆無對應 endpoint**
                                     (兩家都是 SPA 表單頁,需 browser 自動化)
                                     run() 會 log warning 提醒主公,別 silent skip
  - attention         (注意股)   — TWSE /announcement/notice + /announcement/notetrans
                                  + TPEx tpex_trading_warning_information
  - disposition       (處置股)   — TWSE /announcement/punish
                                  + TPEx tpex_disposal_information
  - full_cash         (全額交割) — TPEx tpex_cmode 之 ManagedStock=Ｙ(管理股票)
                                  TWSE 變更交易 endpoint 不分 full_cash vs other,
                                  全進 method_changed(欄位陽春,picks 統一 soft 降權)
  - method_changed    (變更交易方法) — TWSE /exchangeReport/TWT85U
                                  + TPEx tpex_cmode 之 AlteredTrading=Ｙ

主要資料來源(2026-05-16 從 bs4 HTML 改成 OpenAPI JSON,silent 0 rows 修復):
  TWSE (上市):
    - 處置股   https://openapi.twse.com.tw/v1/announcement/punish
    - 注意股   https://openapi.twse.com.tw/v1/announcement/notice
                + https://openapi.twse.com.tw/v1/announcement/notetrans
                  (累計次數補充來源,沒 Date 欄位)
    - 變更交易 https://openapi.twse.com.tw/v1/exchangeReport/TWT85U
                (僅含 Code/Name/PeriodicCallAuctionTrading,無 Date/Reason/迄日;
                 全標 method_changed)
    - 違約交割 :TWSE OpenAPI v1 swagger 143 paths 無對應 endpoint(2026-05 確認);
                只有 SPA 表單頁 /zh/announcement/bfigtu.html,需 browser
                automation,違反專案禁止 selenium 原則 → 暫不抓,run() log
                warning 提醒。see docs/twse-warnings-still-broken.md
  TPEx (上櫃):
    - 注意股   https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information
    - 處置股   https://www.tpex.org.tw/openapi/v1/tpex_disposal_information
    - 變更交易方法 https://www.tpex.org.tw/openapi/v1/tpex_cmode
    - 違約交割 :TPEx OpenAPI v1 也無對應 endpoint(同 TWSE 困境)

設計原則:
  - 全 source 走 JSON(TWSE / TPEx OpenAPI v1),不再用 bs4 解 HTML
  - User-Agent 必填(TWSE / TPEx 都會擋 python-requests UA)
  - retry 3 次(走 src._retry.with_retry)
  - HTTP error / JSON parse fail → raise,讓 CI exit 1 觸發告警
  - 「endpoint 整體壞掉」防呆:run 後檢查兩條基線 (TWSE punish + TWT85U) 是否
    皆 0 rows;若是 → raise(這兩條源歷史上一定有資料,同時 0 表示 endpoint
    壞掉,不是「假日沒事件」)
  - 「假日沒事件」自然 0 rows → 不 raise(notice / notetrans / TPEx 三條合理 0)
  - upsert 進 stock_warnings,同 PK (stock_id, warning_type, announced_date) 覆寫

Exit code:
  0 = 成功(寫入 0 筆也算成功,只要 baseline 兩條源沒同時 0)
  1 = 抓取或解析失敗 / baseline 同時 0 rows
"""
from __future__ import annotations

import argparse
import json
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
# TWSE 的 SSL 憑證鏈缺 SubjectKeyIdentifier,新版 OpenSSL 會擋 → 用同 pattern
# 處理,公開資料 read-only 無 MITM 風險
_VERIFY_SSL = False
# python-requests 預設 UA 會被 TWSE 擋進 redirect loop / 403,必填常見瀏覽器 UA
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
    ),
    "Accept": "application/json,text/json,*/*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


# === TWSE OpenAPI v1 endpoints(2026-05-16 從 bs4 改 JSON) ===

URL_PUNISH = "https://openapi.twse.com.tw/v1/announcement/punish"
URL_NOTICE = "https://openapi.twse.com.tw/v1/announcement/notice"
URL_NOTETRANS = "https://openapi.twse.com.tw/v1/announcement/notetrans"
URL_METHOD_CHANGED = "https://openapi.twse.com.tw/v1/exchangeReport/TWT85U"

# TPEx (上櫃) — 走官方 OpenAPI v1 JSON,結構穩定。
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
    return resp.text


def fetch_url_with_retry(url: str, label: str) -> str:
    """打 URL 拿原始 JSON 字串,失敗 retry 3 次(指數退避 1s/2s/4s)。

    給上層 parser 餵原始字串。網路 / 5xx 連續失敗會 raise,讓 CLI exit 1。
    """
    return with_retry(
        lambda: _http_get(url),
        max_attempts=3,
        base_delay=1.0,
        label=label,
    )


# === 共用解析 ===

# TWSE / TPEx 日期常見格式:
#   "1150506"    (民國連寫 YYYMMDD,如 OpenAPI punish.Date)
#   "20260506"   (西元連寫 YYYYMMDD)
#   "115/05/07"  (民國有分隔符,如 punish.DispositionPeriod 起迄)
#   "2026-05-07" (西元有分隔符)
#   "民國 114 年 05 月 12 日" / "114年5月3日" (民國中文)
_ROC_PATTERN = re.compile(r"^(\d{2,3})[/\-年]\s*(\d{1,2})[/\-月]\s*(\d{1,2})")
_AD_PATTERN = re.compile(r"^(\d{4})[/\-年]\s*(\d{1,2})[/\-月]\s*(\d{1,2})")
_ROC_COMPACT = re.compile(r"^(\d{3})(\d{2})(\d{2})$")
_AD_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def normalize_date(raw: str | None) -> str | None:
    """民國 / 西元日期 → ISO YYYY-MM-DD。

    支援:
      "1150506"  (民國 115/05/06 連寫) → "2026-05-06"
      "20260506" (西元 2026-05-06 連寫) → "2026-05-06"
      "115/05/07" / "114年5月3日" → "2026-05-07" / "2025-05-03"
      "2025/05/12" / "2025-05-12" → "2025-05-12"
      空字串 / None / 解析失敗 → None
    """
    if not raw:
        return None
    s = str(raw).strip().replace(" ", "").replace("民國", "")
    if not s:
        return None
    # 連寫格式優先(OpenAPI Date 常見)
    m_ad_c = _AD_COMPACT.match(s)
    if m_ad_c:
        y, mo, d = m_ad_c.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    m_roc_c = _ROC_COMPACT.match(s)
    if m_roc_c:
        y_roc, mo, d = m_roc_c.groups()
        try:
            year = int(y_roc) + 1911
            return f"{year:04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    # 有分隔符:西元(4 位數年)優先試
    m_ad = _AD_PATTERN.match(s)
    if m_ad:
        y, mo, d = m_ad.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    m_roc = _ROC_PATTERN.match(s)
    if m_roc:
        y_roc, mo, d = m_roc.groups()
        try:
            year = int(y_roc) + 1911
            return f"{year:04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    return None


# TPEx 連寫日期專用 alias(向後相容測試)。
# 行為與 normalize_date 完全一致(都支援民國/西元連寫 + 分隔符)。
def normalize_tpex_date(raw: str | None) -> str | None:
    """TPEx 連寫 / 分隔符日期 → ISO YYYY-MM-DD(alias to normalize_date)。"""
    return normalize_date(raw)


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


def _parse_period(raw: str | None) -> tuple[str | None, str | None]:
    """通用「處置起~迄」字串 → (from, to)。

    支援:
      "1150515~1150528"      (TPEx 連寫 + ASCII tilde)
      "115/05/07～115/05/20"  (TWSE 帶分隔符 + 全形波浪)
      "114/05/01-114/05/12"  (短橫線)
    缺值或單邊解析失敗 → (None, None) 或 (from, None)
    """
    if not raw:
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None
    parts = re.split(r"[~～\-至]", s, maxsplit=1)
    if len(parts) != 2:
        return normalize_date(s), None
    return normalize_date(parts[0]), normalize_date(parts[1])


def _decode_json(raw_text: str) -> list[dict]:
    """OpenAPI v1 一律回 JSON list[dict]。空字串 / 非 list 回 []。

    parse 失敗(JSONDecodeError)直接 raise,讓上層 fetcher exit 1。
    """
    s = (raw_text or "").strip()
    if not s:
        return []
    data = json.loads(s)
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _is_empty_placeholder_row(r: dict) -> bool:
    """OpenAPI 假日 / 沒事件回 1 筆 sentinel row(欄位全空 + Number='0')。

    例:notice endpoint 沒事件時回 [{"Number":"0","Code":"","Name":"",...}]
    這種 row 要 skip,不寫進 DB。
    """
    code = str(r.get("Code", "") or "").strip()
    name = str(r.get("Name", "") or "").strip()
    if code or name:
        return False
    return True


# === TWSE OpenAPI parsers ===

def parse_twse_punish_json(raw_text: str, source_url: str) -> list[dict]:
    """TWSE /announcement/punish JSON → 處置股(warning_type='disposition')。

    Schema 欄位(2026-05 確認):
      Number, Date(民國連寫 1150506), Code, Name, NumberOfAnnouncement,
      ReasonsOfDisposition(處置條件), DispositionPeriod("115/05/07～115/05/20"),
      DispositionMeasures(第一次/第二次處置), Detail(完整文字), LinkInformation
    """
    rows = _decode_json(raw_text)
    out: list[dict] = []
    for r in rows:
        if _is_empty_placeholder_row(r):
            continue
        sid = _extract_stock_id(r.get("Code"))
        announced = normalize_date(r.get("Date"))
        if not sid or not announced:
            continue
        eff_from, eff_to = _parse_period(r.get("DispositionPeriod"))
        reason_parts = [
            str(r.get(k, "") or "").strip()
            for k in ("ReasonsOfDisposition", "DispositionMeasures")
        ]
        reason = " / ".join(p for p in reason_parts if p) or "TWSE 處置股公告"
        out.append({
            "stock_id": sid,
            "warning_type": "disposition",
            "announced_date": announced,
            "effective_from": eff_from or announced,
            "effective_to": eff_to,
            "reason": reason[:500],
            "source_url": source_url,
        })
    return out


def parse_twse_notice_json(raw_text: str, source_url: str) -> list[dict]:
    """TWSE /announcement/notice JSON → 當日注意股(warning_type='attention')。

    Schema 欄位:
      Number, Code, Name, NumberOfAnnouncement,
      TradingInfoForAttention(注意交易資訊), Date, ClosingPrice, PE
    假日 / 沒事件 → 回 1 筆 Number='0' 全空 sentinel,被 _is_empty_placeholder_row 過濾。
    """
    rows = _decode_json(raw_text)
    out: list[dict] = []
    for r in rows:
        if _is_empty_placeholder_row(r):
            continue
        sid = _extract_stock_id(r.get("Code"))
        announced = normalize_date(r.get("Date"))
        if not sid or not announced:
            continue
        reason = str(r.get("TradingInfoForAttention", "") or "").strip() \
            or "TWSE 注意股公告"
        out.append({
            "stock_id": sid,
            "warning_type": "attention",
            "announced_date": announced,
            "effective_from": announced,
            "effective_to": None,
            "reason": reason[:500],
            "source_url": source_url,
        })
    return out


# 從 notetrans 的 RecentlyMetAttentionSecuritiesCriteria 文字抽日期。
# 範例:"115年5月14日至115年5月15日連續二次" → 取最後一個日期作為 announced。
_NOTETRANS_DATE_RE = re.compile(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日")


def parse_twse_notetrans_json(
    raw_text: str,
    source_url: str,
    fallback_date: str | None = None,
) -> list[dict]:
    """TWSE /announcement/notetrans JSON → 注意累計次數(warning_type='attention')。

    Schema 欄位:
      Code, Name, RecentlyMetAttentionSecuritiesCriteria
      (内含「115年5月14日至115年5月15日連續二次」這種句子,沒獨立 Date 欄位)

    日期處理:從 criteria 文字正則抽出最後一個日期(代表最近一次達標);
    抽不到 → 用 fallback_date(預設為 UTC 今天)。
    """
    rows = _decode_json(raw_text)
    if fallback_date is None:
        fallback_date = datetime.now(timezone.utc).date().isoformat()
    out: list[dict] = []
    for r in rows:
        if _is_empty_placeholder_row(r):
            continue
        sid = _extract_stock_id(r.get("Code"))
        if not sid:
            continue
        criteria = str(r.get("RecentlyMetAttentionSecuritiesCriteria", "") or "")
        matches = _NOTETRANS_DATE_RE.findall(criteria)
        if matches:
            # 用最後一個日期(criteria 文字結尾通常是最近的日期)
            y_roc, mo, d = matches[-1]
            try:
                year = int(y_roc) + 1911
                announced = f"{year:04d}-{int(mo):02d}-{int(d):02d}"
            except ValueError:
                announced = fallback_date
        else:
            announced = fallback_date
        reason = criteria.strip() or "TWSE 注意股累計次數異常"
        out.append({
            "stock_id": sid,
            "warning_type": "attention",
            "announced_date": announced,
            "effective_from": announced,
            "effective_to": None,
            "reason": reason[:500],
            "source_url": source_url,
        })
    return out


def parse_twse_method_changed_json(
    raw_text: str,
    source_url: str,
    fallback_date: str | None = None,
) -> list[dict]:
    """TWSE /exchangeReport/TWT85U JSON → 變更交易(warning_type='method_changed')。

    Schema 欄位(陽春):
      Code, Name, PeriodicCallAuctionTrading(分盤集合競價 flag,"**" 或 "  ")
      **無 Date 欄位、無 reason、無迄日** — endpoint 限制。

    處理:
      announced_date = fallback_date(預設今天 UTC),全標 method_changed。
      因 schema 無法區分 full_cash vs 一般變更方法,picks 統一 soft 降權。
      reason 標記是否為「分盤集合競價」。
    """
    rows = _decode_json(raw_text)
    if fallback_date is None:
        fallback_date = datetime.now(timezone.utc).date().isoformat()
    out: list[dict] = []
    for r in rows:
        if _is_empty_placeholder_row(r):
            continue
        sid = _extract_stock_id(r.get("Code"))
        if not sid:
            continue
        flag = str(r.get("PeriodicCallAuctionTrading", "") or "").strip()
        if flag == "**":
            reason = "TWSE 變更交易方法:分盤集合競價"
        else:
            reason = "TWSE 變更交易方法"
        out.append({
            "stock_id": sid,
            "warning_type": "method_changed",
            "announced_date": fallback_date,
            "effective_from": fallback_date,
            "effective_to": None,
            "reason": reason[:500],
            "source_url": source_url,
        })
    return out


# === TPEx (上櫃) OpenAPI v1 JSON parsers ===

def _decode_tpex_json(raw_text: str) -> list[dict]:
    """alias for _decode_json,測試向後相容用。"""
    return _decode_json(raw_text)


def _parse_tpex_period(raw: str | None) -> tuple[str | None, str | None]:
    """alias for _parse_period,測試向後相容用。"""
    return _parse_period(raw)


def parse_tpex_disposition_json(raw_text: str, source_url: str) -> list[dict]:
    """TPEx 上櫃處置 JSON → list of stock_warnings rows(warning_type='disposition')。

    JSON 欄位:Date, SecuritiesCompanyCode, CompanyName, DispositionPeriod,
              DispositionReasons, DisposalCondition
    """
    rows = _decode_json(raw_text)
    out: list[dict] = []
    for r in rows:
        sid = _extract_stock_id(r.get("SecuritiesCompanyCode"))
        announced = normalize_date(r.get("Date"))
        if not sid or not announced:
            continue
        eff_from, eff_to = _parse_period(r.get("DispositionPeriod"))
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
    rows = _decode_json(raw_text)
    out: list[dict] = []
    for r in rows:
        sid = _extract_stock_id(r.get("SecuritiesCompanyCode"))
        announced = normalize_date(r.get("Date"))
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
    rows = _decode_json(raw_text)
    out: list[dict] = []
    for r in rows:
        sid = _extract_stock_id(r.get("SecuritiesCompanyCode"))
        announced = normalize_date(r.get("Date"))
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
#
# TWSE 違約交割不在表內 — OpenAPI 無 endpoint(swagger 143 paths 確認),
# fetch_and_parse_all() 會 log warning 提醒。
_SOURCES: list[tuple[str, str, callable, str]] = [
    # --- TWSE 上市 (OpenAPI v1 JSON) ---
    ("disposition", URL_PUNISH, parse_twse_punish_json, "TWSE"),
    ("attention", URL_NOTICE, parse_twse_notice_json, "TWSE"),
    ("attention_notetrans", URL_NOTETRANS, parse_twse_notetrans_json, "TWSE"),
    ("method_changed", URL_METHOD_CHANGED, parse_twse_method_changed_json, "TWSE"),
    # --- TPEx 上櫃 (OpenAPI v1 JSON) ---
    ("attention", TPEX_URL_ATTENTION, parse_tpex_attention_json, "TPEx"),
    ("disposition", TPEX_URL_DISPOSITION, parse_tpex_disposition_json, "TPEx"),
    (
        "method_changed_or_full_cash",
        TPEX_URL_CMODE,
        parse_tpex_cmode_json,
        "TPEx",
    ),
]

# baseline endpoints — 這兩條源歷史上絕對有資料,若同時 0 rows 表示 endpoint
# 整體壞掉(網域改、schema 變、被擋等),raise 警告主公自己處理。
# (notice / notetrans / TPEx 三條允許 0 rows = 假日沒事件,不在 baseline 內)
_BASELINE_URLS = {URL_PUNISH, URL_METHOD_CHANGED}


def fetch_and_parse_all(
    html_overrides: dict[str, str] | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """打所有 source、parse、合併成單一 rows list(不寫 DB)。

    Args:
        html_overrides: {url: json_text} — 測試用,跳過真實 HTTP。
            key 對應 _SOURCES 內的 URL_* constant。
            (鍵 "html_overrides" 是歷史名稱,實際接 JSON 字串)

    Returns:
        (all_rows, per_source_counts):per_source_counts 是 {url: row_count}。

    任何 source 抓 / parse 失敗 → raise(讓 CI exit 1 觸發告警),不要 silent skip。

    Baseline 防呆:跑完後若 TWSE punish + TWT85U 兩條源都 0 rows → raise
    (這兩條歷史上一定有資料,同時 0 表示 endpoint 整體壞掉)。

    額外行為:TWSE / TPEx 違約交割無 OpenAPI v1 endpoint(2026-05 確認),每跑一次
    log warning,讓主公知道這條線目前還沒覆蓋(別 silent miss)。
    """
    html_overrides = html_overrides or {}
    all_rows: list[dict] = []
    per_source: dict[str, int] = {}

    logger.warning(
        "[WARNINGS] default_settlement(違約交割):TWSE/TPEx OpenAPI v1 皆無對應 "
        "endpoint,本次 run 不抓;見 docs/twse-warnings-still-broken.md"
    )

    for label, url, parser, market in _SOURCES:
        if url in html_overrides:
            text = html_overrides[url]
        else:
            text = fetch_url_with_retry(url, label=f"{market} {label}")

        try:
            rows = parser(text, url)
        except Exception as ex:
            raise RuntimeError(
                f"[WARNINGS] parse {market} {label} 失敗:"
                f"{type(ex).__name__}: {ex}"
            ) from ex

        per_source[url] = len(rows)
        print(
            f"[WARNINGS] [{market}] {label:<28s} {len(rows):>4d} rows",
            flush=True,
        )
        all_rows.extend(rows)

    # baseline 偵測:TWSE punish + TWT85U 兩條源歷史一定有資料,
    # 同時 0 rows → endpoint 壞掉,raise 警告主公。
    baseline_zero = [
        u for u in _BASELINE_URLS if per_source.get(u, 0) == 0
    ]
    if len(baseline_zero) == len(_BASELINE_URLS):
        raise RuntimeError(
            "[WARNINGS] baseline TWSE endpoints (punish + TWT85U) 同時 0 rows — "
            "OpenAPI 整體壞掉?請檢查 endpoint URL/schema 是否變更。"
            f"baseline_zero={baseline_zero}"
        )

    return all_rows, per_source


def run(
    html_overrides: dict[str, str] | None = None,
    db_path: str | Path | None = None,
) -> dict:
    """主流程:抓 + parse 全部來源 → upsert stock_warnings。

    Returns summary dict {rows_parsed, rows_written, by_type, per_source,
                          elapsed_secs}.
    """
    t0 = time.time()
    db.init_db(db_path)
    rows, per_source = fetch_and_parse_all(html_overrides=html_overrides)
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
        "per_source": per_source,
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
            rows, _ = fetch_and_parse_all()
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
