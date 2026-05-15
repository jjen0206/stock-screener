"""
Telegram Bot 推播模組。

提供:
- send_telegram_message(text, bot_token, chat_id) -> bool
    底層發送函式;缺 token 印 warning 回 False,網路 / API 錯誤回 False
- format_short_picks(picks, date) -> str
    把 screen_short 結果包成 Markdown 訊息(每檔一段)
- notify_short_picks(date, params) -> bool
    整合短線選股 + 推播,給排程腳本用

排程方式:
- Streamlit Cloud 不支援自定 cron,要用主機 crontab 或 GitHub Actions
  (yaml 範例見 README「Telegram 推播」章節)
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
import time
from datetime import date as _date
from typing import Optional
from urllib.parse import quote_plus

import pandas as pd
import requests

from src import config, database as db
from src.screener_short import screen_short
from src.universe import TW_TOP_50


# 推播失敗 retry 設定:總共最多 3 次嘗試,失敗等 1s 再重試
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECS = 1.0

# 動態策略權重 — 開 True 讓 _compute_pick_score 套 30d hit rate weight,
# 改 False 退回 phase 1 純 ml_prob 排序(出事時 kill-switch)。
STRATEGY_DYNAMIC_WEIGHT_ENABLED = True

# 警示股標註 + 軟降權(2026-05-15 主公拍板;同日 amendment:不替主公隱藏決定,
# 拿掉 hard exclude,改成全 soft + UI badge):
# - SEVERE (default_settlement / full_cash) → ml_prob × 0.3 嚴重的自動沉到末段但仍顯
# - SOFT   (attention / disposition / method_changed) → ml_prob × 0.7
# 模組級 cache:_select_top_picks 跑完後把帶 warning 的 picks 存這(包含被警示但
# 仍出現在推薦中的 picks),format_top_picks_message 撈出組 caption「⚠️ 推薦中含 N 檔
# 警示股」— 文案是提示而非「已濾掉」(主公規矩:不替他做隱藏決定)。
_LAST_ANNOTATED_WARNINGS: list[dict] = []

# 大盤 regime gating — 開 True 讓 _select_top_picks 根據 TAIEX regime 動態縮
# 推薦量 + 拉 ML threshold uplift。env REGIME_GATING_ENABLED=false 時 src.
# regime_gating.is_enabled() 回 False,gating 退回 bull params(等同關掉)。
REGIME_GATING_ENABLED = True


logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_message(
    text: str,
    bot_token: str | None = None,
    chat_id: str | None = None,
    parse_mode: str = "Markdown",
) -> bool:
    """發送 Telegram 訊息。

    參數優先順序:傳入參數 > config(由 .env / Streamlit Secrets 載入)。
    缺 token 或 chat_id 印 warning 回 False;網路/API 錯誤回 False。

    parse_mode:Telegram 解析模式 — "Markdown"(legacy)/ "MarkdownV2" /
    "HTML" / "" 純文字。news_notify 走 HTML 避開 Markdown entity 解析坑。
    """
    token = bot_token or config.TELEGRAM_BOT_TOKEN
    cid = chat_id or config.TELEGRAM_CHAT_ID

    if not token or not cid:
        logger.warning(
            "[NOTIFIER] 缺 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID,跳過推播"
        )
        return False

    url = TELEGRAM_API_URL.format(token=token)
    payload: dict = {"chat_id": cid, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    # 失敗 retry:網路 exception 或 5xx 才重試,4xx 是 client error 不重試
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.post(url, json=payload, timeout=15)
        except requests.RequestException as ex:
            logger.warning(
                "[NOTIFIER] 網路錯誤 (attempt %d/%d): %s",
                attempt + 1, _MAX_ATTEMPTS, ex,
            )
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAY_SECS)
                continue
            logger.error("[NOTIFIER] 網路錯誤,放棄重試: %s", ex)
            return False

        if r.status_code == 200:
            return True
        if r.status_code < 500:
            # 4xx (401/400 等)→ client error, 重試也沒用
            logger.error(
                "[NOTIFIER] Telegram API client error %d: %s",
                r.status_code, r.text[:200],
            )
            return False
        # 5xx → 重試
        logger.warning(
            "[NOTIFIER] Telegram API %d (attempt %d/%d),retry...",
            r.status_code, attempt + 1, _MAX_ATTEMPTS,
        )
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_RETRY_DELAY_SECS)
    return False


def _weekend_hint(date_iso: str) -> str:
    """如果 date_iso 不是 today(週末 / 假日跑 → 用最後交易日),回提示字串
    給訊息 header append。否則回空字串。
    """
    try:
        if _date.fromisoformat(date_iso) != _date.today():
            return "\n📅 (今日為週末/假日,顯示最後交易日結果)"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _empty_pick_suffix() -> str:
    """0 入選時的補充說明:多數情況不是 bug 而是 cache 歷史不足。

    顯示 cache 健康度;若多數個股 < 60 天,加註「歷史累積中」避免誤判。
    """
    try:
        health = db.cache_health_summary()
    except Exception:  # noqa: BLE001
        return ""
    b = health["buckets"]
    eligible = b["60+"] + b["20-59"]
    suffix = (
        f"\n\n📦 Cache: 60+天 {b['60+']}・20-59天 {b['20-59']}"
        f"・<20天 {b['<14'] + b['14-19']}"
    )
    if eligible < 100:
        suffix += "\n⏳ 多數個股歷史累積中(短線策略需 14-60 天),請等待 1-2 週"
    return suffix


def compute_entry_range(
    sid: str,
    close: float | None,
    conn: sqlite3.Connection,
) -> Optional[tuple[float, float]]:
    """計算建議進場區間(U3 進場價區間建議,Phase 1 簡單版)。

    公式:
        下緣 = min(close - 0.5 × ATR_14, BB_lower_20)   # ATR-based 防低估
        上緣 = close                                     # 當前價即進場價

    取兩者較低值作為下緣 → 提供買方較大彈性(BB_lower 在跌勢中更深 / ATR 在
    盤整時更代表合理波動)。

    參數:
        sid:股票代號(讀 daily_prices)
        close:當前收盤價(可由 caller 傳入,容許未來 intraday 用)
        conn:SQLite 連線(由 caller 控制生命週期,避免反覆開關)

    回傳:
        (low, high) 兩位小數;資料 < 20 天 / ATR/BB 算不出 / close 無效 → None
        Caller 看到 None 應 graceful skip 不顯這行。
    """
    if close is None:
        return None
    try:
        close_f = float(close)
    except (TypeError, ValueError):
        return None
    if not (close_f > 0) or math.isnan(close_f):
        return None

    # 撈最近 30 天 OHLC(BB 需要 20、ATR 需要 15,30 留 buffer)
    rows = conn.execute(
        "SELECT date, high, low, close FROM daily_prices "
        "WHERE stock_id = ? ORDER BY date DESC LIMIT 30",
        (sid,),
    ).fetchall()
    if len(rows) < 20:
        return None

    # 反序成 ascending,indicators 需時序由舊到新
    df = pd.DataFrame(
        [
            {"high": r["high"], "low": r["low"], "close": r["close"]}
            for r in reversed(rows)
        ]
    )
    if df[["high", "low", "close"]].isna().any().any():
        return None

    from src.indicators import atr, bollinger
    try:
        atr_series = atr(df, period=14)
        bb_df = bollinger(df, period=20, num_std=2.0)
    except (KeyError, ValueError):
        return None

    last_atr = atr_series.iloc[-1]
    last_bb_lower = bb_df["lower"].iloc[-1]
    if math.isnan(last_atr) or math.isnan(last_bb_lower):
        return None

    atr_floor = close_f - 0.5 * float(last_atr)
    low = min(atr_floor, float(last_bb_lower))
    high = close_f
    if low <= 0 or low >= high:
        return None
    return (round(low, 2), round(high, 2))


_SEPARATOR = "━" * 16


# Telegram legacy Markdown 會把以下字元當特殊語法:* (bold) / _ (italic) /
# [ (link 開頭) / ` (inline code)。TWSE 公司名常帶 *(如「國巨*」庫藏股標記),
# 主旨內也常含 ()[] 等字元 — 不 escape 直接送 Telegram 會回 400「can't parse
# entities」整批訊息死光(2026-05-09 主公手機 24 小時 silence root cause)。
#
# 2026-05-15 補:SHAP 行 feature 名(`atr_normalized` / `macd_dif` 等)含 _,
# 不 escape 會被當斜體 marker 配對失敗 → 同樣 400「can't parse entities」。
# 任何嵌入訊息的「動態字串」(股票名 / 備註 / 公司名 / SHAP feature / 策略
# label / industry)都必須過 _md_escape — 純靜態 emoji + 中文 label 才能 raw。
_TG_MD_ESCAPE_RE = re.compile(r"([\*_\[`])")


def _md_escape(text: str, channel: str) -> str:
    """Escape Telegram legacy Markdown 特殊字元 (* _ [ `)。Discord 走 ** 雙星
    標記對裸的 * _ [ ` 不敏感,no-op 直接回原字串。

    給 format_pick_block / format_*_block 等動態字串(SHAP feature 名 / 公司
    名 / industry / label)用,避免 Telegram API 回 400「can't parse entities」。
    """
    if channel != "telegram":
        return text
    return _TG_MD_ESCAPE_RE.sub(r"\\\1", str(text))


def _bold(text: str, channel: str) -> str:
    """**bold**(Discord)/ *bold*(Telegram Markdown legacy)。Telegram 端會先
    escape 內文特殊字元避免 parse 錯誤。"""
    if channel == "discord":
        return f"**{text}**"
    return f"*{_md_escape(text, channel)}*"


def _compute_pick_score(
    sid: str,
    ml_prob: float | None,
    matched_strategies: list[str] | None,
    analyst_target_mean: float | None = None,
    strategy_weights: dict[str, float] | None = None,
) -> tuple[float, float, int, str]:
    """Pick 排序 key — 共用於 _select_top_picks 與 format_yesterday_recap。

    Tuple 字典序 ascending → 排前面的 picks 數值越小:
      - analyst_target_mean 有值 → -100(法人共識 picks 整體加分)
      - ml_prob × avg(strategy_weights) desc(命中率高的策略上排)
      - 命中策略多 desc
      - sid asc(穩定 tiebreaker)

    strategy_weights: dict[strategy_key, weight] — 由
    src.strategy_weighting.get_strategy_weights_30d 產;missing key 預設 1.0。
    None 或空 dict → 退回 phase 1 純 ml_prob 排序(legacy 行為)。

    讓 recap 算出來的 top picks 順序跟昨天實際推播的順序一致(M4/U1 known issue)。
    """
    matched = matched_strategies or []
    if strategy_weights and matched:
        avg_w = (
            sum(strategy_weights.get(s, 1.0) for s in matched)
            / len(matched)
        )
    else:
        avg_w = 1.0
    weighted_ml = (ml_prob or 0.0) * avg_w
    return (
        -(100 if analyst_target_mean else 0),
        -weighted_ml,
        -len(matched),
        sid,
    )


def compute_top_picks(
    date: str,
    top_n: int = 5,
    confluence_n: int = 2,
    params: dict | None = None,
    universe: list[str] | None = None,
) -> list[dict]:
    """公開 wrapper:給排程腳本(daily_notify)在推播後抓同樣 picks 餵 auto_seed。

    內部委派 `_select_top_picks`(notify_top_picks 用的同一隻);保留底線 helper
    當 module-internal 實作細節,公開介面從這裡走。
    """
    return _select_top_picks(
        date, top_n=top_n, confluence_n=confluence_n,
        params=params, universe=universe,
    )


def _select_top_picks(
    date: str,
    top_n: int = 5,
    confluence_n: int = 2,
    params: dict | None = None,
    universe: list[str] | None = None,
) -> list[dict]:
    """跑全 universe 策略 → confluence ≥ N + per-strategy ML 雙層過濾 → top by ml_prob。

    回 list[dict],每筆含格式化所需欄位(sid / name / close / pct_change /
    matched_strategies / matched_labels / ml_prob / target_low/high / stop /
    ev / risk_reward)。沒過濾出來 → 空 list。
    """
    from collections import defaultdict
    from src import config
    from src.ml_predictor import (
        load_model, load_short_pick_calibrator, load_strategy_calibrator,
        load_strategy_model, predict_for_strategy,
    )
    from src.strategies import (
        STRATEGY_LABELS, STRATEGY_ML_THRESHOLDS, run_all_strategies,
    )
    from src.universe import pure_stock_universe

    if universe is None:
        universe = pure_stock_universe(min_history=20)
    if not universe:
        return []
    agg = run_all_strategies(date, params=params, stock_ids=universe)
    if not agg:
        return []

    # Per-pick ML routing(取最嚴格 threshold strategy 對應的 model)
    def _routing(matched: list[str]) -> str | None:
        cands = [
            (s, STRATEGY_ML_THRESHOLDS[s]) for s in matched
            if STRATEGY_ML_THRESHOLDS.get(s) is not None
        ]
        if not cands:
            return None
        return max(cands, key=lambda kv: kv[1])[0]

    def _strict_thr(matched: list[str]) -> float | None:
        ths = [
            STRATEGY_ML_THRESHOLDS[s] for s in matched
            if STRATEGY_ML_THRESHOLDS.get(s) is not None
        ]
        return max(ths) if ths else None

    general_path = config.PROJECT_ROOT / "models" / "short_pick.pkl"
    general_model = load_model(general_path) if general_path.exists() else None
    general_calibrator = load_short_pick_calibrator()
    sid_to_chosen: dict[str, str | None] = {}
    for sid, info in agg.items():
        sid_to_chosen[sid] = _routing(
            list((info.get("details") or {}).keys())
        )
    groups: dict[str | None, list[str]] = defaultdict(list)
    for sid, chosen in sid_to_chosen.items():
        groups[chosen].append(sid)
    strategy_models: dict[str, object] = {}
    strategy_calibrators: dict[str, object] = {}
    ml_probs: dict[str, float | None] = {}
    for chosen, sids in groups.items():
        sm = None
        sc = None
        if chosen:
            if chosen not in strategy_models:
                strategy_models[chosen] = load_strategy_model(chosen)
                strategy_calibrators[chosen] = load_strategy_calibrator(chosen)
            sm = strategy_models[chosen]
            sc = strategy_calibrators[chosen]
        try:
            probs = predict_for_strategy(
                strategy_name=chosen, stock_ids=sids, target_date=date,
                fallback_model=general_model, strategy_model=sm,
                strategy_calibrator=sc,
                fallback_calibrator=general_calibrator,
            )
            ml_probs.update(probs)
        except Exception:  # noqa: BLE001
            ml_probs.update({s: None for s in sids})

    # 撈每檔 prev_close 算漲跌(批量 SQL)
    prev_close_map: dict[str, float] = {}
    if agg:
        with db.get_conn() as conn:
            for sid in agg.keys():
                row = conn.execute(
                    "SELECT close FROM daily_prices "
                    "WHERE stock_id=? AND date < ? "
                    "ORDER BY date DESC LIMIT 1",
                    (sid, date),
                ).fetchone()
                if row and row["close"]:
                    prev_close_map[sid] = float(row["close"])

    # 撈每檔 industry + 算 industry_heat(同此批 picks 內同產業 fire 數)
    # 走相同 logic 跟 strategies.enrich_with_industry_heat 對齊。
    qualified_sids = [
        sid for sid, info in agg.items()
        if len(list((info.get("details") or {}).keys())) >= confluence_n
    ]
    industries_map: dict[str, str] = {}
    if qualified_sids:
        placeholders = ",".join("?" * len(qualified_sids))
        with db.get_conn() as conn:
            rows = conn.execute(
                f"SELECT stock_id, industry FROM stocks "
                f"WHERE stock_id IN ({placeholders}) "
                f"AND industry IS NOT NULL AND industry != ''",
                qualified_sids,
            ).fetchall()
        industries_map = {r["stock_id"]: r["industry"] for r in rows}
    industry_counts: dict[str, int] = {}
    for sid in qualified_sids:
        ind = industries_map.get(sid)
        if ind:
            industry_counts[ind] = industry_counts.get(ind, 0) + 1

    # 撈 strategy → win_rate(126 日歷史回測,週一 nightly 跑)— 給每張 pick
    # 算「命中策略平均勝率」加進推播。低樣本(<10 fires)由 load 函式過濾。
    strategy_wr_map = db.load_latest_strategy_backtest()

    # 動態策略權重 — 從 pick_outcomes 近 30 天算 hit rate,套到 _compute_pick_score
    # 的 ml_prob 排序。STRATEGY_DYNAMIC_WEIGHT_ENABLED=False 時退回純 ml_prob 排序。
    strategy_weights: dict[str, float] = {}
    if STRATEGY_DYNAMIC_WEIGHT_ENABLED:
        try:
            from src.strategy_weighting import get_strategy_weights_30d
            with db.get_conn() as conn:
                strategy_weights = get_strategy_weights_30d(conn)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[NOTIFIER] 撈動態策略權重失敗,退回純 ml_prob: %s", ex)
            strategy_weights = {}

    # 撈 analyst_targets(法人目標價共識)— 平日 watchlist+picks / 週日全市場
    # fetch 進 SQLite。bulk lookup 一次,enrich 進 pick dict 後排序加分。
    from src.analyst_targets import get_analyst_targets_for_sids
    analyst_target_map = get_analyst_targets_for_sids(qualified_sids)

    # 撈 shareholder_concentration(TDCC 千張大戶週快照)— 週六凌晨 weekly-
    # shareholder-fetch.yml 抓進 SQLite。沒資料(該檔當週沒公布 / 還沒抓過)→
    # enrich 後 holders_1000up_count 為 None → format_pick_block graceful skip
    sc_map = db.get_shareholder_concentration_for_sids(qualified_sids)

    def _build_candidate(
        sid: str,
        info: dict,
        min_confluence: int,
        apply_ml_thr: bool,
        confidence_tier: str,
    ) -> dict | None:
        """組單張 candidate(過 confluence + 可選 ML threshold)。回 None = 不過濾。

        confidence_tier: 'high'(高信心,經 ML threshold)/ 'weak'(弱訊號,
        fallback 模式關 threshold)— 寫進 pick dict 給 format_pick_block /
        format_top_picks_message 顯示 caption。
        """
        matched_local = list((info.get("details") or {}).keys())
        if len(matched_local) < min_confluence:
            return None
        thr_local = _strict_thr(matched_local)
        prob_local = ml_probs.get(sid)
        if apply_ml_thr and thr_local is not None and (
            prob_local is None or prob_local < thr_local
        ):
            return None
        return _enrich_pick(
            sid=sid, info=info, matched=matched_local, prob=prob_local,
            confidence_tier=confidence_tier,
        )

    def _enrich_pick(
        sid: str, info: dict, matched: list[str], prob: float | None,
        confidence_tier: str,
    ) -> dict | None:
        details = info.get("details") or {}
        close = target_low = target_high = stop = rr = None
        for d in details.values():
            if not isinstance(d, dict):
                continue
            if close is None and d.get("close"):
                close = float(d["close"])
            if target_low is None and d.get("target_low"):
                target_low = float(d.get("target_low"))
                target_high = float(d.get("target_high"))
                stop = float(d.get("stop_loss")) if d.get("stop_loss") else None
                rr = d.get("risk_reward")
        if close is None:
            return None
        prev_close = prev_close_map.get(sid)
        pct_change = (
            (close - prev_close) / prev_close * 100
            if prev_close and prev_close > 0 else None
        )
        # EV 估算:固定 5% target / 3% stop × ml_prob 期望
        ev = (
            prob * 0.05 - (1 - prob) * 0.03
            if prob is not None else None
        )
        industry = industries_map.get(sid)
        industry_heat = industry_counts.get(industry, 0) if industry else 0
        # win_rate:命中策略 backtest WR 算術平均(跟 _enrich_df_with_win_rate 同邏輯)
        valid_wrs = [strategy_wr_map[s] for s in matched if s in strategy_wr_map]
        win_rate = sum(valid_wrs) / len(valid_wrs) if valid_wrs else None
        # analyst_target:有就 enrich,排序時加分(讓有法人共識的 picks 排前面)
        at_row = analyst_target_map.get(sid) or {}
        analyst_target_mean = at_row.get("target_mean")
        analyst_num = at_row.get("num_analysts")
        analyst_source = at_row.get("source")
        analyst_target_prev = at_row.get("previous_target_mean")
        # 千張大戶(TDCC 週快照):無資料 → None,format_pick_block graceful skip
        sc_row = sc_map.get(sid) or {}
        holders_1000up_count = sc_row.get("holders_1000up_count")
        holders_delta_w = sc_row.get("holders_delta_w")
        holders_pct = sc_row.get("holders_pct")
        return {
            "rank": 0,  # caller fills
            "sid": sid,
            "name": info.get("name", ""),
            "close": close,
            "pct_change": pct_change,
            "matched_strategies": matched,
            "matched_labels": [STRATEGY_LABELS.get(s, s) for s in matched],
            "ml_prob": prob,
            "target_low": target_low,
            "target_high": target_high,
            "stop": stop,
            "ev": ev,
            "risk_reward": float(rr) if rr else None,
            "industry": industry,
            "industry_heat": industry_heat,
            "win_rate": win_rate,
            "analyst_target_mean": analyst_target_mean,
            "analyst_target_high": at_row.get("target_high"),
            "analyst_target_low": at_row.get("target_low"),
            "analyst_num": analyst_num,
            "analyst_source": analyst_source,
            "analyst_target_prev_mean": analyst_target_prev,
            "holders_1000up_count": holders_1000up_count,
            "holders_delta_w": holders_delta_w,
            "holders_pct": holders_pct,
            "confidence_tier": confidence_tier,
        }

    # 大盤 regime gating — 算當前 regime 的 max_count + threshold_uplift。
    # bear 時拉 ML threshold uplift,本來只該過 base threshold 的 picks 必須
    # 過 (base + uplift) 才入選;否則篩掉。max_count 在排序後截斷(下方)。
    # 失敗(env / DB 異常)→ 退回 bull params(等同關 gating)。
    regime_gating: dict = {}
    threshold_uplift = 0.0
    if REGIME_GATING_ENABLED:
        try:
            from src.regime_gating import get_regime_gating_params
            with db.get_conn() as conn:
                regime_gating = dict(get_regime_gating_params(conn))
            threshold_uplift = float(
                regime_gating.get("confidence_threshold_uplift") or 0.0
            )
        except Exception as ex:  # noqa: BLE001
            logger.warning("[NOTIFIER] regime gating 失敗,退回不縮:%s", ex)
            regime_gating = {}
            threshold_uplift = 0.0

    qualified: list[dict] = []
    for sid, info in agg.items():
        cand = _build_candidate(
            sid=sid, info=info, min_confluence=confluence_n,
            apply_ml_thr=True, confidence_tier="high",
        )
        if cand is None:
            continue
        # regime gating threshold uplift — 空頭時拉 (base + uplift)
        # 只在 ml_prob / threshold 都有值時做(避免 None 比較)
        if threshold_uplift > 0:
            prob_local = cand.get("ml_prob")
            thr_local = _strict_thr(cand.get("matched_strategies") or [])
            if (
                thr_local is not None and prob_local is not None
                and prob_local < thr_local + threshold_uplift
            ):
                continue
        qualified.append(cand)

    # Fallback(主公 2026-05-15 拍板):若 confluence + threshold 過濾後 = 0
    # 但 daily_picks 仍有命中 → 降級回 Top 3 弱訊號(只看 confluence ≥ 1,
    # 不過 ML threshold)。讓「全市場 0 高信心」的死寂日子至少有 Top 3 給主公看,
    # confidence_tier='weak' 標記讓 format_top_picks_message 加 caption 警語。
    fallback_used = False
    if not qualified and agg:
        fallback_top_n = min(top_n, 3)
        fallback_min_confluence = 1
        for sid, info in agg.items():
            cand = _build_candidate(
                sid=sid, info=info, min_confluence=fallback_min_confluence,
                apply_ml_thr=False, confidence_tier="weak",
            )
            if cand is not None:
                qualified.append(cand)
        fallback_used = True
        top_n = fallback_top_n  # downgrade output 上限到 3

    # 警示股 annotate + 軟降權(2026-05-15 amendment:不 hard exclude,主公自己看):
    #   - annotate:命中 active warning → 注 'warnings' 欄位給 UI badge / caption
    #   - SEVERE penalty:default_settlement / full_cash → ml_prob × 0.3 沉到末段
    #   - SOFT   penalty:attention / disposition / method_changed → ml_prob × 0.7
    # 警示股仍會出現在 picks(只是排序往後),WARNING_ANNOTATE_ENABLED=false → no-op。
    global _LAST_ANNOTATED_WARNINGS
    _LAST_ANNOTATED_WARNINGS = []
    if qualified:
        try:
            from src.warnings_filter import (
                annotate_warned_stocks, apply_soft_warning_penalty,
            )
            with db.get_conn() as _wconn:
                annotated_sids = annotate_warned_stocks(
                    _wconn, qualified, as_of=date,
                )
                # soft penalty(分嚴重等級)— in-place 改 ml_prob,讓下面 sort 自動排後
                apply_soft_warning_penalty(_wconn, qualified, as_of=date)
            # 收集被 annotate 的 picks 給 caption(注意:這是 reference,
            # 後續 sort 後仍指向同 dict,'warnings' 欄位不會丟)
            _LAST_ANNOTATED_WARNINGS = [
                p for p in qualified if p.get("warnings")
            ]
            if annotated_sids:
                logger.info(
                    "[NOTIFIER] 警示股標註 %d 檔(仍在 picks,排序自動往後): %s",
                    len(annotated_sids),
                    [(p.get("sid"), p.get("warning_types"))
                     for p in _LAST_ANNOTATED_WARNINGS],
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[NOTIFIER] 警示股標註失敗,picks 不過警示標註(救火 fallback)"
            )

    # 排序:有 analyst_target 的優先(+100 分讓共識票排前面)
    # → ml_prob desc → 命中策略多 desc → sid asc
    # scoring 抽到 _compute_pick_score 共用,讓 format_yesterday_recap 算 top
    # picks 的順序跟實際推播一致(M4/U1 known issue)。
    qualified.sort(key=lambda p: _compute_pick_score(
        sid=p["sid"],
        ml_prob=p["ml_prob"],
        matched_strategies=p["matched_strategies"],
        analyst_target_mean=p.get("analyst_target_mean"),
        strategy_weights=strategy_weights,
    ))
    # 大盤 regime gating truncation — bear 2 / range 5 / bull 10。
    # fallback path(降級弱訊號 Top 3)時 top_n 已被改成 3,gating 仍會生效
    # (gating cap 通常比 fallback 3 寬,但 bear 時 cap=2 會把弱訊號也壓到 2)。
    effective_top_n = top_n
    if regime_gating:
        cap = int(regime_gating.get("short_pick_max_count") or top_n)
        if cap < effective_top_n:
            effective_top_n = cap
    out = qualified[:effective_top_n]
    for i, p in enumerate(out, start=1):
        p["rank"] = i
    if fallback_used:
        # 標 module 級 flag 給 format_top_picks_message 讀(避免 picks 為空時
        # caller 無法分辨「真的空」vs「fallback 也空」)。實際上 caller 只看
        # picks[0].get('confidence_tier') == 'weak' 也能判斷,所以 flag 為輔助。
        for p in out:
            p["confidence_tier"] = "weak"
    # regime_gating metadata 寫進每筆 pick — format_top_picks_message 從
    # picks[0]["regime_gating"] 取 caption 注入訊息開頭。picks 為空時走另一條
    # 路(下方 caller 用 fallback fetch)。
    if regime_gating and out:
        for p in out:
            p["regime_gating"] = regime_gating

    # U3 進場區間建議:對 top N picks 算 (entry_low, entry_high)
    # 只算 out 內的 picks(top N,通常 ≤ 10),省 DB query。
    # 算不出(< 20 天歷史)→ 不寫欄位,format_pick_block graceful skip
    if out:
        with db.get_conn() as conn:
            for p in out:
                rng = compute_entry_range(p["sid"], p.get("close"), conn)
                if rng is not None:
                    p["entry_low"], p["entry_high"] = rng
    return out


def format_pick_block(pick: dict, channel: str = "telegram") -> str:
    """組單張 pick 的訊息區塊。Telegram(Markdown legacy)+ Discord(Markdown)共用排版。

    輸入 pick dict 由 _select_top_picks 產;channel 控制 bold 語法。
    """
    b = _bold
    rank = pick.get("rank", 0)
    sid = pick.get("sid", "")
    name = pick.get("name", "")
    close = pick.get("close")
    pct_change = pick.get("pct_change")
    matched_labels = pick.get("matched_labels") or []
    ml_prob = pick.get("ml_prob")
    target_low = pick.get("target_low")
    target_high = pick.get("target_high")
    stop = pick.get("stop")
    ev = pick.get("ev")
    rr = pick.get("risk_reward")
    industry = pick.get("industry")
    industry_heat = int(pick.get("industry_heat") or 0)

    lines = [f"▎{b(f'#{rank}', channel)}  {b(f'{sid} {name}', channel)}"]
    # 收盤 + 漲跌
    if close is not None:
        if pct_change is not None:
            arrow = "↑" if pct_change > 0 else ("↓" if pct_change < 0 else "→")
            pct_str = f" ({arrow}{abs(pct_change):.1f}%)"
        else:
            pct_str = ""
        lines.append(f"   收盤 {close:.2f}{pct_str}")
    # 產業 badge:industry_heat ≥ 3 → 🔥 加 bold(熱門類股輪動)
    if industry:
        ind_str = str(industry).strip()
        if ind_str:
            if industry_heat >= 3:
                lines.append(
                    f"   🔥 {b(ind_str, channel)} (今日 {industry_heat} 檔同類)"
                )
            else:
                lines.append(f"   🏭 {_md_escape(ind_str, channel)}")
    # 命中策略
    n = len(matched_labels)
    if n > 0:
        lines.append(f"   📊 命中 {n} 策略")
        for label in matched_labels:
            lines.append(f"       · {_md_escape(str(label), channel)}")
    # ML 機率
    if ml_prob is not None:
        lines.append(f"   🤖 ML 機率 {ml_prob * 100:.0f}%")
    # SHAP Top 3 feature 解釋(2026-05-14 加)— caller(notify_top_picks)從 cache
    # 撈 / 算後注入 pick["shap_reason"]。沒有 / 失敗 → 整行 graceful skip。
    # _md_escape 必須套(SHAP feature 名 atr_normalized / macd_dif 含 _,
    # 不 escape 會被 Telegram 當斜體 marker → 400「can't parse entities」)。
    shap_reason = pick.get("shap_reason")
    if shap_reason:
        lines.append(f"   {_md_escape(shap_reason, channel)}")
    # 法人共識目標價(yfinance / Gemini news)— 在勝率之前
    # 加 Δ 標示(主公 2026-05-08 拍板):跟 previous_target_mean 比 |Δ| ≥ 1%
    # 才顯示「(↑ +5.1%)」/「(↓ -3.2%)」,小變動省略避免雜訊。
    analyst_mean = pick.get("analyst_target_mean")
    if analyst_mean:
        n_analyst = pick.get("analyst_num") or "?"
        upside_str = ""
        if close is not None and close > 0:
            upside = (analyst_mean - close) / close * 100
            sign = "+" if upside >= 0 else "-"
            upside_str = f" ({sign}{abs(upside):.0f}%)"
        delta_str = ""
        prev_mean = pick.get("analyst_target_prev_mean")
        if prev_mean and float(prev_mean) > 0:
            delta_pct = (analyst_mean - float(prev_mean)) / float(prev_mean) * 100
            if abs(delta_pct) >= 1.0:
                arrow = "↑" if delta_pct > 0 else "↓"
                sign_d = "+" if delta_pct > 0 else "-"
                delta_str = f" ({arrow} {sign_d}{abs(delta_pct):.1f}%)"
        lines.append(
            f"   📊 共識目標 {b(f'{analyst_mean:.0f}', channel)}"
            f"{upside_str}(券商 {n_analyst} 家){delta_str}"
        )
    # 歷史勝率(126 日回測平均,跟卡片勝率欄同來源)
    win_rate = pick.get("win_rate")
    if win_rate is not None and win_rate > 0:
        emoji = "🎯" if win_rate >= 0.55 else "📊"
        lines.append(
            f"   {emoji} 勝率 {b(f'{win_rate * 100:.0f}%', channel)}(126d 回測)"
        )
    # U3 進場區間建議(ATR/BB based)— 沒算出來(< 20 天歷史)graceful skip
    entry_low = pick.get("entry_low")
    entry_high = pick.get("entry_high")
    if entry_low is not None and entry_high is not None:
        try:
            lines.append(
                f"   💰 進場區間 {float(entry_low):.2f} ~ {float(entry_high):.2f}"
            )
        except (TypeError, ValueError):
            pass
    # 目標 / 停損
    if target_low and target_high and stop:
        lines.append(
            f"   🎯 保守 {target_low:.0f} / 積極 {target_high:.0f} / 停損 {stop:.0f}"
        )
    # 期望值 + R:R
    if ev is not None:
        rr_str = f"  (R:R {rr:.1f}:1)" if rr else ""
        sign = "+" if ev >= 0 else ""
        lines.append(f"   📈 期望值 {sign}{ev * 100:.1f}%{rr_str}")
    # 千張大戶(TDCC 週快照)— 主公拍板:不納入 ML,只當附加資訊
    # 沒資料(該檔當週沒公布 / 還沒抓過) → 整行 graceful skip 不顯
    # delta_w=None(第一次抓,沒上週可比) → 省略「週變」段
    holders_count = pick.get("holders_1000up_count")
    if holders_count is not None:
        try:
            count_int = int(holders_count)
            delta_w = pick.get("holders_delta_w")
            pct = pick.get("holders_pct")
            parts = [f"👥 千張戶 {count_int}"]
            sub: list[str] = []
            if delta_w is not None:
                try:
                    sub.append(f"週變 {int(delta_w):+d}")
                except (TypeError, ValueError):
                    pass
            if pct is not None:
                try:
                    sub.append(f"占比 {float(pct):.1%}")
                except (TypeError, ValueError):
                    pass
            if sub:
                parts.append(f" ({', '.join(sub)})")
            lines.append(f"   {''.join(parts)}")
        except (TypeError, ValueError):
            pass  # 任何 cast 失敗 silent skip,不擋整段推播
    return "\n".join(lines)


def format_premium_picks_block(
    premium_rows: list[dict], channel: str = "telegram",
) -> str:
    """組「✨ 高信心精選」section(法人連買 ≥ 3 + 千張戶進場 + ML 過門檻)。

    輸入 premium_rows 為 db.get_strong_follower_premium(...) 回傳的 list[dict]。
    empty → 回空 string,caller 看到 falsy 就 graceful skip 不顯該 section。

    格式(Markdown legacy / Discord 共用,bold 自動切換):
        ✨ *高信心精選(法人連買 ≥ 3 + 千張戶進場 + ML 過門檻)*
        1. [2330] *台積電* 779.0
           🏛️ 法人連買 3 天 | 🐋 千張戶 +12 | 🎯 ML 0.71
    """
    if not premium_rows:
        return ""
    b = _bold
    title = "高信心精選(法人連買 ≥ 3 + 千張戶進場 + ML 過門檻)"
    lines = [f"✨ {b(title, channel)}"]
    for i, r in enumerate(premium_rows, start=1):
        sid = str(r.get("sid") or "")
        name = str(r.get("name") or "")
        close = r.get("close")
        cd = r.get("consensus_days") or 0
        dw = r.get("holders_delta_w") or 0
        ml = r.get("ml_prob")
        try:
            close_str = f"{float(close):.2f}" if close is not None else "—"
        except (TypeError, ValueError):
            close_str = "—"
        header = f"{i}. [{sid}] {b(name, channel)} {close_str}"
        sub_parts = [f"🏛️ 法人連買 {int(cd)} 天", f"🐋 千張戶 +{int(dw)}"]
        if ml is not None:
            try:
                sub_parts.append(f"🎯 ML {float(ml):.2f}")
            except (TypeError, ValueError):
                pass
        lines.append(header)
        lines.append("   " + " | ".join(sub_parts))
    return "\n".join(lines)


def format_big_holder_movers_block(
    movers_rows: list[dict], channel: str = "telegram",
) -> str:
    """組「👥 大戶進場 Top N」section(holders_delta_w > 0 排行)。

    主公 2026-05-15 拍板:把大戶進場列獨立 section,跟「短線精選」/「高信心精選」
    並列呈現,而不是只 inline 在 per-pick block 內。

    輸入 movers_rows 為 db.get_top_shareholder_movers(...) 回傳的 list[dict]。
    empty → 回空 string,caller 看到 falsy 就 graceful skip 不顯該 section。

    格式(Markdown legacy / Discord 共用):
        👥 *大戶進場 Top N(本週千張戶人數增加排行)*
        1. [2330] *台積電* 779.0
           🐋 千張戶 +12 (占比 25.3%) | 🎯 ML 0.71
    """
    if not movers_rows:
        return ""
    b = _bold
    n = len(movers_rows)
    title = f"大戶進場 Top {n}(本週千張戶人數增加排行)"
    lines = [f"👥 {b(title, channel)}"]
    for i, r in enumerate(movers_rows, start=1):
        sid = str(r.get("sid") or "")
        name = str(r.get("name") or "")
        close = r.get("close")
        dw = r.get("holders_delta_w") or 0
        pct = r.get("holders_pct")
        ml = r.get("ml_prob")
        try:
            close_str = f"{float(close):.2f}" if close is not None else "—"
        except (TypeError, ValueError):
            close_str = "—"
        header = f"{i}. [{sid}] {b(name, channel)} {close_str}"
        try:
            dw_int = int(dw)
        except (TypeError, ValueError):
            dw_int = 0
        sub_parts = [f"🐋 千張戶 +{dw_int}"]
        if pct is not None:
            try:
                sub_parts.append(f"占比 {float(pct):.1%}")
            except (TypeError, ValueError):
                pass
        if ml is not None:
            try:
                sub_parts.append(f"🎯 ML {float(ml):.2f}")
            except (TypeError, ValueError):
                pass
        lines.append(header)
        lines.append("   " + " | ".join(sub_parts))
    return "\n".join(lines)


def format_yesterday_recap(
    channel: str = "telegram",
    top_n: int = 5,
    confluence_n: int = 2,
) -> str:
    """組「昨日 picks 複盤」section,放每日推播訊息頂部(headers 下、picks 上)。

    Source:db.get_pick_outcomes_for_date(yesterday) + db.load_daily_picks(yesterday)。
    Mirror `_select_top_picks` 的 confluence + ML threshold filter,讓 recap 算
    出來的 picks 跟昨天實際 Telegram 推的那組相同(top_n by ml_prob desc)。

    回空字串時 caller 應 graceful skip 不顯該 section:
        - pick_outcomes 空(weekly backtest 還沒跑過 / pre-launch 過渡期)
        - 該日 daily_picks 沒 precompute
        - 過濾後沒有 qualified picks
        - top picks 在 pick_outcomes 內查不到 return_d1(報酬窗口未到位)
    """
    # 不在 module 頂層 import 避免循環依賴(database 也 import 不到 notifier)
    from src.strategies import STRATEGY_LABELS, STRATEGY_ML_THRESHOLDS

    pick_date = db.get_last_evaluated_pick_date()
    if not pick_date:
        return ""

    outcomes = db.get_pick_outcomes_for_date(pick_date)
    if not outcomes:
        return ""

    # daily_picks 撈當日 agg(用 pure_stock,跟 _select_top_picks 預設一致)
    agg = db.load_daily_picks(pick_date, "pure_stock")
    if not agg:
        return ""

    # Confluence ≥ N + per-sid strictest ML threshold(mirror _select_top_picks)
    qualified: list[tuple[str, float, list[str]]] = []  # (sid, ml_prob, matched)
    for sid, info in agg.items():
        matched = list((info.get("details") or {}).keys())
        if len(matched) < confluence_n:
            continue
        prob = info.get("ml_prob")
        ths = [
            STRATEGY_ML_THRESHOLDS[s] for s in matched
            if STRATEGY_ML_THRESHOLDS.get(s) is not None
        ]
        strict_thr = max(ths) if ths else None
        if strict_thr is not None and (prob is None or prob < strict_thr):
            continue
        qualified.append((sid, prob or 0.0, matched))

    if not qualified:
        return ""

    # Lookup analyst_target_mean(法人共識)讓 sort 跟 _select_top_picks 一致 —
    # 避免 recap 反映的順序跟實際推播 picks 順序不同(M4/U1 known issue)。
    # 表空 / 該批 sids 都沒共識 → analyst_target_mean=None,degenerate 成
    # 純 ml_prob 排序(graceful fallback)。
    try:
        from src.analyst_targets import get_analyst_targets_for_sids
        analyst_target_map = get_analyst_targets_for_sids(
            [sid for sid, _, _ in qualified]
        )
    except Exception:  # noqa: BLE001
        analyst_target_map = {}

    # 跟 _select_top_picks 一致:讀同一份 30d weights 才能保 recap 順序與
    # 實際推播一致(M4/U1 known issue)。flag off → 用空 dict 退回 legacy。
    recap_weights: dict[str, float] = {}
    if STRATEGY_DYNAMIC_WEIGHT_ENABLED:
        try:
            from src.strategy_weighting import get_strategy_weights_30d
            with db.get_conn() as conn:
                recap_weights = get_strategy_weights_30d(conn)
        except Exception:  # noqa: BLE001
            recap_weights = {}

    qualified.sort(key=lambda x: _compute_pick_score(
        sid=x[0],
        ml_prob=x[1],
        matched_strategies=x[2],
        analyst_target_mean=(analyst_target_map.get(x[0]) or {}).get("target_mean"),
        strategy_weights=recap_weights,
    ))
    top_picks = qualified[:top_n]

    # Lookup return_d1 from outcomes(同 sid 跨多策略 → r1 一樣,取第一個 non-null)
    outcome_map: dict[tuple[str, str], float | None] = {}
    for r in outcomes:
        outcome_map[(r["sid"], r["strategy"])] = r["return_d1"]

    pick_results: list[tuple[str, float, list[str]]] = []
    for sid, _, matched in top_picks:
        r1: float | None = None
        for s in matched:
            v = outcome_map.get((sid, s))
            if v is not None:
                r1 = v
                break
        if r1 is None:
            continue  # backtest 還沒 evaluate
        pick_results.append((sid, r1, matched))

    if not pick_results:
        return ""

    n = len(pick_results)
    up = sum(1 for _, r1, _ in pick_results if r1 > 0)
    hit_rate = up / n * 100
    avg_ret = sum(r1 for _, r1, _ in pick_results) / n
    best = max(pick_results, key=lambda x: x[1])
    worst = min(pick_results, key=lambda x: x[1])

    # Per-strategy hit rate(從 outcomes 算,涵蓋全 fires 不限 top_n;min 3 fires)
    strategy_results: dict[str, list[float]] = {}
    for r in outcomes:
        v = r["return_d1"]
        if v is None:
            continue
        strategy_results.setdefault(r["strategy"], []).append(v)
    strategy_hit: list[tuple[str, float, int]] = []
    for strat, rets in strategy_results.items():
        if len(rets) < 3:
            continue
        ups = sum(1 for v in rets if v > 0)
        strategy_hit.append((strat, ups / len(rets) * 100, len(rets)))
    strategy_hit.sort(key=lambda x: (-x[1], -x[2]))

    def _fmt_ret(v: float) -> str:
        sgn = "+" if v >= 0 else ""
        return f"{sgn}{v:.1f}%"

    b = _bold
    avg_sign = "+" if avg_ret >= 0 else ""
    lines = [
        f"📈 {b(f'昨日 picks 複盤({pick_date})', channel)}",
        f"✅ {up}/{n} picks 上漲(命中率 {hit_rate:.0f}%)",
        (
            f"平均報酬:{avg_sign}{avg_ret:.2f}% "
            f"(最佳 {best[0]} {_fmt_ret(best[1])} / "
            f"最差 {worst[0]} {_fmt_ret(worst[1])})"
        ),
    ]
    if strategy_hit:
        top3 = strategy_hit[:3]
        parts = [
            f"{STRATEGY_LABELS.get(s, s)} {hr:.0f}%"
            for s, hr, _ in top3
        ]
        lines.append(f"策略表現:{' / '.join(parts)}")
    return "\n".join(lines)


def _regime_gating_caption(picks: list[dict]) -> str:
    """從 picks[0]["regime_gating"] 取 caption(注入訊息開頭)。

    picks 為空 / regime gating 失敗 → 直接撈一次當前 regime,讓「全市場 0 picks」
    死寂日子也顯。任何例外 → 回空字串(整段 graceful skip,不擋推播)。
    """
    if not REGIME_GATING_ENABLED:
        return ""
    # 1) picks 有 regime_gating metadata 直接用
    if picks:
        rg = picks[0].get("regime_gating") or {}
        cap = rg.get("caption")
        if cap:
            return str(cap)
    # 2) picks 空 / 沒 metadata — 直接撈一次
    try:
        from src.regime_gating import get_regime_gating_params
        with db.get_conn() as conn:
            rg = get_regime_gating_params(conn)
        return str(rg.get("caption") or "")
    except Exception:  # noqa: BLE001
        logger.exception("[NOTIFIER] regime gating caption fetch 失敗,略過")
        return ""


def format_top_picks_message(
    picks: list[dict], date: str, channel: str = "telegram",
    premium_picks: list[dict] | None = None,
    big_holder_movers: list[dict] | None = None,
) -> str:
    """組完整訊息 — 4 個獨立 section + footer。

    Section 結構(主公 2026-05-15 拍板還原獨立 section header):
        🎯 短線精選 · DATE(message header)
        ━━━━
        📈 昨日複盤(section,可空)
        ━━━━
        🎯 短線精選(Top N)(section,可空)
        ━━━━
        ✨ 高信心精選(三維交集 Top N)(section,可空)
        ━━━━
        👥 大戶進場 Top N(section,可空)
        ━━━━
        📊 統計 + 警語(footer)

    空 section graceful skip 不顯。picks 全 weak(_select_top_picks fallback path)
    時短線精選 section 加 caption 警語。

    premium_picks:db.get_strong_follower_premium 結果。
    big_holder_movers:db.get_top_shareholder_movers 結果。caller 沒給 → 自動撈,
        例外或表不存在 → 空 list(整 section graceful skip)。
    """
    b = _bold
    try:
        d = _date.fromisoformat(date)
        week_zh = ["一", "二", "三", "四", "五", "六", "日"][d.weekday()]
        date_label = f"{date}(週{week_zh})"
    except Exception:  # noqa: BLE001
        date_label = date

    # === Section blocks(各自 graceful 空回 ""/[])===
    # 1. 昨日複盤(U1)— pick_outcomes 表內昨天的實際報酬
    try:
        recap_block = format_yesterday_recap(channel=channel)
    except Exception:  # noqa: BLE001
        logger.exception("[NOTIFIER] format_yesterday_recap 失敗,略過該 section")
        recap_block = ""

    # 2. 短線精選 picks block(含 fallback caption)
    is_fallback = bool(picks) and all(
        p.get("confidence_tier") == "weak" for p in picks
    )
    short_picks_block = _format_short_picks_section(
        picks, channel=channel, is_fallback=is_fallback,
    )

    # 3. 高信心精選(三維交集)
    premium_block = format_premium_picks_block(
        premium_picks or [], channel=channel,
    )

    # 4. 大戶進場 Top N(本週千張戶人數增加排行)— caller 沒給 → 自動撈
    if big_holder_movers is None:
        try:
            big_holder_movers = db.get_top_shareholder_movers(limit=5)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[NOTIFIER] get_top_shareholder_movers 失敗,略過該 section"
            )
            big_holder_movers = []
    movers_block = format_big_holder_movers_block(
        big_holder_movers or [], channel=channel,
    )

    # === 組訊息:header + regime caption + 各 section(空 skip)+ footer ===
    parts: list[str] = [
        f"🎯 {b(f'短線精選 · {date_label}', channel)}{_weekend_hint(date)}",
    ]
    # regime gating caption — picks 有值時從 picks[0]["regime_gating"] 拿;
    # picks 空時嘗試直接撈一次(讓「全市場 0 picks」的死寂日子也顯 regime)
    gating_caption = _regime_gating_caption(picks)
    if gating_caption:
        parts.append(gating_caption)
    for block in (recap_block, short_picks_block, premium_block, movers_block):
        if block:
            parts.append(block)

    # Footer(picks 空時走 empty fallback / 有 picks 走統計)
    parts.append(_format_footer_block(picks, is_fallback=is_fallback, channel=channel))

    # Section 間 ━━━━ 分隔(各塊間 1 個 separator,前後空行給呼吸)
    return f"\n\n{_SEPARATOR}\n\n".join(parts)


def _format_short_picks_section(
    picks: list[dict], channel: str, is_fallback: bool,
) -> str:
    """組「🎯 短線精選(Top N)」section — 標題 + fallback caption + per-pick blocks。

    picks 空 → 回 empty fallback 文字(讓 caller 仍把這 section 串進訊息,
    主公看到「📭 無 picks」也算 section 顯示)。
    """
    b = _bold
    if not picks:
        return (
            f"🎯 {b('短線精選', channel)}\n"
            "📭 今日無符合「高信心 + 共識 ≥2」的 picks(過濾條件嚴,留空算正常)"
            f"{_empty_pick_suffix()}"
        )
    n = len(picks)
    title = (
        f"短線精選(降級弱訊號 Top {n})" if is_fallback
        else f"短線精選(Top {n})"
    )
    section_lines: list[str] = [f"🎯 {b(title, channel)}"]
    if is_fallback:
        section_lines.append(
            "⚠️ 今日無高信心 picks,降級顯示弱訊號 Top 3"
            "(僅過 ≥1 策略命中,未過 ML threshold)"
        )
    # 警示股 caption(2026-05-15 amendment:annotate-only,不 hard exclude)。
    # 直接從 picks(已限縮為 top N 推播範圍)撈帶 'warnings' 的,讓 caption 數字
    # 跟訊息內容一致(_LAST_ANNOTATED_WARNINGS 是全 qualified 範圍,可能包含
    # 沒進 top N 的 picks,用 picks 自己的更精準)。
    # 文案:「⚠️ 推薦中含 N 檔警示股 ...」— 不用「已濾掉」這種代主公決定的措辭。
    try:
        from src.warnings_filter import format_warning_caption
        annotated_in_message = [p for p in picks if p.get("warnings")]
        caption = format_warning_caption(annotated_in_message)
        if caption:
            section_lines.append(caption)
    except Exception:  # noqa: BLE001
        logger.exception("[NOTIFIER] 警示股 caption 渲染失敗,略過")
    # per-pick blocks,中間用 mini separator(短 dash)避免跟 section ━━━━ 混淆
    pick_blocks = [format_pick_block(p, channel=channel) for p in picks]
    section_lines.append("")
    section_lines.append(("\n" + ("─" * 8) + "\n").join(pick_blocks))
    return "\n".join(section_lines)


def _format_footer_block(
    picks: list[dict], is_fallback: bool, channel: str,
) -> str:
    """組 footer 統計 + 警語。picks 空 → 只顯警語。"""
    b = _bold
    if not picks:
        return "⚠️ 僅供研究,非投資建議。"
    n = len(picks)
    probs = [p["ml_prob"] for p in picks if p.get("ml_prob") is not None]
    evs = [p["ev"] for p in picks if p.get("ev") is not None]
    avg_ml = (sum(probs) / len(probs)) if probs else 0.0
    avg_ev = (sum(evs) / len(evs)) if evs else 0.0
    lines = [f"📊 {b('今日 picks 統計', channel)}"]
    if is_fallback:
        lines.append(f"   弱訊號(降級顯示):{n} 張")
    else:
        lines.append(f"   高信心 + ≥2 策略:  {n} 張")
    if probs:
        lines.append(f"   平均 ML 機率:    {avg_ml * 100:.0f}%")
    if evs:
        sign = "+" if avg_ev >= 0 else ""
        lines.append(f"   平均期望值:      {sign}{avg_ev * 100:.1f}%")
    lines.append("")
    lines.append("⚠️ 僅供研究,非投資建議。目標價為 ATR 統計參考,非實際預測。")
    return "\n".join(lines)


def _enrich_picks_with_shap(picks: list[dict], target_date: str) -> None:
    """對每張 pick 算 SHAP top-3 features → cache 進 pick_shap_explanations + 注入
    pick["shap_reason"] 給 format_pick_block 顯示。

    in-place 修改 picks(每筆加 "shap_reason" key,失敗時為空字串)。

    Routing 邏輯跟 _select_top_picks 一致:取 matched_strategies 內最嚴格 ML
    threshold 對應的 per-strategy model;沒命中 per-strategy 設定 → 用 general
    short_pick.pkl。任何例外(shap 未裝 / model 缺 / extract_features 失敗)
    silent skip 該檔(其他檔仍照常)。

    Caller(notify_top_picks)在 format_top_picks_message 之前呼叫;Telegram
    pick block 看到 pick["shap_reason"] 非空就加「🧠 SHAP: ...」一行。
    """
    if not picks:
        return
    try:
        from src import config
        from src.ml_predictor import (
            extract_features, load_model, load_strategy_model,
        )
        from src.strategies import STRATEGY_ML_THRESHOLDS
        from src.ml_shap import compute_pick_shap, format_shap_reason
    except ImportError as e:
        logger.warning("[NOTIFIER] SHAP enrich import 失敗,跳過:%s", e)
        for p in picks:
            p["shap_reason"] = ""
        return

    general_path = config.PROJECT_ROOT / "models" / "short_pick.pkl"
    general_model = load_model(general_path) if general_path.exists() else None
    strategy_models: dict[str, object] = {}

    for p in picks:
        sid = p.get("sid")
        if not sid:
            p["shap_reason"] = ""
            continue
        matched = p.get("matched_strategies") or []
        cands = [
            (s, STRATEGY_ML_THRESHOLDS[s]) for s in matched
            if STRATEGY_ML_THRESHOLDS.get(s) is not None
        ]
        chosen = max(cands, key=lambda kv: kv[1])[0] if cands else None
        if chosen and chosen not in strategy_models:
            strategy_models[chosen] = load_strategy_model(chosen)
        sm = strategy_models.get(chosen) if chosen else None
        model = sm if sm is not None else general_model
        strategy_key = chosen or "general"

        if model is None:
            p["shap_reason"] = ""
            continue
        try:
            feats = extract_features(sid, target_date)
        except Exception as e:  # noqa: BLE001
            logger.warning("[NOTIFIER] extract_features %s failed: %s", sid, e)
            feats = None
        explanations = compute_pick_shap(sid, model, feats, top_k=3)
        if explanations:
            try:
                db.save_shap_explanation(
                    target_date, sid, strategy_key, explanations,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[NOTIFIER] save_shap_explanation %s failed: %s", sid, e,
                )
        p["shap_reason"] = format_shap_reason(explanations)


def notify_top_picks(
    date: str | None = None,
    params: dict | None = None,
    top_n: int = 5,
    confluence_n: int = 2,
    send_telegram: bool = True,
    send_discord: bool = True,
    dry_run: bool = False,
) -> dict[str, bool]:
    """跑高信心 + 共識過濾 → top N picks → 並行送 Telegram + Discord。

    dry_run=True 時不送 channel,只 print 訊息到 stdout。
    回 {'telegram': bool, 'discord': bool} — 只包含實際送的通道(dry_run 時皆 True)。
    """
    if date is None:
        date = _date.today().isoformat()
    picks = _select_top_picks(
        date, top_n=top_n, confluence_n=confluence_n, params=params,
    )

    # SHAP 解釋性 enrich + cache(2026-05-14 加)— picks list in-place 加
    # "shap_reason" 給 format_pick_block 顯示。失敗不擋主推播(每筆 graceful skip)。
    try:
        _enrich_picks_with_shap(picks, date)
    except Exception:  # noqa: BLE001
        logger.exception("[NOTIFIER] SHAP enrich 整體失敗,picks 不帶 SHAP 行")
        for p in picks:
            p.setdefault("shap_reason", "")

    # 高信心精選(三維交集:法人連買 ≥3 + 千張戶進場 + ML 過門檻)— 跟 ≥2 共識
    # 並列獨立 section。helper 自己 graceful 空回 []。任何例外不擋主推播。
    try:
        premium_picks = db.get_strong_follower_premium(
            min_inst_days=3, min_delta_w=1, top_n=5,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[NOTIFIER] get_strong_follower_premium 失敗,略過該 section")
        premium_picks = []

    results: dict[str, bool] = {}
    tg_msg = format_top_picks_message(
        picks, date, channel="telegram", premium_picks=premium_picks,
    )
    dc_msg = format_top_picks_message(
        picks, date, channel="discord", premium_picks=premium_picks,
    )

    if dry_run:
        print("\n=== Telegram (Markdown legacy) ===\n", flush=True)
        print(tg_msg, flush=True)
        print("\n=== Discord ===\n", flush=True)
        print(dc_msg, flush=True)
        return {"telegram": True, "discord": True}

    if send_telegram and config.TELEGRAM_BOT_TOKEN:
        # 2026-05-15 主公拍板:Telegram 暫關 parse_mode → 純文字 fallback。
        # 昨晚 daily-notify run 25871610347 主訊息 16/16 fail(can't parse
        # entities byte 2259),escape fix(7a3ae7a)後仍要先確保訊息送出再
        # 回頭重打底。純文字會失去粗體/連結但能 deliver — Step 2 再回 HTML
        # / MarkdownV2 + 完整 escape 動態字串(SHAP feature 名 / 公司名 etc)。
        results["telegram"] = send_telegram_message(tg_msg, parse_mode="")
    if send_discord and config.DISCORD_WEBHOOK_URL:
        from src.discord_notifier import send_discord_message
        results["discord"] = send_discord_message(dc_msg)
    return results


def _build_news_search_url(news: dict) -> str:
    """組 Google News 搜尋 URL(query = 公司名 + subject 前 30 字)。

    TWSE OpenAPI 沒給原始公告 URL,Google News 搜尋當替代深連結。
    """
    name = str(news.get("company_name") or "")
    subject = str(news.get("subject") or "")
    query = f"{name} {subject[:30]}".strip()
    return f"https://www.google.com/search?q={quote_plus(query)}&tbm=nws"


def format_news_block(news: dict, channel: str = "telegram") -> str:
    """組單則重大訊息的訊息區塊。

    Telegram 走 HTML(`<b>...</b>` / `<a href="url">text</a>`,搭配 send_telegram_message
    parse_mode="HTML"),Discord 走 Markdown(`**bold**`)。

    HTML 切換背景:Telegram legacy Markdown 對 *_[` 等字元 escape 規則繁瑣
    (主公 5/9-5/10 silence root cause 之一),改用 HTML 只需 escape <>& 三字元
    就能避開所有 entity 解析錯誤(2026-05-09 主公拍板)。

    Input news dict 必有:sid, company_name, publish_date, publish_time, subject,
    article_no。description / fact_date 可選(目前不顯,只當 SQLite 紀錄)。

    可選欄位 tags:list[str] — 由 list_unsent_important_news 注入,顯示
    sid 的 6 類分組 tag(主公 2026-05-08 拍板)。

    格式(仿口袋台股):
        🔔 公司名 (sid) [⭐ 關注 · 📋 短線 · 🚀 漲停]   ⏰ HH:MM
        第 N 款
        📰 主旨...
        🔗 Google 新聞搜尋
    """
    sid = str(news.get("sid") or "")
    name = str(news.get("company_name") or "")
    time_str = str(news.get("publish_time") or "")
    subject = str(news.get("subject") or "")
    article = str(news.get("article_no") or "")
    tags = news.get("tags") or []

    # HHMMSS → HH:MM(若解析失敗顯原值)
    time_label = ""
    if time_str and time_str.isdigit():
        if len(time_str) >= 6:
            time_label = f"{time_str[:2]}:{time_str[2:4]}"
        elif len(time_str) >= 4:
            # 5 碼如 70003 → 07:00(時間補 0)
            padded = time_str.zfill(6)
            time_label = f"{padded[:2]}:{padded[2:4]}"

    if channel == "telegram":
        from html import escape as _h
        header = f"🔔 <b>{_h(name)} ({_h(sid)})</b>"
        if tags:
            header += f" [{_h(' · '.join(tags))}]"
        if time_label:
            header += f"  ⏰ {_h(time_label)}"
        lines = [header]
        if article:
            lines.append(f"📋 <b>{_h(article)}</b>")
        if subject:
            subj_display = subject if len(subject) <= 200 else subject[:197] + "..."
            lines.append(f"📰 {_h(subj_display)}")
        url = _build_news_search_url(news)
        # URL inside href= 也要 html-escape(處理 & → &amp;)
        lines.append(f'🔗 <a href="{_h(url, quote=True)}">Google 新聞搜尋</a>')
        return "\n".join(lines)

    # Discord:沿用 markdown
    b = _bold
    header = f"🔔 {b(f'{name} ({sid})', channel)}"
    if tags:
        header += f" [{' · '.join(tags)}]"
    if time_label:
        header += f"  ⏰ {time_label}"

    lines = [header]
    if article:
        lines.append(f"📋 {b(article, channel)}")
    if subject:
        subj_display = subject if len(subject) <= 200 else subject[:197] + "..."
        lines.append(f"📰 {subj_display}")
    url = _build_news_search_url(news)
    lines.append(f"🔗 [Google 新聞搜尋]({url})")
    return "\n".join(lines)


def format_news_message(
    news_list: list[dict], channel: str = "telegram",
) -> str:
    """合併多則 news 成單一訊息。Header + each block + footer。

    沒新聞 → 空 string(caller 自己判斷,不送)。長度限制:Telegram 4096,
    Discord 2000;單則平均 ~150-300 字 → 大概 5-10 則就到上限。caller 應控制
    一輪推不超過 5 則。
    """
    if not news_list:
        return ""
    today = _date.today().isoformat()
    if channel == "telegram":
        from html import escape as _h
        header_line = f"🔔 <b>{_h(f'重大訊息 · {today}')}</b>"
    else:
        header_line = f"🔔 {_bold(f'重大訊息 · {today}', channel)}"
    lines = [header_line, _SEPARATOR, ""]
    for news in news_list:
        lines.append(format_news_block(news, channel=channel))
        lines.append("")
        lines.append(_SEPARATOR)
        lines.append("")
    n = len(news_list)
    lines.append(f"📊 本輪推送 {n} 則重訊")
    lines.append("")
    lines.append("⚠️ 來源 TWSE 公開資訊;僅供研究,非投資建議。")
    return "\n".join(lines)


def format_short_picks(picks: pd.DataFrame, date: str) -> str:
    """把短線選股結果包成 Telegram Markdown 訊息。

    空 picks → 回「📭 今日無符合條件」訊息。
    """
    if picks is None or picks.empty:
        return (
            f"📭 *{date}* 今日無符合條件的個股"
            f"{_weekend_hint(date)}"
            f"{_empty_pick_suffix()}"
        )

    lines: list[str] = [
        f"📈 *{date} 短線推薦* ({len(picks)} 檔){_weekend_hint(date)}",
        "",
    ]
    for i, (_, row) in enumerate(picks.iterrows(), start=1):
        sid = row.get("stock_id", "?")
        name = row.get("name", "")
        close = float(row.get("close", 0) or 0)
        vol = float(row.get("volume", 0) or 0)
        ma_vol = float(row.get("ma_volume_5", 0) or 0)
        vol_ratio = (vol / ma_vol) if ma_vol > 0 else 0.0
        k = float(row.get("k", 0) or 0)
        d = float(row.get("d", 0) or 0)
        inst = float(row.get("inst_total_3d", 0) or 0)

        lines.append(f"{i}. *{sid} {name}*")
        lines.append(
            f"   收 {close:.2f} | 量比 {vol_ratio:.1f}x | "
            f"K {k:.1f} > D {d:.1f} | 法人 3 日 {inst / 1000:.0f}K"
        )
        # 詳細分析(歷史不足 / 無法人籌碼會回空字串,append 空字串無害但用 if 保險)
        from src.individual_sections import format_pick_summary
        detail = format_pick_summary(str(sid), indent="   ")
        if detail:
            lines.append(detail)

    lines.append("")
    lines.append("⚠️ 僅供研究,非投資建議")
    return "\n".join(lines)


def notify_short_picks(
    date: str | None = None,
    params: dict | None = None,
    send_telegram: bool = True,
    send_discord: bool = True,
) -> dict[str, bool]:
    """跑短線選股 → 並行送 Telegram + Discord。

    回 {'telegram': bool, 'discord': bool} — 只包含實際送的通道。
    若兩者 secrets 都沒設 → 回 {}(空 dict 視為沒推任何東西)。
    """
    if date is None:
        date = _date.today().isoformat()
    sids = [s for s, _ in TW_TOP_50]
    picks = screen_short(date, params=params, stock_ids=sids)

    results: dict[str, bool] = {}
    if send_telegram and config.TELEGRAM_BOT_TOKEN:
        results["telegram"] = send_telegram_message(
            format_short_picks(picks, date)
        )
    if send_discord and config.DISCORD_WEBHOOK_URL:
        # lazy import 避免 module import cycle / 拖慢啟動
        from src.discord_notifier import (
            format_short_picks_discord,
            send_discord_message,
        )
        results["discord"] = send_discord_message(
            format_short_picks_discord(picks, date)
        )
    return results


# === 多策略並行推播 ===

def format_multi_strategy_picks(
    aggregated: dict[str, dict],
    date: str,
) -> str:
    """把 run_all_strategies 聚合結果包成 Telegram Markdown。

    aggregated: {sid: {"name", "signals": [...], "details": {...}}}
    優先列 信號數 多的(多策略同時看好 = 信心強)。
    """
    if not aggregated:
        return (
            f"📭 *{date}* 今日無任一策略選中個股"
            f"{_weekend_hint(date)}"
            f"{_empty_pick_suffix()}"
        )

    # 按信號數降序、stock_id 升序
    sorted_items = sorted(
        aggregated.items(),
        key=lambda kv: (-len(kv[1]["signals"]), kv[0]),
    )
    n = len(sorted_items)
    # 加週幾(資料日期,非執行日期)
    try:
        d = _date.fromisoformat(date)
        week_zh = ["一", "二", "三", "四", "五", "六", "日"][d.weekday()]
        date_label = f"{date} (週{week_zh})"
    except Exception:  # noqa: BLE001
        date_label = date
    lines = [
        f"📈 *{date_label} 短線推薦* ({n} 檔,多策略並行){_weekend_hint(date)}",
        "",
    ]
    for i, (sid, info) in enumerate(sorted_items, start=1):
        close = None
        target_low = target_high = stop_loss = risk_reward = None
        for d in info["details"].values():
            if close is None and d.get("close"):
                close = d["close"]
            if target_low is None and d.get("target_low"):
                target_low = d.get("target_low")
                target_high = d.get("target_high")
                stop_loss = d.get("stop_loss")
                risk_reward = d.get("risk_reward")
        signals = " + ".join(info["signals"])
        confidence = "🔥" * len(info["signals"])
        lines.append(f"{i}. *{sid} {info['name']}* {confidence}")
        if close:
            lines.append(f"   收 {close:.2f} | 信號: {signals}")
        else:
            lines.append(f"   信號: {signals}")
        if target_low and target_high and stop_loss:
            rr_str = f" (R:R {risk_reward:.1f}:1)" if risk_reward else ""
            lines.append(
                f"   🎯 目標 {target_low:.2f}~{target_high:.2f}"
                f" / 🛑 停損 {stop_loss:.2f}{rr_str}"
            )
        # 詳細分析(reuse 個股頁 helper)
        from src.individual_sections import format_pick_summary
        detail = format_pick_summary(str(sid), indent="   ")
        if detail:
            lines.append(detail)
    lines.append("")
    lines.append("⚠️ 僅供研究,非投資建議。目標價為 ATR 統計參考,非實際預測。")
    return "\n".join(lines)


def notify_multi_strategy(
    date: str | None = None,
    enabled: list[str] | None = None,
    params: dict | None = None,
    send_telegram: bool = True,
    send_discord: bool = True,
) -> dict[str, bool]:
    """跑多策略 → 聚合 → 並行送 Telegram + Discord。

    回 {'telegram': bool, 'discord': bool} — 只包含實際送的通道。
    """
    from src.strategies import run_all_strategies
    if date is None:
        date = _date.today().isoformat()
    sids = [s for s, _ in TW_TOP_50]
    agg = run_all_strategies(
        date, enabled=enabled, params=params, stock_ids=sids,
    )

    results: dict[str, bool] = {}
    if send_telegram and config.TELEGRAM_BOT_TOKEN:
        results["telegram"] = send_telegram_message(
            format_multi_strategy_picks(agg, date)
        )
    if send_discord and config.DISCORD_WEBHOOK_URL:
        from src.discord_notifier import (
            format_multi_strategy_picks_discord,
            send_discord_message,
        )
        results["discord"] = send_discord_message(
            format_multi_strategy_picks_discord(agg, date)
        )
    return results


def format_manual_picks(picks_df: "pd.DataFrame", date: str, limit: int = 7) -> str:
    """把雲端 App 的當前推薦 DataFrame 包成 Telegram 訊息(手動推播專用)。

    跟 cron 推播訊息差別:
      - 限制 limit 檔(避免使用者選一堆把訊息撐爆 / Telegram 4096 字元)
      - footer 加 `📲 來源:雲端 App 手動推播` 區別自動推播
    """
    if picks_df is None or picks_df.empty:
        return f"📭 *{date}* 雲端 App 手動推播:當前無推薦個股"

    # 接受兩種 schema:cron 用的「stock_id/name/close + 量價技術指標」或
    # 短線頁 aggregated_to_dataframe 出的「stock_id/name/close + 信號數/信號 + targets」
    df = picks_df.head(limit)
    n_total = len(picks_df)
    n_show = len(df)
    truncated = f"(顯示前 {n_show} / 共 {n_total})" if n_total > limit else f"({n_show} 檔)"

    try:
        d = _date.fromisoformat(date)
        wk = ["一", "二", "三", "四", "五", "六", "日"][d.weekday()]
        date_label = f"{date} (週{wk})"
    except Exception:  # noqa: BLE001
        date_label = date

    lines = [f"📈 *{date_label} 短線推薦* {truncated}", ""]
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        sid = r.get("stock_id", "?")
        name = r.get("name", "")
        close = r.get("close")
        n_sig = r.get("信號數") or r.get("n_signals") or 0
        signals = r.get("信號") or r.get("signals") or ""
        target_low = r.get("target_low")
        target_high = r.get("target_high")
        stop_loss = r.get("stop_loss")
        rr = r.get("risk_reward")

        confidence = "🔥" * int(n_sig) if n_sig else ""
        lines.append(f"{i}. *{sid} {name}* {confidence}".rstrip())
        if close is not None:
            try:
                close_str = f"{float(close):.2f}"
                if signals:
                    lines.append(f"   收 {close_str} | {signals}")
                else:
                    lines.append(f"   收 {close_str}")
            except (TypeError, ValueError):
                pass
        if target_low and target_high and stop_loss:
            try:
                rr_str = f" (R:R {float(rr):.1f}:1)" if rr else ""
                lines.append(
                    f"   🎯 {float(target_low):.2f}~{float(target_high):.2f}"
                    f" / 🛑 {float(stop_loss):.2f}{rr_str}"
                )
            except (TypeError, ValueError):
                pass

    lines.append("")
    lines.append("⚠️ 僅供研究,非投資建議")
    lines.append("📲 來源:雲端 App 手動推播")
    return "\n".join(lines)


def notify_manual_picks(
    picks_df: "pd.DataFrame",
    date: str | None = None,
    limit: int = 7,
    send_telegram: bool = True,
    send_discord: bool = True,
) -> dict[str, bool]:
    """雲端 App「立即推播」按鈕專用:把當前頁面的 picks 推到 Telegram+Discord。

    Returns: {'telegram': bool, 'discord': bool} — 只含實際送的通道;
             兩者 secrets 都沒設 → 回 {} (caller 該提示沒推任何東西)。
    """
    if date is None:
        date = _date.today().isoformat()
    msg = format_manual_picks(picks_df, date, limit=limit)
    results: dict[str, bool] = {}
    if send_telegram and config.TELEGRAM_BOT_TOKEN:
        results["telegram"] = send_telegram_message(msg)
    if send_discord and config.DISCORD_WEBHOOK_URL:
        from src.discord_notifier import send_discord_message
        results["discord"] = send_discord_message(msg)
    return results


__all__ = [
    "send_telegram_message",
    "format_short_picks",
    "format_multi_strategy_picks",
    "format_manual_picks",
    "format_premium_picks_block",
    "format_top_picks_message",
    "format_yesterday_recap",
    "notify_short_picks",
    "notify_multi_strategy",
    "notify_manual_picks",
    "notify_top_picks",
]
