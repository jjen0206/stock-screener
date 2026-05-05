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
#       第 20、22 款(待 dry-run 驗證)。
IMPORTANT_ARTICLES: frozenset[str] = frozenset({
    "第3款",  "第6款",  "第7款",  "第8款",  "第10款", "第11款",
    "第12款", "第14款", "第15款", "第17款", "第18款", "第23款",
    "第25款", "第30款", "第39款", "第41款",
})


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


def list_unsent_important_news(
    channel: str = "telegram",
    db_path: str | Path | None = None,
    article_whitelist: frozenset[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """撈尚未推到指定 channel 的重要新聞(條款在白名單內)。

    channel='telegram' / 'discord'。article_whitelist 預設 IMPORTANT_ARTICLES。
    依 publish_date / publish_time 升序回(舊的先推,維持時間軸)。
    """
    if channel not in ("telegram", "discord"):
        raise ValueError(f"channel 只能 'telegram' / 'discord',got {channel}")
    if article_whitelist is None:
        article_whitelist = IMPORTANT_ARTICLES

    sent_col = "sent_telegram" if channel == "telegram" else "sent_discord"
    placeholders = ",".join("?" * len(article_whitelist))
    sql = (
        f"SELECT * FROM news "
        f"WHERE {sent_col} = 0 "
        f"AND article_no IN ({placeholders}) "
        f"ORDER BY publish_date ASC, publish_time ASC "
        f"LIMIT ?"
    )
    args = list(article_whitelist) + [limit]
    with db.get_conn(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


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
    "fetch_twse_news_raw",
    "normalize_twse_news",
    "upsert_news",
    "fetch_and_store_news",
    "list_unsent_important_news",
    "mark_news_sent",
]
