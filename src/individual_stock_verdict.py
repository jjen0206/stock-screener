"""個股「軍師判讀」整合模組 — 把 K 線形態 / 警示 / 大盤 regime / ML 機率 /
策略共識 / 題材熱度 / 持倉狀態 全部串起來,給主公一個一目了然的
🟢 可進場 / 🟡 觀望 / 🔴 不進場 結論 + 白話理由。

設計重點(2026-05-18 主公拍板):
  - 主公看不懂技術術語 → 結論必須白話,理由 4-5 行內。
  - 紅燈一定要直接擋:default_settlement / full_cash active → 🔴 不進場。
  - 大盤 bear → 全部降權,bull → 加分(主公會看大盤臉色)。
  - 多個訊號加權累計,正負抵銷後落在三檔之一。
  - 🟢 verdict 才給進場價區間 / 停損 / 停利(三 ATR 守則)。

公開 API:
  - is_enabled() -> bool
  - compute_verdict(sid, db_path=None) -> dict  # 純資料,無 streamlit
  - render_stock_verdict(sid) -> None           # streamlit wrapper
  - verdict_tag_for_card(sid, db_path=None) -> str  # 給卡片用「🟢 可進場」短標
  - PATTERN_LABELS / PATTERN_MEANINGS           # 給 K 線形態 section reuse

verdict_color → emoji 對應:
  🟢 → 可進場   (正面訊號 ≥ 3 + 無紅燈 + 大盤非空頭)
  🟡 → 觀望     (正負相當 / 大盤盤整 / 訊號不足)
  🔴 → 不進場   (紅燈警示 / 大盤空頭 / 負面 ≥ 3)

Kill-switch: STOCK_VERDICT_ENABLED=true 預設 on,設成 false → render 顯停用提示,
compute 回 {'enabled': False, ...} 讓 caller 自己 graceful skip。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date as _date, timedelta
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Pattern label / 白話解釋 — 給 K 線形態 section 與軍師理由 reuse。
# ---------------------------------------------------------------------------

PATTERN_LABELS: dict[str, str] = {
    "three_white_soldiers": "三紅兵",
    "hammer": "槌子線",
    "engulfing": "看漲吞噬",
    "morning_star": "晨星",
    "flag": "旗形突破",
    "doji": "十字星",
}

# 每形態:(中文白話, 偏向 emoji, 主公看懂的一句話解釋)
PATTERN_MEANINGS: dict[str, tuple[str, str, str]] = {
    "three_white_soldiers": ("三紅兵", "🟢", "連 3 根紅 K — 強勢多頭"),
    "hammer":               ("槌子線", "🟢", "下影長 — 跌深反彈訊號"),
    "engulfing":            ("看漲吞噬", "🟢", "大紅吃掉前黑 — 底部反轉"),
    "morning_star":         ("晨星", "🟢", "底部 3 日反轉 — 強烈買訊"),
    "flag":                 ("旗形突破", "🟢", "整理後跳出 — 強勢續攻"),
    "doji":                 ("十字星", "🟡", "多空拉鋸 — 看下一根方向"),
}

BULLISH_PATTERNS: set[str] = {
    "three_white_soldiers", "hammer", "engulfing", "morning_star", "flag",
}
NEUTRAL_PATTERNS: set[str] = {"doji"}


# 給 UI 用的「最近一根」白話
def latest_pattern_phrase(hits: list[dict]) -> str:
    """把最近一根的命中 list 轉成白話一句。沒命中 → 中性。"""
    if not hits:
        return "🟡 無形態訊號 — 中性,看下一根再決定"
    parts: list[str] = []
    for h in hits:
        name = h.get("name", "")
        meaning = PATTERN_MEANINGS.get(name)
        if meaning is None:
            continue
        label, emoji, phrase = meaning
        conf = int(h.get("confidence", 1))
        stars = "★" * conf
        parts.append(f"{emoji} {label}({stars}) — {phrase}")
    if not parts:
        return "🟡 無形態訊號 — 中性"
    return " ｜ ".join(parts)


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """讀 env `STOCK_VERDICT_ENABLED`(預設 true)。

    設成 false → compute_verdict 仍可呼叫但回 {'enabled': False}。
    render_stock_verdict 看到 disabled 直接 caption 提示後 return。
    """
    raw = os.environ.get("STOCK_VERDICT_ENABLED", "true").strip().lower()
    return raw not in ("false", "0", "no", "off", "")


# ---------------------------------------------------------------------------
# 訊號權重(集中可調)
# ---------------------------------------------------------------------------

# 大盤 regime → 偏多 / 偏空 / 中性 加分。bull/weak_bull/sideways/bear/unknown。
_REGIME_SCORE: dict[str, int] = {
    "bull": +2, "weak_bull": +1, "sideways": 0, "bear": -2, "unknown": 0,
}

# ML calibrated 機率區間 → 分數
def _ml_score(prob: float | None) -> int:
    if prob is None:
        return 0
    if prob >= 0.70:
        return +3
    if prob >= 0.65:
        return +2
    if prob >= 0.55:
        return +1
    if prob >= 0.50:
        return 0
    if prob >= 0.40:
        return -1
    return -2


# ---------------------------------------------------------------------------
# 訊號收集(每段 try-except,失敗就 skip 不擋整體)
# ---------------------------------------------------------------------------

def _collect_patterns(sid: str, days: int = 30) -> tuple[dict[str, int], list[dict]]:
    """近 N 日各形態出現次數 + 最後一根命中 list。
    失敗 → ({}, [])。
    """
    try:
        from src import candlestick_patterns as _cp
        from src import database as db
    except Exception:  # noqa: BLE001
        return {}, []
    if not _cp.is_enabled():
        return {}, []
    counts: dict[str, int] = {}
    last_hits: list[dict] = []
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT date, open, high, low, close FROM daily_prices "
                "WHERE stock_id=? AND open IS NOT NULL AND high IS NOT NULL "
                "AND low IS NOT NULL AND close IS NOT NULL "
                "ORDER BY date DESC LIMIT ?",
                (str(sid).strip(), int(days)),
            ).fetchall()
    except sqlite3.OperationalError:
        return {}, []
    if not rows or len(rows) < 3:
        return {}, []
    bars = [dict(r) for r in reversed(rows)]
    df_bars = pd.DataFrame(bars)
    for end in range(3, len(df_bars) + 1):
        window = df_bars.iloc[:end]
        try:
            hits = _cp.detect_all_patterns(sid, window)
        except Exception:  # noqa: BLE001
            continue
        for h in hits:
            name = h.get("name", "")
            counts[name] = counts.get(name, 0) + 1
    try:
        last_hits = _cp.detect_all_patterns(sid, df_bars) or []
    except Exception:  # noqa: BLE001
        last_hits = []
    return counts, last_hits


def _collect_warnings(sid: str) -> list[dict]:
    """active 警示(effective_to None 或 >= today)。失敗 → []。"""
    try:
        from src import database as db
    except Exception:  # noqa: BLE001
        return []
    try:
        hist = db.get_warning_history_for_sid(sid, days=180) or []
    except Exception:  # noqa: BLE001
        return []
    return [w for w in hist if w.get("is_active")]


def _collect_regime() -> dict:
    """大盤 regime。失敗 → {'regime': 'unknown', ...}。"""
    try:
        from src import market_regime as mr
        return mr.compute_regime()
    except Exception:  # noqa: BLE001
        return {
            "regime": "unknown", "label": "未知", "badge_emoji": "❔",
            "close": None, "ma20": None, "ma60": None, "target_date": None,
        }


def _collect_ml_prob(sid: str) -> float | None:
    """ML calibrated 機率。模型不存在或預測失敗 → None。"""
    try:
        from src import ml_predictor as mp
        from src import database as db
        from src import config as _cfg
    except Exception:  # noqa: BLE001
        return None
    try:
        model_path = Path(_cfg.PROJECT_ROOT) / "models" / "short_pick.pkl"
        if not model_path.exists():
            return None
        model = mp.load_model(model_path)
        if model is None:
            return None
        target_date = db.get_latest_trading_date()
        if not target_date:
            return None
        # 嘗試帶 calibrator(沒 calibrator 就走 raw)
        try:
            calibrator = mp.load_short_pick_calibrator()
        except Exception:  # noqa: BLE001
            calibrator = None
        return mp.predict_short_pick_winrate(
            model, sid, target_date, calibrator=calibrator,
        )
    except Exception:  # noqa: BLE001
        return None


def _collect_consensus(sid: str) -> dict:
    """從 daily_picks 撈該 sid 最近一個 trade_date 命中的策略數 + 跨類別數。

    回 {'strategy_count': N, 'category_count': M, 'strategies': [...]}。
    沒命中或失敗 → {'strategy_count': 0, 'category_count': 0, 'strategies': []}。
    """
    try:
        from src import database as db
        from src import consensus as cs
    except Exception:  # noqa: BLE001
        return {"strategy_count": 0, "category_count": 0, "strategies": []}
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) AS d FROM daily_picks WHERE sid=?",
                (str(sid).strip(),),
            ).fetchone()
            if not row or not row["d"]:
                return {"strategy_count": 0, "category_count": 0, "strategies": []}
            latest = row["d"]
            strat_rows = conn.execute(
                "SELECT DISTINCT strategy FROM daily_picks "
                "WHERE sid=? AND trade_date=?",
                (str(sid).strip(), latest),
            ).fetchall()
    except sqlite3.OperationalError:
        return {"strategy_count": 0, "category_count": 0, "strategies": []}
    strategies = sorted({r["strategy"] for r in strat_rows if r["strategy"]})
    if not strategies:
        return {"strategy_count": 0, "category_count": 0, "strategies": []}
    cats = {cs.STRATEGY_CATEGORIES.get(s, "未分類") for s in strategies}
    return {
        "strategy_count": len(strategies),
        "category_count": len(cats),
        "strategies": strategies,
        "trade_date": latest,
    }


def _collect_theme_multiplier(sid: str) -> float | None:
    """題材熱度 multiplier(熱 > 1.0,中性 1.0,冷 None)。失敗 → 1.0(中性)。"""
    try:
        from src import database as db
        from src import theme_heat as th
    except Exception:  # noqa: BLE001
        return 1.0
    try:
        with db.get_conn() as conn:
            return th.get_pick_theme_multiplier(conn, str(sid).strip())
    except Exception:  # noqa: BLE001
        return 1.0


def _collect_active_paper_trade(sid: str) -> dict | None:
    """該 sid 是否有 active paper_trade。回 dict 或 None。"""
    try:
        from src import database as db
    except Exception:  # noqa: BLE001
        return None
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT entry_date, entry_price, target_price, "
                "       stop_price, current_stop, hold_days "
                "FROM paper_trades WHERE sid=? AND status='active' "
                "ORDER BY entry_date DESC LIMIT 1",
                (str(sid).strip(),),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return {
        "entry_date": row["entry_date"],
        "entry_price": row["entry_price"],
        "target_price": row["target_price"],
        "stop_price": row["current_stop"] or row["stop_price"],
        "hold_days": row["hold_days"],
    }


def _collect_levels(sid: str) -> dict | None:
    """關鍵價位(壓力 / 回檔 / 支撐)+ ATR。失敗 / 資料不足 → None。

    直接 reuse src.indicators 算 BB(20, 2) + ATR(14)。不能 reuse
    individual_sections._compute_key_levels(那是 streamlit cache 包過的,
    在 test / 非 streamlit context 下會炸)。
    """
    try:
        from src import database as db
        from src import indicators as ind
    except Exception:  # noqa: BLE001
        return None
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT date, open, high, low, close, volume "
                "FROM daily_prices WHERE stock_id=? "
                "ORDER BY date DESC LIMIT 90",
                (str(sid).strip(),),
            ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows or len(rows) < 20:
        return None
    df = pd.DataFrame([
        {"date": r["date"], "open": r["open"], "high": r["high"],
         "low": r["low"], "close": r["close"], "volume": r["volume"]}
        for r in rows
    ]).sort_values("date").reset_index(drop=True)
    try:
        bb = ind.bollinger(df, period=20, num_std=2.0)
        atr = ind.atr(df, period=14)
    except Exception:  # noqa: BLE001
        return None
    if bb.empty or atr.empty:
        return None
    bb_upper = bb["upper"].iloc[-1]
    bb_mid = bb["mid"].iloc[-1]
    bb_lower = bb["lower"].iloc[-1]
    atr14 = atr.iloc[-1]
    close_last = df["close"].iloc[-1]
    if any(pd.isna(v) for v in [bb_upper, bb_mid, bb_lower, atr14, close_last]):
        return None
    return {
        "close": float(close_last),
        "atr14": float(atr14),
        "bb_upper": float(bb_upper),
        "bb_mid": float(bb_mid),
        "bb_lower": float(bb_lower),
    }


# ---------------------------------------------------------------------------
# 核心:compute_verdict — 純資料,可單元測試
# ---------------------------------------------------------------------------

def compute_verdict(sid: str, db_path: str | Path | None = None) -> dict:
    """整合所有訊號給「軍師判讀」結論。

    回 dict(永遠回完整 keys,失敗欄位用安全 default):
        enabled: bool                  — STOCK_VERDICT_ENABLED 開關
        sid: str
        verdict: str                   — '可進場' / '觀望' / '不進場'
        verdict_color: str             — '🟢' / '🟡' / '🔴'
        score: int                     — 累積分數(正面 - 負面)
        reasons_pro: list[str]         — 可進場理由(白話)
        reasons_con: list[str]         — 不進場理由(白話)
        action_suggestion: str         — 給主公的具體建議(一句)
        entry_zone: (low, high) | None — 🟢 才給,其他 None
        stop_loss: float | None
        take_profit: float | None
        signals: dict                  — 原始訊號 dump 給 UI 二次顯示
            ml_prob, regime, warnings_active, pattern_counts,
            consensus, theme_multiplier, paper_trade, levels,
            last_patterns

    db_path 預設用 config.DATABASE_PATH(透過 src.database)。傳 explicit
    db_path 在 test 內方便。
    """
    sid = str(sid).strip()
    enabled = is_enabled()
    out: dict = {
        "enabled": enabled,
        "sid": sid,
        "verdict": "觀望",
        "verdict_color": "🟡",
        "score": 0,
        "reasons_pro": [],
        "reasons_con": [],
        "action_suggestion": "",
        "entry_zone": None,
        "stop_loss": None,
        "take_profit": None,
        "signals": {},
        # P2-8:EV-based 半 Kelly 倉位 — 從 ml_prob → score_to_ev → position
        # ev: EV fraction (None 表沒 ml_prob);
        # suggested_position_pct: fraction ∈ [0, 0.05] (0 表不該進 / 無 EV)
        "ev": None,
        "suggested_position_pct": 0.0,
    }
    if not enabled:
        out["action_suggestion"] = "軍師判讀已停用(STOCK_VERDICT_ENABLED=false)"
        return out

    # === 1. 收訊號(任一失敗 silent skip)===
    pattern_counts, last_patterns = _collect_patterns(sid)
    warnings_active = _collect_warnings(sid)
    regime = _collect_regime()
    ml_prob = _collect_ml_prob(sid)
    consensus = _collect_consensus(sid)
    theme_mult = _collect_theme_multiplier(sid)
    paper_trade = _collect_active_paper_trade(sid)
    levels = _collect_levels(sid)

    out["signals"] = {
        "ml_prob": ml_prob,
        "regime": regime,
        "warnings_active": warnings_active,
        "pattern_counts": pattern_counts,
        "last_patterns": last_patterns,
        "consensus": consensus,
        "theme_multiplier": theme_mult,
        "paper_trade": paper_trade,
        "levels": levels,
    }

    score = 0
    reasons_pro: list[str] = []
    reasons_con: list[str] = []
    has_red_flag = False  # 嚴重警示 → 強制 🔴

    # === 2. 警示(紅燈)===
    severe_types = {"default_settlement", "full_cash"}
    soft_types = {"attention", "disposition", "method_changed"}
    severe_hits = [w for w in warnings_active if w.get("warning_type") in severe_types]
    soft_hits = [w for w in warnings_active if w.get("warning_type") in soft_types]
    if severe_hits:
        has_red_flag = True
        type_label = {
            "default_settlement": "違約交割",
            "full_cash": "全額交割",
        }
        labels = sorted({type_label.get(w["warning_type"], w["warning_type"]) for w in severe_hits})
        reasons_con.append(f"⚠️ {' / '.join(labels)}警示生效中(主公千萬別碰)")
        score -= 5
    if soft_hits:
        type_label = {
            "attention": "注意股",
            "disposition": "處置股",
            "method_changed": "變更交易方法",
        }
        labels = sorted({type_label.get(w["warning_type"], w["warning_type"]) for w in soft_hits})
        reasons_con.append(f"⚠️ {' / '.join(labels)}(風險偏高)")
        score -= 2

    # === 3. 大盤 regime ===
    regime_key = regime.get("regime", "unknown")
    regime_label = regime.get("label", "未知")
    regime_emoji = regime.get("badge_emoji", "❔")
    rs = _REGIME_SCORE.get(regime_key, 0)
    score += rs
    if rs > 0:
        reasons_pro.append(f"{regime_emoji} 大盤{regime_label}(順風)")
    elif rs < 0:
        reasons_con.append(f"{regime_emoji} 大盤{regime_label}(逆風,全市場降溫)")

    # === 4. ML calibrated 機率 + EV(score_to_ev 翻譯) ===
    if ml_prob is not None:
        prob_pct = ml_prob * 100
        ms = _ml_score(ml_prob)
        score += ms
        # EV 校準後同步顯示 — 讓主公看到「進場期望賺/賠 X%」
        ev_suffix = ""
        try:
            from src.score_to_ev import score_to_ev, render_ev_str
            ev_val = score_to_ev(ml_prob)
            if ev_val is not None:
                ev_suffix = f" · {render_ev_str(ev_val)}"
                out["ev"] = float(ev_val)
                # P2-8:EV → 建議倉位(半 Kelly 分段)
                try:
                    from src.position_sizing import compute_suggested_position
                    out["suggested_position_pct"] = float(
                        compute_suggested_position(ev_val)
                    )
                except Exception:  # noqa: BLE001
                    out["suggested_position_pct"] = 0.0
        except Exception:  # noqa: BLE001
            pass
        if ml_prob >= 0.65:
            reasons_pro.append(
                f"🎯 AI 勝率 {prob_pct:.0f}%(偏高){ev_suffix}"
            )
        elif ml_prob < 0.50:
            reasons_con.append(
                f"🎯 AI 勝率 {prob_pct:.0f}%(偏低){ev_suffix}"
            )

    # === 5. K 線形態 ===
    bullish_total = sum(
        v for k, v in pattern_counts.items() if k in BULLISH_PATTERNS
    )
    if bullish_total >= 5:
        score += 2
        reasons_pro.append(
            f"📊 近 30 日多頭形態 ×{bullish_total}(K 線翻多明顯)"
        )
    elif bullish_total >= 3:
        score += 1
        reasons_pro.append(f"📊 近 30 日多頭形態 ×{bullish_total}")

    # 最近一根有命中 → 加重
    last_bullish = [h for h in last_patterns if h.get("name") in BULLISH_PATTERNS]
    if last_bullish:
        labels_str = " / ".join(
            PATTERN_LABELS.get(h.get("name", ""), h.get("name", ""))
            for h in last_bullish
        )
        score += 1
        reasons_pro.append(f"📊 最近一根:{labels_str}(進場時機)")

    # === 6. 策略共識 ===
    cc = consensus.get("strategy_count", 0)
    cat_c = consensus.get("category_count", 0)
    if cc >= 2 and cat_c >= 2:
        score += 2
        reasons_pro.append(f"⭐ 跨策略共識(命中 {cc} 策略 / {cat_c} 類別)")
    elif cc >= 2:
        score += 1
        reasons_pro.append(f"⭐ 多策略命中(共 {cc} 套)")

    # === 7. 題材熱度 ===
    if theme_mult is None:
        score -= 1
        reasons_con.append("🚫 屬冷題材(近期表現弱)")
    elif theme_mult > 1.0:
        score += 1
        reasons_pro.append(f"🔥 熱題材加分(×{theme_mult:.2f})")

    # === 8. 持倉狀態 ===
    if paper_trade:
        # 已持倉 → 不給「進場建議」,改成「停利停損守則」
        reasons_pro.append(
            f"📌 已持倉(進場價 {paper_trade['entry_price']:.2f})— "
            f"看停損 / 停利,不重複進場"
        )

    # === 9. verdict 決策 ===
    if has_red_flag:
        verdict = "不進場"
        color = "🔴"
    elif paper_trade is not None:
        # 已持倉 → 一律「觀望」(不重複進場)
        verdict = "觀望"
        color = "🟡"
    elif regime_key == "bear":
        # 大盤空頭 → 沒紅燈也要至少觀望
        if score >= 4:
            verdict = "觀望"
            color = "🟡"
        else:
            verdict = "不進場"
            color = "🔴"
    elif score >= 4 and len(reasons_pro) >= 3 and not reasons_con:
        verdict = "可進場"
        color = "🟢"
    elif score >= 3 and len(reasons_pro) >= 3:
        verdict = "可進場"
        color = "🟢"
    elif score <= -3 or len(reasons_con) >= 3:
        verdict = "不進場"
        color = "🔴"
    else:
        verdict = "觀望"
        color = "🟡"

    out["verdict"] = verdict
    out["verdict_color"] = color
    out["score"] = score
    out["reasons_pro"] = reasons_pro
    out["reasons_con"] = reasons_con

    # === 10. 進場價區間 / 停損 / 停利(只在 🟢 給)===
    if verdict == "可進場" and levels is not None:
        atr = levels["atr14"]
        bb_upper = levels["bb_upper"]
        bb_lower = levels["bb_lower"]
        half_atr = 0.5 * atr
        entry_low = bb_lower - half_atr
        entry_high = bb_lower + half_atr
        stop_loss = bb_lower - 1.5 * atr
        take_profit = bb_upper + half_atr
        # mobile 顯示用 2 位小數 round 一下(內部仍存 float)
        out["entry_zone"] = (float(entry_low), float(entry_high))
        out["stop_loss"] = float(stop_loss)
        out["take_profit"] = float(take_profit)

    # === 11. action_suggestion(一句白話) ===
    if has_red_flag:
        sug = "暫不進場,等警示解除(通常 3-5 個交易日)再評估"
    elif paper_trade is not None:
        sug = (
            f"已持倉 — 守停損 {paper_trade.get('stop_price', '—')},"
            f"看停利 {paper_trade.get('target_price', '—')},不要追加"
        )
    elif regime_key == "bear":
        sug = "大盤空頭,先別搶反彈;等大盤回到 MA20 之上再評估"
    elif verdict == "可進場":
        if out["entry_zone"] is not None:
            elow, ehigh = out["entry_zone"]
            sug = (
                f"分批進場 {elow:.2f}-{ehigh:.2f},停損 {out['stop_loss']:.2f},"
                f"停利 {out['take_profit']:.2f}"
            )
        else:
            sug = "訊號正面,等技術面進場區間出來再切入"
    else:
        sug = "正負面互見,先觀望;等紅燈解除或多頭訊號加重再說"
    out["action_suggestion"] = sug

    return out


# ---------------------------------------------------------------------------
# 卡片用「短標」 — 給短線 / 長線 / 關注頁的卡片標題後面掛
# ---------------------------------------------------------------------------

def verdict_tag_for_card(sid: str, db_path: str | Path | None = None) -> str:
    """回「🟢 可進場」/ 「🟡 觀望」/ 「🔴 不進場」短字串。

    給卡片標題、推播 pick block reuse。任何例外 → 回 ''(caller 自己 graceful)。
    """
    try:
        v = compute_verdict(sid, db_path=db_path)
    except Exception:  # noqa: BLE001
        return ""
    if not v.get("enabled"):
        return ""
    return f"{v['verdict_color']} {v['verdict']}"


# ---------------------------------------------------------------------------
# Streamlit render — thin wrapper,不可在非 streamlit context import
# ---------------------------------------------------------------------------

def render_stock_verdict(sid: str) -> dict:
    """個股深度頁頂端「🎯 軍師判讀」區塊。回 verdict dict(讓 caller reuse)。"""
    import streamlit as st  # lazy import — 非 streamlit context 時不該觸發

    verdict = compute_verdict(sid)
    if not verdict.get("enabled"):
        st.caption("💤 軍師判讀已停用(STOCK_VERDICT_ENABLED=false)")
        return verdict

    color = verdict["verdict_color"]
    v_text = verdict["verdict"]
    # mobile-first:大字 + 顏色塊
    color_map = {"🟢": "#2ca02c", "🟡": "#BA7517", "🔴": "#d62728"}
    bg = color_map.get(color, "#888")

    st.markdown(
        f"<div style='border:2px solid {bg};border-radius:8px;"
        f"padding:14px 16px;margin:8px 0 12px 0;"
        f"background:rgba(128,128,128,0.04)'>"
        f"<div style='font-size:14px;color:#888;margin-bottom:4px'>🎯 軍師判讀</div>"
        f"<div style='font-size:28px;font-weight:700;color:{bg}'>"
        f"{color} {v_text}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # 理由(進場 + 不進場 各列一段)
    if verdict["reasons_pro"]:
        st.markdown("**進場理由**")
        for r in verdict["reasons_pro"]:
            st.markdown(f"- ✓ {r}")
    if verdict["reasons_con"]:
        st.markdown("**不進場理由**")
        for r in verdict["reasons_con"]:
            st.markdown(f"- ✗ {r}")
    if not verdict["reasons_pro"] and not verdict["reasons_con"]:
        st.caption("⚠️ 訊號不足(歷史資料 / 模型 / 警示都缺),無法給結論")

    # 具體建議
    if verdict["action_suggestion"]:
        st.info(f"💡 **軍師建議**:{verdict['action_suggestion']}")

    # 🟢 verdict 才顯數字
    if verdict["verdict"] == "可進場" and verdict["entry_zone"] is not None:
        elow, ehigh = verdict["entry_zone"]
        stop = verdict["stop_loss"]
        take = verdict["take_profit"]
        cols = st.columns(3)
        cols[0].markdown(
            f"**🎯 進場價**\n\n<span style='color:#2ca02c;"
            f"font-size:18px;font-weight:600'>{elow:.2f} ~ {ehigh:.2f}</span>",
            unsafe_allow_html=True,
        )
        cols[1].markdown(
            f"**🛡️ 停損**\n\n<span style='color:#d62728;"
            f"font-size:18px;font-weight:600'>{stop:.2f}</span>",
            unsafe_allow_html=True,
        )
        cols[2].markdown(
            f"**🚀 停利**\n\n<span style='color:#1f77b4;"
            f"font-size:18px;font-weight:600'>{take:.2f}</span>",
            unsafe_allow_html=True,
        )

    # P2-8:EV-based 半 Kelly 建議倉位 — 有正 EV 才顯,讓主公一眼看「該投多少」。
    # 跟 entry/stop/profit 同層級顯示(主公拍板:EV→倉位 是進場決策的一部分)。
    pos_pct = float(verdict.get("suggested_position_pct") or 0.0)
    ev_val = verdict.get("ev")
    if pos_pct > 0:
        from src.position_sizing import render_position_str as _rps
        ev_str = ""
        if ev_val is not None:
            try:
                ev_str = f"(EV {'+' if float(ev_val) >= 0 else ''}{float(ev_val) * 100:.1f}%)"
            except (TypeError, ValueError):
                ev_str = ""
        st.markdown(
            f"<div style='font-size:14px;color:#1f77b4;margin:6px 0 8px 0'>"
            f"💼 <strong>{_rps(pos_pct)}</strong> {ev_str}"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.caption(
        "判讀整合:K 線形態 + 警示 + 大盤 regime + ML 機率 + 策略共識 + 題材熱度。"
        "**參考用,非投資建議**。"
    )
    return verdict


__all__ = [
    "is_enabled",
    "compute_verdict",
    "render_stock_verdict",
    "verdict_tag_for_card",
    "latest_pattern_phrase",
    "PATTERN_LABELS",
    "PATTERN_MEANINGS",
    "BULLISH_PATTERNS",
    "NEUTRAL_PATTERNS",
]
