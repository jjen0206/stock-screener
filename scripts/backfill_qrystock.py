"""TDCC qryStock 股權分散表多週 backfill (theme universe)。

對 `data/themes/*.yaml` union 出來的個股集合,從 TDCC qryStock 介面
POST 各 scaDate 把過去 N 週的「千張大戶人數 / 總股東人數」灌進
`shareholder_concentration` 表 — 補既有 weekly opendata fetcher 只有
最新一週的歷史空檔。

不取代既有 weekly cron(`scripts/fetch_shareholder_concentration.py`),
只補歷史。

使用範例::

    # 用預設 themes 目錄、12 週,扣已存在週次
    python scripts/backfill_qrystock.py

    # 換目錄 / 改週數 / 用 dry-run smoke
    python scripts/backfill_qrystock.py --themes-dir data/themes \\
        --weeks 4 --dry-run

設計:
  - 單 session,GET 一次拿 SYNCHRONIZER_TOKEN + firDate 重複用,
    若 POST 偵測 token 失效再 refresh 一次(自動重試)。
  - 每 POST 預設 sleep 2 秒(rate limit,TDCC 沒公開限速規則,保守)。
  - DB 已存在的 (sid, week_end) 一律 skip,resume-friendly。
  - `holders_delta_w`:同 batch 內用上一週實際值算;最早那週沒前週 → NULL。
  - 進度 log:每 50 個 POST 印一行。
"""
from __future__ import annotations

import argparse
import glob
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

import yaml  # noqa: E402

from src import database as db  # noqa: E402

logger = logging.getLogger(__name__)

TDCC_URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 30
_BIG_HOLDER_LEVEL = 15  # TDCC qryStock 級距 15 = 1,000,001 股以上 = 千張大戶
_TOTAL_LEVEL = 17       # 最後一列「合計」(本頁是 17,非 opendata 的 99)


# === HTTP helper ===

def _build_session():
    import urllib3
    import requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    sess = requests.Session()
    sess.headers.update({"User-Agent": _UA})
    sess.verify = False
    return sess


