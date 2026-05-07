"""法人(券商研究員)目標價共識:A+B 雙來源策略。

A) **yfinance** — 直接拿 Yahoo Finance Analyst Estimates(`targetMeanPrice` /
   `numberOfAnalystOpinions` 等)。命中即省 LLM token,主要來源。

B) **gemini_news** — yfinance 拿不到時(冷門股 / 美股 ADR 等)走 fallback:
   yfinance.Ticker.news 拿 5 則新聞 → Gemini 1.5 Flash 解析「目標價 / 券商家數」。
   走 company_profile 的 quota guard pattern,當輪 429 後同輪不再呼叫。

寫進 SQLite analyst_targets 表,(stock_id, source) 雙主鍵 — 同股可保兩種來源,
UI / notifier 顯示時優先 yfinance(較準);兩個都缺 → 不顯目標價欄。

排程:
- 平日(daily-notify.yml):scope=watchlist + scope=picks(只抓「今天會看到的」)
- 週日(weekly-targets.yml):scope=all 全市場(~1500 檔,quota 內)
"""
from __future__ import annotations

import datetime as _datetime
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src import config, database as db

logger = logging.getLogger(__name__)


# === yfinance / gemini SDK 動態載入(兩者都允許缺) ===

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except Exception as _yfe:  # noqa: BLE001
    yf = None  # type: ignore[assignment]
    _YF_AVAILABLE = False
    logger.info("[ANALYST] yfinance 未安裝(%s),A 來源停用", _yfe)

try:
    import google.generativeai as genai
    _GEMINI_AVAILABLE = True
except Exception as _ge:  # noqa: BLE001
    genai = None  # type: ignore[assignment]
    _GEMINI_AVAILABLE = False
    logger.info("[ANALYST] google-generativeai 未安裝(%s),B 來源停用", _ge)


GEMINI_MODEL = "gemini-2.5-flash-lite"

# 跟 company_profile 共用同一旗標 key(打到 429 後當輪所有 LLM 走 cache-only)
_QUOTA_FLAG_KEY = "_gemini_quota_exceeded_date"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# === Quota guard(複用 company_profile 同邏輯,避免兩個模組各自打 429)===

def _is_quota_exceeded(exc: BaseException) -> bool:
    cls_name = type(exc).__name__
    if cls_name in ("ResourceExhausted", "TooManyRequests"):
        return True
    msg = str(exc)
    if "429" in msg:
        return True
    msg_low = msg.lower()
    if "quota" in msg_low and ("exceeded" in msg_low or "exhausted" in msg_low):
        return True
    return False


def _safe_get_session_state():
    try:
        import streamlit as st
        return st.session_state
    except Exception:  # noqa: BLE001
        return None


def _is_quota_flag_set_today() -> bool:
    state = _safe_get_session_state()
    if state is None:
        return False
    today = _datetime.date.today().isoformat()
    try:
        return state.get(_QUOTA_FLAG_KEY) == today
    except Exception:  # noqa: BLE001
        return False


def _set_quota_flag_today() -> None:
    state = _safe_get_session_state()
    if state is None:
        return
    try:
        state[_QUOTA_FLAG_KEY] = _datetime.date.today().isoformat()
    except Exception:  # noqa: BLE001
        pass


# === A 來源:yfinance ===

def _yf_info_for_sid(sid: str) -> dict | None:
    """先試 .TW(上市)→ 失敗 fallback .TWO(上櫃)→ 都 raise / 空就回 None。

    yfinance 對冷門 / 不存在的代號可能拋 / 回空 dict,統一 try 兩次。
    """
    if not _YF_AVAILABLE:
        return None
    for suffix in (".TW", ".TWO"):
        try:
            ticker = yf.Ticker(f"{sid}{suffix}")
            info = getattr(ticker, "info", None) or {}
            if info.get("targetMeanPrice") and info.get("numberOfAnalystOpinions"):
                return info
        except Exception as e:  # noqa: BLE001
            logger.debug("[ANALYST] yf %s%s 失敗:%s", sid, suffix, e)
            continue
    return None


def fetch_analyst_target_yfinance(sid: str) -> dict[str, Any] | None:
    """A 來源:yfinance Analyst Estimates。

    Returns:
        命中 → {target_mean, target_median, target_high, target_low,
               num_analysts, source='yfinance'}
        全缺 → None(讓 caller fallback B)
    """
    info = _yf_info_for_sid(sid)
    if not info:
        return None
    target_mean = info.get("targetMeanPrice")
    num = info.get("numberOfAnalystOpinions")
    if not target_mean or not num:
        return None
    try:
        return {
            "target_mean": float(target_mean),
            "target_median": (
                float(info["targetMedianPrice"])
                if info.get("targetMedianPrice") else None
            ),
            "target_high": (
                float(info["targetHighPrice"])
                if info.get("targetHighPrice") else None
            ),
            "target_low": (
                float(info["targetLowPrice"])
                if info.get("targetLowPrice") else None
            ),
            "num_analysts": int(num),
            "source": "yfinance",
        }
    except (TypeError, ValueError) as e:
        logger.debug("[ANALYST] yf parse %s 失敗:%s", sid, e)
        return None


