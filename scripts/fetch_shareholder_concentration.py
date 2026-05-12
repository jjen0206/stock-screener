"""TDCC 集保「股權分散表」千張大戶週快照抓取 CLI。

使用範例:
    # 抓最新一週(週六凌晨 by GitHub Actions cron)
    python scripts/fetch_shareholder_concentration.py

    # 限制檔數(debug / smoke)
    python scripts/fetch_shareholder_concentration.py --limit 10

    # 跳過 dump CSV(僅寫 SQLite,測試用)
    python scripts/fetch_shareholder_concentration.py --no-dump

資料源:TDCC opendata `https://opendata.tdcc.com.tw/getOD.ashx?id=1-5`
  - 政府開放資料(免費 + OGL 可商用 + 無 token + 無 rate limit)
  - 每週六上午公告前一週五的股權分散表
  - 全部上市 / 上櫃個股一次回傳

訊號定義(主公拍板):
  - 大戶 = 持股 ≥ 1000 張的股東(TDCC 分級 ≥ 15,即 > 1,000,000 股)
  - holders_1000up_count: 千張大戶人數
  - holders_pct: 千張大戶 / 全部股東(占比,0-1)
  - holders_delta_w: 本週 - 上週(人數差,正值 = 大戶增加)

Exit code:
  0 = 成功(寫入 0 筆也算成功,e.g. CSV 是空週末)
  1 = 抓取失敗 / SQLite 寫入失敗
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from src import database as db  # noqa: E402

logger = logging.getLogger(__name__)

# TDCC 級距 15-17 = 持股 > 1,000,000 股 = 持股 ≥ 1000 張(嚴格: ≥1001 張,
# 但「千張大戶」業界口語涵蓋 ≥ 級 15;14 結尾是 1,000,000 股 = 1000 張整)。
_BIG_HOLDER_LEVEL_MIN = 15
_LEVEL_TOTAL = 99  # TDCC 自帶的「合計」行(部分週才有,fallback 自己 sum)

TDCC_OPENDATA_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
_HTTP_TIMEOUT = 60  # TDCC CSV 在台灣晨間下載偶有慢峰
# TDCC opendata 沒帶 User-Agent 會被丟進 redirect loop(疑似 bot detect),
# 必須帶常見瀏覽器 UA。requests 預設 UA 'python-requests/x.y' 會撞 30 redirects。
_TDCC_HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_tdcc_csv_text(url: str = TDCC_OPENDATA_URL) -> str:
    """打 TDCC opendata endpoint 拿原始 CSV 字串(UTF-8 或 Big5,試 UTF-8 優先)。

    回原始 CSV 字串給上層 parse;網路失敗 raise(讓 CLI exit 1)。
    抽出來成函式是為了測試可以 monkeypatch / 用 fixture CSV 灌假資料,不打真網。

    SSL verify=False:TDCC / TWSE 等政府公開資料服務的 SSL 憑證缺 Subject
    Key Identifier,新版 OpenSSL(Python 3.12+)會拒,跟 src/financial_fetcher_free.py
    處理 TWSE 同 pattern。公開資料 read-only 無 MITM 風險。
    """
    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = requests.get(
        url, timeout=_HTTP_TIMEOUT, verify=False, headers=_TDCC_HEADERS,
    )
    resp.raise_for_status()
    # TDCC opendata 通常回 UTF-8 BOM,requests 預設能正確 decode
    return resp.text


def parse_tdcc_csv(csv_text: str) -> pd.DataFrame:
    """把 TDCC CSV 字串 parse 成 DataFrame,欄位 normalize 成英文。

    TDCC 欄位常見格式(可能微調,所以做寬鬆 mapping):
      資料日期, 證券代號, 持股分級, 人數, 股數, 占集保庫存比例%

    回 DataFrame 欄位:date(YYYYMMDD or YYYY-MM-DD)/ sid / level / count / shares / pct
    """
    if not csv_text or not csv_text.strip():
        return pd.DataFrame(
            columns=["date", "sid", "level", "count", "shares", "pct"]
        )

    df = pd.read_csv(io.StringIO(csv_text), dtype=str)
    # 寬鬆 column rename:容忍 TDCC 微調 / 不同檔頭命名
    col_map: dict[str, str] = {}
    for c in df.columns:
        cs = c.strip()
        if cs in ("資料日期", "Data Date", "DataDate", "date"):
            col_map[c] = "date"
        elif cs in ("證券代號", "Stock ID", "StockID", "sid", "stock_id"):
            col_map[c] = "sid"
        elif cs in ("持股分級", "Holding Level", "level"):
            col_map[c] = "level"
        elif cs in ("人數", "Holders", "count"):
            col_map[c] = "count"
        elif cs in ("股數", "Shares", "shares"):
            col_map[c] = "shares"
        elif "占" in cs or "比例" in cs or cs.lower() in ("pct", "percentage"):
            col_map[c] = "pct"
    df = df.rename(columns=col_map)
    # 必要欄位缺失 → 回空,讓 caller 走 0 入 path
    required = {"date", "sid", "level", "count"}
    if not required.issubset(df.columns):
        logger.warning(
            "[TDCC] 必要欄位缺失,實有 %s,需 %s,回空 df",
            list(df.columns), sorted(required),
        )
        return pd.DataFrame(
            columns=["date", "sid", "level", "count", "shares", "pct"]
        )
    df["sid"] = df["sid"].astype(str).str.strip()
    # level 可能是 "1" / "01" / "15" / "99(合計)",pd.to_numeric coerce
    df["level"] = pd.to_numeric(df["level"], errors="coerce").astype("Int64")
    df["count"] = pd.to_numeric(df["count"], errors="coerce").fillna(0).astype(int)
    if "shares" in df.columns:
        df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    if "pct" in df.columns:
        df["pct"] = pd.to_numeric(df["pct"], errors="coerce")
    return df


def _normalize_date(raw: str) -> str:
    """TDCC date 欄位 → YYYY-MM-DD。容忍 'YYYYMMDD' / 'YYYY/MM/DD' / 已 normalized。"""
    s = str(raw).strip()
    if not s:
        return ""
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if "/" in s:
        return s.replace("/", "-")
    return s


def aggregate_to_rows(
    df: pd.DataFrame,
    previous_counts: dict[str, int] | None = None,
) -> list[dict]:
    """從 parse 過的 TDCC DataFrame 聚合成 shareholder_concentration row。

    Args:
        df: parse_tdcc_csv 回的 DataFrame(必含 date / sid / level / count)
        previous_counts: {sid: previous_holders_1000up_count},給 delta_w 用。
            None / 沒對應 sid → delta_w 寫 None(第一次 fetch 沒上週可比)。

    每筆 row:{sid, week_end, holders_1000up_count, total_holders,
              holders_pct, holders_delta_w, fetched_at}
    """
    if df.empty or "sid" not in df.columns:
        return []
    previous_counts = previous_counts or {}
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows: list[dict] = []
    # 每檔 sid 聚合一筆
    grouped = df.groupby("sid", sort=False)
    for sid, sub in grouped:
        if not sid or sid in ("", "nan"):
            continue
        # week_end:該檔資料日期取 max(同 sid 同週應該都一樣,保險 max)
        dates = [d for d in sub["date"].astype(str).tolist() if d and d != "nan"]
        if not dates:
            continue
        week_end = _normalize_date(max(dates))

        # 千張大戶人數 = level >= 15(<99,排合計列)的 count 加總
        big = sub[
            (sub["level"].notna())
            & (sub["level"] >= _BIG_HOLDER_LEVEL_MIN)
            & (sub["level"] < _LEVEL_TOTAL)
        ]
        holders_1000up = int(big["count"].sum())

        # 總股東人數:優先用 level=99 合計列;沒有 fallback sum level 1-17
        total_row = sub[sub["level"] == _LEVEL_TOTAL]
        if not total_row.empty:
            total_holders = int(total_row["count"].sum())
        else:
            non_total = sub[(sub["level"].notna()) & (sub["level"] < _LEVEL_TOTAL)]
            total_holders = int(non_total["count"].sum())

        if total_holders <= 0:
            # 資料缺失 / 該檔當週無公告 → skip
            continue

        holders_pct = holders_1000up / total_holders if total_holders > 0 else None
        prev = previous_counts.get(sid)
        holders_delta_w = (
            holders_1000up - int(prev) if prev is not None else None
        )

        rows.append({
            "sid": sid,
            "week_end": week_end,
            "holders_1000up_count": holders_1000up,
            "total_holders": total_holders,
            "holders_pct": holders_pct,
            "holders_delta_w": holders_delta_w,
            "fetched_at": fetched_at,
        })
    return rows


def _load_previous_week_counts(
    new_week_end: str, db_path: str | Path | None = None,
) -> dict[str, int]:
    """從 SQLite 撈「比 new_week_end 早的最近一筆」每檔的 holders_1000up_count。

    給 aggregate_to_rows 算 holders_delta_w 用。沒舊紀錄 → 空 dict,delta 寫 None。
    """
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT sc.sid, sc.holders_1000up_count
            FROM shareholder_concentration sc
            JOIN (
                SELECT sid, MAX(week_end) AS mw
                FROM shareholder_concentration
                WHERE week_end < ?
                GROUP BY sid
            ) last ON sc.sid=last.sid AND sc.week_end=last.mw
            """,
            (new_week_end,),
        ).fetchall()
    return {r["sid"]: int(r["holders_1000up_count"]) for r in rows}


