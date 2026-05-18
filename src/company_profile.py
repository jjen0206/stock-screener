"""個股公司資訊整合:FinMind facts + Gemini LLM 生成。

兩層資料:
1. **FinMind facts**(快、無 cost):industry / market / listing_date —
   從 ensure_stock_info + TaiwanStockInfo cache 直接拿,寫進 SQLite。
2. **LLM 生成**(慢、要 token):description / uniqueness / moat — 用 Gemini
   1.5 Flash 跑一次,結果寫 SQLite cache,個股頁 lazy load 不卡 boot。

`get_company_profile(stock_id)` 是公開入口:
- cache 有 + 沒過期 → 直接回(秒級)
- cache 沒有 → 抓 FinMind 補 facts,LLM 生 description(看 GEMINI_API_KEY)
- regenerate=True → 強制重打 Gemini(其他 facts 也順便 refresh)

沒設 GEMINI_API_KEY → description/uniqueness/moat 留 None,UI 顯示 placeholder。
"""
from __future__ import annotations

import datetime as _datetime
import json
import logging
from datetime import datetime, timezone
from typing import Any

from src import config, database as db
from src import data_fetcher

logger = logging.getLogger(__name__)


# === Gemini SDK 動態載入 ===
# 包在 try/except 內 — 雲端環境少裝 google-generativeai 不該炸 boot。
# UI 拿到 ImportError 訊息會顯示 fallback 提示。
try:
    import google.generativeai as genai
    _GEMINI_AVAILABLE = True
except Exception as _e:  # noqa: BLE001
    genai = None  # type: ignore[assignment]
    _GEMINI_AVAILABLE = False
    logger.info("[COMPANY] google-generativeai 未安裝(%s),LLM 生成停用", _e)


GEMINI_MODEL = "gemini-2.5-flash-lite"

# session_state 旗標 key — 當輪打到 429 後寫入今日日期,後續同 sid / 異 sid
# 都跳過 LLM 直到明天(當輪指 streamlit session,跨 process 不共享)
_QUOTA_FLAG_KEY = "_gemini_quota_exceeded_date"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# === Quota / 錯誤分類 helpers ===

def _is_quota_exceeded(exc: BaseException) -> bool:
    """偵測 Gemini 429 quota 錯誤。

    可能來源:
    - google.api_core.exceptions.ResourceExhausted(canonical 429)
    - generic exception 字串內含 "429" / "quota exceeded" / "ResourceExhausted"
    用 class name + 字串雙重 match,避免硬綁特定 google 版本。
    """
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


def _is_not_configured_error(exc: BaseException) -> bool:
    """偵測 SDK / API key 缺失或失效。

    涵蓋兩類:
    - generate_with_gemini 自己拋的 RuntimeError(訊息含 GEMINI_API_KEY /
      google-generativeai)— 整個 SDK 沒裝 / env 沒設
    - Gemini API 回 400 API_KEY_INVALID(key 形狀對但實際失效 / 被撤銷)—
      訊息形如 `400 API key not valid. Please pass a valid API key.`
      → 此情況 fail-fast 整批,免得 500 檔 ×3s 全打一輪才發現
    """
    msg = str(exc)
    if "GEMINI_API_KEY" in msg or "google-generativeai" in msg:
        return True
    if "API_KEY_INVALID" in msg or "API key not valid" in msg:
        return True
    return False


def _safe_get_session_state():
    """取 streamlit.session_state — 不在 streamlit context 下回 None。
    company_profile 也會被 CLI / pytest 直接呼叫,不能 hard depend on streamlit。
    """
    try:
        import streamlit as st
        return st.session_state
    except Exception:  # noqa: BLE001
        return None


def _is_quota_flag_set_today() -> bool:
    """今日 quota 旗標是否已設(避免反覆撞 429 浪費 RTT)。"""
    state = _safe_get_session_state()
    if state is None:
        return False
    today = _datetime.date.today().isoformat()
    try:
        return state.get(_QUOTA_FLAG_KEY) == today
    except Exception:  # noqa: BLE001
        return False