# === B 來源:Gemini parse 新聞 ===

_GEMINI_NEWS_PROMPT = """請從以下台股 {sid}{name_suffix} 的新聞標題與摘要中,
萃取「券商研究員給的股價目標價(target price)」共識資訊。

新聞:
{news_text}

請回傳 JSON,**只**含以下欄位(都拿不到時回空 {{}}):
{{
  "target_mean": 數字 (最常被提及的目標價,單位元),
  "target_high": 數字 (最樂觀目標) 或 null,
  "target_low": 數字 (最保守目標) 或 null,
  "num_analysts": 整數 (提及的券商家數,通常 1-5),
  "rationale": 字串 (一句話說明,例:「外資看好 AI 訂單動能」)
}}

規則:
- 找不到任何目標價 → 回 {{}}
- 只回 JSON,不要 ``` 程式區塊符號或註解
- target_mean 取數值最頻繁出現的價格,不是極端值"""


def _strip_code_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _yf_news_for_sid(sid: str, limit: int = 5) -> list[dict]:
    """yfinance Ticker.news 抓近期新聞;空 / 失敗 → []。

    yfinance news 結構為 list[{title, publisher, link, providerPublishTime, ...}];
    新版可能改為 {content: {title, summary, ...}}。兩種都 cover。
    """
    if not _YF_AVAILABLE:
        return []
    for suffix in (".TW", ".TWO"):
        try:
            ticker = yf.Ticker(f"{sid}{suffix}")
            news = getattr(ticker, "news", None) or []
            if news:
                return list(news)[:limit]
        except Exception as e:  # noqa: BLE001
            logger.debug("[ANALYST] yf news %s%s 失敗:%s", sid, suffix, e)
            continue
    return []


def _format_news_for_prompt(news: list[dict]) -> str:
    """把 yfinance news list 攤成純文字餵給 Gemini。

    新舊版 schema 都 cover:
    - 舊:{title, publisher}
    - 新:{content: {title, summary, ...}}
    """
    out: list[str] = []
    for i, item in enumerate(news, start=1):
        if not isinstance(item, dict):
            continue
        title = ""
        summary = ""
        publisher = ""
        # 新 schema
        content = item.get("content")
        if isinstance(content, dict):
            title = str(content.get("title") or "").strip()
            summary = str(content.get("summary") or "").strip()
            provider = content.get("provider") or {}
            if isinstance(provider, dict):
                publisher = str(provider.get("displayName") or "").strip()
        # 舊 schema fallback
        if not title:
            title = str(item.get("title") or "").strip()
        if not publisher:
            publisher = str(item.get("publisher") or "").strip()
        if not title:
            continue
        line = f"{i}. {title}"
        if publisher:
            line += f" ({publisher})"
        if summary:
            line += f"\n   {summary[:200]}"
        out.append(line)
    return "\n".join(out)


def fetch_analyst_target_from_news(
    sid: str, name: str = "",
) -> dict[str, Any] | None:
    """B 來源:Gemini 解析 yfinance.Ticker(sid).news 5 則 → 目標價共識。

    Quota guard:當輪今日已撞 429 → 直接回 None 不浪費 RTT。

    Returns:
        命中 → {target_mean, target_high, target_low, num_analysts,
               target_median=None, source='gemini_news'}
        全缺 / quota 用完 / SDK 缺 → None
    """
    if not _GEMINI_AVAILABLE:
        return None
    if _is_quota_flag_set_today():
        logger.debug("[ANALYST] %s quota 旗標已設,跳過 LLM", sid)
        return None
    api_key = config.GEMINI_API_KEY
    if not api_key:
        return None

    news = _yf_news_for_sid(sid, limit=5)
    if not news:
        return None
    news_text = _format_news_for_prompt(news)
    if not news_text.strip():
        return None

    name_suffix = f" {name}" if name else ""
    prompt = _GEMINI_NEWS_PROMPT.format(
        sid=sid, name_suffix=name_suffix, news_text=news_text,
    )

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(GEMINI_MODEL)
        resp = model.generate_content(prompt)
        text = getattr(resp, "text", None) or ""
    except Exception as e:  # noqa: BLE001
        if _is_quota_exceeded(e):
            _set_quota_flag_today()
            logger.warning("[ANALYST] %s Gemini 429 quota 用完", sid)
        else:
            logger.warning("[ANALYST] %s Gemini call 失敗:%s", sid, str(e)[:200])
        return None

    if not text.strip():
        return None
    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.debug("[ANALYST] %s Gemini 回非 JSON:%s", sid, cleaned[:120])
        return None

    if not isinstance(data, dict) or not data:
        return None
    target_mean = data.get("target_mean")
    if target_mean is None:
        return None
    try:
        return {
            "target_mean": float(target_mean),
            "target_median": None,
            "target_high": (
                float(data["target_high"])
                if data.get("target_high") is not None else None
            ),
            "target_low": (
                float(data["target_low"])
                if data.get("target_low") is not None else None
            ),
            "num_analysts": (
                int(data["num_analysts"])
                if data.get("num_analysts") is not None else None
            ),
            "source": "gemini_news",
        }
    except (TypeError, ValueError) as e:
        logger.debug("[ANALYST] %s Gemini parse fail:%s", sid, e)
        return None


