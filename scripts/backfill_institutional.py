"""排程入口:補 institutional 表缺失的歷史資料(date-by-date, all stocks)。

背景
----
`daily_prices` 從 2024-01-02 開始有資料(~569 交易日),`institutional` 只回溯到
2025-11-04(~125 天)。缺 22 個月歷史 → 法人籌碼 long backtest 跑不了。

設計
----
不同於 backfill_revenue / backfill_dividend 的 per-stock shard 模式,本 script
**以日期為單位**遍歷:每個交易日打 1 次 TWSE T86(全市場 ~1100 檔)+ 1 次 TPEx
3insti(OTC ~700 檔),merge 後 bulk upsert。

這樣 22 月 × ~21 交易日 = ~440 date-batches(而不是 ~1700 stocks)。

資料源優先序
------------
1. **TWSE OpenAPI (legacy JSON endpoint)** — `rwd/zh/fund/T86`,無 token,日期參數
   `YYYYMMDD`,回 18-19 column 上市股全市場
2. **TPEx legacy endpoint** — `web/stock/3insti/daily_trade/3itrade_hedge_result.php`,
   無 token,日期參數 `ROC/MM/DD`(民國年),回 OTC 全市場
3. **FinMind fallback** — 上面兩個都失敗時改用
   `TaiwanStockInstitutionalInvestorsBuySell`(start_date+end_date+無 data_id),
   單次 call 拿整個日期區間。需要 `FINMIND_TOKEN` env 才會啟用。

CLI
---
::

    # default: 2024-01-02 ~ 2025-11-03 (補 daily_prices 跟 institutional 之間的 22 月落差)
    python scripts/backfill_institutional.py

    # 試水溫(1 個月)
    python scripts/backfill_institutional.py --start 2025-10-01 --end 2025-11-03

    # 自訂 sleep / retry
    python scripts/backfill_institutional.py --sleep 2.0 --max-retries 5

    # 補完後同時 dump 進 data/twse_snapshot/institutional.parquet(雲端 reload 用,zstd 壓縮)
    python scripts/backfill_institutional.py --dump-format parquet

    # 舊行為(CSV)— 22 月全市場約 280MB,撞 GitHub 100MB 上限,僅 debug 用
    python scripts/backfill_institutional.py --dump-format csv

Exit code
---------
- 0  全部 OK 或大部分 OK(失敗日 < 全部 25%)
- 1  超過 25% 日期失敗(rate ban / endpoint 掛掉)
- 2  CLI 參數錯誤
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src import config  # noqa: E402
from src._retry import with_retry  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"

# === Endpoints ===
TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_3INSTI_URL = (
    "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
)
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


# === Endpoint exceptions ===

class EndpointError(RuntimeError):
    """HTTP / parse 錯誤。會 raise 給 with_retry 重試,全部失敗才往上拋。"""


# === Date helpers ===

def _iter_workdays(start: str, end: str) -> Iterable[str]:
    """生成 [start, end] 區間內所有工作日(週一到週五),ISO 格式。

    國定假日無法事前精準排除(每年不同),回到迴圈內處理:打 API 拿空回應就跳過。
    """
    d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    while d <= end_d:
        if d.weekday() < 5:
            yield d.isoformat()
        d += timedelta(days=1)


def _iso_to_roc(iso: str) -> str:
    """'2025-11-03' → '114/11/03' (TPEx 用)。"""
    dt = date.fromisoformat(iso)
    return f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"


def _iso_to_twse(iso: str) -> str:
    """'2025-11-03' → '20251103' (TWSE T86 用)。"""
    return iso.replace("-", "")


# === Parsing helpers ===

def _parse_int(s) -> int:
    """把 TWSE/TPEx 回的 '1,234,567' / '--' / '' / None 轉成 int(預設 0)。"""
    if s is None:
        return 0
    if isinstance(s, (int, float)):
        try:
            return int(s)
        except (ValueError, OverflowError):
            return 0
    t = str(s).strip().replace(",", "")
    if not t or t == "--" or t == "-":
        return 0
    try:
        return int(float(t))
    except (ValueError, OverflowError):
        return 0


# === TWSE T86 ===

def _twse_field_index(fields: list[str]) -> dict[str, int]:
    """從 TWSE T86 的 fields 名稱解析出 column index map。

    欄位名常見:
      - 證券代號 / 證券名稱
      - 外陸資買賣超股數(不含外資自營商)
      - 外資自營商買賣超股數
      - 投信買賣超股數
      - 自營商買賣超股數(自行買賣) / 自營商買賣超股數(避險)
      - 三大法人買賣超股數

    回 {'sid', 'foreign_excl_prop', 'foreign_prop', 'trust',
        'dealer_self', 'dealer_hedge', 'dealer_total', 'total'}
    缺欄回 -1。
    """
    idx = {
        "sid": -1, "foreign_excl_prop": -1, "foreign_prop": -1, "trust": -1,
        "dealer_self": -1, "dealer_hedge": -1, "dealer_total": -1, "total": -1,
    }
    for i, name in enumerate(fields):
        n = name.replace(" ", "")
        if "證券代號" in n:
            idx["sid"] = i
        elif n.startswith("外陸資買賣超"):
            # 「外陸資買賣超股數(不含外資自營商)」— 字串內雖含「外資自營商」(備註),
            # 但只要看 prefix 就能跟下一個「外資自營商買賣超股數」區分。
            idx["foreign_excl_prop"] = i
        elif n.startswith("外資自營商買賣超"):
            idx["foreign_prop"] = i
        elif n.startswith("投信買賣超"):
            idx["trust"] = i
        elif n.startswith("自營商買賣超") and "自行" in n:
            idx["dealer_self"] = i
        elif n.startswith("自營商買賣超") and "避險" in n:
            idx["dealer_hedge"] = i
        elif n.startswith("三大法人買賣超"):
            idx["total"] = i
        elif n.startswith("自營商買賣超股數") and idx["dealer_total"] == -1:
            # 單一彙總欄(老格式),沒有 self / hedge 子欄。注意此 branch 排
            # 在 dealer_self / dealer_hedge 之後,有子分類時不會搶分配。
            idx["dealer_total"] = i
    return idx


def parse_twse_t86(payload: dict, iso_date: str) -> list[dict]:
    """把 TWSE T86 JSON 解析成 institutional rows。

    Args:
      payload: requests.get(...).json() 的結果
      iso_date: 'YYYY-MM-DD',用來寫進 row['date']

    Returns:
      list[dict],每筆 = {stock_id, date, foreign_buy_sell, trust_buy_sell,
                         dealer_buy_sell, total_buy_sell}。
      非交易日 / 沒資料 → 空 list(不 raise)。
    """
    if not payload or payload.get("stat") not in ("OK", None):
        return []
    fields = payload.get("fields") or []
    data = payload.get("data") or []
    if not fields or not data:
        return []
    idx = _twse_field_index(fields)
    if idx["sid"] < 0:
        raise EndpointError(
            f"TWSE T86 找不到證券代號欄位: fields={fields[:6]}"
        )
    rows: list[dict] = []
    for r in data:
        if not r or len(r) <= idx["sid"]:
            continue
        sid = str(r[idx["sid"]]).strip()
        if not sid:
            continue
        # 外資合計 = 外陸資(不含外資自營) + 外資自營商
        foreign = (
            _parse_int(r[idx["foreign_excl_prop"]])
            if idx["foreign_excl_prop"] >= 0 else 0
        ) + (
            _parse_int(r[idx["foreign_prop"]])
            if idx["foreign_prop"] >= 0 else 0
        )
        trust = _parse_int(r[idx["trust"]]) if idx["trust"] >= 0 else 0
        # 自營合計優先 self+hedge,沒有的話用 dealer_total
        if idx["dealer_self"] >= 0 or idx["dealer_hedge"] >= 0:
            dealer = (
                (_parse_int(r[idx["dealer_self"]]) if idx["dealer_self"] >= 0 else 0)
                + (_parse_int(r[idx["dealer_hedge"]]) if idx["dealer_hedge"] >= 0 else 0)
            )
        elif idx["dealer_total"] >= 0:
            dealer = _parse_int(r[idx["dealer_total"]])
        else:
            dealer = 0
        if idx["total"] >= 0:
            total = _parse_int(r[idx["total"]])
        else:
            total = foreign + trust + dealer
        rows.append({
            "stock_id": sid,
            "date": iso_date,
            "foreign_buy_sell": foreign,
            "trust_buy_sell": trust,
            "dealer_buy_sell": dealer,
            "total_buy_sell": total,
        })
    return rows


def fetch_twse_t86(
    iso_date: str,
    session: requests.Session | None = None,
    timeout: float = 30.0,
) -> list[dict]:
    """打 TWSE T86 endpoint,回 parsed rows。"""
    params = {
        "date": _iso_to_twse(iso_date),
        "selectType": "ALL",
        "response": "json",
    }
    sess = session or requests
    try:
        resp = sess.get(TWSE_T86_URL, params=params, timeout=timeout)
    except requests.RequestException as ex:
        raise EndpointError(f"TWSE T86 網路錯誤: {ex}") from ex
    if resp.status_code != 200:
        raise EndpointError(
            f"TWSE T86 HTTP {resp.status_code}: {resp.text[:200]}"
        )
    try:
        payload = resp.json()
    except ValueError as ex:
        raise EndpointError(f"TWSE T86 非 JSON: {resp.text[:200]}") from ex
    return parse_twse_t86(payload, iso_date)


# === TPEx 3insti ===

def _tpex_field_index(fields: list[str]) -> dict[str, int]:
    """從 TPEx 3insti 的 fields 名稱解析出 column index map。

    TPEx 有兩種變體:

    A) **新格式(2024+)**: 24-25 欄,欄位名稱「重複」— '代號', '名稱', 後接 7 組
       buy/sell/net,每組欄名都叫「買進股數/賣出股數/買賣超股數」。完全靠 position
       區分,fields 文字相同無法 disambiguate。對應位置:
         2-4   外資及陸資 (excl prop) 買/賣/淨
         5-7   外資自營商
         8-10  外資合計
         11-13 投信
         14-16 自營商自行
         17-19 自營商避險
         20-22 自營商合計
         23    三大法人合計

    B) **舊格式(語意化命名)**: 欄位名稱含「外資及陸資」「外資自營商」等可
       辨識的 prefix,直接 name-based。

    自動偵測:看 fields 中是否有可辨識的 semantic prefix。
    """
    idx = {
        "sid": -1, "foreign_excl_prop": -1, "foreign_prop": -1, "trust": -1,
        "dealer_self": -1, "dealer_hedge": -1, "dealer_total": -1, "total": -1,
    }
    # 先找 sid / total — 兩個格式都該有
    for i, name in enumerate(fields):
        n = name.replace(" ", "")
        if idx["sid"] == -1 and ("代號" in n or "證券代號" in n):
            idx["sid"] = i
        elif n.startswith("三大法人買賣超"):
            idx["total"] = i

    # 偵測是否為新格式(repeated 「買賣超股數」)
    net_positions = [
        i for i, name in enumerate(fields)
        if name.replace(" ", "").startswith("買賣超股數")
    ]
    # 新格式:7 個重複的「買賣超股數」,在 idx 4/7/10/13/16/19/22
    if len(net_positions) >= 7 and net_positions[:3] == [4, 7, 10]:
        idx["foreign_excl_prop"] = 4
        idx["foreign_prop"] = 7
        # 8-10 是外資合計,跳過(會跟 excl_prop + prop 相加重複)
        idx["trust"] = 13
        idx["dealer_self"] = 16
        idx["dealer_hedge"] = 19
        # 20-22 是自營商合計,跳過(會跟 self + hedge 相加重複)
        # total 已經在上面從欄名 detect 到
        return idx

    # 舊格式 — 用 semantic prefix match
    for i, name in enumerate(fields):
        n = name.replace(" ", "")
        if n.startswith("外資及陸資") and "買賣超" in n:
            idx["foreign_excl_prop"] = i
        elif n.startswith("外資自營商") and "買賣超" in n:
            idx["foreign_prop"] = i
        elif n.startswith("投信買賣超"):
            idx["trust"] = i
        elif n.startswith("自營商") and "自行" in n and "買賣超" in n:
            idx["dealer_self"] = i
        elif n.startswith("自營商") and "避險" in n and "買賣超" in n:
            idx["dealer_hedge"] = i
        elif n.startswith("自營商買賣超") and idx["dealer_total"] == -1:
            idx["dealer_total"] = i
    return idx


def parse_tpex_3insti(payload: dict, iso_date: str) -> list[dict]:
    """把 TPEx 3insti JSON 解析成 institutional rows。

    TPEx 回傳結構幾種變體都看過:
      - 老格式: top-level 'aaData'(jQuery DataTables)
      - 新格式: top-level 'tables'[0]['data']  /  top-level 'data'
    我們三種都試。
    """
    if not payload:
        return []
    fields: list[str] = []
    data: list = []
    # 試 tables[0]
    if isinstance(payload.get("tables"), list) and payload["tables"]:
        t0 = payload["tables"][0]
        fields = t0.get("fields") or payload.get("fields") or []
        data = t0.get("data") or []
    if not data:
        data = payload.get("aaData") or payload.get("data") or []
    if not fields:
        fields = payload.get("fields") or []
    if not data:
        return []
    if not fields:
        # 沒 fields → 用 fallback 固定 index(舊 aaData 格式常見)
        # 多數舊格式: [代號, 名稱, 外資買, 外資賣, 外資淨, 投信買, 投信賣, 投信淨,
        #              自營買, 自營賣, 自營淨, ..., 三大法人淨]
        return _parse_tpex_fallback(data, iso_date)
    idx = _tpex_field_index(fields)
    if idx["sid"] < 0:
        raise EndpointError(
            f"TPEx 3insti 找不到代號欄位: fields={fields[:6]}"
        )
    rows: list[dict] = []
    for r in data:
        if not r or len(r) <= idx["sid"]:
            continue
        sid = str(r[idx["sid"]]).strip()
        if not sid:
            continue
        foreign = (
            (_parse_int(r[idx["foreign_excl_prop"]])
             if idx["foreign_excl_prop"] >= 0 else 0)
            + (_parse_int(r[idx["foreign_prop"]])
               if idx["foreign_prop"] >= 0 else 0)
        )
        trust = _parse_int(r[idx["trust"]]) if idx["trust"] >= 0 else 0
        if idx["dealer_self"] >= 0 or idx["dealer_hedge"] >= 0:
            dealer = (
                (_parse_int(r[idx["dealer_self"]]) if idx["dealer_self"] >= 0 else 0)
                + (_parse_int(r[idx["dealer_hedge"]])
                   if idx["dealer_hedge"] >= 0 else 0)
            )
        elif idx["dealer_total"] >= 0:
            dealer = _parse_int(r[idx["dealer_total"]])
        else:
            dealer = 0
        if idx["total"] >= 0:
            total = _parse_int(r[idx["total"]])
        else:
            total = foreign + trust + dealer
        rows.append({
            "stock_id": sid,
            "date": iso_date,
            "foreign_buy_sell": foreign,
            "trust_buy_sell": trust,
            "dealer_buy_sell": dealer,
            "total_buy_sell": total,
        })
    return rows


def _parse_tpex_fallback(data: list, iso_date: str) -> list[dict]:
    """沒 fields header 時的硬編碼解析(老 aaData 格式)。

    保守一點 — 用 col 0=sid,col 4=foreign_net,col 7=trust_net,col 10=dealer_net,
    最後一欄 = total。
    """
    rows: list[dict] = []
    for r in data:
        if not r or len(r) < 5:
            continue
        sid = str(r[0]).strip()
        if not sid:
            continue
        foreign = _parse_int(r[4]) if len(r) > 4 else 0
        trust = _parse_int(r[7]) if len(r) > 7 else 0
        dealer = _parse_int(r[10]) if len(r) > 10 else 0
        total = _parse_int(r[-1]) if len(r) >= 12 else foreign + trust + dealer
        rows.append({
            "stock_id": sid,
            "date": iso_date,
            "foreign_buy_sell": foreign,
            "trust_buy_sell": trust,
            "dealer_buy_sell": dealer,
            "total_buy_sell": total,
        })
    return rows


def fetch_tpex_3insti(
    iso_date: str,
    session: requests.Session | None = None,
    timeout: float = 30.0,
) -> list[dict]:
    """打 TPEx 3insti endpoint,回 parsed rows。"""
    params = {
        "l": "zh-tw",
        "se": "AL",
        "t": "D",
        "d": _iso_to_roc(iso_date),
    }
    sess = session or requests
    try:
        resp = sess.get(TPEX_3INSTI_URL, params=params, timeout=timeout)
    except requests.RequestException as ex:
        raise EndpointError(f"TPEx 3insti 網路錯誤: {ex}") from ex
    if resp.status_code != 200:
        raise EndpointError(
            f"TPEx 3insti HTTP {resp.status_code}: {resp.text[:200]}"
        )
    try:
        payload = resp.json()
    except ValueError as ex:
        raise EndpointError(f"TPEx 3insti 非 JSON: {resp.text[:200]}") from ex
    return parse_tpex_3insti(payload, iso_date)


# === FinMind fallback ===

def fetch_finmind_range(
    start: str,
    end: str,
    token: str | None = None,
    session: requests.Session | None = None,
    timeout: float = 60.0,
) -> list[dict]:
    """FinMind fallback:一次抓 [start, end] 全市場(無 data_id)。

    成功回 institutional rows(已 pivot per stock/date)。
    失敗 raise EndpointError。
    """
    params = {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "start_date": start,
        "end_date": end,
    }
    if token:
        params["token"] = token
    sess = session or requests
    try:
        resp = sess.get(FINMIND_URL, params=params, timeout=timeout)
    except requests.RequestException as ex:
        raise EndpointError(f"FinMind 網路錯誤: {ex}") from ex
    try:
        payload = resp.json()
    except ValueError as ex:
        raise EndpointError(f"FinMind 非 JSON: {resp.text[:200]}") from ex
    if payload.get("status") != 200:
        raise EndpointError(
            f"FinMind status={payload.get('status')} "
            f"msg={payload.get('msg')}"
        )
    raw = payload.get("data") or []
    if not isinstance(raw, list):
        raise EndpointError(f"FinMind data 非 list: type={type(raw).__name__}")
    # 用 data_fetcher._pivot_institutional 的同一邏輯
    grouped: dict[tuple[str, str], dict] = {}
    for r in raw:
        sid = r.get("stock_id")
        d = r.get("date")
        if not sid or not d:
            continue
        key = (str(sid), str(d))
        if key not in grouped:
            grouped[key] = {
                "stock_id": key[0],
                "date": key[1],
                "foreign_buy_sell": 0,
                "trust_buy_sell": 0,
                "dealer_buy_sell": 0,
            }
        net = (r.get("buy") or 0) - (r.get("sell") or 0)
        name = (r.get("name") or "").lower()
        if "foreign" in name:
            grouped[key]["foreign_buy_sell"] += net
        elif "trust" in name:
            grouped[key]["trust_buy_sell"] += net
        elif "dealer" in name:
            grouped[key]["dealer_buy_sell"] += net
    for v in grouped.values():
        v["total_buy_sell"] = (
            v["foreign_buy_sell"] + v["trust_buy_sell"] + v["dealer_buy_sell"]
        )
    return list(grouped.values())


# === Existing-date check ===

def existing_dates(
    start: str, end: str, db_path: str | Path | None = None,
    min_rows: int = 100,
) -> set[str]:
    """回 institutional 表中 [start, end] 範圍內,row 數 >= min_rows 的日期集合。

    min_rows=100 是門檻:正常交易日 ~1700 檔,partially-loaded 日(<100)當沒有,
    讓後續 backfill 補齊。
    """
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT date, COUNT(*) as c FROM institutional "
            "WHERE date BETWEEN ? AND ? GROUP BY date HAVING c >= ?",
            (start, end, min_rows),
        ).fetchall()
    return {r["date"] for r in rows}


# === Main per-date orchestration ===

def backfill_one_date(
    iso_date: str,
    session: requests.Session,
    *,
    max_retries: int = 3,
    use_finmind_fallback: bool = True,
    finmind_token: str | None = None,
    db_path: str | Path | None = None,
) -> tuple[int, str]:
    """處理單一日期 — 抓 TWSE + TPEx,merge,upsert。

    Returns:
      (rows_upserted, source)。source ∈ {'twse+tpex', 'twse_only', 'tpex_only',
                                          'finmind', 'empty'}
    Raises:
      EndpointError 當所有 source 都失敗。
    """
    twse_rows: list[dict] = []
    tpex_rows: list[dict] = []
    twse_err: Exception | None = None
    tpex_err: Exception | None = None

    try:
        twse_rows = with_retry(
            lambda: fetch_twse_t86(iso_date, session=session),
            max_attempts=max_retries,
            base_delay=2.0,
            label=f"TWSE T86 {iso_date}",
            quiet=True,
        )
    except Exception as ex:  # noqa: BLE001
        twse_err = ex
        logger.warning("TWSE T86 %s 失敗: %s", iso_date, ex)

    # TWSE 跟 TPEx 之間 sleep,避免兩個 host 一起被觸發
    time.sleep(1.0)

    try:
        tpex_rows = with_retry(
            lambda: fetch_tpex_3insti(iso_date, session=session),
            max_attempts=max_retries,
            base_delay=2.0,
            label=f"TPEx 3insti {iso_date}",
            quiet=True,
        )
    except Exception as ex:  # noqa: BLE001
        tpex_err = ex
        logger.warning("TPEx 3insti %s 失敗: %s", iso_date, ex)

    combined = twse_rows + tpex_rows
    source = "empty"
    if twse_rows and tpex_rows:
        source = "twse+tpex"
    elif twse_rows:
        source = "twse_only"
    elif tpex_rows:
        source = "tpex_only"

    # 兩個都失敗 → FinMind fallback(若有 token)
    if not combined and use_finmind_fallback and finmind_token:
        try:
            combined = fetch_finmind_range(
                iso_date, iso_date, token=finmind_token, session=session,
            )
            if combined:
                source = "finmind"
        except Exception as ex:  # noqa: BLE001
            logger.warning("FinMind fallback %s 失敗: %s", iso_date, ex)

    if not combined:
        # 兩個 endpoint 都拿不到資料:可能是國定假日(空 data) 或 都掛
        if twse_err is not None and tpex_err is not None:
            raise EndpointError(
                f"{iso_date}: TWSE + TPEx 都失敗 — "
                f"twse={twse_err} | tpex={tpex_err}"
            )
        # 沒 error 但空 data → 假日,正常 skip
        return 0, "empty"

    db.upsert_institutional(combined, db_path=db_path)
    return len(combined), source


def _load_existing_snapshot() -> pd.DataFrame:
    """讀既有 institutional 快照(parquet 優先,fallback csv,都沒 → 空 DF)。

    回 DataFrame schema 跟 db.institutional 表一致(stock_id, date, foreign_buy_sell,
    trust_buy_sell, dealer_buy_sell, total_buy_sell)。
    """
    pq = SNAPSHOT_DIR / "institutional.parquet"
    csv = SNAPSHOT_DIR / "institutional.csv"
    if pq.exists():
        try:
            df = pd.read_parquet(pq)
            if "stock_id" in df.columns:
                df["stock_id"] = df["stock_id"].astype(str)
            return df
        except Exception as ex:  # noqa: BLE001
            logger.warning("讀既有 parquet 失敗,fallback csv: %s", ex)
    if csv.exists():
        try:
            return pd.read_csv(csv, dtype={"stock_id": str})
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
    return pd.DataFrame()


def dump_snapshot(
    start: str,
    end: str,
    fmt: str = "parquet",
    db_path: str | Path | None = None,
) -> int:
    """把 [start, end] 範圍的 institutional 資料 dump 成 snapshot 檔。

    `fmt` ∈ {'parquet', 'csv'}:
      - 'parquet'(預設):寫 institutional.parquet,zstd level 9 壓縮
        (對 22 月 ~280MB CSV 壓到 ~30MB,避過 GitHub 100MB single-file 上限)
      - 'csv':寫 institutional.csv(舊行為,debug / 向後相容)

    既有 snapshot(parquet/csv 任一)讀進來 merge,同 (stock_id, date) 用 new 蓋掉。
    """
    if fmt not in ("parquet", "csv"):
        raise ValueError(f"fmt 必須是 'parquet' 或 'csv',收到: {fmt!r}")

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    with db.get_conn(db_path) as conn:
        new_df = pd.read_sql(
            "SELECT stock_id, date, foreign_buy_sell, trust_buy_sell, "
            "dealer_buy_sell, total_buy_sell FROM institutional "
            "WHERE date BETWEEN ? AND ? ORDER BY date, stock_id",
            conn, params=(start, end),
        )
    if new_df.empty:
        logger.info(
            "DB 沒有 [%s, %s] 範圍的資料,skip dump %s",
            start, end, fmt,
        )
        return 0
    if "stock_id" in new_df.columns:
        new_df["stock_id"] = new_df["stock_id"].astype(str)

    old_df = _load_existing_snapshot()
    if not old_df.empty:
        merged = pd.concat([old_df, new_df], ignore_index=True)
        merged = merged.drop_duplicates(
            subset=["stock_id", "date"], keep="last",
        ).sort_values(["date", "stock_id"]).reset_index(drop=True)
    else:
        merged = new_df

    if fmt == "parquet":
        out_path = SNAPSHOT_DIR / "institutional.parquet"
        # zstd level 9:壓比 ~9-10x vs CSV;pyarrow 預設 engine
        merged.to_parquet(
            out_path, compression="zstd", compression_level=9, index=False,
        )
    else:
        out_path = SNAPSHOT_DIR / "institutional.csv"
        merged.to_csv(out_path, index=False)

    logger.info(
        "[%s] 寫 %s: %d 行(merge 後)",
        fmt.upper(), out_path.name, len(merged),
    )
    return len(merged)


def dump_snapshot_csv(
    start: str, end: str, db_path: str | Path | None = None,
) -> int:
    """向後相容 wrapper:呼叫 dump_snapshot(fmt='csv')。

    舊測試 / 外部 caller 用。新流程請用 dump_snapshot(..., fmt='parquet')。
    """
    return dump_snapshot(start, end, fmt="csv", db_path=db_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="補 institutional 表缺失的歷史資料(TWSE + TPEx)",
    )
    parser.add_argument(
        "--start", default="2024-01-02",
        help="起始日(ISO,含),預設 2024-01-02(daily_prices 同步起點)",
    )
    parser.add_argument(
        "--end", default="2025-11-03",
        help="終止日(ISO,含),預設 2025-11-03(institutional 既有資料前 1 日)",
    )
    parser.add_argument(
        "--sleep", type=float, default=2.0,
        help="每個日期間 sleep 秒數(default 2.0,避免 TWSE/TPEx ban)",
    )
    parser.add_argument(
        "--max-retries", type=int, default=3,
        help="單一 endpoint 失敗重試次數(default 3)",
    )
    parser.add_argument(
        "--no-finmind-fallback", action="store_true",
        help="關掉 FinMind fallback(只用 TWSE/TPEx)",
    )
    parser.add_argument(
        "--dump-format", choices=["parquet", "csv", "none"], default="none",
        help=(
            "跑完後 dump 進 data/twse_snapshot/institutional.{parquet|csv}。"
            "parquet 預設用 zstd lvl 9(壓比 ~9x,避過 GitHub 100MB 上限);"
            "csv 為舊行為(22 月全量 ~280MB)。預設 none = 不 dump"
        ),
    )
    parser.add_argument(
        "--dump-csv", action="store_true",
        help=(
            "[DEPRECATED] 等同 --dump-format csv,留著相容既有 workflow。"
            "新用法請改 --dump-format parquet"
        ),
    )
    parser.add_argument(
        "--progress-every", type=int, default=50,
        help="每 N 個日期印一次進度(default 50)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="只跑前 N 個工作日(debug 用,0 = 全跑)",
    )
    args = parser.parse_args(argv)

    setup_file_logging("backfill_institutional")

    try:
        date.fromisoformat(args.start)
        date.fromisoformat(args.end)
    except ValueError as ex:
        print(f"❌ 日期格式錯誤: {ex}", file=sys.stderr, flush=True)
        return 2
    if args.start > args.end:
        print("❌ --start 必須 <= --end", file=sys.stderr, flush=True)
        return 2

    db.init_db()
    existing = existing_dates(args.start, args.end)
    workdays = list(_iter_workdays(args.start, args.end))
    todo = [d for d in workdays if d not in existing]
    if args.limit > 0:
        todo = todo[: args.limit]

    logger.info(
        "[BACKFILL-INST] 區間 %s ~ %s: %d 工作日,已有 %d,待補 %d",
        args.start, args.end, len(workdays), len(existing), len(todo),
    )
    if not todo:
        logger.info("[BACKFILL-INST] 全部都有了,nothing to do")
        return 0

    finmind_token = config.FINMIND_TOKEN if not args.no_finmind_fallback else None
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; stock-screener-backfill/1.0; "
            "+https://github.com/jjen0206/stock-screener)"
        ),
    })

    t0 = time.time()
    ok = 0
    fail = 0
    holiday = 0
    total_rows = 0
    source_counts: dict[str, int] = {}

    for i, d in enumerate(todo, start=1):
        try:
            n, source = backfill_one_date(
                d, session,
                max_retries=args.max_retries,
                use_finmind_fallback=not args.no_finmind_fallback,
                finmind_token=finmind_token,
            )
            if n == 0:
                holiday += 1
            else:
                ok += 1
                total_rows += n
                source_counts[source] = source_counts.get(source, 0) + 1
        except Exception as ex:  # noqa: BLE001
            fail += 1
            logger.error("[BACKFILL-INST] %s 失敗: %s", d, ex)
            if fail > max(20, len(todo) // 4):
                logger.error(
                    "[BACKFILL-INST] 失敗 > 25%%,中斷(避免 ban)",
                )
                break

        if i % args.progress_every == 0 or i == len(todo):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta_min = (len(todo) - i) / rate / 60 if rate > 0 else 0
            logger.info(
                "[BACKFILL-INST] 進度 %d/%d ok=%d hol=%d fail=%d "
                "rows=%d ETA=%.1fm",
                i, len(todo), ok, holiday, fail, total_rows, eta_min,
            )

        time.sleep(args.sleep)

    logger.info(
        "[BACKFILL-INST] 完成: ok=%d hol(假日空回)=%d fail=%d rows=%d",
        ok, holiday, fail, total_rows,
    )
    if source_counts:
        logger.info("[BACKFILL-INST] 來源分布: %s", source_counts)

    # --dump-csv (legacy) → --dump-format csv
    dump_fmt = args.dump_format
    if args.dump_csv and dump_fmt == "none":
        dump_fmt = "csv"
    if dump_fmt != "none":
        dump_snapshot(args.start, args.end, fmt=dump_fmt)

    # exit code: fail > 25% 視為失敗
    if fail > 0 and fail > len(todo) // 4:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
