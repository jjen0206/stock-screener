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


GEMINI_MODEL = "gemini-2.0-flash"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    stock_id: str, regenerate: bool = False,
) -> dict[str, Any]:
    """個股公司資訊查詢 — cache-first,需要才補 FinMind facts + LLM 生成。

    Args:
        stock_id: 股號(e.g. "2330")
        regenerate: True = 強制重打 Gemini 重生 description/uniqueness/moat

    回:
        {
          stock_id, name, industry, market, listing_date, foreign_limit,
          description, uniqueness, moat,
          finmind_updated_at, llm_updated_at,
          llm_error: str | None,    # LLM 生成失敗的 user-facing 訊息
        }
        所有欄位可為 None。description 為 None 表示「沒生過 / 失敗」。
    """
    sid = (stock_id or "").strip()
    if not sid:
        return _empty_profile(sid)

    cached = _read_profile(sid)
    needs_finmind = cached is None or not cached.get("industry")
    needs_llm = (
        regenerate
        or cached is None
        or not cached.get("description")
    )

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
            logger.warning("[COMPANY] LLM 生成失敗 sid=%s: %s", sid, e)
            llm_error = f"LLM 暫時失敗:{e}"

    if profile_to_write:
        _upsert_profile(sid, profile_to_write)

    # 重讀回最新狀態(讓 cache + 新寫入合併)
    final = _read_profile(sid) or _empty_profile(sid)
    final["name"] = name or final.get("name", "")
    final["llm_error"] = llm_error
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
    }


__all__ = [
    "fetch_taiwan_stock_info",
    "generate_with_gemini",
    "get_company_profile",
    "GEMINI_MODEL",
]
