"""C-AI 軍師(2026-05-17 加):用 Gemini 對「今天所有資料」給綜合判讀。

主公在 Telegram inline keyboard、Discord `/ask`、Streamlit「💬 問軍師」頁
按按鈕或下指令 → 此模組統一處理:
- 個股問題:`ask_about_stock("2330", "怎麼樣")` → 拉該 sid 全部資料 → 組 prompt → Gemini
- 大盤問題:`ask_about_market("今天大盤怎麼樣")` → regime + sentiment + 熱題材 → Gemini

設計重點:
- 結構化 prompt(固定軍師人設:嚴謹+主動提示風險)避免 LLM 亂跑
- 拉資料一律走既有 db API(daily_prices / institutional / shareholder_concentration /
  news / stock_warnings / daily_picks / pick_shap_explanations / company_profiles)
- Gemini 失敗 / quota / 缺 key 一律 graceful → 回中文 fallback 訊息(不 raise)
- kill-switch:env AI_ASSISTANT_ENABLED=false → is_enabled() 回 False,
  ask_* 直接回「軍師暫時下班」訊息

公開 API:
- is_enabled() -> bool
- ask_about_stock(sid: str, question: str = "") -> dict
- ask_about_market(question: str = "") -> dict
回傳 dict 結構:
    {"ok": bool, "answer": str, "context_summary": str, "model": str, "error": str | None}
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date as _date
from typing import Any

from src import config, database as db

logger = logging.getLogger(__name__)


# === Gemini SDK 動態載入(同 company_profile.py)===
try:
    import google.generativeai as genai
    _GEMINI_AVAILABLE = True
except Exception as _e:  # noqa: BLE001
    genai = None  # type: ignore[assignment]
    _GEMINI_AVAILABLE = False
    logger.info("[AI] google-generativeai 未安裝(%s),AI 軍師停用", _e)


GEMINI_MODEL = "gemini-2.5-flash-lite"

# 軍師人設 — 固定 system prompt 部分;
# 嚴謹優先 + 主動提示風險 + 不替主公做決定(主公規矩:不做隱藏決定)
SYSTEM_PROMPT = """你是資深量化分析師「軍師」,專責服務主公(零成本個人選股工具的單一使用者)。

行為準則:
1. 用繁體中文簡潔回答(<300 字),先給結論再給細節
2. 嚴謹優先 — 不確定的事說「資料不足」,絕不編造數字
3. 主動提示風險 — 看到警示股 / drawdown / 流動性差 / 法人連賣一定要點出
4. 不替主公做隱藏決定 — 給判讀和選項,最終決策權留給主公
5. 引用具體數據 — 例如「MA5 上穿 MA20、量比 1.5×」而非「技術面轉強」
6. 結尾固定加一行「⚠️ 僅供研究,非投資建議」

訊息結構建議:
  📊 [結論一句]
  • 技術面:...
  • 籌碼面:...
  • 風險:...
  ⚠️ 僅供研究,非投資建議