def fetch_form_meta(sess) -> dict:
    """GET qryStock 拿 scaDate options + SYNCHRONIZER_TOKEN + firDate。

    回 {token, firDate, scaDates: [YYYYMMDD, ...]}
    scaDates 已照頁面順序(新到舊)排好。
    """
    r = sess.get(TDCC_URL, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    html = r.text
    m_tok = re.search(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"', html)
    m_fir = re.search(r'name="firDate" value="([^"]+)"', html)
    if not m_tok or not m_fir:
        raise RuntimeError("TDCC qryStock GET 拿不到 SYNCHRONIZER_TOKEN / firDate")
    sca_dates = re.findall(r'<option[^>]*value="(\d{8})"', html)
    return {"token": m_tok.group(1), "firDate": m_fir.group(1), "scaDates": sca_dates}


def post_query(sess, meta: dict, sca_date: str, stock_no: str) -> str:
    """單筆 POST,回 response body(string, Big5 decode)。"""
    payload = {
        "SYNCHRONIZER_TOKEN": meta["token"],
        "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock",
        "method": "submit",
        "firDate": meta["firDate"],
        "scaDate": sca_date,
        "sqlMethod": "StockNo",
        "stockNo": stock_no,
        "stockName": "",
    }
    r = sess.post(TDCC_URL, data=payload, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    # qryStock 頁面是 Big5 編碼;requests 預設 latin-1 fallback 會把中文糊掉,
    # 但 parse 只用數字 + level number,所以 decode 失敗不致命。保守用 big5。
    try:
        return r.content.decode("big5", errors="replace")
    except Exception:
        return r.text


# === Parser ===

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_qrystock_html(html: str) -> dict | None:
    """從 qryStock POST response 抽 (holders_1000up_count, total_holders)。

    結構(已驗證 2026-04-24 / 2330):
      ┌──level──┬──range──┬──count──┬──shares──┬──pct──┐
      │ 1       │ 1-999   │ ...     │ ...      │ ...   │
      │ ...     │         │         │          │       │
      │ 15      │ 1000001以上 │ 1503   │ ...      │ ...  │ ← 千張大戶
      │ 16      │ 差異數調整 │       │          │       │ ← 跳過
      │ 17      │ 合計     │ 2464344 │ ...      │ ...   │ ← 總股東
      └─────────┴─────────┴─────────┴──────────┴───────┘

    解析策略:每一列頭一欄為純整數時當級距列,挑 level==15 / level==17。
    回 None 表示無資料 / 表格抓不到。
    """
    if not html or "<table" not in html:
        return None

    holders_1000up: int | None = None
    total_holders: int | None = None

    for tr in _TR_RE.findall(html):
        tds = _TD_RE.findall(tr)
        if len(tds) < 3:
            continue
        cells = [_strip_tags(t) for t in tds]
        # 第 0 欄要是整數(級距 1-17),才認定是分級列
        try:
            level = int(cells[0])
        except (ValueError, IndexError):
            continue
        # count 在第 2 欄(0-indexed),逗號數字
        count_raw = cells[2].replace(",", "").replace("&nbsp;", "").strip()
        if not count_raw:
            continue
        try:
            count = int(count_raw)
        except ValueError:
            continue
        if level == _BIG_HOLDER_LEVEL:
            holders_1000up = count
        elif level == _TOTAL_LEVEL:
            total_holders = count

    if holders_1000up is None or total_holders is None or total_holders <= 0:
        return None
    return {
        "holders_1000up_count": holders_1000up,
        "total_holders": total_holders,
    }


def _sca_date_to_week_end(sca: str) -> str:
    """20260424 → 2026-04-24"""
    return f"{sca[:4]}-{sca[4:6]}-{sca[6:8]}"


# === Theme loader ===

def load_theme_sids(themes_dir: str | Path) -> list[str]:
    """Glob themes_dir/*.yaml,union 所有 `sids` 欄位。"""
    paths = sorted(glob.glob(str(Path(themes_dir) / "*.yaml")))
    union: set[str] = set()
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for s in data.get("sids", []) or []:
            sid = str(s).strip()
            if sid:
                union.add(sid)
    return sorted(union)


# === DB helper ===

def _load_existing_pairs(
    sids: Iterable[str], weeks: Iterable[str], db_path=None,
) -> set[tuple[str, str]]:
    """撈 DB 已存在的 (sid, week_end) pair,resume 用。"""
    sids_list = [str(s) for s in sids]
    weeks_list = list(weeks)
    if not sids_list or not weeks_list:
        return set()
    db.init_db(db_path)
    placeholders_s = ",".join("?" * len(sids_list))
    placeholders_w = ",".join("?" * len(weeks_list))
    sql = (
        f"SELECT sid, week_end FROM shareholder_concentration "
        f"WHERE sid IN ({placeholders_s}) AND week_end IN ({placeholders_w})"
    )
    with db.get_conn(db_path) as conn:
        rows = conn.execute(sql, sids_list + weeks_list).fetchall()
    return {(r["sid"], r["week_end"]) for r in rows}


# === Main backfill ===

def run_backfill(
    themes_dir: str | Path,
    weeks: int,
    *,
    rate_limit_secs: float = 2.0,
    dry_run: bool = False,
    db_path=None,
    session_factory=None,
    sleep_fn=time.sleep,
) -> dict:
    """主流程。

    Args:
        themes_dir: data/themes 目錄
        weeks: 要 backfill 最近幾週(扣已存在週次)
        rate_limit_secs: 每筆 POST 後 sleep 秒數
        dry_run: True = 只 plan、不真打 POST、不寫 DB
        db_path: 測試用
        session_factory: 測試用注入 requests-style session
        sleep_fn: 測試用 monkey patch sleep
    """
    t0 = time.time()
    sids = load_theme_sids(themes_dir)
    if not sids:
        print(f"[BACKFILL] themes_dir={themes_dir} 沒抓到任何 sid", flush=True)
        return {"sids": 0, "weeks": 0, "ok": 0, "failed": 0, "skipped": 0}

    sess = (session_factory or _build_session)()
    meta = fetch_form_meta(sess)
    sca_list = meta["scaDates"][:weeks]
    week_ends = [_sca_date_to_week_end(s) for s in sca_list]
    print(
        f"[BACKFILL] sids={len(sids)} weeks_window={weeks} "
        f"scaDate={sca_list[0]}..{sca_list[-1]} "
        f"(week_end={week_ends[0]}..{week_ends[-1]})",
        flush=True,
    )

    existing = _load_existing_pairs(sids, week_ends, db_path=db_path)
    if existing:
        print(
            f"[BACKFILL] DB 已存在 {len(existing)} 個 (sid, week) pair,跳過",
            flush=True,
        )

    # 從舊週到新週處理,讓 delta_w 算對
    work = list(zip(reversed(sca_list), reversed(week_ends)))

    # 載入「比最早 backfill 週更早一週」的 count 當 prev seed —
    # 沒有就空(那一週 delta_w 寫 None)。簡單起見不撈 DB,沿 batch 走。
    prev_counts: dict[str, int] = {}

    ok = failed = skipped = 0
    post_idx = 0
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for sca_date, week_end in work:
        week_rows: list[dict] = []
        new_prev: dict[str, int] = {}
        for sid in sids:
            if (sid, week_end) in existing:
                skipped += 1
                # 已存在的 row 也要把 count 灌到 prev,讓下一週 delta 算得對
                latest = db.get_latest_shareholder_concentration(sid, db_path=db_path)
                # 但 latest 可能不是這週;以 week_end exact match 比較準
                with db.get_conn(db_path) as conn:
                    r = conn.execute(
                        "SELECT holders_1000up_count FROM shareholder_concentration "
                        "WHERE sid=? AND week_end=?",
                        (sid, week_end),
                    ).fetchone()
                if r is not None:
                    new_prev[sid] = int(r["holders_1000up_count"])
                continue

            if dry_run:
                ok += 1
                continue

            post_idx += 1
            try:
                html = post_query(sess, meta, sca_date, sid)
                parsed = parse_qrystock_html(html)
            except Exception as ex:  # noqa: BLE001
                logger.warning(
                    "[BACKFILL] sid=%s week=%s POST 失敗: %s",
                    sid, week_end, ex,
                )
                parsed = None

            if parsed is None:
                # 如果 token 過期 → 重抓一次再試 1 次
                try:
                    meta = fetch_form_meta(sess)
                    html = post_query(sess, meta, sca_date, sid)
                    parsed = parse_qrystock_html(html)
                except Exception as ex:  # noqa: BLE001
                    logger.warning(
                        "[BACKFILL] sid=%s week=%s retry 失敗: %s",
                        sid, week_end, ex,
                    )
                    parsed = None

            if parsed is None:
                failed += 1
                sleep_fn(rate_limit_secs)
                if post_idx % 50 == 0:
                    print(
                        f"[BACKFILL] sid={sid} week={week_end} FAIL "
                        f"(total={post_idx} ok={ok} failed={failed} skipped={skipped})",
                        flush=True,
                    )
                continue

            cnt = parsed["holders_1000up_count"]
            total = parsed["total_holders"]
            prev = prev_counts.get(sid)
            row = {
                "sid": sid,
                "week_end": week_end,
                "holders_1000up_count": cnt,
                "total_holders": total,
                "holders_pct": cnt / total if total > 0 else None,
                "holders_delta_w": (cnt - int(prev)) if prev is not None else None,
                "fetched_at": fetched_at,
            }
            week_rows.append(row)
            new_prev[sid] = cnt
            ok += 1

            if post_idx % 50 == 0:
                print(
                    f"[BACKFILL] sid={sid} week={week_end} ok "
                    f"(total={post_idx} ok={ok} failed={failed} skipped={skipped})",
                    flush=True,
                )

            sleep_fn(rate_limit_secs)

        if week_rows and not dry_run:
            db.upsert_shareholder_concentration(week_rows, db_path=db_path)
            print(
                f"[BACKFILL] week={week_end} 寫入 {len(week_rows)} 筆 "
                f"(rolling ok={ok} failed={failed} skipped={skipped})",
                flush=True,
            )

        # 下一週的 prev 用本週剛 fetch 到 + 本週 skip 但 DB 有的混合
        prev_counts = new_prev

    elapsed = round(time.time() - t0, 2)
    print(
        f"[BACKFILL] DONE total_posts={post_idx} ok={ok} failed={failed} "
        f"skipped={skipped} elapsed={elapsed}s",
        flush=True,
    )
    return {
        "sids": len(sids),
        "weeks": len(work),
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "elapsed_secs": elapsed,
    }


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="TDCC qryStock 多週歷史 backfill (theme universe)",
    )
    p.add_argument("--themes-dir", default="data/themes")
    p.add_argument("--weeks", type=int, default=12)
    p.add_argument("--rate-limit-secs", type=float, default=2.0)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)

    try:
        summary = run_backfill(
            args.themes_dir, args.weeks,
            rate_limit_secs=args.rate_limit_secs,
            dry_run=args.dry_run,
        )
    except Exception as ex:  # noqa: BLE001
        print(f"[BACKFILL] FATAL: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 1
    print("=" * 60)
    print("[BACKFILL SUMMARY]")
    for k, v in summary.items():
        print(f"  {k:<14s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