def _set_quota_flag_today() -> None:
    """打到 429 後寫旗標,當輪不再呼叫 LLM 直到明天。"""
    state = _safe_get_session_state()
    if state is None:
        return
    try:
        state[_QUOTA_FLAG_KEY] = _datetime.date.today().isoformat()
    except Exception as e:  # noqa: BLE001
        logger.debug("[COMPANY] 寫 quota flag 失敗(忽略): %s", e)


# === FinMind facts ===

def fetch_taiwan_stock_info(stock_id: str) -> dict[str, Any]:
    """從 FinMind TaiwanStockInfo 撈個股 facts(industry/market/listing_date)。

    回 {industry, market, listing_date, foreign_limit, name}(任何欄位可為 None)。
    流程:走既有 ensure_stock_info,再從 _fetch_all_stock_info 撈額外欄位。
    """
    info = data_fetcher.ensure_stock_info(stock_id)
    if not info:
        return {
            "industry": None, "market": None, "listing_date": None,
            "foreign_limit": None, "name": "",
        }

    # 從全市場 cache 撈額外欄位(date / foreign_limit 若有)
    out: dict[str, Any] = {
        "industry": info.get("industry"),
        "market": "TW",  # 此專案僅做台股,固定 TW
        "listing_date": None,
        "foreign_limit": None,
        "name": info.get("name", ""),
    }
    try:
        all_info = data_fetcher._fetch_all_stock_info()
        raw = all_info.get(stock_id) or {}
        # FinMind TaiwanStockInfo 沒有 listing_date 欄位(date 是資料更新日,
        # 不是上市日)— 留 None;type 區分上市/上櫃當 sub-market
        sub_market = raw.get("type")
        if sub_market in ("twse", "tpex"):
            out["market"] = "上市" if sub_market == "twse" else "上櫃"
    except Exception as e:  # noqa: BLE001
        logger.debug("[COMPANY] _fetch_all_stock_info 失敗,只回基本: %s", e)
    return out


# === LLM 生成 ===

_GEMINI_PROMPT_TEMPLATE = """請用繁體中文,簡潔描述股票 {stock_id} {name}({industry})這間公司。

請回傳 JSON 格式,**只**包含以下三個欄位,各 1-2 句話:
{{
  "description": "做什麼業務 / 主要產品",
  "uniqueness": "技術或市場上的獨特性",
  "moat": "壟斷性或護城河(規模、專利、客戶綁定等)"
}}

不要加註解、不要寫 ``` 程式區塊符號。"""


def _strip_code_fence(text: str) -> str:
    """LLM 偶爾會夾 ```json ... ``` fence,strip 掉再 parse。"""
    s = text.strip()
    if s.startswith("```"):
        # 第一行可能是 ``` 或 ```json
        lines = s.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def generate_with_gemini(
    stock_id: str, name: str, industry: str | None,
) -> dict[str, str]:
    """用 Gemini API 生 description/uniqueness/moat。

    回 {description, uniqueness, moat}。失敗或缺 API key 拋 RuntimeError —
    呼叫端負責 catch + fallback message。
    """
    if not _GEMINI_AVAILABLE:
        raise RuntimeError(
            "google-generativeai 未安裝,執行 `pip install google-generativeai`"
        )
    api_key = config.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 未設定")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = _GEMINI_PROMPT_TEMPLATE.format(
        stock_id=stock_id, name=name or "", industry=industry or "未分類",
    )
    resp = model.generate_content(prompt)
    text = getattr(resp, "text", None) or ""
    if not text:
        raise RuntimeError("Gemini 回空字串")

    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini 回非 JSON: {cleaned[:200]}") from e

    return {
        "description": str(data.get("description", "")).strip(),
        "uniqueness": str(data.get("uniqueness", "")).strip(),
        "moat": str(data.get("moat", "")).strip(),
    }


# === SQLite cache ===

def _read_profile(stock_id: str) -> dict[str, Any] | None:
    """從 SQLite 讀整筆 profile,沒有回 None。"""
    db.init_db()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT stock_id, industry, market, listing_date, foreign_limit, "
            "description, uniqueness, moat, finmind_updated_at, llm_updated_at "
            "FROM company_profiles WHERE stock_id=?",
            (stock_id,),
        ).fetchone()
    return dict(row) if row else None