def run(
    limit: int | None = None,
    csv_text: str | None = None,
    dump_csv: bool = True,
    db_path: str | Path | None = None,
) -> dict:
    """主流程:抓 TDCC CSV → parse → 算 delta → upsert → dump snapshot CSV。

    Args:
        limit: None = 全部寫入;>0 = 只寫前 N 筆(smoke / debug)。
        csv_text: None = 真打 TDCC;非 None = 用 caller 傳的 CSV(測試用)。
        dump_csv: True = 寫 data/twse_snapshot/shareholder_concentration.csv。
        db_path: 預設 config.DATABASE_PATH;測試傳 tmp_path/test.db。

    回 summary dict:{rows_written, total_in_csv, week_end, csv_dumped, elapsed_secs}。
    """
    t0 = time.time()
    db.init_db(db_path)

    if csv_text is None:
        print(f"[TDCC] GET {TDCC_OPENDATA_URL}", flush=True)
        csv_text = fetch_tdcc_csv_text()
    print(f"[TDCC] CSV 長度 {len(csv_text)} chars", flush=True)

    df = parse_tdcc_csv(csv_text)
    total_in_csv = int(df["sid"].nunique()) if not df.empty else 0
    print(f"[TDCC] parse 完 {len(df)} 行 / {total_in_csv} 檔", flush=True)

    if df.empty:
        return {
            "rows_written": 0, "total_in_csv": 0,
            "week_end": None, "csv_dumped": 0,
            "elapsed_secs": round(time.time() - t0, 2),
        }

    # 取本批 max(date) 當判定 week_end,撈上一週 count 算 delta
    dates = df["date"].astype(str).tolist()
    week_end = _normalize_date(max(d for d in dates if d and d != "nan"))
    prev_counts = _load_previous_week_counts(week_end, db_path=db_path)
    print(
        f"[TDCC] week_end={week_end}; 上週基準 {len(prev_counts)} 檔可算 delta",
        flush=True,
    )

    rows = aggregate_to_rows(df, previous_counts=prev_counts)
    if limit is not None and limit > 0:
        rows = rows[:limit]
        print(f"[TDCC] --limit {limit} → 只寫 {len(rows)} 檔", flush=True)

    n_written = db.upsert_shareholder_concentration(rows, db_path=db_path)
    print(f"[TDCC] 寫入 {n_written} 筆 shareholder_concentration", flush=True)

    csv_dumped = 0
    if dump_csv:
        try:
            csv_dumped = db.dump_shareholder_concentration_csv(db_path=db_path)
            if csv_dumped >= 0:
                print(
                    f"[TDCC] dump snapshot CSV ok({csv_dumped} 行)", flush=True,
                )
            else:
                print("[TDCC] dump snapshot CSV skip(silent)", flush=True)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[TDCC] dump CSV 失敗:%s", ex)

    elapsed = round(time.time() - t0, 2)
    print(
        f"[TDCC] DONE n_written={n_written} csv_dumped={csv_dumped} "
        f"elapsed={elapsed}s",
        flush=True,
    )
    return {
        "rows_written": n_written,
        "total_in_csv": total_in_csv,
        "week_end": week_end,
        "csv_dumped": csv_dumped,
        "elapsed_secs": elapsed,
    }


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="TDCC 股權分散表千張大戶週快照 fetch + dump",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="只寫前 N 檔(smoke 用,預設全部)",
    )
    p.add_argument(
        "--no-dump", action="store_true",
        help="只寫 SQLite,不 dump snapshot CSV",
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    try:
        summary = run(limit=args.limit, dump_csv=not args.no_dump)
    except Exception as ex:  # noqa: BLE001
        print(f"[TDCC] FATAL: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 1
    print("=" * 60, flush=True)
    print("[TDCC SUMMARY]", flush=True)
    for k, v in summary.items():
        print(f"  {k:<16s} {v}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
