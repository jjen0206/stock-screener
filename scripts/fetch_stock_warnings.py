"""TWSE / TPEx 警示股紀錄抓取 CLI(2026-05-15 主公拍板加入,違約交割教訓)。

抓取的警示分類(寫入 stock_warnings.warning_type):
  - default_settlement (違約交割) — **TPEx 違約公告專區「個股違約資訊」**(主來源,
                                     2026-05-17 加入,3105 4/27-4/28 違約教訓 root
                                     cause)+ MOPS 重大訊息 RSS(輔助來源,實測
                                     滾動視窗只 8 筆 / 24h,僅作備援不可單獨依賴)。
                                     TPEx breach endpoint 第 2 表「個股達違約資訊
                                     揭露標準」涵蓋全市場(上市+上櫃+興櫃,實測
                                     3105 穩懋雖為上市股仍可從此抓到),門檻為
                                     同一標的當日違約金額達 1000 萬元。
                                     另存「每日全市場違約金額彙總」進
                                     default_settlement_daily 表(TWSE BFIGTU +
                                     TPEx breach 第 1 表),即使個股未達 1000 萬
                                     揭露門檻仍可警示市場異常日。
  - attention         (注意股)   — TWSE /announcement/notice + /announcement/notetrans
                                  + TPEx tpex_trading_warning_information
  - disposition       (處置股)   — TWSE /announcement/punish
                                  + TPEx tpex_disposal_information
  - full_cash         (全額交割) — TPEx tpex_cmode 之 ManagedStock=Ｙ(管理股票)
                                  TWSE 變更交易 endpoint 不分 full_cash vs other,
                                  全進 method_changed(欄位陽春,picks 統一 soft 降權)
  - method_changed    (變更交易方法) — TWSE /exchangeReport/TWT85U
                                  + TPEx tpex_cmode 之 AlteredTrading=Ｙ

主要資料來源(2026-05-16 從 bs4 HTML 改成 OpenAPI JSON,silent 0 rows 修復;
              2026-05-17 加 TWSE BFIGTU + TPEx breach,違約交割 root cause):
  TWSE (上市):
    - 處置股   https://openapi.twse.com.tw/v1/announcement/punish
    - 注意股   https://openapi.twse.com.tw/v1/announcement/notice
                + https://openapi.twse.com.tw/v1/announcement/notetrans
                  (累計次數補充來源,沒 Date 欄位)
    - 變更交易 https://openapi.twse.com.tw/v1/exchangeReport/TWT85U
                (僅含 Code/Name/PeriodicCallAuctionTrading,無 Date/Reason/迄日;
                 全標 method_changed)
    - 違約交割 https://www.twse.com.tw/announcement/BFIGTU?response=json&startDate=&endDate=
                **每日全市場彙總金額** — 上市市場無個股細目(TWSE 不公開);
                寫進 default_settlement_daily (market='TWSE'),供 UI alert 市場
                異常日。個股細目要從 TPEx breach 抓(下方說明)。
  違約交割(全市場 — 個股細目):
    - TPEx breach https://www.tpex.org.tw/www/zh-tw/bulletin/breach?response=json&...
                  **主來源**。回兩個 table:
                  table[0] 「證券商申報投資人違約金額」每日上櫃/興櫃彙總 →
                           寫 default_settlement_daily (market='TPEX_LISTED'/
                           'TPEX_EMERGING')
                  table[1] 「個股達違約資訊揭露標準(註1)之證券資訊」 →
                           寫 stock_warnings.default_settlement。揭露門檻:
                           同一標的當日違約金額 >= 1000 萬元。涵蓋全市場
                           (上市+上櫃+興櫃,3105 穩懋為上市股仍在此 endpoint)
                  日期參數:startDate=YYYY/MM/DD&endDate=YYYY/MM/DD(斜線分隔)
    - MOPS 重大訊息 RSS(輔助來源,降權使用)
      https://mopsov.twse.com.tw/nas/rss/mopsrss201001.xml
      編碼 cp950,**實測滾動只 8 筆 / 24h 視窗**(原 spec「100 筆」與實測不符),
      parser 過濾標題/內文含「違約」關鍵字 → warning_type='default_settlement'。
      因視窗太短,僅作 TPEx 主來源備援(若 TPEx endpoint 壞掉時仍能抓到部分
      公司主動 MOPS 公告的違約案)。違約事件年數筆,0 rows 屬正常,不在 baseline
      偵測內。
  TPEx (上櫃):
    - 注意股   https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information
    - 處置股   https://www.tpex.org.tw/openapi/v1/tpex_disposal_information
    - 變更交易方法 https://www.tpex.org.tw/openapi/v1/tpex_cmode
  TPEx (上櫃) — 其他警示:
    - 注意股   https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information
    - 處置股   https://www.tpex.org.tw/openapi/v1/tpex_disposal_information
    - 變更交易方法 https://www.tpex.org.tw/openapi/v1/tpex_cmode

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
from src.logging_setup import setup_file_logging  # noqa: E402

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

# === MOPS 公開資訊觀測站 重大訊息 RSS(2026-05-16 加入,輔助來源)===
# **實測**滾動只 8 筆 / 24h 視窗(舊註解寫「100 筆」已查證錯誤,2026-05-17 修);
# 因視窗太短,僅作 TPEx 主來源備援。parser 過濾標題/內文含「違約」關鍵字
# → default_settlement。編碼 cp950(XML 宣告 big5,big5/cp950 通用)。
URL_MOPS_DEFAULT_SETTLEMENT_RSS = (
    "https://mopsov.twse.com.tw/nas/rss/mopsrss201001.xml"
)

# === TWSE 違約交割「每日全市場彙總金額」(2026-05-17 加,違約交割教訓 R3)===
# 上市市場無個股細目(TWSE 不公開);只回每日彙總,寫進 default_settlement_daily
# (market='TWSE')供 UI alert 市場異常日。個股細目要從 TPEx breach 抓(下方)。
# 日期參數格式:startDate=YYYYMMDD&endDate=YYYYMMDD(無分隔)。
URL_TWSE_BFIGTU_BASE = "https://www.twse.com.tw/announcement/BFIGTU"

# === TPEx 違約交割(主來源,涵蓋全市場)===
# 回兩個 table:
#   table[0] 證券商申報投資人違約金額 — 上櫃/興櫃每日彙總(寫
#            default_settlement_daily market='TPEX_LISTED'/'TPEX_EMERGING')
#   table[1] 個股達違約資訊揭露標準(註1)之證券資訊 — 個股細目,門檻同一
#            標的當日違約金額 >= 1000 萬。涵蓋全市場(含上市股,如 3105)。
#            寫 stock_warnings(warning_type='default_settlement')。
# 日期參數格式:startDate=YYYY/MM/DD&endDate=YYYY/MM/DD(斜線分隔)。
URL_TPEX_BREACH_BASE = "https://www.tpex.org.tw/www/zh-tw/bulletin/breach"

# 違約交割 backfill 預設窗口(天)。TPEx + TWSE endpoint 都接受 6 個月內單次查
# 詢,無需切窗(實測 2025/11 - 2026/05 共 6 個月,單次 11K bytes 內可回完整)。
DEFAULT_SETTLEMENT_BACKFILL_DAYS = 90

# TPEx (上櫃) — 走官方 OpenAPI v1 JSON,結構穩定。
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


# === MOPS 重大訊息 RSS(違約交割專用)===

# MOPS RSS title 格式:"(NNNN)NAME-重大訊息" — 抽 4-6 碼數字代號。
_MOPS_TITLE_SID_RE = re.compile(r"\((\d{4,6})\)")

# RSS item tag splitter — 用正則切而不用 ET.parse,因為 cp950 解碼後內含
# CDATA 與 big5 控制字元,xml parser 容易 choke。
_MOPS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL)
_MOPS_TITLE_RE = re.compile(
    r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", re.DOTALL,
)
_MOPS_DESC_RE = re.compile(
    r"<description>\s*<!\[CDATA\[(.*?)\]\]>\s*</description>", re.DOTALL,
)
_MOPS_PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>")
_MOPS_LINK_RE = re.compile(
    r"<link>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</link>", re.DOTALL,
)

# 違約交割關鍵字:標題或內文任一含「違約」即標記。
# 「違約交割」是最直接的關鍵字;「違約」單詞也包含「違約金」等假陽性,
# 但 MOPS 重大訊息中「違約」單詞出現頻率本就極低(年數筆),且寧抓多
# 不漏掉(主公曾踩過違約股 → 偽陽性可接受,silent miss 不可接受)。
_MOPS_DEFAULT_KEYWORDS = ("違約交割", "違約")


def _fetch_mops_rss_text(url: str = URL_MOPS_DEFAULT_SETTLEMENT_RSS) -> str:
    """打 MOPS RSS 拿原始 cp950 解碼文字。

    跟 _http_get 分開因為 MOPS RSS 是 cp950 編碼,requests.text 用 charset
    猜測常猜錯(會回 latin-1 / replacement chars)。改成讀 .content bytes
    再 cp950 解碼。SSL verify=False / UA / retry 規則跟其他 source 一致。
    """
    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _once() -> str:
        resp = requests.get(
            url, timeout=_HTTP_TIMEOUT, verify=_VERIFY_SSL,
            headers=_HTTP_HEADERS,
        )
        resp.raise_for_status()
        # XML 宣告 encoding='big5',但實際內容有 big5 / cp950 通用區段,
        # cp950 是 big5 的超集,errors='replace' 防個別控制字元 choke。
        return resp.content.decode("cp950", errors="replace")

    return with_retry(_once, max_attempts=3, base_delay=1.0, label="MOPS RSS")


def parse_mops_default_settlement_rss(
    xml_text: str,
    source_url: str,
    fallback_date: str | None = None,
) -> list[dict]:
    """MOPS 重大訊息 RSS → list[default_settlement rows]。

    過濾規則:item 的 title 或 description 含「違約」關鍵字 → 抓進來。
    title 格式 "(NNNN)NAME-重大訊息" 用正則抽 4-6 碼證券代號。

    pubDate 是 RFC 822 格式 "Sat, 16 May 2026 13:09:59 +0800",
    parse 失敗 → fallback_date(預設今天 UTC)。

    reason 組合 title + description,截 500 字。
    source_url 用 item 的 <link>(指向 MOPS 該則公告詳情),
    fallback 用 RSS feed URL。

    空 / malformed XML → 回 [](不 raise,因為 RSS 偶爾會回 502 / 空頁;
    上層 _fetch_mops_rss_text 已 retry 3 次,真壞了應該 raise 在那邊)。
    """
    if fallback_date is None:
        fallback_date = datetime.now(timezone.utc).date().isoformat()

    items = _MOPS_ITEM_RE.findall(xml_text or "")
    out: list[dict] = []
    for raw in items:
        title_m = _MOPS_TITLE_RE.search(raw)
        desc_m = _MOPS_DESC_RE.search(raw)
        title = (title_m.group(1) if title_m else "").strip()
        desc = (desc_m.group(1) if desc_m else "").strip()
        haystack = title + "\n" + desc
        if not any(kw in haystack for kw in _MOPS_DEFAULT_KEYWORDS):
            continue

        sid_m = _MOPS_TITLE_SID_RE.search(title)
        if not sid_m:
            continue
        sid = sid_m.group(1)

        announced = fallback_date
        pubdate_m = _MOPS_PUBDATE_RE.search(raw)
        if pubdate_m:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pubdate_m.group(1).strip())
                if dt is not None:
                    announced = dt.date().isoformat()
            except Exception:
                pass

        link_m = _MOPS_LINK_RE.search(raw)
        item_link = (link_m.group(1) if link_m else "").strip()

        reason_full = f"{title} | {desc}".strip(" |")
        out.append({
            "stock_id": sid,
            "warning_type": "default_settlement",
            "announced_date": announced,
            "effective_from": announced,
            "effective_to": None,
            "reason": reason_full[:500] or "MOPS 重大訊息含『違約』關鍵字",
            "source_url": item_link or source_url,
        })
    return out


# === TWSE BFIGTU(違約交割每日全市場彙總,2026-05-17 加)===

def _twse_bfigtu_url(start_iso: str, end_iso: str) -> str:
    """組 BFIGTU URL。日期參數格式 YYYYMMDD(無分隔)。"""
    s = start_iso.replace("-", "")
    e = end_iso.replace("-", "")
    return f"{URL_TWSE_BFIGTU_BASE}?response=json&startDate={s}&endDate={e}"


def parse_twse_bfigtu_json(
    raw_text: str, source_url: str,
) -> list[dict]:
    """TWSE BFIGTU JSON → list[default_settlement_daily rows](market='TWSE')。

    Response 結構:
      {"stat":"OK","flag":104,"hints":"單位:元",
       "tables":[{"title":"...","fields":["申報日期","買進、賣出合計總金額",
       "買進、賣出相抵後金額"],"data":[["115/03/02","9,724,458","564,180"],...]}]}

    Schema 限制(2026-05-17 確認):TWSE 上市市場「不公開」個股細目,此 endpoint
    只回每日彙總金額。所以 return 的 row 都是 (market='TWSE', report_date,
    gross_amount, net_amount) 結構,不會有 stock_id。

    空資料 / parse 失敗 → 回 []。但若 endpoint 回 stat != 'OK' 或結構異常 → raise,
    讓 fetch_and_parse_all 在 default_settlement source 全 0 時可 log warning。
    """
    s = (raw_text or "").strip()
    if not s:
        return []
    data = json.loads(s)
    # stat="OK" 必填,其他可能是 "查無資料" 之類的 sentinel
    if not isinstance(data, dict):
        return []
    stat = str(data.get("stat", "")).strip()
    if stat and stat != "OK":
        # 查無資料屬正常,不 raise
        return []
    tables = data.get("tables") or []
    out: list[dict] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        for row in table.get("data", []) or []:
            if not isinstance(row, list) or len(row) < 3:
                continue
            date_iso = normalize_date(row[0])
            if not date_iso:
                continue
            try:
                gross = int(str(row[1]).replace(",", "").strip() or "0")
                net = int(str(row[2]).replace(",", "").strip() or "0")
            except (ValueError, IndexError):
                continue
            out.append({
                "market": "TWSE",
                "report_date": date_iso,
                "gross_amount": gross,
                "net_amount": net,
                "source_url": source_url,
            })
    return out


# === TPEx breach(違約交割,2026-05-17 加,主來源)===

def _tpex_breach_url(start_iso: str, end_iso: str) -> str:
    """組 TPEx breach URL。日期參數格式 YYYY/MM/DD(斜線分隔)。"""
    s = start_iso.replace("-", "/")
    e = end_iso.replace("-", "/")
    return f"{URL_TPEX_BREACH_BASE}?response=json&startDate={s}&endDate={e}"


# table[0] 「證券商申報投資人違約金額」每日彙總,fields 例:
#   ["申報日期","類別","買進、賣出合計總金額","買進、賣出相抵後金額"]
# 類別欄位是「上櫃」或「興櫃」字串。
# table[1] 「個股達違約資訊揭露標準(註1)之證券資訊」個股細目,fields 例:
#   ["申報日期","證券名稱","證券代號","證券商名稱","個股違約總金額(註1)"]
_TPEX_BREACH_DAILY_TITLE_KEY = "證券商申報"
_TPEX_BREACH_PERSTOCK_TITLE_KEY = "個股達違約"

# 「類別」中文 → market enum 對應(寫 default_settlement_daily.market)
_TPEX_CATEGORY_TO_MARKET = {
    "上櫃": "TPEX_LISTED",
    "興櫃": "TPEX_EMERGING",
}


def parse_tpex_breach_json(
    raw_text: str, source_url: str,
) -> tuple[list[dict], list[dict]]:
    """TPEx breach JSON → (per_stock_rows, daily_rows)。

    per_stock_rows:list of stock_warnings row(warning_type='default_settlement'),
                    來自 table[1]「個股達違約資訊揭露標準」。涵蓋全市場
                    (含上市股,如 3105)。
    daily_rows:list of default_settlement_daily row(market='TPEX_LISTED'/
                'TPEX_EMERGING'),來自 table[0]「證券商申報投資人違約金額」。

    空 / parse 失敗 → ([], [])(空資料屬正常,不 raise;malformed JSON 才 raise)。
    """
    s = (raw_text or "").strip()
    if not s:
        return [], []
    data = json.loads(s)
    if not isinstance(data, dict):
        return [], []
    tables = data.get("tables") or []

    per_stock: list[dict] = []
    daily: list[dict] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        title = str(table.get("title", "") or "")
        rows = table.get("data", []) or []
        if _TPEX_BREACH_DAILY_TITLE_KEY in title:
            # 每日彙總:[申報日期, 類別, 買賣合計, 買賣相抵]
            for row in rows:
                if not isinstance(row, list) or len(row) < 4:
                    continue
                date_iso = normalize_date(row[0])
                if not date_iso:
                    continue
                market = _TPEX_CATEGORY_TO_MARKET.get(
                    str(row[1] or "").strip()
                )
                if not market:
                    continue
                try:
                    gross = int(str(row[2]).replace(",", "").strip() or "0")
                    net = int(str(row[3]).replace(",", "").strip() or "0")
                except ValueError:
                    continue
                daily.append({
                    "market": market,
                    "report_date": date_iso,
                    "gross_amount": gross,
                    "net_amount": net,
                    "source_url": source_url,
                })
        elif _TPEX_BREACH_PERSTOCK_TITLE_KEY in title:
            # 個股細目:[申報日期, 證券名稱, 證券代號, 證券商名稱, 違約總金額]
            for row in rows:
                if not isinstance(row, list) or len(row) < 5:
                    continue
                date_iso = normalize_date(row[0])
                if not date_iso:
                    continue
                sid = _extract_stock_id(row[2])
                if not sid:
                    continue
                name = str(row[1] or "").strip()
                # 證券商名稱可能用 <br> 串多家,replace 成 ' / ' 給 reason 顯示
                brokers = (
                    str(row[3] or "").strip()
                    .replace("<br>", " / ")
                    .replace("<br/>", " / ")
                    .replace("<br />", " / ")
                )
                try:
                    amount = int(str(row[4]).replace(",", "").strip() or "0")
                except ValueError:
                    amount = 0
                amount_yi = amount / 100_000_000  # 億
                if amount_yi >= 1:
                    amount_pretty = f"{amount_yi:.2f} 億"
                else:
                    amount_pretty = f"{amount / 10_000:,.0f} 萬"
                reason = (
                    f"{name} {sid} 違約交割 {amount_pretty}(申報券商:{brokers})"
                )
                per_stock.append({
                    "stock_id": sid,
                    "warning_type": "default_settlement",
                    "announced_date": date_iso,
                    "effective_from": date_iso,
                    "effective_to": None,
                    "reason": reason[:500],
                    "source_url": source_url,
                })
    return per_stock, daily


def _fetch_twse_default_settlement(
    start_iso: str,
    end_iso: str,
    override: str | None = None,
) -> str:
    """打 TWSE BFIGTU 拿原始 JSON 字串(或 override)。retry 3 次。"""
    if override is not None:
        return override
    url = _twse_bfigtu_url(start_iso, end_iso)
    return fetch_url_with_retry(url, label="TWSE BFIGTU 違約彙總")


def _fetch_tpex_default_settlement(
    start_iso: str,
    end_iso: str,
    override: str | None = None,
) -> str:
    """打 TPEx breach 拿原始 JSON 字串(或 override)。retry 3 次。"""
    if override is not None:
        return override
    url = _tpex_breach_url(start_iso, end_iso)
    return fetch_url_with_retry(url, label="TPEx breach 違約彙總/個股")


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
    default_settlement_days: int = DEFAULT_SETTLEMENT_BACKFILL_DAYS,
    today_iso: str | None = None,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """打所有 source、parse、合併成 rows list(不寫 DB)。

    Args:
        html_overrides: {url: text} — 測試用,跳過真實 HTTP。
            key 對應 _SOURCES 內的 URL_* constant,以及 BFIGTU / TPEx breach
            的具體 URL(含 startDate/endDate query string)。為了測試方便,
            BFIGTU / TPEx breach 額外接受不含 query string 的 base URL
            (URL_TWSE_BFIGTU_BASE / URL_TPEX_BREACH_BASE)當 override key。
        default_settlement_days: TWSE BFIGTU + TPEx breach 抓近 N 天資料
            (預設 90)。日常 schedule 可用較小值(7-14),手動 backfill 用 90+。
        today_iso: 視同今日的 ISO 日期(測試 / backfill 用,預設 UTC today)。

    Returns:
        (warnings_rows, daily_rows, per_source_counts)
          warnings_rows:list of stock_warnings row(個股警示)
          daily_rows:list of default_settlement_daily row(每日全市場違約彙總)
          per_source_counts:{url_or_label: row_count}

    任何 source 抓 / parse 失敗 → raise(讓 CI exit 1 觸發告警),不要 silent skip。

    Baseline 防呆:跑完後若 TWSE punish + TWT85U 兩條源都 0 rows → raise
    (這兩條歷史上一定有資料,同時 0 表示 endpoint 整體壞掉)。

    違約交割:
      - TPEx breach 為主來源(個股細目 + 上櫃/興櫃每日彙總);若 fetch 失敗
        → raise(主來源不能 silent miss)。
      - TWSE BFIGTU(上市每日彙總,無個股細目)。fetch 失敗 → raise
        (主公拍板:違約類 source 任一壞掉都要 fail loud)。
      - MOPS RSS 為輔助來源(視窗 8 筆 / 24h,僅備援);fetch 失敗 → log
        warning 不 raise(避免拖垮主流程)。
    """
    html_overrides = html_overrides or {}
    all_rows: list[dict] = []
    daily_rows: list[dict] = []
    per_source: dict[str, int] = {}

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

    # === 違約交割專區 (TPEx breach 主來源 + TWSE BFIGTU 每日彙總)===
    from datetime import date, timedelta
    if today_iso is None:
        today_iso = datetime.now(timezone.utc).date().isoformat()
    end_iso = today_iso
    start_iso = (
        date.fromisoformat(today_iso) - timedelta(days=int(default_settlement_days))
    ).isoformat()

    # TPEx breach — 主來源(全市場個股細目 + 上櫃/興櫃每日彙總)
    tpex_breach_url = _tpex_breach_url(start_iso, end_iso)
    tpex_text = html_overrides.get(URL_TPEX_BREACH_BASE)
    if tpex_text is None:
        tpex_text = html_overrides.get(tpex_breach_url)
    if tpex_text is None:
        tpex_text = _fetch_tpex_default_settlement(start_iso, end_iso)
    try:
        tpex_perstock, tpex_daily = parse_tpex_breach_json(
            tpex_text, source_url=URL_TPEX_BREACH_BASE,
        )
    except Exception as ex:
        raise RuntimeError(
            f"[WARNINGS] parse TPEx breach 失敗:"
            f"{type(ex).__name__}: {ex}"
        ) from ex
    per_source["TPEx_breach_perstock"] = len(tpex_perstock)
    per_source["TPEx_breach_daily"] = len(tpex_daily)
    print(
        f"[WARNINGS] [TPEx] breach_perstock            "
        f"{len(tpex_perstock):>4d} rows",
        flush=True,
    )
    print(
        f"[WARNINGS] [TPEx] breach_daily_aggregate     "
        f"{len(tpex_daily):>4d} rows",
        flush=True,
    )
    all_rows.extend(tpex_perstock)
    daily_rows.extend(tpex_daily)

    # TWSE BFIGTU — 上市每日彙總(無個股細目,TWSE 不公開)
    twse_bfigtu_url = _twse_bfigtu_url(start_iso, end_iso)
    twse_text = html_overrides.get(URL_TWSE_BFIGTU_BASE)
    if twse_text is None:
        twse_text = html_overrides.get(twse_bfigtu_url)
    if twse_text is None:
        twse_text = _fetch_twse_default_settlement(start_iso, end_iso)
    try:
        twse_daily = parse_twse_bfigtu_json(
            twse_text, source_url=URL_TWSE_BFIGTU_BASE,
        )
    except Exception as ex:
        raise RuntimeError(
            f"[WARNINGS] parse TWSE BFIGTU 失敗:"
            f"{type(ex).__name__}: {ex}"
        ) from ex
    per_source["TWSE_BFIGTU_daily"] = len(twse_daily)
    print(
        f"[WARNINGS] [TWSE] BFIGTU_daily_aggregate     "
        f"{len(twse_daily):>4d} rows",
        flush=True,
    )
    daily_rows.extend(twse_daily)

    # === MOPS 違約交割 RSS(輔助來源:cp950 編碼,8 筆 / 24h)===
    # 實測視窗極短,僅備援(主來源是 TPEx breach 第 2 表)。fetch 失敗 → log 不
    # raise,不阻擋主流程(主來源已涵蓋個股違約)。
    mops_url = URL_MOPS_DEFAULT_SETTLEMENT_RSS
    if mops_url in html_overrides:
        mops_text = html_overrides[mops_url]
    else:
        try:
            mops_text = _fetch_mops_rss_text(mops_url)
        except Exception as ex:
            logger.warning(
                "[WARNINGS] MOPS 違約交割 RSS fetch 失敗(輔助來源,不阻擋):"
                "%s: %s — 本次 run 跳過",
                type(ex).__name__, ex,
            )
            mops_text = ""

    try:
        mops_rows = parse_mops_default_settlement_rss(mops_text, mops_url)
    except Exception as ex:
        raise RuntimeError(
            f"[WARNINGS] parse MOPS default_settlement 失敗:"
            f"{type(ex).__name__}: {ex}"
        ) from ex

    per_source[mops_url] = len(mops_rows)
    print(
        f"[WARNINGS] [MOPS] default_settlement_rss     "
        f"{len(mops_rows):>4d} rows (輔助)",
        flush=True,
    )
    all_rows.extend(mops_rows)

    return all_rows, daily_rows, per_source


def run(
    html_overrides: dict[str, str] | None = None,
    db_path: str | Path | None = None,
    default_settlement_days: int = DEFAULT_SETTLEMENT_BACKFILL_DAYS,
    today_iso: str | None = None,
) -> dict:
    """主流程:抓 + parse 全部來源 → upsert stock_warnings + default_settlement_daily。

    Args:
        default_settlement_days: 違約交割抓近 N 天(backfill 可給較大值)。
        today_iso: 視同今日 ISO date(測試用)。

    Returns summary dict {rows_parsed, rows_written, daily_rows_written, by_type,
                          per_source, elapsed_secs}.
    """
    t0 = time.time()
    db.init_db(db_path)
    rows, daily_rows, per_source = fetch_and_parse_all(
        html_overrides=html_overrides,
        default_settlement_days=default_settlement_days,
        today_iso=today_iso,
    )
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in rows:
        r.setdefault("fetched_at", fetched_at)
    for r in daily_rows:
        r.setdefault("fetched_at", fetched_at)
    n_written = db.upsert_stock_warnings(rows, db_path=db_path)
    n_daily_written = db.upsert_default_settlement_daily(
        daily_rows, db_path=db_path,
    )
    by_type: dict[str, int] = {}
    for r in rows:
        wt = r["warning_type"]
        by_type[wt] = by_type.get(wt, 0) + 1
    elapsed = round(time.time() - t0, 2)
    print(
        f"[WARNINGS] DONE rows_parsed={len(rows)} rows_written={n_written} "
        f"daily_rows_written={n_daily_written} by_type={by_type} "
        f"elapsed={elapsed}s",
        flush=True,
    )
    return {
        "rows_parsed": len(rows),
        "rows_written": n_written,
        "daily_rows_parsed": len(daily_rows),
        "daily_rows_written": n_daily_written,
        "by_type": by_type,
        "per_source": per_source,
        "elapsed_secs": elapsed,
    }


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "TWSE / TPEx 警示股紀錄抓取 + upsert"
            "(stock_warnings + default_settlement_daily)"
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只 fetch + parse,不寫 DB(smoke 用)",
    )
    p.add_argument(
        "--backfill-default-settlement", action="store_true",
        help=(
            "違約交割 backfill 模式:--days 給的天數套用到 TWSE BFIGTU + "
            "TPEx breach 兩條 endpoint(都支援 6 個月以內單次窗口),"
            "歷史違約資料一併補進 DB。"
        ),
    )
    p.add_argument(
        "--days", type=int, default=None,
        help=(
            f"違約交割抓近 N 天(預設 {DEFAULT_SETTLEMENT_BACKFILL_DAYS});"
            "搭配 --backfill-default-settlement 可拉更長窗口(建議 ≤180)。"
        ),
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    setup_file_logging("fetch_stock_warnings", mirror_print=True)

    days = args.days
    if days is None:
        days = DEFAULT_SETTLEMENT_BACKFILL_DAYS
    if args.backfill_default_settlement and args.days is None:
        # backfill 預設拉 180 天(6 個月,endpoint 支援單次)
        days = 180
        print(
            "[WARNINGS] --backfill-default-settlement 模式,預設拉 180 天",
            flush=True,
        )
    if days > 365:
        print(
            f"[WARNINGS] WARN: --days={days} 超過 1 年,endpoint 可能拒絕或回斷層,"
            "建議分次跑 ≤180 天窗口",
            flush=True,
        )

    try:
        if args.dry_run:
            rows, daily_rows, _ = fetch_and_parse_all(
                default_settlement_days=days,
            )
            print(
                f"[WARNINGS] DRY RUN parsed {len(rows)} warning rows "
                f"+ {len(daily_rows)} daily aggregate rows",
                flush=True,
            )
            return 0
        summary = run(default_settlement_days=days)
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