# === 寫入 / 讀取 SQLite ===

def upsert_analyst_target(
    sid: str,
    data: dict[str, Any],
    db_path: str | Path | None = None,
) -> None:
    """INSERT OR REPLACE INTO analyst_targets。

    data 必有 source(yfinance / gemini_news);觸發 dump CSV snapshot。
    """
    if not data or not data.get("source"):
        return
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO analyst_targets (
                stock_id, target_mean, target_median, target_high, target_low,
                num_analysts, source, fetched_at
            ) VALUES (
                :stock_id, :target_mean, :target_median, :target_high, :target_low,
                :num_analysts, :source, :fetched_at
            )
            ON CONFLICT(stock_id, source) DO UPDATE SET
                target_mean   = excluded.target_mean,
                target_median = excluded.target_median,
                target_high   = excluded.target_high,
                target_low    = excluded.target_low,
                num_analysts  = excluded.num_analysts,
                fetched_at    = excluded.fetched_at
            """,
            {
                "stock_id": sid,
                "target_mean": data.get("target_mean"),
                "target_median": data.get("target_median"),
                "target_high": data.get("target_high"),
                "target_low": data.get("target_low"),
                "num_analysts": data.get("num_analysts"),
                "source": data["source"],
                "fetched_at": _now_iso(),
            },
        )
    db._dump_analyst_targets_snapshot(db_path)


def get_analyst_target(
    sid: str,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """讀單檔最佳目標價 row(優先 yfinance,fallback gemini_news)。

    Returns: dict 含 stock_id / target_mean / ... / source / fetched_at;
             一筆都沒有 → None。
    """
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT stock_id, target_mean, target_median, target_high, target_low,
                   num_analysts, source, fetched_at
            FROM analyst_targets
            WHERE stock_id=?
            ORDER BY CASE source WHEN 'yfinance' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (sid,),
        ).fetchone()
    return dict(row) if row else None


def get_analyst_targets_for_sids(
    sids: Iterable[str],
    db_path: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """bulk 讀多檔最佳目標價 → {sid: row dict}。

    用於 notifier _select_top_picks / strategies.enrich_with_analyst_target。
    同 sid 兩種 source 取 yfinance(較準)。
    """
    sids_list = [s for s in sids if s]
    if not sids_list:
        return {}
    db.init_db(db_path)
    placeholders = ",".join("?" * len(sids_list))
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT stock_id, target_mean, target_median, target_high, target_low,
                   num_analysts, source, fetched_at
            FROM analyst_targets
            WHERE stock_id IN ({placeholders})
            """,
            sids_list,
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        sid = r["stock_id"]
        cur = out.get(sid)
        # 優先 yfinance:已存 yfinance row 不被 gemini_news 覆蓋
        if cur is not None and cur["source"] == "yfinance":
            continue
        out[sid] = dict(r)
    return out


# === 公開入口:fetch + upsert ===

def fetch_and_store(
    sid: str,
    name: str = "",
    db_path: str | Path | None = None,
    use_gemini_fallback: bool = True,
) -> dict[str, Any] | None:
    """先試 yfinance,失敗才走 Gemini 解析新聞;命中即寫 SQLite。

    Returns: 寫進去的 data dict;兩來源都失敗 → None。
    """
    data = fetch_analyst_target_yfinance(sid)
    if data is None and use_gemini_fallback:
        data = fetch_analyst_target_from_news(sid, name=name)
    if data is None:
        return None
    upsert_analyst_target(sid, data, db_path=db_path)
    return data


__all__ = [
    "fetch_analyst_target_yfinance",
    "fetch_analyst_target_from_news",
    "fetch_and_store",
    "upsert_analyst_target",
    "get_analyst_target",
    "get_analyst_targets_for_sids",
    "GEMINI_MODEL",
]