def _upsert_profile(stock_id: str, fields: dict[str, Any]) -> None:
    """合併寫入 — 只更新 fields 內有的 key,缺的保留舊值。
    用 INSERT ON CONFLICT DO UPDATE 配合 COALESCE 達成 partial update。
    """
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO company_profiles (
                stock_id, industry, market, listing_date, foreign_limit,
                description, uniqueness, moat,
                finmind_updated_at, llm_updated_at
            )
            VALUES (
                :stock_id, :industry, :market, :listing_date, :foreign_limit,
                :description, :uniqueness, :moat,
                :finmind_updated_at, :llm_updated_at
            )
            ON CONFLICT(stock_id) DO UPDATE SET
                industry           = COALESCE(excluded.industry, industry),
                market             = COALESCE(excluded.market, market),
                listing_date       = COALESCE(excluded.listing_date, listing_date),
                foreign_limit      = COALESCE(excluded.foreign_limit, foreign_limit),
                description        = COALESCE(excluded.description, description),
                uniqueness         = COALESCE(excluded.uniqueness, uniqueness),
                moat               = COALESCE(excluded.moat, moat),
                finmind_updated_at = COALESCE(excluded.finmind_updated_at, finmind_updated_at),
                llm_updated_at     = COALESCE(excluded.llm_updated_at, llm_updated_at)
            """,
            {
                "stock_id": stock_id,
                "industry": fields.get("industry"),
                "market": fields.get("market"),
                "listing_date": fields.get("listing_date"),
                "foreign_limit": fields.get("foreign_limit"),
                "description": fields.get("description"),
                "uniqueness": fields.get("uniqueness"),
                "moat": fields.get("moat"),
                "finmind_updated_at": fields.get("finmind_updated_at"),
                "llm_updated_at": fields.get("llm_updated_at"),
            },
        )


# === 公開入口 ===

def get_company_profile(
    stock_id: str,
    regenerate: bool = False,
    llm_call: bool = True,
) -> dict[str, Any]:
    """個股公司資訊查詢 — cache-first,需要才補 FinMind facts + LLM 生成。

    Args:
        stock_id: 股號(e.g. "2330")
        regenerate: True = 強制重打 Gemini 重生 description/uniqueness/moat
            (即使當輪已 quota_exceeded,仍嘗試 — 讓 user 手動重試)
        llm_call: False = **純 SQLite cache lookup 模式**,絕不打 LLM。
            cache miss 回 narrative_status='not_loaded',user 主動點按鈕
            才走 llm_call=True path。預設 True 維持 backward compat。
            regenerate=True 一律強制 LLM call(忽略 llm_call 設定)。

    回 dict:
        stock_id, name, industry, market, listing_date, foreign_limit,
        description, uniqueness, moat,
        finmind_updated_at, llm_updated_at,
        llm_error: str | None       — user-facing 短訊(< 100 字,不含 raw API dump)
        narrative_status: str       — 6 種狀態:
            * "ok"             = description 有值(cache 或新生成)
            * "not_loaded"     = cache miss + llm_call=False(尚未請求 LLM)
            * "quota_exceeded" = 今日 Gemini 配額用完(或被旗標跳過)
            * "not_configured" = 缺 GEMINI_API_KEY 或 SDK 沒裝
            * "failed"         = 其他 LLM 錯誤(網路 / parse 失敗等)
            * "empty"          = 邊角情況(LLM 回空字串等)

    Quota 防呆:打到 429 後設 session_state 旗標,當輪後續同/異 sid 直接跳
    過 LLM(不浪費 RTT)。regenerate=True 仍會再試。
    """
    sid = (stock_id or "").strip()
    if not sid:
        return _empty_profile(sid)

    cached = _read_profile(sid)
    has_cached_narrative = bool(cached and cached.get("description"))
    needs_finmind = cached is None or not cached.get("industry")

    # 決定是否跑 LLM:
    # - cache 有 narrative + 沒 regenerate → 不跑
    # - llm_call=False + 沒 regenerate → 不跑(cache-only mode,等 user 主動點)
    # - 當輪今日已 quota_exceeded + 沒 regenerate → 不跑
    # - 否則跑
    needs_llm = regenerate or not has_cached_narrative
    skipped_due_to_no_call = False
    if needs_llm and not regenerate and not llm_call:
        needs_llm = False
        skipped_due_to_no_call = True
    quota_was_exceeded = _is_quota_flag_set_today()
    skipped_due_to_quota = False
    if needs_llm and quota_was_exceeded and not regenerate:
        needs_llm = False
        skipped_due_to_quota = True

    profile_to_write: dict[str, Any] = {}
    name = ""

    # 1. FinMind facts(快;只缺才補)
    finmind_info: dict[str, Any] = {}
    if needs_finmind:
        finmind_info = fetch_taiwan_stock_info(sid)
        name = finmind_info.get("name", "") or ""
        profile_to_write.update({
            "industry": finmind_info.get("industry"),
            "market": finmind_info.get("market"),
            "listing_date": finmind_info.get("listing_date"),
            "foreign_limit": finmind_info.get("foreign_limit"),
            "finmind_updated_at": _now_iso(),
        })
    else:
        # cache 已有 facts → 從 stocks 表撈 name(LLM prompt 用得到)
        try:
            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT name FROM stocks WHERE stock_id=?", (sid,),
                ).fetchone()
                name = row["name"] if row else ""
        except Exception:  # noqa: BLE001
            name = ""

    # 2. LLM 生成
    llm_error: str | None = None
    failure_status: str | None = None  # "quota_exceeded" / "not_configured" / "failed"
    if needs_llm:
        industry_for_prompt = (
            profile_to_write.get("industry")
            or (cached or {}).get("industry")
            or finmind_info.get("industry")
        )
        try:
            llm_data = generate_with_gemini(sid, name, industry_for_prompt)
            profile_to_write.update({
                "description": llm_data["description"],
                "uniqueness": llm_data["uniqueness"],
                "moat": llm_data["moat"],
                "llm_updated_at": _now_iso(),
            })
        except Exception as e:  # noqa: BLE001
            # **不**把 raw exception dump 給 UI(google API 錯訊很長)。
            # 分三類顯示對應友善訊息,只有 logger 內保留完整字串。
            logger.warning(
                "[COMPANY] LLM 生成失敗 sid=%s: %s", sid, str(e)[:300],
            )
            if _is_quota_exceeded(e):
                _set_quota_flag_today()
                failure_status = "quota_exceeded"
                llm_error = "今日 Gemini 免費額度已用完(明天重置)"
            elif _is_not_configured_error(e):
                failure_status = "not_configured"
                llm_error = "LLM 未設定 — 請設 GEMINI_API_KEY"
            else:
                failure_status = "failed"
                llm_error = "LLM 暫時無法呼叫,請稍後重試"
    elif skipped_due_to_quota:
        failure_status = "quota_exceeded"
        llm_error = "今日 Gemini 免費額度已用完(明天重置)"
    elif skipped_due_to_no_call:
        # cache miss + llm_call=False → user 還沒按按鈕,不算錯誤
        failure_status = "not_loaded"

    if profile_to_write:
        _upsert_profile(sid, profile_to_write)

    # 重讀回最新狀態(讓 cache + 新寫入合併)
    final = _read_profile(sid) or _empty_profile(sid)
    final["name"] = name or final.get("name", "")
    final["llm_error"] = llm_error

    # narrative_status 優先:有 description(cache 或新)→ ok;否則看 failure_status
    if final.get("description"):
        final["narrative_status"] = "ok"
    elif failure_status:
        final["narrative_status"] = failure_status
    else:
        final["narrative_status"] = "empty"
    return final


def _empty_profile(sid: str) -> dict[str, Any]:
    return {
        "stock_id": sid,
        "name": "",
        "industry": None,
        "market": None,
        "listing_date": None,
        "foreign_limit": None,
        "description": None,
        "uniqueness": None,
        "moat": None,
        "finmind_updated_at": None,
        "llm_updated_at": None,
        "llm_error": None,
        "narrative_status": "empty",
    }


__all__ = [
    "fetch_taiwan_stock_info",
    "generate_with_gemini",
    "get_company_profile",
    "GEMINI_MODEL",
]
