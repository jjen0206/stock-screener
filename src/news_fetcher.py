"""TWSE 重大訊息(t187ap04_L)抓取 + 條款白名單過濾。

來源:https://openapi.twse.com.tw/v1/opendata/t187ap04_L
免費官方 endpoint,加 User-Agent 即可訪問(避過 anti-bot)。
回 JSON list,每筆是某公司當日發布的重大訊息。

欄位(注意「主旨 」key 尾有空格):
  - 出表日期 / 發言日期 / 發言時間(民國年 yyy mm dd / HHMMSS)
  - 公司代號 / 公司名稱
  - 主旨 (key 帶空格!)
  - 符合條款("第8款" 等)
  - 事實發生日(民國年)
  - 說明(全文)

設計:
  - dedup by url_hash = (sid + publish_date + subject head[60]) sha1
    (TWSE 沒提供 globally unique news id,就用 content hash)
  - 民國年 → 西元年(1150505 → 2026-05-05)
  - 白名單過濾在 caller 端做(此 module 只抓 + 寫,不過濾)
"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src import database as db

logger = logging.getLogger(__name__)


TWSE_NEWS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Chrome/120 stock-screener/1.0"

# 白名單條款 — 推哪些(基於市場關注度 + 對股價影響度)。
# 排除:第 31 款(財報會議預告,雜訊量 60%+)、第 51 款(公司名稱變更,影響小)、
#       第 17 款(召開股東常會,法定例行)。
# 2026-05-05 dry-run 第二輪後擴充:
#   + 第 12 款(法人說明會公告)— 主公拍板留;雖內容是預告,但市場常根據法說會
#                              時間布局,值得推
#   + 第 20 款(廠房興建 / 工程發包等重大資產購置)— 高 impact 投資相關
#   + 第 22 款(資金貸與相關)— 跟第 23 款同類,量極少
# 剩 17 款。
IMPORTANT_ARTICLES: frozenset[str] = frozenset({
    "第3款",  "第6款",  "第7款",  "第8款",  "第10款", "第11款",
    "第12款", "第14款", "第15款", "第18款", "第20款", "第22款",
    "第23款", "第25款", "第30款", "第39款", "第41款",
})

# 條款 base score。手動股價 impact 排序:
#   100 第3款 法律訴訟 / 95 第8款 重大契約 / 95 第10款 處分資產
#   90  第11款 簽證會計師更換 / 85 第14款 董事會決議 / 90 第15款 現金增資
#   85  第18款 庫藏股 / 80 第20款 資產購置 / 80 第22款 資金貸與
#   80  第23款 背書保證 / 75 第25款 再融資 / 75 第30款 內部稽核
#   60  第6款 一般 / 50 第12款 法說會預告(預告型噪音較大,基線壓低)
ARTICLE_PRIORITY: dict[str, int] = {
    "第3款": 100, "第8款": 95, "第10款": 95, "第11款": 90,
    "第14款": 85, "第15款": 90, "第18款": 85, "第20款": 80,
    "第22款": 80, "第23款": 80, "第25款": 75, "第30款": 75,
    "第6款": 60, "第12款": 50,
}
_DEFAULT_ARTICLE_SCORE = 70  # 白名單內未列在 ARTICLE_PRIORITY 表的條款預設分數


def _roc_to_iso(roc_date: str | int | None) -> str | None:
    """民國年 yyymmdd / yyyymmdd → 西元 YYYY-MM-DD;非法輸入回 None。

    例:1150505 → "2026-05-05"。
    """
    if roc_date is None:
        return None
    s = str(roc_date).strip()
    if not s.isdigit() or len(s) < 7:
        return None
    # 民國年 = 西元年 - 1911。yyymmdd:前 3 碼 yyy,後 4 碼 mmdd。
    # yyyymmdd:前 4 碼 yyyy(大於等於 1000)— 容錯不太可能但仍處理。
    try:
        if len(s) == 7:
            y = int(s[:3]) + 1911
            m = int(s[3:5])
            d = int(s[5:7])
        elif len(s) == 8:
            y = int(s[:4])
            m = int(s[4:6])
            d = int(s[6:8])
        else:
            return None
        return f"{y:04d}-{m:02d}-{d:02d}"
    except ValueError:
        return None


def _compute_url_hash(sid: str, publish_date: str, subject: str) -> str:
    """url_hash = sha1(sid + '|' + publish_date + '|' + subject_head[:80])。

    TWSE OpenAPI 沒給 unique news id,用 content hash 當 dedup key。
    """
    head = (subject or "")[:80].strip()
    s = f"{sid}|{publish_date}|{head}"
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def fetch_twse_news_raw(timeout: int = 15) -> list[dict]:
    """打 TWSE OpenAPI 抓全市場當日重大訊息 list[dict]。

    錯誤處理:404 / 500 / timeout / JSON 解析失敗都 raise(讓 caller log + skip
    這輪 cron,下次再試)。
    """
    req = urllib.request.Request(
        TWSE_NEWS_URL, headers={"User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"TWSE news 回傳非 list:{type(data).__name__}")
    return data


def normalize_twse_news(raw: list[dict]) -> list[dict]:
    """把 raw TWSE news entries 正規化成 SQLite 寫入格式。

    注意「主旨 」欄位 key 尾有空格(TWSE 真的這樣設計)— 用 .get('主旨 ') 抓。
    回每筆 dict 含 sid / company_name / publish_date / publish_time / subject /
    article_no / description / fact_date / url_hash。
    """
    rows: list[dict] = []
    for d in raw:
        sid = str(d.get("公司代號") or "").strip()
        if not sid:
            continue
        # 主旨 key 帶尾空格,一併試 fallback
        subject = (
            d.get("主旨 ") or d.get("主旨") or ""
        ).replace("\r\n", " ").replace("\n", " ").strip()
        if not subject:
            continue
        publish_date = _roc_to_iso(d.get("發言日期"))
        if not publish_date:
            # 發言日期解析失敗,fallback 出表日期(必有)
            publish_date = _roc_to_iso(d.get("出表日期"))
        if not publish_date:
            continue
        rows.append({
            "sid": sid,
            "company_name": str(d.get("公司名稱") or "").strip(),
            "publish_date": publish_date,
            "publish_time": str(d.get("發言時間") or "").strip(),
            "subject": subject,
            "article_no": str(d.get("符合條款") or "").strip(),
            "description": str(d.get("說明") or "").strip(),
            "fact_date": _roc_to_iso(d.get("事實發生日")),
            "url_hash": _compute_url_hash(sid, publish_date, subject),
        })
    return rows


def upsert_news(
    rows: list[dict], db_path: str | Path | None = None,
) -> tuple[int, int]:
    """寫入 news 表(INSERT OR IGNORE 走 url_hash UNIQUE)。

    回 (inserted, skipped):inserted = 真的新加,skipped = 已存在跳過。
    """
    if not rows:
        return 0, 0
    inserted = 0
    skipped = 0
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.get_conn(db_path) as conn:
        for r in rows:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO news
                    (sid, company_name, publish_date, publish_time, subject,
                     article_no, description, fact_date, url_hash, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["sid"], r.get("company_name"), r["publish_date"],
                    r.get("publish_time"), r["subject"],
                    r.get("article_no"), r.get("description"),
                    r.get("fact_date"), r["url_hash"], now_iso,
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
    return inserted, skipped


def fetch_and_store_news(
    db_path: str | Path | None = None,
) -> tuple[list[dict], int, int]:
    """完整流程:fetch TWSE → normalize → upsert SQLite。

    回 (rows, inserted, skipped)。raw 回傳給 caller 統計用。
    """
    raw = fetch_twse_news_raw()
    rows = normalize_twse_news(raw)
    inserted, skipped = upsert_news(rows, db_path=db_path)
    return rows, inserted, skipped


def get_watchlist_sids(db_path: str | Path | None = None) -> set[str]:
    """讀 watchlist 表 → set of stock_id。空 / 表不存在 → 空 set。"""
    try:
        with db.get_conn(db_path) as conn:
            rows = conn.execute("SELECT stock_id FROM watchlist").fetchall()
    except Exception:  # noqa: BLE001
        return set()
    return {str(r["stock_id"]) for r in rows if r["stock_id"]}


def get_today_picks_sids(
    trade_date: str | None = None,
    db_path: str | Path | None = None,
    min_strategies: int = 2,
) -> set[str]:
    """讀今日 daily_picks → set of sid:命中策略 ≥ min_strategies(預設 2)。

    無資料 / 表不存在 → 空 set。trade_date None 取今日 ISO。
    """
    from datetime import date as _date
    if trade_date is None:
        trade_date = _date.today().isoformat()
    try:
        with db.get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT sid, COUNT(DISTINCT strategy) AS n "
                "FROM daily_picks WHERE trade_date=? GROUP BY sid",
                (trade_date,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return set()
    return {
        str(r["sid"]) for r in rows
        if r["sid"] and r["n"] is not None and r["n"] >= min_strategies
    }


def _priority_score(
    news: dict,
    watchlist_sids: set[str] | None = None,
    picks_sids: set[str] | None = None,
) -> int:
    """組合分數:watchlist (+1000) > today picks (+500) > 條款 base (50-100)。

    Tie-break 由 caller 用 publish_time ASC(舊的先推)補。
    None → 視為空 set。
    """
    watchlist_sids = watchlist_sids or set()
    picks_sids = picks_sids or set()
    article = str(news.get("article_no") or "")
    score = ARTICLE_PRIORITY.get(article, _DEFAULT_ARTICLE_SCORE)
    sid = str(news.get("sid") or "")
    if sid in watchlist_sids:
        score += 1000
    if sid in picks_sids:
        score += 500
    return score


def list_unsent_important_news(
    channel: str = "telegram",
    db_path: str | Path | None = None,
    article_whitelist: frozenset[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """撈尚未推到指定 channel 的重要新聞(條款在白名單內)。

    channel='telegram' / 'discord'。article_whitelist 預設 IMPORTANT_ARTICLES。
    排序:_priority_score DESC(watchlist 最優先 → today picks → 條款分數)→
    publish_time ASC(同分舊的先推維持時間軸)→ publish_date ASC fallback。
    """
    if channel not in ("telegram", "discord"):
        raise ValueError(f"channel 只能 'telegram' / 'discord',got {channel}")
    if article_whitelist is None:
        article_whitelist = IMPORTANT_ARTICLES

    sent_col = "sent_telegram" if channel == "telegram" else "sent_discord"
    placeholders = ",".join("?" * len(article_whitelist))
    # 全撈再用 Python 排序 + cap by limit。SQLite 沒法直接表達 watchlist /
    # picks 的 score(跨表),Python 端排序 ~10ms 數量級沒差。
    sql = (
        f"SELECT * FROM news "
        f"WHERE {sent_col} = 0 "
        f"AND article_no IN ({placeholders})"
    )
    args = list(article_whitelist)
    with db.get_conn(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    items = [dict(r) for r in rows]
    if not items:
        return []

    watchlist_sids = get_watchlist_sids(db_path=db_path)
    picks_sids = get_today_picks_sids(db_path=db_path)

    items.sort(key=lambda n: (
        -_priority_score(n, watchlist_sids, picks_sids),
        str(n.get("publish_date") or ""),
        str(n.get("publish_time") or ""),
    ))
    return items[:limit]


def mark_news_sent(
    news_ids: list[int],
    channel: str = "telegram",
    db_path: str | Path | None = None,
) -> int:
    """更新 sent_{channel}=1。回更新筆數。"""
    if not news_ids:
        return 0
    if channel not in ("telegram", "discord"):
        raise ValueError(f"channel 只能 'telegram' / 'discord',got {channel}")
    sent_col = "sent_telegram" if channel == "telegram" else "sent_discord"
    placeholders = ",".join("?" * len(news_ids))
    with db.get_conn(db_path) as conn:
        cur = conn.execute(
            f"UPDATE news SET {sent_col} = 1 WHERE id IN ({placeholders})",
            news_ids,
        )
    return cur.rowcount


__all__ = [
    "TWSE_NEWS_URL",
    "IMPORTANT_ARTICLES",
    "ARTICLE_PRIORITY",
    "fetch_twse_news_raw",
    "normalize_twse_news",
    "upsert_news",
    "fetch_and_store_news",
    "list_unsent_important_news",
    "mark_news_sent",
    "get_watchlist_sids",
    "get_today_picks_sids",
]