"""


def is_enabled() -> bool:
    """讀 env AI_ASSISTANT_ENABLED(預設 true)。"""
    raw = os.getenv("AI_ASSISTANT_ENABLED", "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def _disabled_payload(reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "answer": f"💤 軍師暫時下班:{reason}",
        "context_summary": "",
        "model": "",
        "error": reason,
    }


# === 資料蒐集 ===

def _latest_price_block(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    """最近 1 / 5 / 20 日 close + 量。回 dict 給 prompt 組裝。"""
    rows = conn.execute(
        "SELECT date, close, volume FROM daily_prices "
        "WHERE stock_id=? ORDER BY date DESC LIMIT 20",
        (sid,),
    ).fetchall()
    if not rows:
        return {}
    closes = [float(r["close"]) for r in rows if r["close"] is not None]
    vols = [float(r["volume"]) for r in rows if r["volume"] is not None]
    if not closes:
        return {}
    out: dict[str, Any] = {
        "as_of": rows[0]["date"],
        "close": closes[0],
        "n_days": len(closes),
    }
    if len(closes) >= 5:
        out["change_5d_pct"] = round((closes[0] / closes[4] - 1) * 100, 2)
    if len(closes) >= 20:
        out["change_20d_pct"] = round((closes[0] / closes[19] - 1) * 100, 2)
    if len(vols) >= 5:
        out["vol_ratio_5d"] = round(vols[0] / (sum(vols[:5]) / 5), 2) if sum(vols[:5]) > 0 else None
    return out


def _technical_block(sid: str) -> dict[str, Any]:
    """走 individual_sections._compute_technical_summary — 7 項 rule-based 解讀。

    individual_sections 用 @st.cache_data,在 CLI / pytest context 沒事(streamlit
    decorator 在 non-streamlit context 等同 no-op)。失敗回空 dict。
    """
    try:
        from src.individual_sections import _compute_technical_summary
        result = _compute_technical_summary(sid)
        # st.cache_data 在 non-streamlit 下也 ok;但若回 error 字典,只留 error
        if "error" in result:
            return {"note": result["error"]}
        # 只挑核心欄位,避免 prompt 爆字
        return {
            "trend": result.get("trend"),
            "price_pos": result.get("price_pos"),
            "ma_align": result.get("ma_align"),
            "vol_text": result.get("vol_text"),
            "bb_pos": result.get("bb_pos"),
            "summary": result.get("summary"),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("[AI] technical_block 失敗 %s: %s", sid, e)
        return {}


def _institutional_block(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    """最近 3 日法人買賣超合計。"""
    rows = conn.execute(
        "SELECT date, foreign_buy_sell AS f, trust_buy_sell AS t, "
        "dealer_buy_sell AS d FROM institutional "
        "WHERE stock_id=? ORDER BY date DESC LIMIT 3",
        (sid,),
    ).fetchall()
    if not rows:
        return {}
    f = sum(int(r["f"] or 0) for r in rows)
    t = sum(int(r["t"] or 0) for r in rows)
    d = sum(int(r["d"] or 0) for r in rows)
    return {
        "foreign_3d": f, "trust_3d": t, "dealer_3d": d,
        "total_3d": f + t + d, "n_days": len(rows),
    }


def _shareholder_block(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    """最近一週千張戶人數 + Δ。"""
    try:
        row = conn.execute(
            "SELECT week_end, holders_1000up_count, holders_delta_w, holders_pct "
            "FROM shareholder_concentration WHERE sid=? ORDER BY week_end DESC LIMIT 1",
            (sid,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    return {
        "week_end": row["week_end"],
        "holders_1000up_count": row["holders_1000up_count"],
        "holders_delta_w": row["holders_delta_w"],
        "holders_pct": row["holders_pct"],
    }


def _news_block(conn: sqlite3.Connection, sid: str, limit: int = 5) -> list[dict[str, Any]]:
    """最近 5 則重大訊息 — 只取 subject + 日期 給 prompt(內文太長)。"""
    try:
        rows = conn.execute(
            "SELECT publish_date, subject FROM news WHERE sid=? "
            "ORDER BY publish_date DESC, publish_time DESC LIMIT ?",
            (sid, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"date": r["publish_date"], "subject": r["subject"]} for r in rows]


def _warnings_block(conn: sqlite3.Connection, sid: str) -> list[dict[str, Any]]:
    """生效中的警示股紀錄(effective_to IS NULL 或 > today)。"""
    today = _date.today().isoformat()
    try:
        rows = conn.execute(
            "SELECT warning_type, announced_date, reason FROM stock_warnings "
            "WHERE stock_id=? AND (effective_to IS NULL OR effective_to > ?) "
            "ORDER BY announced_date DESC",
            (sid, today),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "type": r["warning_type"],
            "announced": r["announced_date"],
            "reason": r["reason"],
        }
        for r in rows
    ]


def _picks_block(conn: sqlite3.Connection, sid: str, limit: int = 5) -> list[dict[str, Any]]:
    """系統最近被選中為 picks 的歷史(daily_picks)— 只取 trade_date + strategy。"""
    try:
        rows = conn.execute(
            "SELECT trade_date, strategy, score, ml_prob FROM daily_picks "
            "WHERE sid=? ORDER BY trade_date DESC LIMIT ?",
            (sid, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "date": r["trade_date"],
            "strategy": r["strategy"],
            "score": r["score"],
            "ml_prob": r["ml_prob"],
        }
        for r in rows
    ]


def _shap_block(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    """最近一次 SHAP 解釋(top 3 features only)。"""
    try:
        row = conn.execute(
            "SELECT pick_date, strategy, top_features FROM pick_shap_explanations "
            "WHERE sid=? ORDER BY pick_date DESC LIMIT 1",
            (sid,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    try:
        feats = json.loads(row["top_features"])
        # 只取 top 3,prompt 不要太肥
        feats = feats[:3] if isinstance(feats, list) else []
    except (json.JSONDecodeError, TypeError):
        return {}
    return {
        "pick_date": row["pick_date"],
        "strategy": row["strategy"],
        "top_features": feats,
    }


def _profile_block(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    """company_profiles description / industry。"""
    try:
        row = conn.execute(
            "SELECT industry, description FROM company_profiles WHERE stock_id=?",
            (sid,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    return {"industry": row["industry"], "description": row["description"]}


def _positions_block(conn: sqlite3.Connection, sid: str) -> list[dict[str, Any]]:
    """主公對該 sid 持倉 / paper trades(在 open 中的)。"""
    out = []
    try:
        rows = conn.execute(
            "SELECT entry_date, entry_price, shares, stop_loss, take_profit "
            "FROM user_positions WHERE stock_id=? AND is_open=1",
            (sid,),
        ).fetchall()
        for r in rows:
            out.append({
                "source": "user",
                "entry_date": r["entry_date"],
                "entry_price": r["entry_price"],
                "shares": r["shares"],
                "stop_loss": r["stop_loss"],
                "take_profit": r["take_profit"],
            })
    except sqlite3.OperationalError:
        pass
    return out


def collect_stock_context(sid: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """彙整單一 sid 的全部 context(prompt 組裝 + 測試共用)。

    回 dict:
      - meta: {sid, name}
      - profile: {industry, description}
      - price: {as_of, close, change_5d_pct, change_20d_pct, vol_ratio_5d}
      - technical: {trend, ma_align, ...}
      - institutional: {foreign_3d, trust_3d, dealer_3d}
      - shareholder: {week_end, holders_1000up_count, holders_delta_w}
      - news: [{date, subject}, ...]
      - warnings: [{type, announced, reason}, ...]
      - picks: [{date, strategy, ml_prob}, ...]
      - shap: {pick_date, top_features}
      - positions: [{entry_date, entry_price, shares}, ...]
    """
    out: dict[str, Any] = {"meta": {"sid": sid, "name": ""}}
    close_conn_after = conn is None
    if conn is None:
        db.init_db()
        conn_ctx = db.get_conn()
        conn = conn_ctx.__enter__()
    else:
        conn_ctx = None

    try:
        # name
        try:
            r = conn.execute(
                "SELECT name FROM stocks WHERE stock_id=? LIMIT 1", (sid,)
            ).fetchone()
            out["meta"]["name"] = (r["name"] or "") if r else ""
        except sqlite3.OperationalError:
            pass

        out["profile"] = _profile_block(conn, sid)
        out["price"] = _latest_price_block(conn, sid)
        out["technical"] = _technical_block(sid)
        out["institutional"] = _institutional_block(conn, sid)
        out["shareholder"] = _shareholder_block(conn, sid)
        out["news"] = _news_block(conn, sid)
        out["warnings"] = _warnings_block(conn, sid)
        out["picks"] = _picks_block(conn, sid)
        out["shap"] = _shap_block(conn, sid)
        out["positions"] = _positions_block(conn, sid)
    finally:
        if close_conn_after and conn_ctx is not None:
            conn_ctx.__exit__(None, None, None)

    return out


def collect_market_context(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """大盤層級 context:regime + sentiment + 熱題材 + 警示概況。"""
    out: dict[str, Any] = {}
    close_conn_after = conn is None
    if conn is None:
        db.init_db()
        conn_ctx = db.get_conn()
        conn = conn_ctx.__enter__()
    else:
        conn_ctx = None

    try:
        # regime
        try:
            from src.market_regime import compute_regime
            regime = compute_regime()
            out["regime"] = {
                "label": regime.get("label"),
                "regime": regime.get("regime"),
            }
        except Exception as e:  # noqa: BLE001
            logger.debug("[AI] compute_regime 失敗: %s", e)
            out["regime"] = {}

        # 美股 sentiment
        try:
            from src.market_sentiment import get_us_sentiment
            out["us_sentiment"] = get_us_sentiment()
        except Exception as e:  # noqa: BLE001
            logger.debug("[AI] get_us_sentiment 失敗: %s", e)
            out["us_sentiment"] = {}

        # 熱題材 top 3
        try:
            from src.theme_heat import compute_theme_heat
            heat = compute_theme_heat()
            # heat 結構不固定;取 top 3 keys
            if isinstance(heat, dict):
                items = list(heat.items())[:3]
                out["theme_heat_top"] = [
                    {"theme": k, "data": v if isinstance(v, dict) else {"value": v}}
                    for k, v in items
                ]
            else:
                out["theme_heat_top"] = []
        except Exception as e:  # noqa: BLE001
            logger.debug("[AI] theme_heat 失敗: %s", e)
            out["theme_heat_top"] = []

        # 警示股總數(生效中)
        today = _date.today().isoformat()
        try:
            row = conn.execute(
                "SELECT COUNT(DISTINCT stock_id) AS c FROM stock_warnings "
                "WHERE effective_to IS NULL OR effective_to > ?",
                (today,),
            ).fetchone()
            out["active_warnings_count"] = row["c"] if row else 0
        except sqlite3.OperationalError:
            out["active_warnings_count"] = 0

        # 今日 picks 數量(各 universe / strategy 加總)
        try:
            row = conn.execute(
                "SELECT COUNT(DISTINCT sid) AS c FROM daily_picks "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM daily_picks)"
            ).fetchone()
            out["picks_today"] = row["c"] if row else 0
        except sqlite3.OperationalError:
            out["picks_today"] = 0
    finally:
        if close_conn_after and conn_ctx is not None:
            conn_ctx.__exit__(None, None, None)

    return out


# === Prompt 組裝 ===

def build_stock_prompt(sid: str, question: str, ctx: dict[str, Any]) -> str:
    """把 ctx dict 攤平成中文 markdown context block + 主公提問。"""
    meta = ctx.get("meta", {})
    name = meta.get("name", "")
    profile = ctx.get("profile") or {}
    price = ctx.get("price") or {}
    tech = ctx.get("technical") or {}
    inst = ctx.get("institutional") or {}
    sh = ctx.get("shareholder") or {}
    news = ctx.get("news") or []
    warns = ctx.get("warnings") or []
    picks = ctx.get("picks") or []
    shap = ctx.get("shap") or {}
    positions = ctx.get("positions") or []

    lines: list[str] = []
    lines.append(f"# 個股資料:{sid} {name}".rstrip())

    if profile.get("industry"):
        lines.append(f"產業:{profile['industry']}")
    if profile.get("description"):
        lines.append(f"主營:{profile['description']}")

    if price:
        seg = [f"資料日:{price.get('as_of', '?')}", f"收盤:{price.get('close')}"]
        if price.get("change_5d_pct") is not None:
            seg.append(f"5日 {price['change_5d_pct']:+.2f}%")
        if price.get("change_20d_pct") is not None:
            seg.append(f"20日 {price['change_20d_pct']:+.2f}%")
        if price.get("vol_ratio_5d") is not None:
            seg.append(f"量比 5MA {price['vol_ratio_5d']:.2f}×")
        lines.append("## 價量\n" + " / ".join(seg))

    if tech:
        if tech.get("note"):
            lines.append(f"## 技術\n備註:{tech['note']}")
        else:
            tech_parts = []
            for k in ("trend", "ma_align", "vol_text", "bb_pos", "summary"):
                v = tech.get(k)
                if v:
                    tech_parts.append(v)
            if tech_parts:
                lines.append("## 技術\n" + " / ".join(tech_parts))

    if inst and inst.get("n_days"):
        lines.append(
            f"## 法人 ({inst['n_days']} 日合計)\n"
            f"外資 {inst.get('foreign_3d', 0):+,}・"
            f"投信 {inst.get('trust_3d', 0):+,}・"
            f"自營 {inst.get('dealer_3d', 0):+,}・"
            f"合計 {inst.get('total_3d', 0):+,}(張)"
        )

    if sh:
        lines.append(
            f"## 千張大戶 ({sh.get('week_end')})\n"
            f"人數 {sh.get('holders_1000up_count')},週Δ {sh.get('holders_delta_w'):+d}"
            if sh.get("holders_delta_w") is not None
            else f"## 千張大戶 ({sh.get('week_end')}) 人數 {sh.get('holders_1000up_count')}"
        )

    if warns:
        warn_lines = [
            f"- {w['type']}({w['announced']}) {w.get('reason') or ''}"
            for w in warns
        ]
        lines.append("## ⚠️ 警示股紀錄\n" + "\n".join(warn_lines))

    if news:
        news_lines = [f"- {n['date']} {n['subject']}" for n in news]
        lines.append("## 近期重大訊息\n" + "\n".join(news_lines))

    if picks:
        pick_lines = []
        for p in picks:
            ml_str = f" ml={p['ml_prob']:.2f}" if p.get("ml_prob") is not None else ""
            pick_lines.append(f"- {p['date']} {p['strategy']}{ml_str}")
        lines.append("## 系統推薦紀錄\n" + "\n".join(pick_lines))

    if shap and shap.get("top_features"):
        feat_lines = [
            f"- {f.get('feature')} 貢獻 {f.get('contribution_pct', 0):.1f}% "
            f"({f.get('direction', '?')})"
            for f in shap["top_features"]
        ]
        lines.append(
            f"## SHAP 解釋 (最近一次 {shap.get('pick_date')})\n"
            + "\n".join(feat_lines)
        )

    if positions:
        pos_lines = []
        for p in positions:
            pos_lines.append(
                f"- {p['entry_date']} 進場 {p['entry_price']} × {p['shares']} 股 "
                f"停損 {p.get('stop_loss') or '-'} / 停利 {p.get('take_profit') or '-'}"
            )
        lines.append("## 持倉\n" + "\n".join(pos_lines))

    context_block = "\n\n".join(lines)
    q = question.strip() or "綜合判讀這檔股票目前狀況,給進場 / 觀望 / 出場建議。"

    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"--- 資料 ---\n{context_block}\n--- /資料 ---\n\n"
        f"主公問:{q}"
    )


def build_market_prompt(question: str, ctx: dict[str, Any]) -> str:
    regime = ctx.get("regime") or {}
    us = ctx.get("us_sentiment") or {}
    themes = ctx.get("theme_heat_top") or []
    warns_n = ctx.get("active_warnings_count", 0)
    picks_n = ctx.get("picks_today", 0)

    lines: list[str] = ["# 大盤狀態"]
    if regime:
        lines.append(f"TAIEX regime:{regime.get('label')} ({regime.get('regime')})")
    if us:
        # us_sentiment 結構不固定,只取常見鍵
        sample = {
            k: us.get(k)
            for k in ("nasdaq", "sp500", "vix", "label", "summary", "score")
            if us.get(k) is not None
        }
        if sample:
            lines.append("美股:" + ", ".join(f"{k}={v}" for k, v in sample.items()))
    if themes:
        t_lines = [f"- {t['theme']}" for t in themes]
        lines.append("熱題材 Top3:\n" + "\n".join(t_lines))
    lines.append(f"今日 picks 數:{picks_n}")
    lines.append(f"生效中警示股:{warns_n} 檔")

    context_block = "\n".join(lines)
    q = question.strip() or "綜合判讀今天大盤狀況,給操作偏多 / 偏空 / 觀望建議。"

    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"--- 資料 ---\n{context_block}\n--- /資料 ---\n\n"
        f"主公問:{q}"
    )


# === Gemini 呼叫 ===

def _call_gemini(prompt: str) -> str:
    """同步打 Gemini,回 text。失敗 raise RuntimeError。"""
    if not _GEMINI_AVAILABLE:
        raise RuntimeError("google-generativeai 未安裝")
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY 未設定")

    genai.configure(api_key=config.GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    resp = model.generate_content(prompt)
    text = getattr(resp, "text", None) or ""
    if not text:
        raise RuntimeError("Gemini 回空字串")
    return text.strip()


def _short_context_summary(ctx: dict[str, Any]) -> str:
    """給 UI 顯示「我用了什麼資料」— 一行概覽。"""
    bits = []
    if ctx.get("price", {}).get("close"):
        bits.append(f"收 {ctx['price']['close']}")
    if ctx.get("institutional", {}).get("total_3d") is not None:
        bits.append(f"法人3日 {ctx['institutional']['total_3d']:+,}")
    if ctx.get("warnings"):
        bits.append(f"⚠️警示×{len(ctx['warnings'])}")
    if ctx.get("news"):
        bits.append(f"新聞×{len(ctx['news'])}")
    if ctx.get("picks"):
        bits.append(f"picks×{len(ctx['picks'])}")
    return " · ".join(bits)


def ask_about_stock(
    sid: str,
    question: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """主公問該股票 → 拉全部資料 → Gemini 判讀。

    回 dict:{ok, answer, context_summary, model, error}
    """
    if not is_enabled():
        return _disabled_payload("AI_ASSISTANT_ENABLED=false")
    if not sid or not str(sid).strip():
        return {
            "ok": False, "answer": "請給股票代號", "context_summary": "",
            "model": "", "error": "empty_sid",
        }
    sid = str(sid).strip()

    try:
        ctx = collect_stock_context(sid, conn=conn)
    except Exception as e:  # noqa: BLE001
        logger.exception("[AI] collect_stock_context 失敗 %s", sid)
        return {
            "ok": False, "answer": "資料庫讀取失敗,請稍後再試",
            "context_summary": "", "model": "", "error": str(e),
        }

    if not ctx.get("price"):
        return {
            "ok": False,
            "answer": f"❓ 找不到 {sid} 的歷史資料,可能未抓取過。",
            "context_summary": "",
            "model": "",
            "error": "no_data",
        }

    prompt = build_stock_prompt(sid, question, ctx)
    ctx_summary = _short_context_summary(ctx)

    try:
        answer = _call_gemini(prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning("[AI] Gemini call failed: %s", e)
        return {
            "ok": False,
            "answer": f"🤖 軍師暫時無法回應({type(e).__name__})。\n資料摘要:{ctx_summary}",
            "context_summary": ctx_summary,
            "model": GEMINI_MODEL,
            "error": str(e),
        }

    return {
        "ok": True,
        "answer": answer,
        "context_summary": ctx_summary,
        "model": GEMINI_MODEL,
        "error": None,
    }


def ask_about_market(
    question: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """主公問大盤 → 拉 regime / sentiment / 熱題材 → Gemini。"""
    if not is_enabled():
        return _disabled_payload("AI_ASSISTANT_ENABLED=false")

    try:
        ctx = collect_market_context(conn=conn)
    except Exception as e:  # noqa: BLE001
        logger.exception("[AI] collect_market_context 失敗")
        return {
            "ok": False, "answer": "大盤資料讀取失敗", "context_summary": "",
            "model": "", "error": str(e),
        }

    prompt = build_market_prompt(question, ctx)

    bits = []
    if ctx.get("regime", {}).get("label"):
        bits.append(f"regime={ctx['regime']['label']}")
    if ctx.get("picks_today") is not None:
        bits.append(f"picks {ctx['picks_today']}")
    ctx_summary = " · ".join(bits)

    try:
        answer = _call_gemini(prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning("[AI] Gemini market call failed: %s", e)
        return {
            "ok": False,
            "answer": f"🤖 軍師暫時無法回應({type(e).__name__})。\n資料摘要:{ctx_summary}",
            "context_summary": ctx_summary,
            "model": GEMINI_MODEL,
            "error": str(e),
        }
    return {
        "ok": True,
        "answer": answer,
        "context_summary": ctx_summary,
        "model": GEMINI_MODEL,
        "error": None,
    }


__all__ = [
    "is_enabled",
    "collect_stock_context",
    "collect_market_context",
    "build_stock_prompt",
    "build_market_prompt",
    "ask_about_stock",
    "ask_about_market",
    "SYSTEM_PROMPT",
    "GEMINI_MODEL",
]
