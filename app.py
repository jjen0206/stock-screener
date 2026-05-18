"""
Stock Screener — Streamlit 入口。

T4-A 完成:sidebar 路由 + 個股查詢頁 + 設定頁
T4-B 完成:短線推薦頁 + 長線占位頁 + sidebar「更新今日資料」按鈕
"""
# ruff: noqa: E402
# E402:profiling helpers (_tic / _toc / _TIMING_START) 必須在 heavy imports
# (pandas / plotly / streamlit ~1s)前定義,讓 timing capture cold-load 全程
from __future__ import annotations

import os
import re
import sqlite3
import time as _time_perf
from datetime import date, timedelta


# === Profiling helpers — 設 DEBUG_TIMING=1 環境變數時才有效果 ===
# 永遠定義(否則 wrap 處要 try/except),但寫入 session_state 在 streamlit
# context 外是 no-op,profile script 透過 at.session_state 讀回時間。
def _tic(label: str) -> None:
    """記錄 label 的開始時間到 module-level _TIMING_START。"""
    _TIMING_START[label] = _time_perf.perf_counter()


def _toc(label: str) -> None:
    """記錄 label 的耗時(秒)到 st.session_state["_timing"]。"""
    elapsed = _time_perf.perf_counter() - _TIMING_START.pop(label, _time_perf.perf_counter())
    try:
        # streamlit context 外 session_state 寫入會 warn 但不 raise
        import streamlit as _st
        if "_timing" not in _st.session_state:
            _st.session_state["_timing"] = {}
        _st.session_state["_timing"][label] = elapsed
    except Exception:  # noqa: BLE001
        pass


_TIMING_START: dict[str, float] = {}

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src import config, database as db, indicators as ind
from src.cache_utils import clear_all_caches
from src.data_fetcher import (
    FinMindAPIError,
    ensure_stock_info,
    fetch_all_daily_prices_bulk,
    fetch_daily_price,
    fetch_dividend,
    fetch_institutional,
)
from src.individual_sections import (
    _compute_main_force_signal,  # noqa: F401  個股頁不直接用,e2e test 從 app namespace 拿
    _load_recent_ohlc,
    _render_action_suggestion,
    _render_key_levels,
    _render_main_force_signal,
    _render_technical_summary,
)
from src.market_sentiment import (
    compute_total_net_per_day,
    fetch_institutional_total,
    fetch_margin_balance,
    fetch_taiex,
    fetch_vix,
)
from src.industry_filter import (
    MAINSTREAM_INDUSTRIES,
    filter_sids_by_industry,
    get_all_canonical_industries,
    get_other_industries,
)
from src.screener_short import DEFAULT_SHORT_PARAMS

# 以下 3 個只在特定頁 / 特定按鈕用,改 lazy import 省 cold load ~290ms:
#   src.backtester.backtest_short        — 只 _page_backtest
#   src.screener_long.screen_long        — 只 _page_long
#   src.financial_fetcher_free.update_long_term_data_free — 只系統頁 re-fetch button
# 2026-05-06 主公拍板:盡量減少 cold load 不用到的 module import,主要拿
# src.backtester ~289ms 跟 src.screener_long / financial_fetcher_free 數十 ms。
from src.ui_cards import (
    render_picks_cards, render_picks_cards_paginated, view_mode_toggle,
)
from src.strategies import (
    DEFAULT_BIAS_PARAMS,
    DEFAULT_BB_REBOUND_PARAMS,
    DEFAULT_GAP_UP_PARAMS,
    DEFAULT_INST_CONSENSUS_PARAMS,
    DEFAULT_INST_SILENT_PARAMS,
    DEFAULT_MA_PARAMS,
    DEFAULT_MACD_PARAMS,
    DEFAULT_RSI_RECOVERY_PARAMS,
    DEFAULT_SQUEEZE_PARAMS,
    DEFAULT_VOL_BREAKOUT_PARAMS,
    STRATEGY_LABELS,
    aggregated_to_dataframe, compute_target_prices, run_all_strategies,
)
from src.universe import (
    TW_TOP_50, WATCHLIST_PATH, get_full_universe, is_pure_stock,
    load_watchlist, pure_stock_universe,
)


PAGES = [
    "🏠 首頁", "🔥 短線", "💎 長線", "📈 回測",
    "🔍 個股", "📊 個股深度",
    "⭐ 關注", "🌡️ 市場熱度", "👥 大戶入場", "📊 強者跟蹤",
    "📊 大盤",
    "💼 交易紀錄", "🛡️ 持倉管理", "🚨 警報設定", "🧪 實測追蹤", "📊 策略歷史",
    "📈 績效分析",
    "📋 系統結論", "💬 問軍師", "⚙️ 系統", "⚙️ 設定",
]

_CACHE_TABLES = [
    "stocks", "daily_prices", "institutional",
    "financials", "dividend", "sync_log",
]


# === 全域 CSS:介面字體放大 1.25x(老花友善) ===

_GLOBAL_CSS = """
<style>
/* 全域字體放大 — 給老花使用者(~1.4x) */
html, body, [class*="css"] {
    font-size: 20px !important;
}

/* 主標題 */
h1 { font-size: 2.8rem !important; }
h2 { font-size: 2.3rem !important; }
h3 { font-size: 1.9rem !important; }

/* sidebar 文字放大 */
section[data-testid="stSidebar"] {
    font-size: 19px;
}
section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3 {
    font-size: 1.6rem !important;
}

/* radio / checkbox 標籤 */
[data-baseweb="radio"] label,
[data-baseweb="checkbox"] label {
    font-size: 19px !important;
}

/* 按鈕文字放大 */
button[kind="primary"], button[kind="secondary"], .stButton button {
    font-size: 20px !important;
    padding: 0.65rem 1.3rem !important;
}

/* input / selectbox / date input */
.stTextInput input, .stDateInput input, .stSelectbox div[role="combobox"], .stNumberInput input {
    font-size: 19px !important;
}

/* metric (st.metric) 數值放大 */
[data-testid="stMetricValue"] {
    font-size: 2.4rem !important;
}
[data-testid="stMetricLabel"] {
    font-size: 1.15rem !important;
}

/* DataFrame 字體 */
[data-testid="stDataFrame"] {
    font-size: 17px;
}

/* tab 標題 */
[data-baseweb="tab"] {
    font-size: 19px !important;
}

/* alert / info / warning 訊息 */
[data-testid="stAlert"] {
    font-size: 19px;
}

/* footer 風險警語(微放大,仍是小字) */
.footer-warning {
    font-size: 14px !important;
    color: #888;
}

/* 黏性「執行」主按鈕 — 手機捲動時按鈕仍貼底,不用滾回頂端 */
/* 注意:Streamlit 父層若加 overflow:hidden 會破壞 sticky;
   實測 Streamlit 1.56 主流 layout 的 [data-testid="stMain"] 是 scroll container,
   primary button 在 stVerticalBlock 內,sticky 可生效。 */
div[data-testid="stButton"] > button[kind="primary"] {
    position: sticky !important;
    bottom: 0.75rem;
    z-index: 999;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}
</style>
"""


def _inject_global_css() -> None:
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)


# === PWA(iOS Safari「加到主畫面」全螢幕體驗) ===

@st.cache_data(show_spinner=False)
def _build_pwa_html() -> str:
    """組 PWA meta tags + manifest(都用 data URI,不依賴 static folder)。

    iOS Safari 把 app 加到主畫面後:
    - apple-mobile-web-app-capable=yes → 全螢幕(無 Safari 網址列)
    - apple-touch-icon → 主畫面圖示
    - manifest → Android Chrome 也支援 PWA 安裝
    """
    import base64
    import json

    icon_path = config.PROJECT_ROOT / "static" / "icon-180.png"
    if icon_path.exists():
        with open(icon_path, "rb") as f:
            icon_b64 = base64.b64encode(f.read()).decode("ascii")
        icon_uri = f"data:image/png;base64,{icon_b64}"
    else:
        icon_uri = ""

    manifest = {
        "name": "個人選股工具",
        "short_name": "股票 Pro",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0e1117",
        "theme_color": "#1f77b4",
        "icons": (
            [{"src": icon_uri, "sizes": "180x180", "type": "image/png"}]
            if icon_uri else []
        ),
    }
    manifest_b64 = base64.b64encode(
        json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
    ).decode("ascii")

    return f"""
<link rel="manifest" href="data:application/manifest+json;base64,{manifest_b64}"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="apple-mobile-web-app-title" content="股票 Pro"/>
<meta name="theme-color" content="#1f77b4"/>
{f'<link rel="apple-touch-icon" href="{icon_uri}"/>' if icon_uri else ''}
{f'<link rel="icon" type="image/png" sizes="180x180" href="{icon_uri}"/>' if icon_uri else ''}
"""


def _inject_pwa() -> None:
    """注入 PWA meta + manifest;失敗不阻擋 app 啟動。"""
    try:
        st.markdown(_build_pwa_html(), unsafe_allow_html=True)
    except Exception:  # noqa: BLE001
        pass


# === 雲端 fallback:從 CSV snapshot 灌資料(TWSE OpenAPI 在 Streamlit Cloud 被擋) ===

_BOOT_DONE_KEY = "_boot_setup_done"


# === run_all_strategies cache wrapper(兩段式 lookup)===
# Stage 1: daily_picks 表(nightly 預跑;default params + 已知 universe → 0ms)
# Stage 2: @st.cache_data 包 run_all_strategies(custom params / unknown
#          universe → 算過後 ttl 600s 內 cache hit)

PRECOMPUTE_PARAMS_HASH = "default_v1"


def _with_etf_universe_sids(min_history: int = 20) -> list[str]:
    """20+ 天歷史所有股(含 ETF / 債券)— 對齊 precompute_strategies 同名 helper。
    放在這裡是讓 _universe_to_label 不用去 import scripts.precompute_strategies。
    """
    sids = set(db.stocks_with_min_history(min_history))
    if not sids:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id FROM stocks WHERE market='TW' "
            "AND name IS NOT NULL AND name != '' "
            "ORDER BY stock_id"
        ).fetchall()
    return [r["stock_id"] for r in rows if r["stock_id"] in sids]


@st.cache_data(ttl=600, show_spinner=False)
def _known_universe_sets() -> dict[str, frozenset[str]]:
    """3 個預跑 universe 的 sid 集合(frozenset 用於與 caller 傳入的
    universe_tuple 比較)。@st.cache_data 避免每 rerun 都重打 SQLite。
    """
    return {
        "pure_stock": frozenset(pure_stock_universe(min_history=20)),
        "with_etf": frozenset(_with_etf_universe_sids(min_history=20)),
        "top_50": frozenset(s for s, _ in TW_TOP_50),
    }


def _universe_to_label(universe_tuple: tuple[str, ...]) -> str | None:
    """把 universe tuple 對應到 daily_picks 預跑的 universe label。
    mismatch 回 None(custom universe → 走 stage 2 runtime)。
    """
    if not universe_tuple:
        return None
    target = frozenset(universe_tuple)
    for label, sids_set in _known_universe_sets().items():
        if target == sids_set:
            return label
    return None


def _canonical_default_params_key() -> tuple[tuple[str, object], ...]:
    """precompute 用 params=None,各策略走 DEFAULT_*_PARAMS。caller 端
    短線頁會傳合併過的 params dict;若所有 sliders 都在 default,那 dict
    對應到這裡的 canonical key。

    來源:_SHORT_PARAM_DEFAULTS 對應的 strategy params(volume_kd / bias /
    squeeze / inst_consensus / bb_rebound / rsi / inst_silent / vol_breakout /
    gap_up)。
    """
    return tuple(sorted({
        "volume_multiplier": float(DEFAULT_SHORT_PARAMS["volume_multiplier"]),
        "kd_threshold_low": float(DEFAULT_SHORT_PARAMS["kd_threshold_low"]),
        "inst_buy_days": int(DEFAULT_SHORT_PARAMS["inst_buy_days"]),
        "bias_low": float(DEFAULT_BIAS_PARAMS["bias_low"]),
        "bias_high": float(DEFAULT_BIAS_PARAMS["bias_high"]),
        "vol_ratio_min": float(DEFAULT_BIAS_PARAMS["vol_ratio_min"]),
        "squeeze_pct_max": float(DEFAULT_SQUEEZE_PARAMS["squeeze_pct_max"]),
        "consecutive_days": int(DEFAULT_INST_CONSENSUS_PARAMS["consecutive_days"]),
        "bb_touch_lookback": int(DEFAULT_BB_REBOUND_PARAMS["bb_touch_lookback"]),
        "rsi_oversold": float(DEFAULT_RSI_RECOVERY_PARAMS["rsi_oversold"]),
        "rsi_recovered": float(DEFAULT_RSI_RECOVERY_PARAMS["rsi_recovered"]),
        "pct_change_max": float(DEFAULT_INST_SILENT_PARAMS["pct_change_max"]),
        "bb_position_max": float(DEFAULT_INST_SILENT_PARAMS["bb_position_max"]),
        "vbo_vol_ratio_min": float(DEFAULT_VOL_BREAKOUT_PARAMS["vbo_vol_ratio_min"]),
        "highest_lookback": int(DEFAULT_VOL_BREAKOUT_PARAMS["highest_lookback"]),
        "gap_pct_min": float(DEFAULT_GAP_UP_PARAMS["gap_pct_min"]),
        "gap_vol_ratio_min": float(DEFAULT_GAP_UP_PARAMS["gap_vol_ratio_min"]),
    }.items()))


def _canonical_default_enabled_key() -> tuple[str, ...]:
    """全 11 套策略 sorted tuple — 短線頁 user 沒取消任何策略時等於這個。"""
    from src.strategies import ALL_STRATEGIES
    return tuple(sorted(ALL_STRATEGIES.keys()))


def _is_precompute_eligible(
    enabled_key: tuple[str, ...] | None,
    params_key: tuple[tuple[str, object], ...] | None,
) -> bool:
    """判斷 args 是否對應到 nightly precompute 跑的 default 路徑。

    精確對應 precompute_strategies.py 的 run_all_strategies(date, stock_ids=...)
    呼叫(enabled=None, params=None)。
    Caller 端若傳:
    - 都 None → True(dashboard 路徑)
    - enabled = 全部 11 策略 + params = canonical defaults → True(短線頁無
      動 sliders 路徑)
    - 任一 mismatch → False(custom params / 取消策略 → runtime 算)

    enabled 用 frozenset 比較避免 caller 傳入順序差異(短線頁 multiselect
    回傳順序是 user click 順序,不是 sorted)。
    """
    if enabled_key is None and params_key is None:
        return True
    if frozenset(enabled_key or ()) != frozenset(_canonical_default_enabled_key()):
        return False
    return params_key == _canonical_default_params_key()


@st.cache_data(ttl=300, show_spinner=False)
def _get_intraday_cached(sids_tuple: tuple[str, ...]) -> dict:
    """5 分鐘 cache 的盤中即時報價 batch fetcher。

    sids 是 tuple(hashable for cache key)。回 {sid: quote_dict | None}。
    非交易時段不應呼叫(caller 用 is_market_hours() 守住),避免抓 stale 浪費 quota。
    """
    from src.intraday import get_intraday_quote
    return get_intraday_quote(list(sids_tuple))


def _inject_intraday_quotes(rows: list[dict], sids: list[str]) -> list[dict]:
    """對 rows(每筆有 stock_id)注入 intraday_quote;非交易時段直接回原 rows。

    給 _page_short / _page_watchlist 共用 — render_pick_card 才會看 row['intraday_quote']。
    """
    from src.intraday import is_market_hours
    if not is_market_hours():
        return rows
    quotes = _get_intraday_cached(tuple(sorted(set(sids))))
    for r in rows:
        sid = r.get("stock_id")
        if sid:
            r["intraday_quote"] = quotes.get(str(sid))
    return rows


@st.cache_data(ttl=600, show_spinner=False)
def _run_all_strategies_runtime(
    trade_date: str,
    universe_key: tuple[str, ...],
    enabled_key: tuple[str, ...] | None,
    params_key: tuple[tuple[str, object], ...] | None,
) -> dict:
    """Stage 2:cache miss 才走;真的呼叫 run_all_strategies 算一輪。
    @st.cache_data ttl 600 → 同 args 短時間內重複 call 直接 hit memory。
    """
    enabled = list(enabled_key) if enabled_key else None
    params = dict(params_key) if params_key else None
    sids = list(universe_key) if universe_key else None
    return run_all_strategies(
        trade_date,
        enabled=enabled,
        params=params,
        stock_ids=sids,
    )


def _run_all_strategies_cached(
    trade_date: str,
    universe_key: tuple[str, ...],
    enabled_key: tuple[str, ...] | None,
    params_key: tuple[tuple[str, object], ...] | None,
) -> dict:
    """兩段式 lookup:先試 daily_picks 預跑表,miss 走 runtime cache。

    Stage 1 條件(全滿足才 hit):
    1. (enabled, params) 對應 precompute 的 default 路徑
    2. universe 是 3 個預跑 universe 之一(pure_stock / with_etf / top_50)
    3. SQLite daily_picks 有這 (trade_date, universe_label, default_v1) 記錄

    Stage 2:任何 stage 1 miss 走原 runtime path,@st.cache_data 涵蓋 ttl 內
    重 rerun 不重算(短線 sticky-submit 後 rerun 0ms 命中)。
    """
    if _is_precompute_eligible(enabled_key, params_key):
        u_label = _universe_to_label(universe_key)
        if u_label is not None:
            cached = db.load_daily_picks(
                trade_date, u_label, PRECOMPUTE_PARAMS_HASH,
            )
            if cached is not None:
                # ⚡ 0ms 命中 nightly 預跑 — 不打 SQL helpers,不算 indicator
                return cached
    # Stage 2:custom params / unknown universe / precompute 沒這天的資料
    return _run_all_strategies_runtime(
        trade_date, universe_key, enabled_key, params_key,
    )


def _make_params_key(params: dict | None) -> tuple[tuple[str, object], ...] | None:
    """把 params dict 轉 sorted tuple 給 cache_data 當 hashable key。

    value 若是 list / dict 也要遞迴轉 tuple,但本專案 params 只有 scalar,
    直接 sort+items 就行。
    """
    if not params:
        return None
    return tuple(sorted(params.items()))


@st.cache_resource(show_spinner=False)
def _get_ml_model_for_enrich():
    """lazy load + cache ML model 給 _enrich_df_with_ml_prob 用。

    跟 individual_sections._get_ml_model 同 pattern,但獨立 cache_resource
    讓 sticky-submit 重 rerun 不重 load(joblib.load 約 50-150ms)。
    Pickle 失敗 / 檔不存在一律回 None,_enrich 拿到 None 就跳過 enrich。
    """
    try:
        from src.ml_predictor import load_model
        path = config.PROJECT_ROOT / "models" / "short_pick.pkl"
        if not path.exists():
            return None
        return load_model(path)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ML/enrich] _get_ml_model_for_enrich exception:"
            f"{type(e).__name__}: {e}",
            flush=True,
        )
        return None


def _render_high_confidence_sidebar() -> None:
    """sidebar 「🎯 高信心模式」toggle — Stage 2B per-strategy 模式。

    每個策略各自校準 ML 門檻(per-strategy retrain + grid search)。當前生效
    6 個策略 thresholds(STRATEGY_ML_THRESHOLDS dict);其餘 5 個結構性低 fire
    維持不過濾。

    跨頁面 session_state 共用,預設開。
    """
    st.sidebar.markdown("---")
    st.sidebar.markdown("**🎯 信心過濾**")
    st.sidebar.toggle(
        "高信心模式 (per-strategy)",
        value=True,
        help="每策略各自校準 ML 門檻過濾;關閉看全部 picks。",
        key="high_confidence_mode",
    )
    st.sidebar.caption(
        "📊 per-strategy 模式自動套用(6 個策略,Stage 2B 校準):\n"
        "・**乖離收斂 / 量爆突破** ≥ 0.65\n"
        "・**MACD 黃金交叉 / 跳空缺口** ≥ 0.60\n"
        "・**多頭排列** ≥ 0.55\n"
        "・**布林下軌反彈** ≥ 0.50\n"
        "・其他 5 套策略不過濾(sample 太小或 audit 顯示無效)"
    )

    # 🥇 共識過濾(confluence):多策略同時命中才算高品質訊號
    st.sidebar.markdown("**📊 共識過濾(confluence)**")
    st.sidebar.toggle(
        "需多策略共識命中",
        value=False,
        help="只顯示同時被 ≥ N 個策略命中的 picks;雙重過濾把勝率再拉一階。",
        key="confluence_filter_on",
    )
    st.sidebar.slider(
        "最少命中策略數 N",
        min_value=1, max_value=5, value=2, step=1,
        help="該 pick 需被 N 個策略同時命中才顯示。N=1 等同關閉,N=2 預設。",
        key="confluence_n",
    )
    # 跨策略共識 quick filter — 用 consensus meta(含分類)而不是 raw matched
    # length,跨類別才會被算進來;同類別重複命中不算。預設 off。
    st.sidebar.toggle(
        "⭐ 只顯示 2+ 策略共識",
        value=False,
        help=(
            "只顯示被 ≥ 2 策略共識命中的 picks(根據 src.consensus 統計)。"
            "與上方 confluence N 差異:此 filter 用『跨類別』維度 — 跨類共識 "
            "≥ 1 才視為共識股,純同類別重複不算。"
        ),
        key="consensus_only_on",
    )


def _matched_strategies_for_sid(agg: dict[str, dict], sid: str) -> list[str]:
    """從 agg dict 拿某 sid 命中的全部 strategy_keys(英文 key,非中文 label)。

    agg[sid]["details"] 是 {strategy_key: row_dict};.keys() 即 list of keys。
    sid 不在 agg 內 / details 為空 → 回 []。
    """
    if not agg or sid not in agg:
        return []
    details = agg[sid].get("details") or {}
    return list(details.keys())


def _enrich_df_with_matched_strategies(
    df: "pd.DataFrame", agg: dict[str, dict],
) -> "pd.DataFrame":
    """把 agg 的 details 鍵(strategy_keys)展開成 df 的 'matched_strategies'
    list 欄。給 _apply_confidence_filter 的 per-strategy 模式查 threshold 用。
    """
    if df is None or df.empty:
        return df
    enriched = df.copy()
    enriched["matched_strategies"] = enriched["stock_id"].map(
        lambda sid: _matched_strategies_for_sid(agg, sid)
    )
    return enriched


def _enrich_df_with_consensus(
    df: "pd.DataFrame", agg: dict[str, dict],
) -> "pd.DataFrame":
    """把跨策略共識 meta 注入 df['consensus'] — ui_cards 渲 badge 用。

    與 _enrich_df_with_matched_strategies 不同:matched_strategies 是 list,
    consensus 是 dict(含 strategy_count / categories / category_count)。
    沒命中策略的 sid → None。
    """
    if df is None or df.empty:
        return df
    from src.consensus import compute_strategy_consensus
    strategy_to_sids: dict[str, list[str]] = {}
    for sid, info in (agg or {}).items():
        for strat_key in (info.get("details") or {}).keys():
            strategy_to_sids.setdefault(strat_key, []).append(sid)
    consensus_map = compute_strategy_consensus(strategy_to_sids)
    enriched = df.copy()
    enriched["consensus"] = enriched["stock_id"].map(
        lambda sid: consensus_map.get(str(sid))
    )
    return enriched


def _row_has_consensus(row: dict, min_count: int = 2) -> bool:
    """判斷某 row 是否被 ≥ min_count 個策略共識命中。

    優先看 row['consensus']['strategy_count'](新),沒值就 fallback 看
    row['matched_strategies'] 的 length(legacy)。讓「⭐ 只顯示 2+ 策略
    共識」filter 對未 enrich 的 caller 也仍能 graceful 運作。
    """
    cons = row.get("consensus") if isinstance(row, dict) else None
    if isinstance(cons, dict):
        try:
            return int(cons.get("strategy_count", 0)) >= min_count
        except (TypeError, ValueError):
            pass
    return len(row.get("matched_strategies") or []) >= min_count


def _per_strategy_threshold_for_pick(matched: list[str]) -> float | None:
    """從 STRATEGY_ML_THRESHOLDS 找該 pick 適用的最嚴格(最高)門檻。

    一張 pick 同時命中多個策略時,取**最高的** threshold(最嚴格)— 避免
    pick 因為命中其他寬鬆策略就被放過。命中策略全部不在 dict → 回 None
    (該 pick 不過濾)。
    """
    from src.strategies import STRATEGY_ML_THRESHOLDS
    if not matched:
        return None
    thresholds = [
        STRATEGY_ML_THRESHOLDS[s]
        for s in matched
        if STRATEGY_ML_THRESHOLDS.get(s) is not None
    ]
    if not thresholds:
        return None
    return max(thresholds)


def _routing_strategy_for_pick(matched: list[str]) -> str | None:
    """Stage 2B inference 路由 — 取「最嚴格 threshold」的 strategy_name(用來
    決定該 pick 的 ml_prob 走哪個 per-strategy model)。

    沒任何 matched strategy 在 STRATEGY_ML_THRESHOLDS 內 → 回 None
    (caller 走通用 model fallback)。跟 _per_strategy_threshold_for_pick
    成對 — 一個回 threshold 值用來過濾,一個回 strategy_name 用來載 model,
    確保 filter decision 跟 prob 來源同 model。
    """
    from src.strategies import STRATEGY_ML_THRESHOLDS
    if not matched:
        return None
    candidates = [
        (s, STRATEGY_ML_THRESHOLDS[s])
        for s in matched
        if STRATEGY_ML_THRESHOLDS.get(s) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda kv: kv[1])[0]


@st.cache_resource(show_spinner=False)
def _get_strategy_ml_model(strategy_name: str):
    """lazy + cache load per-strategy model — 跟 _get_ml_model_for_enrich 同 pattern。

    每次 sticky-submit rerun 命中 streamlit cache,不重 joblib.load。檔不存在
    或 load 失敗 → 回 None,caller 用 predict_for_strategy 自動 fallback 到
    通用模型。
    """
    try:
        from src.ml_predictor import load_strategy_model
        return load_strategy_model(strategy_name)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ML/enrich] _get_strategy_ml_model({strategy_name}) exception:"
            f"{type(e).__name__}: {e}",
            flush=True,
        )
        return None


@st.cache_resource(show_spinner=False)
def _get_strategy_ml_calibrator(strategy_name: str):
    """cache load per-strategy calibrator;不存在 → None(caller fallback raw prob)。"""
    try:
        from src.ml_predictor import load_strategy_calibrator
        return load_strategy_calibrator(strategy_name)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ML/enrich] _get_strategy_ml_calibrator({strategy_name}) exception:"
            f"{type(e).__name__}: {e}",
            flush=True,
        )
        return None


@st.cache_resource(show_spinner=False)
def _get_short_pick_calibrator():
    """cache load 通用 short_pick calibrator;不存在 → None。"""
    try:
        from src.ml_predictor import load_short_pick_calibrator
        return load_short_pick_calibrator()
    except Exception as e:  # noqa: BLE001
        print(
            f"[ML/enrich] _get_short_pick_calibrator exception:"
            f"{type(e).__name__}: {e}",
            flush=True,
        )
        return None


def _apply_confidence_filter(rows: list[dict]) -> tuple[list[dict], int]:
    """套用 picks 過濾鏈 — confluence(共識)+ confidence(ML 機率)雙層。

    流程:
    1. confluence(若 toggle on)— 過濾掉命中策略數 < N 的 picks
    2. confidence(若 toggle on)— 套 per-strategy ML 門檻

    回 (filtered, total_before):total 永遠是原 rows 長度,讓 caller 顯
    「N/M 檔」caption 反映過濾前後對比。
    """
    total = len(rows)
    if not rows:
        return [], 0

    # 1. Confluence(共識)過濾 — 命中策略數 < N → 砍掉
    if st.session_state.get("confluence_filter_on", False):
        n_required = int(st.session_state.get("confluence_n", 2))
        rows = [
            r for r in rows
            if len(r.get("matched_strategies") or []) >= n_required
        ]
        if not rows:
            return [], total

    # 1b. 跨策略共識 quick filter(可獨立 on/off)— 只留 strategy_count ≥ 2 的
    # picks。consensus 欄未 enrich(legacy 路徑)時 fallback 看 matched_strategies。
    if st.session_state.get("consensus_only_on", False):
        rows = [r for r in rows if _row_has_consensus(r, min_count=2)]
        if not rows:
            return [], total

    # 2. Confidence(ML 機率)per-strategy 過濾
    if not st.session_state.get("high_confidence_mode", True):
        return rows, total

    filtered: list[dict] = []
    for r in rows:
        matched = r.get("matched_strategies") or []
        thr = _per_strategy_threshold_for_pick(matched)
        if thr is None:
            # 該 pick 命中策略全 None(無 threshold)→ 不過濾
            filtered.append(r)
            continue
        prob = r.get("ml_prob")
        if prob is None:
            continue
        try:
            p = float(prob)
            if p != p:  # NaN
                continue
        except (TypeError, ValueError):
            continue
        if p >= thr:
            filtered.append(r)
    return filtered, total


def _enrich_df_with_ml_prob(
    df: "pd.DataFrame",
    trade_date: str,
    agg: dict[str, dict] | None = None,
) -> "pd.DataFrame":
    """加 'ml_prob' 欄到 agg DataFrame。

    Stage 2B 起,若給 agg → 走 per-strategy model 路由(每 pick 用其最嚴格
    threshold strategy 對應的 model;沒就 fallback 到通用)。沒給 agg →
    沿用 Stage 1 全 picks 走通用 model 的舊路徑。

    沒任何 model 載入 / 個別 pick 資料不足 → 該行 ml_prob = None。重複呼叫
    安全:若 df 已經有 ml_prob 欄(可能來自 daily_picks 預跑),原欄保留
    不重算。
    """
    if df is None or df.empty:
        return df
    # 已 enrich 過(daily_picks 預跑帶 ml_prob)→ 不重算
    if "ml_prob" in df.columns and df["ml_prob"].notna().any():
        return df

    general_model = _get_ml_model_for_enrich()

    # 沒 agg → 退舊路徑:全 picks 走通用 model
    if agg is None:
        if general_model is None:
            enriched = df.copy()
            enriched["ml_prob"] = None
            return enriched
        from src.ml_predictor import predict_batch
        sids = df["stock_id"].tolist()
        general_calibrator = _get_short_pick_calibrator()
        try:
            probs = predict_batch(
                general_model, sids, trade_date,
                calibrator=general_calibrator,
            )
        except Exception as e:  # noqa: BLE001
            print(
                f"[ML/enrich] predict_batch 失敗:{type(e).__name__}: {e}",
                flush=True,
            )
            probs = {}
        enriched = df.copy()
        enriched["ml_prob"] = enriched["stock_id"].map(probs)
        return enriched

    # 有 agg → per-strategy 路由
    enriched = df.copy()
    enriched["_chosen_strategy"] = enriched["stock_id"].map(
        lambda sid: _routing_strategy_for_pick(_matched_strategies_for_sid(agg, sid))
    )

    from src.ml_predictor import predict_for_strategy
    general_calibrator = _get_short_pick_calibrator()
    all_probs: dict[str, float | None] = {}
    for chosen, group_df in enriched.groupby(
        "_chosen_strategy", dropna=False, sort=False,
    ):
        sids = group_df["stock_id"].tolist()
        if pd.isna(chosen) or chosen is None:
            chosen_name = None
            sm = None
            sc = None
        else:
            chosen_name = str(chosen)
            sm = _get_strategy_ml_model(chosen_name)
            sc = _get_strategy_ml_calibrator(chosen_name)
        try:
            probs = predict_for_strategy(
                strategy_name=chosen_name,
                stock_ids=sids,
                target_date=trade_date,
                fallback_model=general_model,
                strategy_model=sm,
                strategy_calibrator=sc,
                fallback_calibrator=general_calibrator,
            )
        except Exception as e:  # noqa: BLE001
            print(
                f"[ML/enrich] predict_for_strategy({chosen_name}) 失敗:"
                f"{type(e).__name__}: {e}",
                flush=True,
            )
            probs = {sid: None for sid in sids}
        all_probs.update(probs)

    enriched["ml_prob"] = enriched["stock_id"].map(all_probs)
    enriched = enriched.drop(columns=["_chosen_strategy"])
    return enriched


def _enrich_df_with_win_rate(
    df: "pd.DataFrame", agg: dict[str, dict],
) -> "pd.DataFrame":
    """加 'win_rate' 欄到 agg DataFrame — 每張 pick 的 win_rate 是該檔命中
    各 strategy 的 win_rate **算術平均**(不含 max,避免 cherry-pick 高分)。

    Strategy → win_rate 從 db.load_latest_strategy_backtest 拿(每 strategy
    取 MAX(period_end))。某 strategy 沒回測資料 → 從平均裡剔除;全 N 個
    都沒資料 → win_rate = None(卡片渲染勝率欄會顯「—」)。

    回傳 DataFrame copy(不就地改 input df)。
    """
    rates = db.load_latest_strategy_backtest()

    def _avg_for_sid(sid: str):
        info = agg.get(sid, {}) if agg else {}
        strategy_keys = list((info.get("details") or {}).keys())
        valid = [rates[s] for s in strategy_keys if s in rates]
        if not valid:
            return None
        return sum(valid) / len(valid)

    enriched = df.copy()
    enriched["win_rate"] = enriched["stock_id"].map(_avg_for_sid)
    return enriched


def _set_session_flag(key: str) -> None:
    """on_click callback — 設 session_state flag(避免 lambda + late-binding)。"""
    st.session_state[key] = True


def _load_snapshot_if_needed() -> None:
    """Streamlit cloud startup 時 preload snapshot CSV 進 SQLite。

    解決 Streamlit Cloud IP 被 TWSE OpenAPI 擋的問題,改用每週六 GitHub Actions
    自動抓資料 commit CSV → Cloud 容器 git pull 時自動拿到 → app 啟動時讀進 SQLite。

    **Guard 用 st.session_state**(原本 module-level global `_snapshot_loaded`
    每次 streamlit script rerun 都被 reset 回 False — script 是 top-to-bottom
    重執行,不是 module reload)。session_state 才能跨 rerun persist。
    每位 user 第一輪 rerun 跑一次,後續免錢。

    實際 CSV preload 邏輯抽到 db.preload_snapshots,給 GitHub Actions workflow
    runner 也能 reuse(daily_fetch / daily_notify 入口 script 開頭呼叫,避免
    fresh container 沒走 streamlit boot path 導致 SQLite 空)。
    """
    if st.session_state.get(_BOOT_DONE_KEY):
        return

    db.preload_snapshots()  # 6 個 CSV(stocks / metrics / fin / prices / inst / taiex)

    # 灌 watchlist(避免雲端 reboot 後 user 關注清單丟光)
    # 走 safe_boot_load:會優先嘗試 watchlist-sync 遠端,任何錯誤(ImportError、
    # 認證失敗、parse error 等)一律 silent fallback 到本機 seed CSV,絕不 raise
    # — boot 路徑禁止 crash。watchlist 是 streamlit 專屬,不在 db.preload_snapshots
    # 裡(workflow runner 不需要 watchlist)。
    from src import watchlist_snapshot
    watchlist_snapshot.safe_boot_load()

    # 灌 trades(P&L 紀錄)— 跟 watchlist 同 pattern:remote-first GitHub →
    # fallback 本機 trades.csv。雲端容器重啟仍能還原使用者新加的交易。
    from src import portfolio_snapshot
    portfolio_snapshot.safe_boot_load()

    # 灌 paper_trades(實測追蹤)— 同 watchlist / trades pattern。
    # **修補(2026-05-08 主公二度回報「實測追蹤又不見」)**:
    # 之前只在 db.preload_snapshots 內呼叫 paper_trades_snapshot.load_from_csv
    # (本機 file-only),但雲端 main 分支沒這檔(paper_trades 走 watchlist-sync
    # 分支),容器 reboot 讀不到 → DB 空 → user 加新一輪 → dump 覆蓋 watchlist-sync
    # → 舊資料 LOST。改 wire safe_boot_load(remote-first GitHub fetch)修這個漏。
    from src import paper_trades_snapshot
    paper_trades_snapshot.safe_boot_load()

    # 灌 analyst_targets(法人目標價)— 雙保險(workflow 已 commit 進 main,但雲端
    # 容器 git pull 之間若有 race,safe_boot_load fetch watchlist-sync 多一層保護)。
    from src import analyst_targets_snapshot
    analyst_targets_snapshot.safe_boot_load()

    st.session_state[_BOOT_DONE_KEY] = True


# === 主程式 ===

def main() -> None:
    _tic("main_total")
    st.set_page_config(
        page_title="個人選股工具",
        page_icon="📈",
        layout="wide",
    )
    _tic("boot_setup")
    _inject_global_css()
    _inject_pwa()
    _load_snapshot_if_needed()
    _toc("boot_setup")

    _tic("sidebar")
    st.sidebar.title("📈 個人選股工具")
    st.sidebar.caption("台股 · 短線 + 長線")

    # 全域「重新載入」按鈕(放最頂端,各頁都看得到)
    if st.sidebar.button(
        "🔄 重新載入頁面",
        use_container_width=True,
        help="清空所有 in-memory 快取重新抓資料(大盤情緒 / TWSE / FinMind cache 都會打破)",
    ):
        clear_all_caches()
        st.toast("✅ 已清空快取,重新載入...", icon="🔄")
        st.rerun()
    _toc("sidebar")

    # 上方水平 tabs 取代 sidebar radio(手機優先)
    # 註:用 segmented_control 而非 st.tabs — st.tabs 會把所有頁渲染,
    # 觸發不必要的選股 + 大盤情緒抓取。segmented_control 走 session_state
    # 單頁路由,行為等同舊版 radio 但放在主區頂端。
    #
    # pending_nav 旗標:程式化跳頁(例 watchlist 點擊行 → 跳「🔍 個股」)
    # 不能直接寫 st.session_state["nav_segmented"]=X(widget instantiate 後 raise
    # StreamlitAPIException)。要在 segmented_control render 「之前」消費旗標,
    # 改 default 即生效,沒有 widget key 衝突。
    if "pending_nav" in st.session_state:
        target = st.session_state.pop("pending_nav")
        st.session_state["active_page"] = target
        # 同步清掉舊的 nav_segmented widget state,讓新一輪 segmented_control
        # 拿 default=target 開新 widget(沒衝突,因為前一輪 widget 已銷毀)
        st.session_state.pop("nav_segmented", None)
    if "active_page" not in st.session_state:
        st.session_state["active_page"] = PAGES[0]  # 預設首頁 Dashboard
    page = st.segmented_control(
        "頁面", PAGES,
        default=st.session_state["active_page"],
        key="nav_segmented",
        label_visibility="collapsed",
    ) or st.session_state["active_page"]
    st.session_state["active_page"] = page

    _tic(f"page_{page}")
    if page == "🏠 首頁":
        _page_dashboard()
    elif page == "🔥 短線":
        _page_short()
    elif page == "💎 長線":
        _page_long()
    elif page == "📈 回測":
        _page_backtest()
    elif page == "🔍 個股":
        _page_stock_query()
    elif page == "📊 個股深度":
        _page_stock_detail()
    elif page == "⭐ 關注":
        _page_watchlist()
    elif page == "🌡️ 市場熱度":
        _page_market_heat()
    elif page == "👥 大戶入場":
        _page_big_buyer()
    elif page == "📊 強者跟蹤":
        _page_strong_follower()
    elif page == "📊 大盤":
        _page_market_sentiment()
    elif page == "💼 交易紀錄":
        _page_trades()
    elif page == "🛡️ 持倉管理":
        _page_position_management()
    elif page == "🚨 警報設定":
        _page_price_alerts()
    elif page == "🧪 實測追蹤":
        _page_paper_tracking()
    elif page == "📊 策略歷史":
        _page_strategy_history()
    elif page == "📈 績效分析":
        _page_performance()
    elif page == "📋 系統結論":
        _page_system_brief()
    elif page == "💬 問軍師":
        _page_ai_assistant()
    elif page == "⚙️ 系統":
        _page_system()
    elif page == "⚙️ 設定":
        _page_settings()
    _toc(f"page_{page}")

    _render_sidebar_update()
    # 高信心過濾 — 跨頁共用,必須在 page render 之後(session_state 已 init)
    _render_high_confidence_sidebar()
    st.sidebar.markdown("---")
    st.sidebar.caption(
        f"市場:{config.DEFAULT_MARKET}　·　"
        f"FinMind:{'有 token' if config.FINMIND_TOKEN else '無 token'}"
    )

    _render_footer()
    _toc("main_total")

    # DEBUG_TIMING=1 時顯示計時 panel(sidebar 底)
    if os.getenv("DEBUG_TIMING") == "1":
        timing = st.session_state.get("_timing", {})
        if timing:
            with st.sidebar.expander("⏱️ Timing(本輪 rerun)", expanded=True):
                # 排序由大到小
                items = sorted(timing.items(), key=lambda kv: -kv[1])
                for k, v in items:
                    st.write(f"`{k}`: {v*1000:.1f}ms")


# === 大盤 regime gating badge(短線 / 長線 / 系統結論共用)===

# regime → badge 顏色(綠 / 黃 / 紅 — mobile-first 顯眼可見)
# 主公拍板 2026-05-15:bear 用紅 / range 用黃 / bull 用綠 / 失敗 fallback 灰
_REGIME_BADGE_COLORS: dict[str, str] = {
    "bull":  "#2ecc71",  # 綠
    "range": "#f1c40f",  # 黃
    "bear":  "#e74c3c",  # 紅
}


def _render_regime_gating_badge(location: str = "page") -> None:
    """渲染 regime gating badge — 短線 / 長線 / 系統結論頁標題下方共用。

    location:
      - "page": 頁面內顯示成 caption + 大色塊 badge(綠/黃/紅,顯眼)
                點 expander 展開「目前推薦最多 N 檔 / 信心 ≥ X」說明

    任何例外(DB / regime_gating import 失敗)→ silent skip,不擋頁面渲染。
    """
    try:
        from src import regime_gating as _rg
        from src import database as _db
        with _db.get_conn() as conn:
            params = _rg.get_regime_gating_params(conn)
    except Exception:  # noqa: BLE001
        return  # 失敗 → silent skip

    regime = params.get("regime", "range")
    caption = params.get("caption", "")
    short_max = params.get("short_pick_max_count", 0)
    long_max = params.get("long_pick_max_count", 0)
    uplift = params.get("confidence_threshold_uplift", 0.0)
    color = _REGIME_BADGE_COLORS.get(regime, "#95a5a6")

    # caption 第一行 (e.g. "📈 大盤多頭") 拿來當 badge 主文字;
    # 後續行(空頭警語)用 st.warning render 凸顯
    cap_lines = (caption or "").split("\n")
    badge_text = cap_lines[0] if cap_lines else regime
    extra_warning = "\n".join(cap_lines[1:]).strip()

    # 大色塊 badge — st.markdown + inline style(mobile-first 大字 + 對比色)
    st.markdown(
        f'<div style="display:inline-block;padding:6px 14px;'
        f'border-radius:6px;background-color:{color};color:white;'
        f'font-weight:600;font-size:0.95rem;margin:4px 0;">'
        f'{badge_text}</div>',
        unsafe_allow_html=True,
    )
    if extra_warning:
        st.warning(extra_warning)

    with st.expander("ℹ️ 大盤 gating 說明", expanded=False):
        st.markdown(
            f"""
- **當前 regime**:`{regime}`
- **短線推薦上限**:{short_max} 檔
- **長線推薦上限**:{long_max} 檔
- **ML 信心門檻 uplift**:+{uplift:.2f}(空頭加嚴,只放最有信心)
- **kill-switch**:env `REGIME_GATING_ENABLED=false` 關掉 gating
            """
        )


# === Verdict banner(首頁 / 系統結論頁共用)===
#
# 設計重點(2026-05-18 主公拍板):
#   - 首頁第一行讓主公看到「今天系統覺得能不能進場」一行,綠 / 黃 / 紅 metric
#     + Top 3 🟢 一覽,不用點四五層。
#   - 系統結論頁同 banner 但展開三段 Top(綠 3 / 黃 5 / 紅 3)。
#   - 雲端 cold load 不能 build_summary live(50 sids × ~200ms),
#     優先讀 data/twse_snapshot/daily_verdict_summary.csv(daily-notify 預跑 + commit),
#     CSV 不存在 / trade_date 對不上 → fallback live build(慢但永遠拿得到結果)。

@st.cache_data(ttl=300, show_spinner=False)
def _get_verdict_summary_cached(trade_date: str) -> dict:
    """讀 daily_verdict_summary.csv(快);沒命中或日期對不上 → live build_summary。

    ttl=300 — 雲端 5 分鐘內 hit 同份 cache,daily 重跑覆寫 CSV 後 5 分鐘內全頁同步。
    """
    from src.verdict_summary import build_summary, load_from_csv

    csv_path = (
        config.PROJECT_ROOT / "data" / "twse_snapshot" / "daily_verdict_summary.csv"
    )
    cached = load_from_csv(csv_path)
    if cached is not None and cached.get("trade_date") == trade_date:
        return cached

    # Fallback:CSV 沒有 / 日期不對 → live 算(慢)
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT p.sid, s.name FROM daily_picks p "
                "LEFT JOIN stocks s ON s.stock_id = p.sid "
                "WHERE p.trade_date=?",
                (trade_date,),
            ).fetchall()
        universe = [{"sid": r["sid"], "name": r["name"]} for r in rows]
    except Exception:  # noqa: BLE001
        universe = []
    # union watchlist
    try:
        wl = db.get_watchlist() or []
        seen = {u["sid"] for u in universe}
        for it in wl:
            sid = str(it.get("stock_id", "")).strip()
            if sid and sid not in seen:
                universe.append({"sid": sid, "name": None})
    except Exception:  # noqa: BLE001
        pass

    return build_summary(universe, trade_date)


def _verdict_banner_metrics(summary: dict) -> None:
    """三個 metric 並排 — 首頁 / 系統結論共用。手機自動 stack。"""
    counts = summary.get("counts") or {"green": 0, "yellow": 0, "red": 0}
    col1, col2, col3 = st.columns(3)
    col1.metric("🟢 可進場", counts.get("green", 0))
    col2.metric("🟡 觀望", counts.get("yellow", 0))
    col3.metric("🔴 不進場", counts.get("red", 0))


_BANNER_COLUMNS = ["sid", "name", "score", "main_reason"]
_BANNER_COL_LABEL = {
    "sid": "代號",
    "name": "名稱",
    "score": "信心",
    "main_reason": "主因",
}


def _banner_df(items: list[dict]) -> "pd.DataFrame":
    if not items:
        return pd.DataFrame(columns=list(_BANNER_COL_LABEL.values()))
    df = pd.DataFrame(items)
    # 只留 banner 用欄位 + 重命名為中文
    keep = [c for c in _BANNER_COLUMNS if c in df.columns]
    df = df[keep].rename(columns=_BANNER_COL_LABEL)
    return df


def _render_verdict_banner_compact(summary: dict) -> None:
    """首頁精簡 banner — 三 metric + Top 3 🟢 一表。"""
    _verdict_banner_metrics(summary)
    top_green = summary.get("top_green") or []
    if top_green:
        st.caption("🟢 可進場 Top 3")
        st.dataframe(
            _banner_df(top_green),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("🟢 今日無可進場標的 — 等下一輪訊號")


def _render_verdict_banner_full(summary: dict) -> None:
    """系統結論完整 banner — 三 metric + 三段 Top 表(綠 3 / 黃 5 / 紅 3)。"""
    _verdict_banner_metrics(summary)

    top_green = summary.get("top_green") or []
    top_yellow = summary.get("top_yellow") or []
    top_red = summary.get("top_red") or []

    st.subheader("🟢 可進場 Top 3")
    if top_green:
        st.dataframe(
            _banner_df(top_green),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("今日無可進場標的")

    st.subheader("🟡 觀望 Top 5")
    if top_yellow:
        st.dataframe(
            _banner_df(top_yellow),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("今日無觀望標的")

    st.subheader("🔴 不進場警示 Top 3")
    if top_red:
        st.dataframe(
            _banner_df(top_red),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("今日無不進場警示")


# === 首頁 Dashboard ===

def _page_dashboard() -> None:
    """5 區塊速覽:軍師判讀 banner / 大盤 / 今日推薦 Top 3 / 關注 Top 3 / 系統狀態。"""
    st.header("🏠 今日速覽")
    st.caption("一頁看完台股最新狀況。詳細分析請切上方 tabs。")

    # === 0. 軍師判讀 banner — 第一行先給結論(2026-05-18 主公拍板)===
    trade_date = _get_latest_data_date()
    if trade_date:
        try:
            summary = _get_verdict_summary_cached(trade_date)
            _render_verdict_banner_compact(summary)
        except Exception as e:  # noqa: BLE001
            st.caption(f"📊 軍師判讀 banner 暫不可用:{type(e).__name__}")

    # === 1. 大盤速覽 ===
    st.markdown("### 📊 大盤速覽")
    try:
        taiex_df = fetch_taiex(days=10)
    except Exception:  # noqa: BLE001
        taiex_df = pd.DataFrame()
    if taiex_df.empty:
        st.caption("加權指數抓取失敗(可能 FinMind 限流),稍後再試。")
    else:
        last_close = float(taiex_df["close"].iloc[-1])
        prev_close = (
            float(taiex_df["close"].iloc[-2])
            if len(taiex_df) >= 2 else last_close
        )
        delta = last_close - prev_close
        delta_pct = delta / prev_close * 100 if prev_close else 0
        with st.container(border=True):
            # 台股慣例 — 漲紅跌綠,用 inverse 反轉 streamlit 預設(預設正綠)
            st.metric(
                f"加權指數 ({taiex_df['date'].iloc[-1]})",
                f"{last_close:,.2f}",
                f"{delta:+.2f} ({delta_pct:+.2f}%)",
                delta_color="inverse",
            )

    # === 2. 今日短線推薦 Top 3(lazy — 預設不自動跑全市場)===
    st.markdown("### 🔥 今日短線推薦 (Top 3)")
    # **Cold load 主痛點修正**:原本一進首頁就自動跑 run_all_strategies 在
    # ~2000 檔全市場,profile 顯示 11 秒。改 lazy:user 點按鈕才跑,且結果
    # 走 _run_all_strategies_cached(同 day 內共享 cache,跨頁也 hit)。
    eligible_sids = pure_stock_universe(min_history=20)
    if not eligible_sids:
        st.caption(
            "📭 cache 內沒有任何個股累積 20 天歷史。"
            "請按 sidebar『⏳ 一次性回補 90 日歷史』。"
        )
    else:
        target_date = _get_default_screen_date()
        today_iso = target_date.isoformat()

        # === 產業 pre-filter (universe-level) ===
        # 標籤先選 → 點按鈕 → strategy 只跑這些產業的 sids。空 selected = 全市場。
        # pills 上方主流 Top 15、下方 expander 冷門類。
        dash_mainstream = st.pills(
            "產業",
            MAINSTREAM_INDUSTRIES,
            selection_mode="multi",
            default=[],
            key="dash_mainstream",
            help="先選產業 tag、再按下方按鈕,strategy 只跑這些產業。空白 = 全市場。",
        )
        with st.expander("➕ 其他產業"):
            with db.get_conn() as _conn:
                _all_inds = get_all_canonical_industries(_conn)
            dash_other = st.pills(
                "其他",
                get_other_industries(_all_inds),
                selection_mode="multi",
                default=[],
                key="dash_other",
                label_visibility="collapsed",
            )
        selected_industries = list(dash_mainstream or []) + list(dash_other or [])

        # session_state flag 控制是否啟動掃描 — streamlit cache_data 沒有
        # cache-only lookup API,只能用 flag 自己擋。flag 設過後,後續 rerun
        # 就走 _run_all_strategies_cached(同 universe / day cache hit 0 SQL)。
        loaded_key = "_dashboard_picks_loaded"
        if not st.session_state.get(loaded_key):
            st.info(
                f"📭 點下方按鈕載入今日 Top 3 推薦"
                f"(掃描 ~{len(eligible_sids)} 檔,首次需 ~10 秒)"
            )
            st.button(
                "🚀 載入今日推薦",
                key="dashboard_load_picks",
                on_click=_set_session_flag,
                args=(loaded_key,),
                use_container_width=True,
            )
        else:
            # 套產業 pre-filter:從 eligible_sids 砍出 strategy 真正要跑的 sids。
            if selected_industries:
                with db.get_conn() as _conn:
                    filtered_sids = filter_sids_by_industry(
                        list(eligible_sids), selected_industries, _conn,
                    )
            else:
                filtered_sids = list(eligible_sids)

            if not filtered_sids:
                st.info("此產業組合下無 picks(filter 後 universe 為空)。")
            else:
                with st.spinner(
                    f"掃描 {len(filtered_sids)} 檔(20+ 天 / 純股票)..."
                ):
                    try:
                        agg = _run_all_strategies_cached(
                            today_iso,
                            tuple(filtered_sids),
                            None,
                            None,
                        )
                        # 先 enrich 全部(不限 head 3),才能對 ML 過濾後再取 Top 3
                        df_full = _enrich_df_with_consensus(
                            _enrich_df_with_matched_strategies(
                                _enrich_df_with_ml_prob(
                                    _enrich_df_with_win_rate(
                                        aggregated_to_dataframe(agg), agg,
                                    ),
                                    trade_date=today_iso,
                                    agg=agg,
                                ),
                                agg,
                            ),
                            agg,
                        )
                        rows_full = df_full.to_dict("records")
                        rows_filtered, total = _apply_confidence_filter(rows_full)
                    except Exception as e:  # noqa: BLE001
                        st.caption(f"掃描失敗:{type(e).__name__}: {e}")
                        rows_filtered = []
                        rows_full = []
                        total = 0
                df_picks = pd.DataFrame(rows_filtered[:3])
                if df_picks.empty:
                    if selected_industries:
                        st.info("此產業組合下無 picks,試試換產業。")
                    elif total > 0:
                        st.caption(
                            f"📭 高信心過濾後無入選(原 {total} 檔,可調 sidebar 門檻)。"
                        )
                    else:
                        st.caption("📭 今日無入選。可切「🔥 短線」放寬參數試試。")
                else:
                    if st.session_state.get("high_confidence_mode", True):
                        st.caption(f"🎯 高信心:{len(df_picks)}/{total} 檔顯示")
                    render_picks_cards(df_picks.to_dict("records"))

    # === 3. 我的關注 Top 3 ===
    st.markdown("### ⭐ 我的關注 (前 3 檔)")
    items = db.get_watchlist()
    if not items:
        st.caption(
            "目前沒有關注的個股。請到「🔍 個股」頁查詢後按 ☆ 圖示加入。"
        )
    else:
        wl_cards: list[dict] = []
        for it in items[:3]:
            sid = it["stock_id"]
            with db.get_conn() as conn:
                meta = conn.execute(
                    "SELECT name FROM stocks WHERE stock_id=?", (sid,),
                ).fetchone()
                recent = conn.execute(
                    "SELECT date, close FROM daily_prices "
                    "WHERE stock_id=? ORDER BY date DESC LIMIT 2",
                    (sid,),
                ).fetchall()
            name = meta["name"] if meta and meta["name"] else "—"
            close = (
                float(recent[0]["close"])
                if recent and recent[0]["close"] else None
            )
            prev = (
                float(recent[1]["close"])
                if len(recent) > 1 and recent[1]["close"] else None
            )
            change_pct = (
                (close - prev) / prev * 100
                if (close and prev) else None
            )
            wl_cards.append({
                "stock_id": sid, "name": name,
                "close": close, "change_pct": change_pct,
            })
        render_picks_cards(
            wl_cards, show_signal=False, show_targets=False, show_change=True,
        )

    # === 4. 系統狀態 ===
    st.markdown("### 📅 系統狀態")
    health = db.cache_health_summary()
    b = health["buckets"]
    latest_date = _get_latest_data_date() or "(無)"
    cols = st.columns(2)
    with cols[0]:
        with st.container(border=True):
            st.metric("daily_prices 最新日", latest_date)
            st.caption(
                f"全 TW 股 {health['total_stocks']} 檔 / "
                f"有價量 {health['with_prices']} 檔"
            )
    with cols[1]:
        with st.container(border=True):
            st.metric(
                "可跑短線策略個股 (20+ 天)",
                f"{b['60+'] + b['20-59']}",
                delta=(
                    f"其中 {b['60+']} 檔可跑全策略 (60+ 天 = MA60)"
                    if b["60+"] else "(MA60 多頭排列暫不可用)"
                ),
                delta_color="off",
            )
            st.caption(
                f"分布:60+ {b['60+']}・20-59 {b['20-59']}"
                f"・14-19 {b['14-19']}・<14 {b['<14']}"
            )
            if b["60+"] + b["20-59"] < 100:
                st.caption(
                    "⏳ 多數歷史不足,請按 sidebar『⏳ 一次性回補 90 日歷史』"
                )


# === 立即推播按鈕(短線頁用) ===

_PUSH_DEBOUNCE_SECS = 30


def _render_manual_push_button(picks_df: "pd.DataFrame") -> None:
    """短線頁「立即推播當前推薦」按鈕。

    防呆:30 秒內按一次後 disable,避免使用者重複狂按 spam Telegram/Discord。
    最多推前 7 檔(format_manual_picks 內部截斷),footer 標註手動推播來源。
    """
    import time as _t
    from src.notifier import notify_manual_picks

    if picks_df is None or picks_df.empty:
        return

    last_ts = st.session_state.get("manual_push_last_ts")
    now = _t.time()
    remaining = (
        int(_PUSH_DEBOUNCE_SECS - (now - last_ts))
        if last_ts is not None else 0
    )
    debounce_active = remaining > 0

    cols = st.columns([2, 5])
    label = (
        f"⏳ 推播冷卻中 ({remaining}s)"
        if debounce_active else "📤 立即推播當前推薦"
    )
    clicked = cols[0].button(
        label,
        key="manual_push_btn",
        use_container_width=True,
        disabled=debounce_active,
        help="把當前頁面的推薦結果(限前 7 檔)即時推到 Telegram + Discord",
    )
    cols[1].caption(
        "上限 7 檔 / 30 秒內只能推一次 / footer 會標『雲端 App 手動推播』。"
    )
    if clicked:
        try:
            results = notify_manual_picks(
                picks_df, date=date.today().isoformat(), limit=7,
            )
        except Exception as ex:  # noqa: BLE001
            st.toast(f"推播失敗:{ex}", icon="❌")
            return
        if not results:
            st.toast(
                "未設定 TELEGRAM_BOT_TOKEN / DISCORD_WEBHOOK_URL,沒推任何通道",
                icon="⚠️",
            )
            return
        sent = [k for k, v in results.items() if v]
        failed = [k for k, v in results.items() if not v]
        msg_parts = []
        if sent:
            msg_parts.append("✅ " + ", ".join(sent))
        if failed:
            msg_parts.append("❌ " + ", ".join(failed))
        st.toast("推播完成 — " + " / ".join(msg_parts), icon="📤")
        st.session_state["manual_push_last_ts"] = _t.time()
        st.rerun()


# === 短線推薦頁 ===

# 短線進階參數 6 個 widget 的 default 值(session_state key → default)
# 用 dict 而非 tuple,callback 才能直接設預設值(不是 pop)。
# Pattern:widget key-only(沒 value= 參數),session_state 是 single source of
# truth — 既避開 streamlit「同時帶 value 跟 key」的 race(雲端 click 後不刷新),
# 又讓 callback 一次寫入就保證 widget 下次 render 拿到正確值。
_SHORT_PARAM_DEFAULTS: dict[str, float | int] = {
    "short_vol_mult": float(DEFAULT_SHORT_PARAMS["volume_multiplier"]),
    "short_kd_low": float(DEFAULT_SHORT_PARAMS["kd_threshold_low"]),
    "short_inst_days": int(DEFAULT_SHORT_PARAMS["inst_buy_days"]),
    "short_bias_low": float(DEFAULT_BIAS_PARAMS["bias_low"]),
    "short_bias_high": float(DEFAULT_BIAS_PARAMS["bias_high"]),
    "short_vol_ratio": float(DEFAULT_BIAS_PARAMS["vol_ratio_min"]),
    # commit 2 新增 2 個 sliders(避開 vol_ratio_min namespace 衝突,只暴露各
    # 策略獨有 threshold;MACD 黃金沒可調 threshold 不暴露)
    "short_squeeze_pct_max": float(DEFAULT_SQUEEZE_PARAMS["squeeze_pct_max"]),
    "short_consensus_days": int(DEFAULT_INST_CONSENSUS_PARAMS["consecutive_days"]),
    # commit 3 新增 5 個 strategies 各自獨有 thresholds
    "short_bb_lookback": int(DEFAULT_BB_REBOUND_PARAMS["bb_touch_lookback"]),
    "short_rsi_oversold": float(DEFAULT_RSI_RECOVERY_PARAMS["rsi_oversold"]),
    "short_rsi_recovered": float(DEFAULT_RSI_RECOVERY_PARAMS["rsi_recovered"]),
    "short_silent_pct_max": float(DEFAULT_INST_SILENT_PARAMS["pct_change_max"]),
    "short_silent_bb_pos_max": float(DEFAULT_INST_SILENT_PARAMS["bb_position_max"]),
    "short_vbo_vol_ratio": float(DEFAULT_VOL_BREAKOUT_PARAMS["vbo_vol_ratio_min"]),
    "short_vbo_highest_lookback": int(DEFAULT_VOL_BREAKOUT_PARAMS["highest_lookback"]),
    "short_gap_pct_min": float(DEFAULT_GAP_UP_PARAMS["gap_pct_min"]),
    "short_gap_vol_ratio": float(DEFAULT_GAP_UP_PARAMS["gap_vol_ratio_min"]),
}


# 策略 → 分類(短線頁 tabs 用)。同一策略只屬一類。
# - 趨勢:多頭排列、MACD 黃金交叉、均線糾結突破(都是趨勢延續/啟動類訊號)
# - 反轉:乖離收斂(超賣後修正)/ BB 下軌反彈 / RSI 回升
# - 籌碼:三大法人連買 / 默默吸貨 / 法人反轉
# - 動能:量價KD / 量爆突破 / 跳空缺口
# Phase 1 加 3 類:
# - 基本面:EPS 加速 / 營收加速
# - 殖利率:高殖利率穩健
# - 大盤:獨立行情(個股逆 TAIEX)
_STRATEGY_CATEGORY: dict[str, str] = {
    "volume_kd": "動能",
    "ma_alignment": "趨勢",
    "bias_convergence": "反轉",
    "macd_golden": "趨勢",
    "ma_squeeze_breakout": "趨勢",
    "inst_consensus": "籌碼",
    # commit 3
    "bb_lower_rebound": "反轉",
    "rsi_recovery": "反轉",
    "inst_silent_accum": "籌碼",
    "volume_breakout": "動能",
    "gap_up": "動能",
    # Phase 1
    "eps_acceleration": "基本面",
    "high_yield_stable": "殖利率",
    "inst_oversold_reversal": "籌碼",
    "taiex_alpha": "大盤",
    "revenue_acceleration": "基本面",
    # 籌碼:千張戶進場(TDCC 千張大戶週快照)
    "big_holder_inflow": "籌碼",
}


# tabs 顏色(CSS injection 用)。順序 = tab 顯示順序(全部 → 4 短線類 → 3 Phase 1 類)。
_TAB_COLORS: dict[str, str] = {
    "全部": "#888888",
    "趨勢": "#185FA5",
    "反轉": "#1D9E75",
    "籌碼": "#7F77DD",
    "動能": "#BA7517",
    "基本面": "#A53A3A",
    "殖利率": "#3DA571",
    "大盤": "#5B5B5B",
}


def _filter_agg_by_category(
    agg: dict[str, dict], category: str,
) -> dict[str, dict]:
    """從 agg(run_all_strategies 結果)挑出至少有一個策略屬於 category 的 picks。
    回傳同 schema 的新 dict — 直接餵給 aggregated_to_dataframe。
    """
    out: dict[str, dict] = {}
    for sid, info in agg.items():
        keys = info.get("details", {}).keys()
        if any(_STRATEGY_CATEGORY.get(k) == category for k in keys):
            out[sid] = info
    return out


def _inject_short_tab_css() -> None:
    """注入短線頁 tabs 的顏色 CSS — 「選中」tab 的下方底線吃顏色,
    讓使用者用顏色快速辨認當前在哪一類策略 tab 上。
    Streamlit 的 tab nth-child 跟 _TAB_COLORS dict 順序對齊,
    新增 / 刪除 category 只改 _TAB_COLORS 即可。
    """
    rules = []
    for i, color in enumerate(_TAB_COLORS.values(), start=1):
        rules.append(
            f'div[data-testid="stTabs"] button[role="tab"]'
            f':nth-child({i})[aria-selected="true"] {{ '
            f'color: {color} !important; '
            f'border-bottom: 3px solid {color} !important; }}'
        )
    st.markdown(f"<style>{''.join(rules)}</style>", unsafe_allow_html=True)


def _reset_short_params() -> None:
    """重設預設值按鈕的 on_click callback。
    直接寫入 session_state[key] = default(不是 pop),配合 widget key-only
    pattern → widget 下次 render 從 session_state 拿到 default 值,UI 立即
    刷新(原本 pop + value= 的 pattern 在雲端某些版本不刷新)。
    """
    for k, v in _SHORT_PARAM_DEFAULTS.items():
        st.session_state[k] = v


def _page_short() -> None:
    st.header("🔥 短線推薦")

    # 大盤 regime gating badge — 標題下方顯示綠/黃/紅 badge + 警語(空頭時凸顯)
    _render_regime_gating_badge(location="page")

    # 大盤環境感知:讀 TAIEX MA20/MA60 算 regime → 預設只開該 regime 適合的策略類別
    from src.market_regime import compute_regime, filter_strategies_by_regime
    from src.strategies import STRATEGY_CATEGORY, STRATEGY_REGIME_FILTER
    regime_info = compute_regime()
    regime = regime_info["regime"]

    # 頁頂 + sidebar 都顯示 regime 徽章
    badge_text = f"{regime_info['badge_emoji']} {regime_info['label']}"
    if regime_info["close"] is not None:
        st.caption(
            f"**大盤環境**:{badge_text}  "
            f"(TAIEX 收 {regime_info['close']:.0f} / "
            f"MA20 {regime_info['ma20']:.0f} / MA60 {regime_info['ma60']:.0f})"
        )
    else:
        st.caption(f"**大盤環境**:{badge_text}(資料不足 60 天,全策略開)")

    st.sidebar.markdown(f"### 大盤 {badge_text}")
    regime_override = st.sidebar.checkbox(
        "🌐 無視大盤環境(全策略開)",
        value=False,
        help="勾選 = 不依 regime 篩策略類別,所有 16 套都開(主公手動 override)",
    )

    # sidebar 多策略選擇
    st.sidebar.markdown("### 啟用策略")
    label_to_key = {v: k for k, v in STRATEGY_LABELS.items()}
    # 算當前 regime 該預設開哪些策略(override 或 unknown 則全開)
    if regime_override or regime == "unknown":
        regime_default_keys = list(STRATEGY_LABELS.keys())
    else:
        regime_default_keys = filter_strategies_by_regime(
            list(STRATEGY_LABELS.keys()),
            regime,
            STRATEGY_CATEGORY,
            STRATEGY_REGIME_FILTER,
        )
    regime_default_labels = [STRATEGY_LABELS[k] for k in regime_default_keys]

    selected_labels = st.sidebar.multiselect(
        "策略",
        list(STRATEGY_LABELS.values()),
        default=regime_default_labels,
        help=(
            "多選 = 多策略並行,信號越多 = 越多策略同時看好。"
            f"預設依大盤 {regime_info['label']} 過濾類別,可自行勾選或勾上方 override 全開。"
        ),
    )
    enabled_keys = [label_to_key[lbl] for lbl in selected_labels]

    # sidebar 參數區(預設收起,進階使用者再展開)
    # 三策略 thresholds 共用一個 expander。值跨 rerun 透過 session_state 保留;
    # 「執行選股」才會套用,避免每次拖 slider 都觸發 SQL 全掃。
    # 進 expander 前先 setdefault init session_state — widget 用 key-only
    # pattern(沒 value= 參數),需要保證 key 已在 session_state 才能 render。
    for _k, _v in _SHORT_PARAM_DEFAULTS.items():
        st.session_state.setdefault(_k, _v)

    with st.sidebar.expander("⚙️ 進階參數", expanded=False):
        st.markdown("**策略 1:量價KD**")
        vol_mult = st.number_input(
            "均量倍數", min_value=1.0, max_value=5.0, step=0.1,
            key="short_vol_mult",
            help="當日量 > 過去 5 日均量 × 此倍數",
        )
        kd_low = st.number_input(
            "KD 門檻 K_low", min_value=0.0, max_value=80.0, step=5.0,
            key="short_kd_low",
            help="K 黃金交叉 D 後,K 至少要超過此值才入選",
        )
        inst_days = st.number_input(
            "法人連續買超天數", min_value=1, max_value=10, step=1,
            key="short_inst_days",
        )

        st.markdown("**策略 2:多頭排列**")
        st.caption(
            "MA5 > MA10 > MA20 > MA60 條件式判斷,**無可調 threshold**"
        )

        st.markdown("**策略 3:乖離率收斂**")
        bias_low = st.slider(
            "乖離下限 (%)", min_value=-15.0, max_value=0.0, step=0.5,
            key="short_bias_low",
            help="20 日乖離率 (close vs MA20) 必須 ≥ 此下限",
        )
        bias_high = st.slider(
            "乖離上限 (%)", min_value=-2.0, max_value=10.0, step=0.5,
            key="short_bias_high",
            help="20 日乖離率必須 ≤ 此上限(過熱就排除)",
        )
        vol_ratio_min = st.slider(
            "量比門檻", min_value=0.5, max_value=3.0, step=0.1,
            key="short_vol_ratio",
            help="今日量 / 5 日均量 ≥ 此值",
        )

        st.markdown("**策略 4:MACD 黃金交叉**")
        st.caption(
            "初升段定義:DIF<0 + 黃金交叉 + 量比 ≥ 1.0,**無可調 threshold**"
        )

        st.markdown("**策略 5:均線糾結突破**")
        squeeze_pct_max = st.slider(
            "MA spread % 上限", min_value=0.5, max_value=5.0, step=0.5,
            key="short_squeeze_pct_max",
            help="過去 5 日 MA5/MA20/MA60 的 spread % 都要 ≤ 此值才算「糾結」",
        )

        st.markdown("**策略 6:三大法人連買**")
        consensus_days = st.slider(
            "連續同買天數", min_value=2, max_value=10, step=1,
            key="short_consensus_days",
            help="外資 + 投信 + 自營商三家 net 都 > 0 連續 N 天",
        )

        st.markdown("**策略 7:布林下軌反彈**")
        bb_lookback = st.slider(
            "下軌觸碰回溯天數", min_value=3, max_value=15, step=1,
            key="short_bb_lookback",
            help="過去 N 日內任一日 close 觸 BB 下軌",
        )

        st.markdown("**策略 8:RSI 回升**")
        rsi_oversold = st.slider(
            "超賣門檻", min_value=20.0, max_value=40.0, step=1.0,
            key="short_rsi_oversold",
            help="14 日內 RSI 須曾低於此值",
        )
        rsi_recovered = st.slider(
            "回升門檻", min_value=45.0, max_value=70.0, step=1.0,
            key="short_rsi_recovered",
            help="今日 RSI 須高於此值",
        )

        st.markdown("**策略 9:主力默默吸貨**")
        silent_pct_max = st.slider(
            "平盤定義 |%|", min_value=0.5, max_value=3.0, step=0.1,
            key="short_silent_pct_max",
            help="今日漲跌幅絕對值 < 此值才算「平盤」",
        )
        silent_bb_pos_max = st.slider(
            "BB 位置 % 上限", min_value=20.0, max_value=70.0, step=5.0,
            key="short_silent_bb_pos_max",
            help="close 在 BB 內位置 %(0=下軌, 100=上軌),須 < 此值",
        )

        st.markdown("**策略 10:量爆突破**")
        vbo_vol_ratio = st.slider(
            "量比門檻 (爆量)", min_value=1.5, max_value=5.0, step=0.1,
            key="short_vbo_vol_ratio",
            help="今日量 / 5 日均量 ≥ 此值",
        )
        vbo_highest_lookback = st.slider(
            "新高回溯天數", min_value=10, max_value=60, step=5,
            key="short_vbo_highest_lookback",
            help="close 須突破近 N 日 max(close)",
        )

        st.markdown("**策略 11:跳空缺口**")
        gap_pct_min = st.slider(
            "跳空門檻 %", min_value=0.5, max_value=5.0, step=0.1,
            key="short_gap_pct_min",
            help="今日 open / 昨日 close − 1 ≥ 此 %",
        )
        gap_vol_ratio = st.slider(
            "量比門檻 (跳空)", min_value=1.0, max_value=3.0, step=0.1,
            key="short_gap_vol_ratio",
        )

        # widget 全 key-only(沒 value=),session_state 是 SoT;callback 直接
        # 寫 session_state[key] = default → widget 下次 render 從 session_state
        # 拿到正確值,UI 立刻刷新。雲端「click 後不刷新」的 race 解掉。
        st.button(
            "🔄 重設預設值",
            use_container_width=True,
            key="short_reset_params",
            on_click=_reset_short_params,
        )
        st.caption("調整後按主畫面「執行選股」生效")

    # cache 健康度(供下方 selectbox + caption 用)
    health = db.cache_health_summary()
    # 20+ 天 = 60+ 桶 + 20-59 桶之和(20 天足夠跑量價KD + 乖離率;
    # ma_alignment 需 MA60 但 backfill 90 calendar days 只能換 ~54 交易日)
    eligible_stocks = health["buckets"]["60+"] + health["buckets"]["20-59"]

    # === 產業 pre-filter (universe-level) ===
    # 標籤先選 → 點「執行選股」→ strategy 只跑這些產業的 sids。空 selected = 全市場。
    page_short_mainstream = st.pills(
        "產業",
        MAINSTREAM_INDUSTRIES,
        selection_mode="multi",
        default=[],
        key="page_short_mainstream",
        help="先選產業 tag、再按「執行選股」,strategy 只跑這些產業。空白 = 全市場。",
    )
    with st.expander("➕ 其他產業"):
        with db.get_conn() as _conn:
            _all_inds = get_all_canonical_industries(_conn)
        page_short_other = st.pills(
            "其他",
            get_other_industries(_all_inds),
            selection_mode="multi",
            default=[],
            key="page_short_other",
            label_visibility="collapsed",
        )
    selected_industries = (
        list(page_short_mainstream or []) + list(page_short_other or [])
    )

    # 上方控制列
    cols = st.columns([2, 3, 1])
    target_date = cols[0].date_input("選股日期", value=_get_default_screen_date())
    universe_options = [
        f"🎯 充足歷史的純股票 ({eligible_stocks} 檔, ≥20 天 / 不含 ETF & 債券)",
        f"📊 充足歷史的股 ({eligible_stocks} 檔, ≥20 天 / 含 ETF & 債券)",
        "全市場 (約 2360 檔, twse + tpex + ETF)",
        "快速:50 檔大型股",
        "我的關注清單",
    ]
    # 預設「🎯 純股票」— 短線交易主軸是個股 K 線,ETF/債券貝塔太雜
    universe_choice = cols[1].selectbox(
        "選股範圍", universe_options, index=0,
        help="預設「🎯 純股票」排除 ETF (代號 00 開頭) / 債券 / 槓桿反向商品。"
             "全市場 / TOP 50 走 GH Actions 每日更新的 SQLite 快取。",
    )
    cols[2].markdown("&nbsp;", unsafe_allow_html=True)
    submit = cols[2].button("執行選股", type="primary", use_container_width=True)

    # cache 健康度 caption
    b = health["buckets"]
    st.caption(
        f"📦 Cache 健康度:總 {health['total_stocks']} 檔 / "
        f"有價量 {health['with_prices']} 檔 — "
        f"60+ 天 {b['60+']}(可跑全策略)・20-59 天 {b['20-59']}・"
        f"14-19 天 {b['14-19']}・<14 天 {b['<14']}"
        + (
            "  ⚠️ 多數個股歷史不足,請按 sidebar『⏳ 一次性回補 90 日歷史』"
            if eligible_stocks < 100 else ""
        )
    )

    # **st.button() 是 edge-triggered**:只在被點的那一輪 rerun 回 True。
    # picks 卡片內的 lazy expander / 載入更多按鈕 → 觸發 rerun → submit
    # 變 False → 短線頁退回「請選擇選股」初始畫面。
    # 修:把 submit 狀態 + 上次參數寫進 session_state,rerun 後可恢復。
    if submit:
        st.session_state["short_submitted"] = True
        st.session_state["short_last_target_date"] = target_date.isoformat()
        st.session_state["short_last_universe"] = universe_choice
    if not st.session_state.get("short_submitted"):
        st.info(
            "選好參數後按「執行選股」。\n\n"
            "全市場 / TOP 50 走 GH Actions 每日更新的 SQLite 快取,秒級回應;"
            "我的關注清單會即時補抓最新資料。"
        )
        return

    import time as _time
    t0 = _time.perf_counter()

    # 取選股範圍
    _tic("short_resolve_universe")
    universe = _resolve_universe(universe_choice)
    _toc("short_resolve_universe")
    if universe is None:
        return

    today_iso = target_date.isoformat()
    start_iso = (target_date - timedelta(days=90)).isoformat()
    failures: list[str] = []
    skipped_prefetch = False

    # 只對「我的關注清單」做即時 prefetch — 全市場 / TOP 50 已由
    # daily_fetch.py (GH Actions cron) 每日更新,逐檔 prefetch 是純浪費。
    if universe_choice == "我的關注清單" and universe:
        progress = st.progress(0.0, text="抓取關注清單最新資料...")
        n = len(universe)
        update_every = max(1, n // 20)  # 最多更新 20 次進度條
        for i, (sid, name) in enumerate(universe):
            if (i + 1) % update_every == 0 or i == n - 1:
                progress.progress(
                    (i + 1) / n,
                    text=f"抓取 {i + 1}/{n}: {sid} {name}",
                )
            try:
                db.upsert_stocks([
                    {"stock_id": sid, "name": name, "market": "TW"},
                ])
                fetch_daily_price(sid, start_iso, today_iso)
                fetch_institutional(sid, start_iso, today_iso)
            except FinMindAPIError as e:
                failures.append(f"{sid}({e})")
            except Exception as e:  # noqa: BLE001
                failures.append(f"{sid}({type(e).__name__}: {e})")
        progress.empty()
    else:
        skipped_prefetch = True

    t1 = _time.perf_counter()

    if failures:
        st.warning(
            f"⚠️ {len(failures)} 檔抓取失敗(可能無 token 被限制):"
            f"{', '.join(failures[:5])}"
            f"{'...' if len(failures) > 5 else ''}"
        )

    # 跑多策略並行(11 套共用一份 params,各自 merge DEFAULT_*_PARAMS,
    # 多餘 key 對該策略無害。共用 key:
    # - vol_ratio_min:bias / macd / squeeze 共用此處 bias 的值(三策略量比期望相近)
    # - 策略 10/11 用獨立 vbo_vol_ratio_min / gap_vol_ratio_min(差異大,獨立暴露)
    params = {
        # 策略 1: volume_kd
        "volume_multiplier": float(vol_mult),
        "kd_threshold_low": float(kd_low),
        "inst_buy_days": int(inst_days),
        # 策略 3: bias_convergence(也共用 vol_ratio_min 給策略 4/5)
        "bias_low": float(bias_low),
        "bias_high": float(bias_high),
        "vol_ratio_min": float(vol_ratio_min),
        # 策略 5: ma_squeeze_breakout
        "squeeze_pct_max": float(squeeze_pct_max),
        # 策略 6: inst_consensus
        "consecutive_days": int(consensus_days),
        # 策略 7: bb_lower_rebound
        "bb_touch_lookback": int(bb_lookback),
        # 策略 8: rsi_recovery
        "rsi_oversold": float(rsi_oversold),
        "rsi_recovered": float(rsi_recovered),
        # rsi_window_days 預設 14,UI 不暴露(改 14 天框架等於改策略本質)
        # 策略 9: inst_silent_accum
        "pct_change_max": float(silent_pct_max),
        "bb_position_max": float(silent_bb_pos_max),
        # 策略 10: volume_breakout
        "vbo_vol_ratio_min": float(vbo_vol_ratio),
        "highest_lookback": int(vbo_highest_lookback),
        # 策略 11: gap_up
        "gap_pct_min": float(gap_pct_min),
        "gap_vol_ratio_min": float(gap_vol_ratio),
    }
    if not enabled_keys:
        st.warning("⚠️ 至少要選一套策略")
        return
    sids_only = [s for s, _ in universe]
    # 套產業 pre-filter:strategy 只跑被選產業內的 sids。
    if selected_industries:
        with db.get_conn() as _conn:
            sids_only = filter_sids_by_industry(
                sids_only, selected_industries, _conn,
            )
        if not sids_only:
            st.info("此產業組合下無 picks(filter 後 universe 為空)。")
            return
    _tic("short_run_all_strategies")
    with st.spinner(
        f"掃描 {len(sids_only)} 檔 × {len(enabled_keys)} 套策略 "
        f"(bulk SQL load)..."
    ):
        # 走 cache 版 — sticky-submit 後 rerun 同 (date, universe, params,
        # enabled) 直接 hit,不再每次重打 ~338ms。
        agg = _run_all_strategies_cached(
            today_iso,
            tuple(sids_only),
            tuple(enabled_keys),
            _make_params_key(params),
        )
    _toc("short_run_all_strategies")

    t2 = _time.perf_counter()

    if not agg:
        # 多數情況 0 入選不是 bug,而是歷史累積中(全市場 bulk 每天 1 筆)
        st.info("📭 任一啟用的策略都無入選。")
        if eligible_stocks < 100:
            st.warning(
                f"⏳ **歷史資料累積中**:目前只有 **{eligible_stocks}** 檔有 20+ 天歷史"
                f"(量價KD 需 14 天 / 乖離率 22 天 / 多頭排列 60 天)。\n\n"
                "**解法**:\n"
                "1. 按 sidebar『⏳ 一次性回補 90 日歷史』觸發 backfill\n"
                "2. 或選「快速:50 檔大型股」(已有完整歷史)\n"
                "3. 等 daily_fetch 自動累積(每日 +1 天)"
            )
        else:
            st.caption("可放寬參數或加開更多策略。")
        st.caption(
            f"⏱️ 載入={t1-t0:.2f}s,選股={t2-t1:.2f}s,共 {t2-t0:.2f}s "
            f"({len(sids_only)} 檔)"
            f"{' [跳過 prefetch — 走 daily_fetch 快取]' if skipped_prefetch else ''}"
        )
        return

    st.success(
        f"✅ 共 {len(agg)} 檔被選中"
        f"({len(enabled_keys)} 套策略並行,按信號數排序)"
    )

    _tic("short_aggregated_to_df")
    from src.strategies import enrich_with_analyst_target
    df = enrich_with_analyst_target(
        _enrich_df_with_consensus(
            _enrich_df_with_matched_strategies(
                _enrich_df_with_ml_prob(
                    _enrich_df_with_win_rate(aggregated_to_dataframe(agg), agg),
                    trade_date=today_iso,
                    agg=agg,
                ),
                agg,
            ),
            agg,
        ),
    )
    _toc("short_aggregated_to_df")
    t3 = _time.perf_counter()

    _tic("short_render_picks")
    # 5 tabs by category — 同一檔可在多個 tab 重複出現(被多策略同時選中)。
    # 「全部」tab 保留完整功能(view_mode / row select / 推播 / 批量加入);
    # 其他 4 tab 走簡單卡片視圖,專注該類策略命中名單。
    _inject_short_tab_css()

    # 從 _TAB_COLORS 取「全部」之外的 category(維持順序)
    cat_order = [c for c in _TAB_COLORS.keys() if c != "全部"]
    cat_counts = {
        cat: len(_filter_agg_by_category(agg, cat))
        for cat in cat_order
    }
    tab_labels = [f"全部 ({len(agg)})"] + [
        f"{cat} ({cat_counts[cat]})" for cat in cat_order
    ]
    all_tabs = st.tabs(tab_labels)
    tab_all = all_tabs[0]
    cat_tabs = dict(zip(cat_order, all_tabs[1:]))

    selection = None  # 只「全部」tab 表格模式才有 selection
    with tab_all:
        # 顯示模式切換(手機預設卡片,桌機可切表格)
        view_mode = view_mode_toggle("short_view_mode")

        if view_mode == "🃏 卡片":
            all_rows = df.to_dict("records")
            filtered_all, total_all = _apply_confidence_filter(all_rows)
            if st.session_state.get("high_confidence_mode", True):
                st.caption(f"🎯 高信心:{len(filtered_all)}/{total_all} 檔顯示")
                show_all = st.checkbox(
                    f"📋 顯示全部 {total_all} 檔",
                    value=False,
                    key="short_show_all_overall",
                )
                if show_all:
                    filtered_all = all_rows
            # 盤中行情注入(非交易時段 no-op)
            filtered_all = _inject_intraday_quotes(
                filtered_all,
                [str(r.get("stock_id")) for r in filtered_all if r.get("stock_id")],
            )
            render_picks_cards_paginated(
                filtered_all,
                state_key="short_全部",
                page_size=10,
                show_add_button=True, button_key_prefix="short",
            )
        else:
            # 表格模式:row select + 上方按鈕加入選中股
            selection = st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="multi-row",
                column_order=[
                    "stock_id", "name", "close",
                    "target_low", "target_high", "stop_loss",
                    "信號數", "信號", "risk_reward", "atr14",
                ],
                column_config={
                    "stock_id": st.column_config.TextColumn("代號", width="small"),
                    "name": st.column_config.TextColumn("名稱", width="small"),
                    "close": st.column_config.NumberColumn(
                        "收盤", format="%.2f", width="small",
                    ),
                    "target_low": st.column_config.NumberColumn(
                        "🎯 保守目標", format="%.2f", help="收盤 + 1.5 × ATR",
                    ),
                    "target_high": st.column_config.NumberColumn(
                        "🚀 積極目標", format="%.2f", help="收盤 + 3 × ATR",
                    ),
                    "stop_loss": st.column_config.NumberColumn(
                        "🛑 停損", format="%.2f", help="收盤 − 1.5 × ATR",
                    ),
                    "信號數": st.column_config.NumberColumn(
                        "🔥", width="small", help="同時被幾套策略選中",
                    ),
                    "信號": st.column_config.TextColumn("策略", width="medium"),
                    "risk_reward": st.column_config.NumberColumn(
                        "R:R", format="%.1f", width="small",
                        help="(積極目標 - 收盤) / (收盤 - 停損)",
                    ),
                    "atr14": st.column_config.NumberColumn(
                        "ATR(14)", format="%.2f", width="small",
                    ),
                },
            )

    for cat, tab_obj in cat_tabs.items():
        with tab_obj:
            sub_agg = _filter_agg_by_category(agg, cat)
            if not sub_agg:
                st.info("📭 此分類本日無入選。")
                continue
            sub_df = enrich_with_analyst_target(
                _enrich_df_with_consensus(
                    _enrich_df_with_matched_strategies(
                        _enrich_df_with_ml_prob(
                            _enrich_df_with_win_rate(
                                aggregated_to_dataframe(sub_agg), sub_agg,
                            ),
                            trade_date=today_iso,
                            agg=sub_agg,
                        ),
                        sub_agg,
                    ),
                    # consensus 用 full agg(不是 sub_agg)— 跨類別共識的票
                    # 來源不能被 category tab 切掉,否則卡片就看不到「跨類」
                    # 的 ⭐⭐ 標籤。
                    agg,
                ),
            )
            sub_rows = sub_df.to_dict("records")
            filtered_sub, total_sub = _apply_confidence_filter(sub_rows)
            if st.session_state.get("high_confidence_mode", True):
                st.caption(f"🎯 高信心:{len(filtered_sub)}/{total_sub} 檔顯示")
                show_all = st.checkbox(
                    f"📋 顯示全部 {total_sub} 檔",
                    value=False,
                    key=f"short_show_all_cat_{cat}",
                )
                if show_all:
                    filtered_sub = sub_rows
            render_picks_cards_paginated(
                filtered_sub,
                state_key=f"short_{cat}",
                page_size=10,
                show_add_button=True,
                button_key_prefix=f"short_{cat}",
            )

    t4 = _time.perf_counter()
    _toc("short_render_picks")
    st.caption(
        f"⏱️ 載入={t1-t0:.2f}s,選股={t2-t1:.2f}s,"
        f"DataFrame={t3-t2:.2f}s,渲染={t4-t3:.2f}s,共 {t4-t0:.2f}s "
        f"({len(sids_only)} 檔 → {len(agg)} 中)"
        f"{' [跳過 prefetch — 走 daily_fetch 快取]' if skipped_prefetch else ''}"
    )

    # === 📤 立即推播按鈕(限 7 檔 + 30 秒 debounce) ===
    _render_manual_push_button(df)

    if selection and selection.selection.rows:
        sel_sids = [df.iloc[i]["stock_id"] for i in selection.selection.rows]
        bcols = st.columns([1, 1, 1, 2])
        if bcols[0].button(
            f"⭐ 批量加入關注 ({len(sel_sids)} 檔)",
            key="short_bulk_star", use_container_width=True,
        ):
            result = db.bulk_add_to_watchlist(sel_sids)
            st.toast(
                f"✅ 新加 {result['ok']} 檔 / ⚠️ 重複 {result['dup']} 檔",
                icon="⭐",
            )
            st.rerun()
        if len(sel_sids) == 1 and bcols[1].button(
            "🔍 查看個股", key="short_view_one", use_container_width=True,
        ):
            st.session_state["query_stock_id"] = sel_sids[0]
            st.info(
                f"已選 **{sel_sids[0]}**,請點 sidebar 切到「🔍 個股」頁。"
            )
        if len(sel_sids) == 1 and bcols[2].button(
            "📊 看細節", key="short_detail_one", use_container_width=True,
            help="跳到「📊 個股深度」頁看 K 線 + 籌碼 + ML + 新聞",
        ):
            st.session_state["detail_sid"] = sel_sids[0]
            st.session_state["pending_nav"] = "📊 個股深度"
            st.rerun()


def _resolve_universe(choice: str) -> list[tuple[str, str]] | None:
    """根據使用者選擇回傳個股清單;回 None 表示已 render 提示、外層直接 return。"""
    if (choice.startswith("🎯") or choice.startswith("📊 充足歷史")
            or choice.startswith("📊 僅有充足歷史")):
        # 過濾 daily_prices 天數 >= 20 的個股(量價KD + 乖離率可跑;
        # MA60 多頭排列需 60 天,但 backfill 90 calendar days 只到 ~54 交易日)
        filter_etf = choice.startswith("🎯")
        sids_set = set(db.stocks_with_min_history(20))
        if not sids_set:
            st.warning(
                "目前沒有任何個股累積到 20 天歷史。"
                "請按 sidebar『⏳ 一次性回補 90 日歷史』觸發 GH Actions backfill。"
            )
            return None
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT stock_id, name FROM stocks "
                "WHERE market='TW' AND name IS NOT NULL AND name != ''"
            ).fetchall()
        universe = [
            (r["stock_id"], r["name"]) for r in rows
            if r["stock_id"] in sids_set
            and (not filter_etf or is_pure_stock(r["stock_id"], r["name"]))
        ]
        return universe or None
    if choice.startswith("全市場"):
        # 從 SQLite stocks 拿(全市場 ~2360 檔已 init);沒 init 觸發 get_full_universe
        sids = get_full_universe()
        if not sids:
            st.error(
                "全市場 universe 還沒 init。"
                "請按 sidebar『🔄 更新全市場價量』(會順便 init)。"
            )
            return None
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT stock_id, name FROM stocks "
                "WHERE market='TW' AND name IS NOT NULL AND name != ''"
            ).fetchall()
        universe = [(r["stock_id"], r["name"]) for r in rows]
        if not universe:
            st.error("stocks 表內無資料。請先按 sidebar『🔄 更新全市場價量』。")
            return None
        return universe
    if choice == "快速:50 檔大型股":
        return TW_TOP_50
    if choice == "我的關注清單":
        wl = load_watchlist()
        if not wl:
            st.warning(
                f"找不到 `{WATCHLIST_PATH}` 或內容為空。\n\n"
                "請建立此檔,一行一個股號(可加 # 註解),例如:\n\n"
                "```\n2330\n2454\n# 這是註解\n2317\n```"
            )
            return None
        return wl
    return None


# === 長線口袋名單頁(占位) ===

def _page_long() -> None:
    st.header("💎 長線口袋名單")

    # 大盤 regime gating badge — 長線同短線都顯,讓主公決策前先看大盤
    _render_regime_gating_badge(location="page")

    st.info(
        "📊 用台灣證交所(TWSE)官方免費資料 + FinMind 免費配息歷史,"
        "**不需任何付費 token**。\n\n"
        "按 sidebar **「📊 更新財報資料(免費版)」** 抓資料(約 1–2 分鐘),"
        "再點下方「執行選股」。"
    )

    st.markdown("### 預設策略(四條件 AND)")
    st.markdown(
        """
- **高 ROE**:近 3 年平均季 ROE > 15%(用 PB 反推:ROE ≈ PB / PE × 100)
- **低 PE**:當日本益比 < 20,或 < 該股票所屬產業平均 PE
- **連續配息**:近 5 年每年都有現金股利
- **殖利率**:近 1 年殖利率 > 4%(TWSE 官方欄位)
        """
    )

    st.markdown("---")
    test_btn = st.button(
        "🚀 執行長線選股",
        type="primary",
        use_container_width=True,
    )

    if test_btn:
        from src.screener_long import screen_long  # lazy
        with st.spinner("執行長線選股 (資料缺會自動從 TWSE 補)..."):
            result = screen_long()
        if result.empty:
            if not _has_long_data():
                st.warning(
                    "📊 仍缺資料。請點 **sidebar →「📊 更新財報資料(免費版)」** "
                    "把財報 + 配息抓進 SQLite 後再試。"
                )
            else:
                st.info(
                    "📭 資料齊全但無入選 → 可能真的沒有符合條件的個股,或可放寬參數。"
                )
        else:
            st.success(f"✅ 共 {len(result)} 檔符合長線條件")
            view_mode = view_mode_toggle("long_view_mode")
            if view_mode == "🃏 卡片":
                # 長線結果欄位:stock_id/name/close/pe/pb/yield/avg_roe etc.
                # 卡片只顯示基本資訊(目標價非長線重點,故關閉)
                # enrich 法人目標價 — 長線跟價值估算強相關,有共識票要看
                from src.strategies import (
                    enrich_with_analyst_target as _enrich_at,
                )
                long_cards = _enrich_at(result).to_dict("records")
                # enrich 千張大戶(TDCC 週快照,主公拍板長線卡才顯)— 批量
                # lookup 一次比 N 張卡片各 N 次 SQL 高效。沒資料 →
                # render_pick_card 內 _render_shareholder_inline 自動 skip。
                _sc_map = db.get_shareholder_concentration_for_sids(
                    [r.get("stock_id") for r in long_cards if r.get("stock_id")]
                )
                for _r in long_cards:
                    _sid = _r.get("stock_id")
                    _sc = _sc_map.get(_sid) or {}
                    _r["holders_1000up_count"] = _sc.get("holders_1000up_count")
                    _r["holders_delta_w"] = _sc.get("holders_delta_w")
                render_picks_cards(
                    long_cards,
                    show_signal=False, show_targets=False, show_change=False,
                    show_add_button=True, button_key_prefix="long",
                )
            else:
                long_sel = st.dataframe(
                    result, use_container_width=True, hide_index=True,
                    on_select="rerun", selection_mode="multi-row",
                )
                if long_sel and long_sel.selection.rows:
                    sel_sids = [
                        result.iloc[i]["stock_id"]
                        for i in long_sel.selection.rows
                    ]
                    if st.button(
                        f"⭐ 批量加入關注 ({len(sel_sids)} 檔)",
                        key="long_bulk_star",
                    ):
                        bres = db.bulk_add_to_watchlist(sel_sids)
                        st.toast(
                            f"✅ 新加 {bres['ok']} 檔 / "
                            f"⚠️ 重複 {bres['dup']} 檔",
                            icon="⭐",
                        )
                        st.rerun()


def _has_long_data() -> bool:
    """檢查 financials.quarterly 與 dividend 表是否都有資料。"""
    db.init_db()
    with db.get_conn() as conn:
        try:
            fin = conn.execute(
                "SELECT COUNT(*) AS c FROM financials WHERE period_type='quarterly'"
            ).fetchone()["c"]
            div = conn.execute(
                "SELECT COUNT(*) AS c FROM dividend"
            ).fetchone()["c"]
        except sqlite3.OperationalError:
            return False
    return fin > 0 and div > 0


# === 個股查詢頁 ===

@st.cache_data(ttl=600, show_spinner=False)
def _load_stock_options() -> list[str]:
    """從 stocks 表組搜尋用 options:["{sid} {name}", ...]。

    ttl 600s — stocks 表平均一天才會新增上市/櫃個股,5-10 分鐘 cache 充足。
    Streamlit selectbox 內建 fuzzy filter,輸入「台積電」或「2330」都能命中。
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id, name FROM stocks "
            "WHERE name IS NOT NULL AND name != '' "
            "ORDER BY stock_id"
        ).fetchall()
    return [f"{r['stock_id']} {r['name']}" for r in rows]


def _page_stock_query() -> None:
    st.header("🔍 個股查詢")
    db.init_db()

    # 短線 / 關注頁可以 push stock_id 進來當預設
    default_stock = st.session_state.pop("query_stock_id", "2330")

    # 輸入區先(input 在 toggle 之前,toggle 拿到的就是最新值)
    # 用 searchable selectbox:輸入股號或名稱都能 fuzzy filter
    options = _load_stock_options()
    default_label = next(
        (o for o in options if o.split(" ", 1)[0] == default_stock), None,
    )
    default_idx = options.index(default_label) if default_label else None

    cols = st.columns([2, 2, 2, 1])
    selected = cols[0].selectbox(
        "🔍 股號 或 名稱",
        options=options,
        index=default_idx,
        placeholder="輸入股號(如 2330)或名稱(如 台積電)...",
        help="支援搜尋:打字會 filter 候選清單",
    )
    # selectbox 找不到 → 走進階輸入(下方 expander)的值;
    # 兩者都空 → 退回 default_stock 維持週末/假日重新整理時的預設體驗
    stock_id = selected.split(" ", 1)[0] if selected else default_stock
    # 用最後交易日當 anchor,週末/假日打開個股頁 K 線結束日 = today 沒交易資料
    # 會抓到空區間或部分區間。跟短線頁 / 回測頁同 _get_default_screen_date 邏輯。
    anchor = _get_default_screen_date()
    start = cols[1].date_input("起始日", value=anchor - timedelta(days=90))
    end = cols[2].date_input("結束日", value=anchor)
    cols[3].markdown("&nbsp;", unsafe_allow_html=True)
    submit = cols[3].button("查詢", use_container_width=True, type="primary")

    # 進階輸入:新上市/櫃個股還沒收錄到 stocks 表 → selectbox 看不到,
    # 在這裡直接輸入代號 override(輸完按 Enter / 點查詢)
    with st.expander("🔧 進階:直接輸入代號(若 selectbox 找不到)", expanded=False):
        raw_sid = st.text_input(
            "代號 override",
            value="",
            key="stock_query_raw_sid",
            help="例如新上市/櫃個股還沒進 stocks 表時用",
        )
        if raw_sid.strip():
            stock_id = raw_sid.strip()

    # ⭐ Toggle 用「當下 input 的 stock_id」,不再卡在 page-load 時的舊值
    sid_clean = stock_id.strip()
    if sid_clean:
        # 確保 stocks 表有此股的 name(即使 TW_TOP_50 沒收錄,例如上櫃 3680 家登)
        info = ensure_stock_info(sid_clean)
        display_name = info["name"] if info and info.get("name") else "(未知代號)"

        starred = db.is_in_watchlist(sid_clean)
        toggle_label = (
            f"⭐ 已關注 {sid_clean} {display_name}" if starred
            else f"☆ 加入關注 {sid_clean} {display_name}"
        )
        if st.button(toggle_label, key=f"star_toggle_{sid_clean}"):
            if starred:
                db.remove_from_watchlist(sid_clean)
                st.toast(f"已從關注移除 {sid_clean}", icon="☆")
            else:
                # 加入前先抓 90 天歷史(補 ATR(14) / 漲跌% 等需要歷史的指標)
                today_iso = date.today().isoformat()
                start_iso = (date.today() - timedelta(days=90)).isoformat()
                with st.spinner(f"抓 {sid_clean} 過去 90 日歷史..."):
                    try:
                        fetch_daily_price(sid_clean, start_iso, today_iso)
                    except Exception as e:  # noqa: BLE001
                        st.warning(
                            f"歷史抓取失敗 ({type(e).__name__}):仍會加入,"
                            "但目標價需要 14+ 日歷史可能算不出"
                        )
                db.add_to_watchlist(sid_clean)
                st.toast(f"已加入關注 {sid_clean} {display_name}", icon="⭐")
            st.rerun()

    if not submit:
        st.info(
            "輸入股票代碼與日期區間後按「查詢」。\n\n"
            "**首次查詢的區間若不在 cache 內**,會打 FinMind API 抓取(無 token 模式較慢);"
            "之後同樣的區間都直接從 SQLite 取。\n\n"
            "💡 預設為過去 90 日,首次查詢約 5–10 秒抓取最新資料。"
        )
        return

    sid = stock_id.strip()
    if not sid:
        st.error("請輸入股票代碼。")
        return
    if start > end:
        st.error("起始日不能晚於結束日。")
        return

    try:
        with st.spinner(f"抓取 {sid} 日線資料中..."):
            df = fetch_daily_price(sid, start.isoformat(), end.isoformat())
    except Exception as e:  # noqa: BLE001
        st.error(f"資料抓取失敗:{type(e).__name__}: {e}")
        st.caption("可能原因:股票代碼錯誤、無 token 模式被頻率限制、網路問題。")
        return

    if df.empty:
        st.warning(
            f"找不到 **{sid}** 在 {start} ~ {end} 的資料。\n\n"
            "可能股票代碼錯誤,或該區間沒有交易日。"
        )
        return

    df = df.copy()
    df["MA5"] = ind.sma(df, 5)
    df["MA20"] = ind.sma(df, 20)
    df["MA60"] = ind.sma(df, 60)
    bb = ind.bollinger(df, period=20, num_std=2.0)
    kd_df = ind.kd(df, n=9)
    rsi14 = ind.rsi(df, period=14)
    macd_df = ind.macd(df, fast=12, slow=26, signal=9)

    st.caption(
        f"取得 {len(df)} 筆,日期 {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}"
    )

    # CSS 注入給 6 main tabs 各自顏色(語意對應:藍=圖表 / 紫=籌碼 / 青=趨勢
    # / 橘=警示 / 粉=行動)。只在個股頁 render 時注入,streamlit page change
    # 整個 main area 重 render → 前一頁 style 自然消,不污染其他頁。
    # nth-child 對 K 線 tab 內 5 sub-tabs 也會 match(沒第 6,所以 reuse 1-5
    # 顏色),視覺上沒太擾接受。
    st.markdown(
        """
        <style>
        .stTabs [role="tab"]:nth-child(1) { color: inherit; }
        .stTabs [role="tab"]:nth-child(2) { color: #185FA5; }
        .stTabs [role="tab"]:nth-child(3) { color: #7F77DD; }
        .stTabs [role="tab"]:nth-child(4) { color: #1D9E75; }
        .stTabs [role="tab"]:nth-child(5) { color: #BA7517; }
        .stTabs [role="tab"]:nth-child(6) { color: #D4537E; }
        .stTabs [role="tab"][aria-selected="true"] {
            border-bottom: 2px solid currentColor;
            font-weight: 500;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # 6 tabs:摘要 預設選中(streamlit 第一個 tab),user 打開個股頁第一眼
    # 看到核心數字 metric grid + 目標價,不用滑、不用點。想看圖才切「📈 K線」。
    (
        tab_overview, tab_kline, tab_chip, tab_trend, tab_shape, tab_action,
    ) = st.tabs([
        "📊 摘要", "📈 K線",
        "🚦 籌碼", "📊 趨勢", "🎯 形勢", "💡 操作",
    ])

    with tab_overview:
        _render_summary(sid, df, kd_df, rsi14, macd_df)

    with tab_kline:
        # 5 sub-tabs:每個視角獨立一張圖,單屏看完不用滾。預設選中「主圖」。
        sub_main, sub_vol, sub_kd, sub_macd, sub_rsi = st.tabs([
            "📈 主圖", "📊 成交量",
            "🔵 KD (9,3,3)", "🟠 MACD (12,26,9)", "🟢 RSI (14)",
        ])
        with sub_main:
            st.plotly_chart(
                _make_candlestick_main(df, bb), use_container_width=True,
            )
        with sub_vol:
            st.plotly_chart(_make_volume_chart(df), use_container_width=True)
        with sub_kd:
            st.plotly_chart(_make_kd_chart(df, kd_df), use_container_width=True)
        with sub_macd:
            st.plotly_chart(
                _make_macd_chart(df, macd_df), use_container_width=True,
            )
        with sub_rsi:
            st.plotly_chart(_make_rsi_chart(df, rsi14), use_container_width=True)

    with tab_chip:
        _render_institutional_table(sid)
        _render_institutional_cumulative_table(sid)
    with tab_trend:
        _render_multi_timeframe(sid)
        _render_technical_summary(sid)
    with tab_shape:
        _render_pattern_analysis(sid)
        _render_main_force_signal(sid)
        _render_key_levels(sid)
    with tab_action:
        _render_action_suggestion(sid)


def _render_summary(
    sid: str,
    df: pd.DataFrame,
    kd_df: pd.DataFrame,
    rsi14: pd.Series,
    macd_df: pd.DataFrame,
) -> None:
    st.markdown("### 📊 最新指標摘要")
    last = df.iloc[-1]
    close = float(last["close"])
    prev_close = df["close"].iloc[-2] if len(df) >= 2 else None
    delta = (close - prev_close) if prev_close is not None else None

    cols = st.columns(4)
    # 個股頁收盤的 delta — 台股慣例 inverse(漲紅跌綠)
    cols[0].metric(
        "收盤", f"{close:.2f}",
        f"{delta:+.2f}" if delta is not None else None,
        delta_color="inverse",
    )
    cols[1].metric("K(9)", _fmt(kd_df["K"].iloc[-1]))
    cols[2].metric("D(9)", _fmt(kd_df["D"].iloc[-1]))
    cols[3].metric("RSI14", _fmt(rsi14.iloc[-1]))

    cols = st.columns(4)
    cols[0].metric("MA5", _fmt(df["MA5"].iloc[-1]))
    cols[1].metric("MA20", _fmt(df["MA20"].iloc[-1]))
    cols[2].metric("MA60", _fmt(df["MA60"].iloc[-1]))
    cols[3].metric("DIF", _fmt(macd_df["DIF"].iloc[-1]))

    # === 目標價參考(ATR 統計) ===
    atr_series = ind.atr(df, period=14)
    atr14 = atr_series.iloc[-1] if not atr_series.empty else None
    if atr14 and not pd.isna(atr14) and close > 0:
        from src.strategies import (
            STOP_LOSS_MULT, TARGET_HIGH_MULT, TARGET_LOW_MULT,
        )
        target_low = close + TARGET_LOW_MULT * atr14
        target_high = close + TARGET_HIGH_MULT * atr14
        stop_loss = close - STOP_LOSS_MULT * atr14
        st.markdown("### 🎯 目標價參考(ATR 統計,**非預測**)")
        cols = st.columns(4)
        # 台股慣例:目標價往上(正)應顯紅、停損往下(負)應顯綠 — 都用 inverse
        cols[0].metric(
            "🎯 保守目標",
            f"{target_low:.2f}",
            f"+{(target_low - close) / close * 100:.1f}%",
            delta_color="inverse",
        )
        cols[1].metric(
            "🚀 積極目標",
            f"{target_high:.2f}",
            f"+{(target_high - close) / close * 100:.1f}%",
            delta_color="inverse",
        )
        cols[2].metric(
            "🛑 建議停損",
            f"{stop_loss:.2f}",
            f"{(stop_loss - close) / close * 100:.1f}%",
            delta_color="inverse",
        )
        rr = TARGET_HIGH_MULT / STOP_LOSS_MULT  # = 2.0
        cols[3].metric("⚖️ 風險報酬比", f"{rr:.1f} : 1")
        st.caption(
            "⚠️ 目標價為 ATR(14) 波動度估計,**非實際預測**;"
            "個人交易仍應自行評估。"
        )

    # === 🏢 公司資訊(FinMind facts + Gemini LLM) ===
    _render_company_profile(sid)

    # === 📊 法人目標價(yfinance / Gemini news fallback) ===
    _render_analyst_target(sid)


def _render_analyst_target(sid: str) -> None:
    """個股頁:法人(券商研究員)目標價共識區塊。

    SQLite analyst_targets 已預先 fetch(平日 watchlist+picks / 週日全市場),
    這裡只 lookup 顯示。

    Header 永遠顯,無資料時顯友善 caption(讓主公知道 section 接上了,只是
    還沒抓到資料,別誤判成 bug)。
    顯:target_mean / target_high / target_low / num_analysts / source / fetched_at
    """
    from src.analyst_targets import get_analyst_target

    st.markdown("### 📊 法人目標價")

    target = get_analyst_target(sid)
    if not target or target.get("target_mean") is None:
        st.caption(
            "🕐 此股目前尚無法人共識資料。"
            "資料每日 22:13 抓 watchlist + 今日 picks,週日 22:13 抓全市場。"
        )
        return

    cols = st.columns(4)
    cols[0].metric("共識目標", f"{target['target_mean']:.0f}")
    if target.get("target_high"):
        cols[1].metric("最樂觀", f"{target['target_high']:.0f}")
    if target.get("target_low"):
        cols[2].metric("最保守", f"{target['target_low']:.0f}")
    n_analyst = target.get("num_analysts")
    if n_analyst:
        cols[3].metric("券商家數", f"{int(n_analyst)} 家")

    src = target.get("source") or "?"
    src_label = "Yahoo 共識" if src == "yfinance" else "Gemini 新聞解析"
    fetched_at = target.get("fetched_at") or ""
    fetched_label = (
        fetched_at[:19].replace("T", " ") if len(fetched_at) >= 19 else fetched_at
    )
    st.caption(
        f"來源:{src_label}  ·  更新:{fetched_label} UTC  ·  "
        "⚠️ 僅供研究參考,非投資建議"
    )


def _render_company_profile(sid: str) -> None:
    """個股頁摘要 tab 內的公司資訊區塊。

    - FinMind facts(industry/market)即時拿(已 cache 在 SQLite stocks 表)
    - LLM 生成(description/uniqueness/moat)是 lazy:打開個股頁第一次
      會 spinner 跑一次,結果寫 SQLite cache 之後秒級
    - regenerate 按鈕:強制重打 Gemini API
    - 沒設 GEMINI_API_KEY → 顯示 placeholder 而非空白
    """
    from src import company_profile as cp

    st.markdown("### 🏢 公司資訊")

    regen_key = f"company_regen_{sid}"
    regenerate = st.session_state.pop(regen_key, False)

    has_gemini = bool(config.GEMINI_API_KEY)
    # 走 spinner — 第一次 view / regenerate 會打 LLM,可能 1-3 秒
    spinner_msg = (
        "重新生成公司資訊..." if regenerate else "載入公司資訊..."
    )
    with st.spinner(spinner_msg):
        try:
            profile = cp.get_company_profile(sid, regenerate=regenerate)
        except Exception as e:  # noqa: BLE001
            st.warning(f"⚠️ 無法載入公司資訊:{e}")
            return

    # 上排:industry / market / 名稱(facts,瞬間出來)
    fact_cols = st.columns(3)
    fact_cols[0].metric("產業類別", profile.get("industry") or "—")
    fact_cols[1].metric("市場別", profile.get("market") or "—")
    fact_cols[2].metric(
        "公司名稱",
        profile.get("name") or profile.get("stock_id") or "—",
    )

    # LLM 生成區塊
    desc = profile.get("description")
    uniq = profile.get("uniqueness")
    moat = profile.get("moat")
    llm_error = profile.get("llm_error")

    if not has_gemini:
        st.info(
            "需設 **GEMINI_API_KEY** 開啟 LLM 生成(description / uniqueness / moat)。"
            "申請:https://aistudio.google.com/apikey"
        )
        return

    if llm_error:
        st.warning(f"⚠️ {llm_error}")

    if desc:
        st.markdown(f"**📝 業務描述**\n\n{desc}")
    if uniq:
        st.markdown(f"**✨ 獨特性**\n\n{uniq}")
    if moat:
        st.markdown(f"**🏰 護城河 / 壟斷性**\n\n{moat}")

    if not (desc or uniq or moat) and not llm_error:
        st.caption("LLM 生成中...(下次刷新會看到)")

    # 重新生成按鈕(強制重打 Gemini)
    if st.button(
        "🔄 重新生成公司資訊",
        key=f"company_regen_btn_{sid}",
        help="重打 Gemini API 重新生成 description / uniqueness / moat",
    ):
        st.session_state[regen_key] = True
        st.rerun()

    if profile.get("llm_updated_at"):
        st.caption(
            f"📅 LLM 上次更新:{profile['llm_updated_at'][:19].replace('T', ' ')} UTC"
        )


def _render_institutional_table(sid: str, days: int = 10) -> None:
    """個股頁:近 N 日三大法人買賣超明細。

    SQLite institutional 欄位是「股」,顯示時除以 1000 轉「張」(四捨五入)。
    法人覆蓋率不到全市場 — 沒資料時顯示 fallback 訊息。
    """
    db.init_db()
    with db.get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT date, foreign_buy_sell, trust_buy_sell, "
                "dealer_buy_sell "
                "FROM institutional WHERE stock_id=? "
                "ORDER BY date DESC LIMIT ?",
                (sid, days),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []

    if not rows:
        st.info(
            "🔍 此股近期無三大法人籌碼資料。"
            "(覆蓋率有限,主要是高市值 / 關注清單個股)"
        )
        return

    inst_df = pd.DataFrame([
        {
            "日期": r["date"],
            "外資": round((r["foreign_buy_sell"] or 0) / 1000),
            "投信": round((r["trust_buy_sell"] or 0) / 1000),
            "自營商": round((r["dealer_buy_sell"] or 0) / 1000),
        }
        for r in rows
    ])
    # 合計 = 三者和(不直接讀 SQLite total_buy_sell,避免 NULL / 不一致)
    inst_df["合計"] = inst_df["外資"] + inst_df["投信"] + inst_df["自營商"]

    num_cols = ["外資", "投信", "自營商", "合計"]

    def _color_pos_neg(v: int) -> str:
        # 台股慣例:紅 = 正 / 綠 = 負(對應漲跌)
        if v > 0:
            return "color: #d62728"
        if v < 0:
            return "color: #2ca02c"
        return ""

    # 改用 st.table 避開 st.dataframe 在 mobile 320px container 把第 5 欄
    # hard-truncate 的問題。st.table 渲染靜態 HTML table 不受 canvas 寬度
    # 限制 — 內容自動 wrap,最差情況觸發頁面 horizontal scroll,5 欄不會消失。
    # st.table 不接 column_config,format 寫進 Styler 一次性 dict-style。
    styled = (
        inst_df.style
        .map(_color_pos_neg, subset=num_cols)
        .format("{:+,}", subset=num_cols)
    )

    with st.expander(
        f"📊 三大法人買賣超(近 {len(rows)} 日,單位:張)",
        expanded=True,
    ):
        st.table(styled)


def _render_institutional_cumulative_table(sid: str, days: int = 10) -> None:
    """個股頁:近 N 日主力進出 5/10 日累計表(+ 收盤價 + 漲跌幅)。

    每日累計 = 三大法人 (外資+投信+自營商) 該日及之前 4 / 9 個交易日的合計總和。
    為了讓最近 N 日的 rolling(10) 都有滿值,SQL 多撈 20 日歷史再 tail(days)。
    """
    db.init_db()
    fetch_days = days + 20
    with db.get_conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT p.date AS date, p.close AS close,
                       i.foreign_buy_sell AS f, i.trust_buy_sell AS t,
                       i.dealer_buy_sell AS d
                FROM daily_prices p
                LEFT JOIN institutional i
                  ON p.stock_id = i.stock_id AND p.date = i.date
                WHERE p.stock_id = ?
                ORDER BY p.date DESC
                LIMIT ?
                """,
                (sid, fetch_days),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []

    if not rows:
        st.info(
            "🔍 此股近期無主力進出累計資料。"
            "(需要 daily_prices 在 SQLite 內)"
        )
        return

    # daily_prices 有但 institutional 全 NULL(LEFT JOIN 留空)→ 走 fallback,
    # 不要渲染累計全 0 的誤導表格(例:01002T 等覆蓋率不足的個股)
    if all(
        r["f"] is None and r["t"] is None and r["d"] is None
        for r in rows
    ):
        st.info(
            "🔍 此股近期無主力進出累計資料。"
            "(覆蓋率有限,主要是高市值 / 關注清單個股)"
        )
        return

    df = pd.DataFrame([
        {
            "date": r["date"],
            "close": r["close"],
            # 股 → 張(整除丟小數,跟三大法人表一致)
            "inst_total": (
                (r["f"] or 0) + (r["t"] or 0) + (r["d"] or 0)
            ) // 1000,
        }
        for r in rows
    ])
    df = df.sort_values("date").reset_index(drop=True)
    # min_periods=1 容忍邊界(歷史不足時顯示 partial sum,不顯示 NaN)
    df["cum5"] = df["inst_total"].rolling(5, min_periods=1).sum().astype(int)
    df["cum10"] = df["inst_total"].rolling(10, min_periods=1).sum().astype(int)
    df["pct"] = df["close"].pct_change() * 100  # 第 1 筆 NaN

    out = df.tail(days).iloc[::-1].reset_index(drop=True)
    # 漲跌幅第 1 列(最早一日)無前日比 → fillna(0)。tail(days) 範圍內
    # 每天都有前日 close,fillna 對輸出實際無影響(防呆)。
    display = pd.DataFrame({
        "日期": out["date"].astype(str),
        "5 日累計": out["cum5"].astype(int),
        "10 日累計": out["cum10"].astype(int),
        "收盤價": out["close"].astype(float),
        "漲跌幅": out["pct"].fillna(0).astype(float),
    })

    def _color_pos_neg(v: float) -> str:
        if pd.isna(v) or v == 0:
            return ""
        return "color: #d62728" if v > 0 else "color: #2ca02c"

    # 改用 st.table 避開 st.dataframe 在 mobile 320px container 把第 5 欄
    # (漲跌幅)hard-truncate 的問題。st.table 渲染靜態 HTML table 不受
    # canvas 寬度限制,內容自動 wrap,5 欄不會消失。
    # 注意 .format 一次性帶 dict,不 chain 多次 — 之前推測 chain format 在
    # streamlit serialize 路徑某處 drop 整欄,單一 .format(dict) 比較穩。
    styled = (
        display.style
        .map(_color_pos_neg, subset=["5 日累計", "10 日累計", "漲跌幅"])
        .format({
            "5 日累計": "{:+,}",
            "10 日累計": "{:+,}",
            "收盤價": "{:.2f}",
            "漲跌幅": "{:+.2f}%",
        })
    )

    with st.expander(
        f"📈 主力進出累計(近 {len(display)} 日,單位:張)",
        expanded=True,
    ):
        st.table(styled)



def _classify_timeframe(
    df: pd.DataFrame,
    ma_periods: tuple[int, int, int],
    label: str,
) -> dict:
    """給定 OHLCV DataFrame(已升序)+ MA 期數三元組(短中長)
    → 算 trend / structure / change 三項解讀。

    回 dict {'trend','structure','change'} 或 {'error': str}。
    """
    short, mid, long = ma_periods
    if len(df) < long:
        return {"error": f"{label}歷史不足,需 ≥{long} 期(目前 {len(df)} 期)"}

    ma_short = ind.sma(df, short).iloc[-1]
    ma_mid = ind.sma(df, mid).iloc[-1]
    ma_long = ind.sma(df, long).iloc[-1]
    bb = ind.bollinger(df, period=mid, num_std=2.0)
    close_last = df["close"].iloc[-1]
    bb_upper = bb["upper"].iloc[-1]
    bb_mid = bb["mid"].iloc[-1]
    sigma = (bb_upper - bb_mid) / 2.0

    if any(pd.isna(v) for v in [
        ma_short, ma_mid, ma_long, bb_upper, bb_mid, sigma, close_last,
    ]):
        return {"error": f"{label}指標 NaN"}

    # 1. trend(close vs MA_mid vs MA_long)
    if close_last > ma_mid > ma_long:
        trend = "多頭趨勢"
    elif close_last < ma_mid < ma_long:
        trend = "空頭趨勢"
    else:
        trend = "盤整"

    # 2. structure(MA 排列)
    if ma_short > ma_mid > ma_long:
        structure = f"多頭排列({short}>{mid}>{long})"
    elif ma_short < ma_mid < ma_long:
        structure = f"空頭排列({short}<{mid}<{long})"
    else:
        structure = "MA 糾結整理"

    # 3. change(收盤位置 vs BB 中軌 / σ)
    diff = close_last - bb_mid
    half_sigma = 0.5 * sigma
    if abs(diff) < 0.3 * sigma:
        change = "中軌附近(整理)"
    elif diff > half_sigma:
        change = "貼近上軌(偏強)"
    elif diff < -half_sigma:
        change = "貼近下軌(偏弱)"
    else:
        change = "通道內波動"

    return {"trend": trend, "structure": structure, "change": change}


def _compute_multi_timeframe(sid: str) -> dict:
    """算日 K + 週 K 雙週期趨勢解讀。

    需要 ≥100 天 daily(週 K MA20 = 20 週 ≈ 100 trading days 起算點)。
    日 K 用 (5, 20, 60) MA;週 K 用 (5, 10, 20) MA(各自配對該時間框架的長期感)。
    """
    df = _load_recent_ohlc(sid, limit=300)
    if len(df) < 100:
        return {"error": f"歷史不足,需 ≥100 天計算週 K(目前 {len(df)} 天)"}

    daily = _classify_timeframe(df, ma_periods=(5, 20, 60), label="日 K")

    # 聚合週 K(以週五收盤為週收盤,符合台股交易慣例)
    df_w = df.copy()
    df_w["date"] = pd.to_datetime(df_w["date"])
    weekly = (
        df_w.set_index("date")
        .resample("W-FRI")
        .agg({
            "open": "first", "high": "max",
            "low": "min", "close": "last", "volume": "sum",
        })
        .dropna()
        .reset_index()
    )
    weekly["date"] = weekly["date"].dt.strftime("%Y-%m-%d")

    weekly_result = _classify_timeframe(
        weekly, ma_periods=(5, 10, 20), label="週 K",
    )
    return {"daily": daily, "weekly": weekly_result}


def _render_multi_timeframe(sid: str) -> None:
    """個股頁:日 K + 週 K 雙週期趨勢並排顯示(2 欄)。"""
    result = _compute_multi_timeframe(sid)
    if "error" in result:
        st.info(f"🔄 多週期趨勢:{result['error']}")
        return

    daily = result["daily"]
    weekly = result["weekly"]
    # 任一週期算不出來 → fallback,不渲染半截
    if "error" in daily or "error" in weekly:
        msg = daily.get("error") or weekly.get("error")
        st.info(f"🔄 多週期趨勢:{msg}")
        return

    st.markdown("### 🔄 多週期趨勢分析")
    cols = st.columns(2)
    with cols[0]:
        st.markdown(
            f"**📅 日 K(中線視角)**\n\n"
            f"- 趨勢:{daily['trend']}\n"
            f"- 結構:{daily['structure']}\n"
            f"- 變化:{daily['change']}"
        )
    with cols[1]:
        st.markdown(
            f"**📊 週 K(長線視角)**\n\n"
            f"- 趨勢:{weekly['trend']}\n"
            f"- 結構:{weekly['structure']}\n"
            f"- 變化:{weekly['change']}"
        )
    st.caption(
        "日 K MA(5/20/60)看中期趨勢;週 K MA(5/10/20)看長期主升 / 主跌段。"
        "兩者一致 → 趨勢可信度高;分歧 → 留意轉折。"
    )


def _find_pivots(
    values, atr: float, window: int = 3, min_prominence_atr: float = 1.0,
) -> tuple[list[int], list[int]]:
    """純 numpy / list 找 local max / min,prominence 用 ATR 倍數過濾雜訊。

    對每個 idx i 看 [i-window, i+window],i 是 max → 候選 high pivot。
    再算 prominence(local extreme 跟 window 內反方向極值的差)≥ ATR ×
    min_prominence_atr 才收。

    比 scipy.signal.find_peaks 簡化但夠用。
    """
    n = len(values)
    high_idx: list[int] = []
    low_idx: list[int] = []
    threshold = atr * min_prominence_atr
    for i in range(window, n - window):
        win = values[i - window: i + window + 1]
        v = values[i]
        if v == max(win) and (v - min(win)) >= threshold:
            high_idx.append(i)
        if v == min(win) and (max(win) - v) >= threshold:
            low_idx.append(i)
    return high_idx, low_idx


def _compute_w_bottom_pattern(
    close: list[float], dates: list[str], atr: float,
) -> dict:
    """W 底偵測。回 dict 含 status / score / 各點位 / 解釋字串。

    結構:左低 → 中高 → 右低,目前已開始反彈 / 突破頸線(中高)。
    限縮在最近 60 天 lookback 內找 pivots。
    """
    n = len(close)
    lookback = min(60, n)
    close_recent = close[-lookback:]
    dates_recent = dates[-lookback:]

    high_idx, low_idx = _find_pivots(close_recent, atr=atr, window=3)
    if len(low_idx) < 2:
        return {
            "status": "未發現明顯型態",
            "score": 0,
            "explanation": "近 60 日內未抓到 2 個低點 pivot",
        }

    # 取最近 2 個 low,且要中間有 high pivot
    right_low_i = low_idx[-1]
    left_low_i = low_idx[-2]
    mid_highs = [i for i in high_idx if left_low_i < i < right_low_i]
    if not mid_highs:
        return {
            "status": "未發現明顯型態",
            "score": 0,
            "explanation": "兩個低點之間缺反彈高點(無 W 結構)",
        }
    mid_high_i = max(mid_highs, key=lambda i: close_recent[i])

    left_low = close_recent[left_low_i]
    right_low = close_recent[right_low_i]
    mid_high = close_recent[mid_high_i]
    current_close = close_recent[-1]

    base_low = max(left_low, right_low)
    low_diff_pct = abs(left_low - right_low) / base_low * 100
    bounce_pct = (mid_high - base_low) / base_low * 100
    breakout = current_close > mid_high

    # 評分(滿 100):兩低相似 40 + 中間反彈 30 + 突破頸線 30
    score_diff = max(0.0, 40.0 * (1 - low_diff_pct / 3.0))
    if bounce_pct >= 8:
        score_bounce = 30
    elif bounce_pct >= 5:
        score_bounce = 15
    else:
        score_bounce = 0
    score_breakout = 30 if breakout else 0
    score = round(score_diff + score_bounce + score_breakout)

    if score >= 70 and breakout:
        status = "已形成"
    elif score >= 50:
        status = "形成中"
    else:
        status = "未形成"

    explanation = (
        f"兩低點相差 {low_diff_pct:.1f}%,中間反彈 {bounce_pct:.1f}%,"
        + ("已突破頸線" if breakout else "未突破頸線")
    )

    return {
        "status": status,
        "score": score,
        "left_date": str(dates_recent[left_low_i]),
        "left_price": float(left_low),
        "mid_date": str(dates_recent[mid_high_i]),
        "mid_price": float(mid_high),
        "right_date": str(dates_recent[right_low_i]),
        "right_price": float(right_low),
        "neckline": float(mid_high),
        "explanation": explanation,
    }


def _compute_m_top_pattern(
    close: list[float], dates: list[str], atr: float,
) -> dict:
    """M 頭偵測(W 底鏡像):左高 → 中低 → 右高,目前下跌 / 跌破頸線(中低)。"""
    n = len(close)
    lookback = min(60, n)
    close_recent = close[-lookback:]
    dates_recent = dates[-lookback:]

    high_idx, low_idx = _find_pivots(close_recent, atr=atr, window=3)
    if len(high_idx) < 2:
        return {
            "status": "未發現明顯型態",
            "score": 0,
            "explanation": "近 60 日內未抓到 2 個高點 pivot",
        }

    right_high_i = high_idx[-1]
    left_high_i = high_idx[-2]
    mid_lows = [i for i in low_idx if left_high_i < i < right_high_i]
    if not mid_lows:
        return {
            "status": "未發現明顯型態",
            "score": 0,
            "explanation": "兩個高點之間缺回檔低點(無 M 結構)",
        }
    mid_low_i = min(mid_lows, key=lambda i: close_recent[i])

    left_high = close_recent[left_high_i]
    right_high = close_recent[right_high_i]
    mid_low = close_recent[mid_low_i]
    current_close = close_recent[-1]

    base_high = min(left_high, right_high)
    high_diff_pct = abs(left_high - right_high) / base_high * 100
    pullback_pct = (base_high - mid_low) / base_high * 100
    breakdown = current_close < mid_low

    score_diff = max(0.0, 40.0 * (1 - high_diff_pct / 3.0))
    if pullback_pct >= 8:
        score_pullback = 30
    elif pullback_pct >= 5:
        score_pullback = 15
    else:
        score_pullback = 0
    score_breakdown = 30 if breakdown else 0
    score = round(score_diff + score_pullback + score_breakdown)

    if score >= 70 and breakdown:
        status = "已形成"
    elif score >= 50:
        status = "形成中"
    else:
        status = "未形成"

    explanation = (
        f"兩高點相差 {high_diff_pct:.1f}%,中間回檔 {pullback_pct:.1f}%,"
        + ("已跌破頸線" if breakdown else "未跌破頸線")
    )

    return {
        "status": status,
        "score": score,
        "left_date": str(dates_recent[left_high_i]),
        "left_price": float(left_high),
        "mid_date": str(dates_recent[mid_low_i]),
        "mid_price": float(mid_low),
        "right_date": str(dates_recent[right_high_i]),
        "right_price": float(right_high),
        "neckline": float(mid_low),
        "explanation": explanation,
    }


def _compute_pattern_analysis(sid: str) -> dict:
    """W 底 + M 頭雙型態偵測。需要 ≥60 天 daily_prices + ATR(14) 算得出來。"""
    df = _load_recent_ohlc(sid, limit=120)
    if len(df) < 60:
        return {"error": f"歷史不足,需 ≥60 天偵測型態(目前 {len(df)} 天)"}

    atr_series = ind.atr(df, period=14)
    atr14 = atr_series.iloc[-1] if not atr_series.empty else None
    if atr14 is None or pd.isna(atr14):
        return {"error": "ATR 算不出(資料異常)"}

    close = df["close"].tolist()
    dates = df["date"].tolist()
    return {
        "w_bottom": _compute_w_bottom_pattern(close, dates, atr14),
        "m_top": _compute_m_top_pattern(close, dates, atr14),
    }


def _render_pattern_analysis(sid: str) -> None:
    """個股頁:W 底 / M 頭 雙型態偵測並排顯示。"""
    result = _compute_pattern_analysis(sid)
    if "error" in result:
        st.info(f"🎭 型態分析:{result['error']}")
        return

    w = result["w_bottom"]
    m = result["m_top"]

    def _block(name: str, pat: dict, role_labels: tuple[str, str, str]) -> str:
        """把單一型態 dict 轉成 markdown 區塊。"""
        if pat.get("score", 0) == 0 and pat.get("status", "").startswith("未發現"):
            # 沒抓到結構 → 簡短顯示
            return (
                f"**{name}**\n\n"
                f"- 狀態:{pat['status']}\n"
                f"- 評分:0/100\n"
                f"- 原理:{pat.get('explanation', '—')}"
            )
        left_label, mid_label, right_label = role_labels
        return (
            f"**{name}**\n\n"
            f"- 狀態:**{pat['status']}**\n"
            f"- {left_label}:{pat['left_date']} 收盤 {pat['left_price']:.2f}\n"
            f"- {mid_label}:{pat['mid_date']} 收盤 {pat['mid_price']:.2f}\n"
            f"- {right_label}:{pat['right_date']} 收盤 {pat['right_price']:.2f}\n"
            f"- 頸線:{pat['neckline']:.2f}\n"
            f"- 評分:**{pat['score']}/100**\n"
            f"- 原理:{pat['explanation']}"
        )

    st.markdown("### 🎭 型態分析")
    cols = st.columns(2)
    with cols[0]:
        st.markdown(_block(
            "🟢 W 底分析", w, ("左低", "中高", "右低"),
        ))
    with cols[1]:
        st.markdown(_block(
            "🔴 M 頭分析", m, ("左高", "中低", "右高"),
        ))
    st.caption(
        "Pivot 用 ATR ≥1× 倍 prominence 過濾雜訊。"
        "W 底:兩低點相似 + 中間反彈 + 突破頸線;M 頭鏡像。**參考用,非預測**"
    )



def _fmt(v: float) -> str:
    if pd.isna(v):
        return "—"
    return f"{v:.2f}"


# === 圖表 ===

def _make_candlestick(df: pd.DataFrame, bb: pd.DataFrame) -> go.Figure:
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.72, 0.28],
    )
    fig.add_trace(
        go.Candlestick(
            x=df["date"],
            open=df["open"], high=df["high"],
            low=df["low"], close=df["close"],
            increasing_line_color="#d62728",
            decreasing_line_color="#2ca02c",
            name="K 線",
        ),
        row=1, col=1,
    )
    for col, color in [("MA5", "#1f77b4"), ("MA20", "#ff7f0e"), ("MA60", "#9467bd")]:
        if col in df.columns and df[col].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=df["date"], y=df[col],
                    name=col, line=dict(width=1.2, color=color),
                ),
                row=1, col=1,
            )
    if not bb.empty and bb["upper"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=bb["upper"],
                name="BB 上",
                line=dict(width=1, dash="dot", color="rgba(120,120,120,0.7)"),
            ),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=bb["lower"],
                name="BB 下",
                line=dict(width=1, dash="dot", color="rgba(120,120,120,0.7)"),
                fill="tonexty", fillcolor="rgba(120,120,120,0.08)",
            ),
            row=1, col=1,
        )
    vol_colors = [
        "#d62728" if c >= o else "#2ca02c"
        for c, o in zip(df["close"], df["open"])
    ]
    fig.add_trace(
        go.Bar(
            x=df["date"], y=df["volume"],
            name="量", marker_color=vol_colors, showlegend=False,
        ),
        row=2, col=1,
    )
    fig.update_layout(
        height=520,
        xaxis_rangeslider_visible=False,
        showlegend=True,
        margin=dict(t=20, b=20, l=20, r=20),
        legend=dict(orientation="h", y=1.02, x=0, font=dict(size=16)),
        font=dict(size=16),
    )
    fig.update_xaxes(type="category", tickfont=dict(size=14), row=1, col=1)
    fig.update_xaxes(type="category", tickfont=dict(size=14), row=2, col=1)
    fig.update_yaxes(title_text="股價", tickfont=dict(size=14),
                     title_font=dict(size=16), row=1, col=1)
    fig.update_yaxes(title_text="成交量", tickfont=dict(size=14),
                     title_font=dict(size=16), row=2, col=1)
    return fig


def _make_candlestick_main(df: pd.DataFrame, bb: pd.DataFrame) -> go.Figure:
    """蠟燭圖 + BB + MA(不含量子圖)— 個股頁 K線 tab 主圖 sub-tab 用,
    跟 _make_candlestick 拆開避免「主圖 + 量」共用一張縱切兩半的圖,單屏滾動少。
    """
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df["date"],
        open=df["open"], high=df["high"],
        low=df["low"], close=df["close"],
        increasing_line_color="#d62728",
        decreasing_line_color="#2ca02c",
        name="K 線",
    ))
    for col, color in [("MA5", "#1f77b4"), ("MA20", "#ff7f0e"), ("MA60", "#9467bd")]:
        if col in df.columns and df[col].notna().any():
            fig.add_trace(go.Scatter(
                x=df["date"], y=df[col],
                name=col, line=dict(width=1.2, color=color),
            ))
    if not bb.empty and bb["upper"].notna().any():
        fig.add_trace(go.Scatter(
            x=df["date"], y=bb["upper"], name="BB 上",
            line=dict(width=1, dash="dot", color="rgba(120,120,120,0.7)"),
        ))
        fig.add_trace(go.Scatter(
            x=df["date"], y=bb["lower"], name="BB 下",
            line=dict(width=1, dash="dot", color="rgba(120,120,120,0.7)"),
            fill="tonexty", fillcolor="rgba(120,120,120,0.08)",
        ))
    fig.update_layout(
        height=400,
        xaxis_rangeslider_visible=False,
        showlegend=True,
        margin=dict(t=20, b=20, l=20, r=20),
        legend=dict(orientation="h", y=1.02, x=0, font=dict(size=16)),
        font=dict(size=16),
    )
    fig.update_xaxes(type="category", tickfont=dict(size=14))
    fig.update_yaxes(title_text="股價", tickfont=dict(size=14),
                     title_font=dict(size=16))
    return fig


def _make_volume_chart(df: pd.DataFrame) -> go.Figure:
    """獨立的量 bar 圖(紅綠對應 K 線),個股頁 K線 tab 量 sub-tab 用。"""
    vol_colors = [
        "#d62728" if c >= o else "#2ca02c"
        for c, o in zip(df["close"], df["open"])
    ]
    fig = go.Figure(go.Bar(
        x=df["date"], y=df["volume"],
        marker_color=vol_colors, name="量", showlegend=False,
    ))
    fig.update_layout(
        height=320, margin=dict(t=20, b=20, l=20, r=20),
        font=dict(size=16),
    )
    fig.update_xaxes(type="category", tickfont=dict(size=14))
    fig.update_yaxes(title_text="成交量", tickfont=dict(size=14),
                     title_font=dict(size=16))
    return fig


def _make_kd_chart(df: pd.DataFrame, kd_df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["date"], y=kd_df["K"], name="K", line=dict(color="#d62728"),
    ))
    fig.add_trace(go.Scatter(
        x=df["date"], y=kd_df["D"], name="D", line=dict(color="#1f77b4"),
    ))
    fig.add_hline(y=80, line_dash="dot", line_color="gray", opacity=0.5)
    fig.add_hline(y=20, line_dash="dot", line_color="gray", opacity=0.5)
    fig.update_layout(
        height=320, margin=dict(t=20, b=20, l=20, r=20),
        yaxis=dict(range=[0, 100], tickfont=dict(size=14)),
        xaxis=dict(tickfont=dict(size=14)),
        font=dict(size=16),
        legend=dict(font=dict(size=16)),
    )
    fig.update_xaxes(type="category")
    return fig


def _make_macd_chart(df: pd.DataFrame, macd_df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["date"], y=macd_df["DIF"], name="DIF", line=dict(color="#d62728"),
    ))
    fig.add_trace(go.Scatter(
        x=df["date"], y=macd_df["DEA"], name="DEA", line=dict(color="#1f77b4"),
    ))
    hist_colors = [
        "#d62728" if v >= 0 else "#2ca02c" for v in macd_df["HIST"].fillna(0)
    ]
    fig.add_trace(go.Bar(
        x=df["date"], y=macd_df["HIST"], name="HIST",
        marker_color=hist_colors, opacity=0.6,
    ))
    fig.add_hline(y=0, line_color="gray", opacity=0.5)
    fig.update_layout(
        height=320, margin=dict(t=20, b=20, l=20, r=20),
        xaxis=dict(tickfont=dict(size=14)),
        yaxis=dict(tickfont=dict(size=14)),
        font=dict(size=16),
        legend=dict(font=dict(size=16)),
    )
    fig.update_xaxes(type="category")
    return fig


def _make_rsi_chart(df: pd.DataFrame, rsi14: pd.Series) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["date"], y=rsi14, name="RSI(14)", line=dict(color="#9467bd"),
    ))
    fig.add_hline(y=70, line_dash="dash", line_color="#d62728", opacity=0.6,
                  annotation_text="超買 70")
    fig.add_hline(y=30, line_dash="dash", line_color="#2ca02c", opacity=0.6,
                  annotation_text="超賣 30")
    fig.add_hline(y=50, line_dash="dot", line_color="gray", opacity=0.4)
    fig.update_layout(
        height=320, margin=dict(t=20, b=20, l=20, r=20),
        yaxis=dict(range=[0, 100], tickfont=dict(size=14)),
        xaxis=dict(tickfont=dict(size=14)),
        font=dict(size=16),
        legend=dict(font=dict(size=16)),
    )
    fig.update_xaxes(type="category")
    return fig


# === 個股深度頁 ===

def _make_detail_kline_chart(df: pd.DataFrame) -> go.Figure:
    """個股深度頁 K 線 + MA20/MA60 + BB + Volume(2-row subplot)。

    跟 _make_candlestick 同概念,但獨立函式讓深度頁不依賴查詢頁的 MA5/MA60
    schema 假設(我們只算 MA20 / MA60)。
    """
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.72, 0.28],
    )
    fig.add_trace(
        go.Candlestick(
            x=df["date"],
            open=df["open"], high=df["high"],
            low=df["low"], close=df["close"],
            increasing_line_color="#d62728",
            decreasing_line_color="#2ca02c",
            name="K 線",
        ),
        row=1, col=1,
    )
    for col, color in [("ma20", "#ff7f0e"), ("ma60", "#9467bd")]:
        if col in df.columns and df[col].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=df["date"], y=df[col],
                    name=col.upper(),
                    line=dict(width=1.2, color=color),
                ),
                row=1, col=1,
            )
    if "bb_upper" in df.columns and df["bb_upper"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=df["bb_upper"], name="BB 上",
                line=dict(width=1, dash="dot",
                          color="rgba(120,120,120,0.7)"),
            ),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=df["bb_lower"], name="BB 下",
                line=dict(width=1, dash="dot",
                          color="rgba(120,120,120,0.7)"),
                fill="tonexty", fillcolor="rgba(120,120,120,0.08)",
            ),
            row=1, col=1,
        )
    vol_colors = [
        "#d62728" if c >= o else "#2ca02c"
        for c, o in zip(df["close"], df["open"])
    ]
    fig.add_trace(
        go.Bar(
            x=df["date"], y=df["volume"],
            name="量", marker_color=vol_colors, showlegend=False,
        ),
        row=2, col=1,
    )
    fig.update_layout(
        height=460,
        xaxis_rangeslider_visible=False,
        showlegend=True,
        margin=dict(t=20, b=20, l=20, r=20),
        legend=dict(orientation="h", y=1.02, x=0, font=dict(size=14)),
        font=dict(size=14),
    )
    fig.update_xaxes(type="category", tickfont=dict(size=12), row=1, col=1)
    fig.update_xaxes(type="category", tickfont=dict(size=12), row=2, col=1)
    fig.update_yaxes(title_text="股價", tickfont=dict(size=12),
                     title_font=dict(size=14), row=1, col=1)
    fig.update_yaxes(title_text="量", tickfont=dict(size=12),
                     title_font=dict(size=14), row=2, col=1)
    return fig


def _make_inst_stacked_bar(inst_rows: list[dict]) -> go.Figure:
    """三大法人 stacked bar(單位:張)。inst_rows 是 DESC sort,我們轉 ASC 畫。"""
    rows = list(reversed(inst_rows))  # ASC 給 x 軸時間由左到右
    dates = [r["date"] for r in rows]

    def _lots(field: str) -> list[int]:
        return [round((r.get(field) or 0) / 1000) for r in rows]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=dates, y=_lots("foreign_buy_sell"), name="外資",
        marker_color="#d62728",
    ))
    fig.add_trace(go.Bar(
        x=dates, y=_lots("trust_buy_sell"), name="投信",
        marker_color="#ff7f0e",
    ))
    fig.add_trace(go.Bar(
        x=dates, y=_lots("dealer_buy_sell"), name="自營商",
        marker_color="#1f77b4",
    ))
    fig.update_layout(
        barmode="relative",  # 同正負同側疊,正負分開
        height=320,
        margin=dict(t=20, b=20, l=20, r=20),
        legend=dict(orientation="h", y=1.05, x=0, font=dict(size=14)),
        font=dict(size=14),
        yaxis_title="買賣超(張)",
    )
    fig.update_xaxes(type="category", tickfont=dict(size=12))
    return fig


def _make_shareholder_chart(rows: list[dict]) -> go.Figure:
    """千張戶人數折線 + delta_w bar 雙軸。rows 已是 ASC。"""
    dates = [r["week_end"] for r in rows]
    counts = [r["holders_1000up_count"] for r in rows]
    deltas = [r.get("holders_delta_w") or 0 for r in rows]

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Scatter(
            x=dates, y=counts, name="千張戶人數",
            mode="lines+markers",
            line=dict(color="#1f77b4", width=2),
            marker=dict(size=6),
        ),
        secondary_y=False,
    )
    bar_colors = [
        "#d62728" if d > 0 else "#2ca02c" if d < 0 else "#aaa"
        for d in deltas
    ]
    fig.add_trace(
        go.Bar(
            x=dates, y=deltas, name="週變動",
            marker_color=bar_colors, opacity=0.6,
        ),
        secondary_y=True,
    )
    fig.update_layout(
        height=320, margin=dict(t=30, b=20, l=20, r=20),
        legend=dict(orientation="h", y=1.05, x=0, font=dict(size=14)),
        font=dict(size=14),
    )
    fig.update_xaxes(type="category", tickfont=dict(size=12))
    fig.update_yaxes(title_text="千張戶數", secondary_y=False,
                     tickfont=dict(size=12))
    fig.update_yaxes(title_text="週變動(人)", secondary_y=True,
                     tickfont=dict(size=12))
    return fig


def _render_detail_header(sid: str) -> None:
    """個股深度頁頂端:股號 / 名稱 / 收盤價 / 漲跌幅 + 產業 badge + 加關注 button。"""
    info = ensure_stock_info(sid)
    name = info.get("name") if info else "(未知代號)"
    industry = info.get("industry") if info else None

    # 最新 close + 前一交易日 close(算漲跌幅)
    with db.get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT date, close FROM daily_prices WHERE stock_id=? "
                "ORDER BY date DESC LIMIT 2",
                (sid,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    close = rows[0]["close"] if rows else None
    prev = rows[1]["close"] if len(rows) > 1 else None
    chg_pct = (
        (close - prev) / prev * 100.0
        if close is not None and prev is not None and prev != 0
        else None
    )
    chg_str = (
        f"({'↑' if chg_pct >= 0 else '↓'}{abs(chg_pct):.2f}%)"
        if chg_pct is not None else ""
    )
    close_str = f"{close:.2f}" if close is not None else "—"
    title = f"📊 [{sid}] {name}　{close_str} {chg_str}"
    st.markdown(f"### {title}")

    cap_parts = []
    if industry:
        cap_parts.append(f"🏭 {industry}")
    if rows:
        cap_parts.append(f"📅 最新:{rows[0]['date']}")
    if cap_parts:
        st.caption("　·　".join(cap_parts))

    starred = db.is_in_watchlist(sid)
    label = (f"⭐ 已關注 移除" if starred else f"☆ 加入關注")
    if st.button(label, key=f"detail_star_{sid}"):
        if starred:
            db.remove_from_watchlist(sid)
            st.toast(f"已從關注移除 {sid}", icon="☆")
        else:
            db.add_to_watchlist(sid)
            st.toast(f"已加入關注 {sid}", icon="⭐")
        st.rerun()

    # 策略命中 + 共識 block — 從最近 daily_picks 撈該 sid 命中過哪些策略 +
    # 分類。沒撈到資料 → graceful skip(該股可能不在最近的選股結果裡)。
    _render_detail_strategy_hits(sid)


def _render_detail_strategy_hits(sid: str) -> None:
    """個股深度頁:列出該 sid 在最近 daily_picks 命中的策略 + 分類 + 共識 tier。

    Mobile-first:單欄文字,每策略一行(中文標籤 + 類別 chip);最後加共識 badge。
    沒命中任何策略(該 sid 不在 daily_picks 內) → 空 caption skip。
    """
    latest_date: str | None = None
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(trade_date) AS d FROM daily_picks "
                "WHERE universe='pure_stock'"
            ).fetchone()
        if row and row["d"]:
            latest_date = str(row["d"])
    except sqlite3.OperationalError:
        latest_date = None
    if not latest_date:
        return
    try:
        agg = db.load_daily_picks(latest_date, "pure_stock")
    except Exception:  # noqa: BLE001
        agg = None
    if not agg or sid not in agg:
        return
    details = agg[sid].get("details") or {}
    matched = list(details.keys())
    if not matched:
        return

    from src.strategies import STRATEGY_LABELS
    from src.consensus import (
        STRATEGY_CATEGORIES, compute_strategy_consensus, consensus_badge,
    )
    # 共識:用同日 agg 全 sid 算(category_count 才會反映全市場跨類維度)
    strategy_to_sids: dict[str, list[str]] = {}
    for _sid, info in agg.items():
        for s in (info.get("details") or {}).keys():
            strategy_to_sids.setdefault(s, []).append(_sid)
    cons_map = compute_strategy_consensus(strategy_to_sids)
    meta = cons_map.get(sid)
    badge_text, tier = consensus_badge(meta)

    with st.expander(f"🎯 策略命中({len(matched)})— {latest_date}", expanded=True):
        if badge_text:
            st.markdown(
                f"**共識**:{badge_text}  "
                f"(跨 {meta['category_count']} 類別 / "
                f"{meta['strategy_count']} 策略)"
            )
        for s in matched:
            label = STRATEGY_LABELS.get(s, s)
            cat = STRATEGY_CATEGORIES.get(s, "未分類")
            st.markdown(f"・**{label}** `<{cat}>`")


def _render_detail_paper_trade_status(sid: str) -> None:
    """若該 sid 有 active paper_trade,顯示進場價 / 停損 / 目標 / 持有天數。"""
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT entry_date, entry_price, target_price, "
                "stop_price, current_stop, hold_days, expected_exit_date, "
                "ml_prob, matched_strategies "
                "FROM paper_trades WHERE sid=? AND status='active' "
                "ORDER BY entry_date DESC LIMIT 1",
                (sid,),
            ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row:
        st.caption("📭 此股無 active 實測倉位")
        return

    today = date.today()
    try:
        entry_dt = date.fromisoformat(row["entry_date"])
        held = (today - entry_dt).days
    except Exception:  # noqa: BLE001
        held = None

    cur_stop = row["current_stop"] if row["current_stop"] is not None else row["stop_price"]
    st.markdown(
        f"**進場日**:{row['entry_date']} @ {row['entry_price']:.2f} ｜ "
        f"**目標**:{row['target_price']:.2f} ｜ "
        f"**停損**:{cur_stop:.2f} ｜ "
        f"**已持有**:{held if held is not None else '—'} 天 "
        f"(預計持有 {row['hold_days']} 天)"
    )
    if row["matched_strategies"]:
        st.caption(f"進場策略:{row['matched_strategies']}")


def _render_detail_kline_tab(sid: str) -> None:
    """K 線 tab:互動 plotly chart + 指標 multiselect + lookback slider + markers。

    控制項:
    - lookback 滑桿(60/120/180/360 days)
    - 指標 multiselect(MA20 / MA60 / Bollinger / Volume / RSI / MACD / KD / Stoch)
    - 標記 toggle:⭐ picks 歷史 / 🎯 持倉價位 / 🕯️ K 線形態

    Mobile-first:plotly responsive,iPhone 直接 swipe / pinch zoom。
    """
    from src.chart_renderer import (
        mark_pattern_signals,
        mark_pick_dates,
        mark_position_levels,
        render_candlestick_chart,
    )

    cols = st.columns([2, 3])
    with cols[0]:
        days = st.select_slider(
            "回看天數",
            options=[60, 120, 180, 360],
            value=120,
            key=f"detail_kline_lookback_{sid}",
        )
    with cols[1]:
        indicators = st.multiselect(
            "指標",
            options=[
                "MA20", "MA60", "Bollinger",
                "Volume", "RSI", "MACD", "KD", "Stoch",
            ],
            default=["MA20", "MA60", "Volume", "RSI", "MACD"],
            key=f"detail_kline_inds_{sid}",
        )

    mark_cols = st.columns(3)
    show_picks = mark_cols[0].toggle(
        "⭐ picks 歷史", value=True, key=f"detail_kline_picks_{sid}",
    )
    show_positions = mark_cols[1].toggle(
        "🎯 持倉價位", value=True, key=f"detail_kline_pos_{sid}",
    )
    show_patterns = mark_cols[2].toggle(
        "🕯️ K 線形態", value=False, key=f"detail_kline_pat_{sid}",
        help="需 candlestick_patterns 模組(B task);未上線時 toggle 不生效",
    )

    df = db.get_stock_kline_with_indicators(sid, days=int(days))
    if df.empty:
        st.info(
            f"📭 找不到 **{sid}** 的歷史日線。"
            "可能該股還沒進 daily_prices(關注後會自動補抓 90 天)。"
        )
        return
    st.caption(
        f"近 {len(df)} 個交易日 · {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}"
    )
    fig = render_candlestick_chart(
        sid, days=int(days), indicators=indicators, df=df,
    )
    if show_picks:
        mark_pick_dates(fig, sid)
    if show_positions:
        mark_position_levels(fig, sid)
    if show_patterns:
        mark_pattern_signals(fig, sid, days=int(days))
    st.plotly_chart(fig, use_container_width=True)


def _render_detail_chip_tab(sid: str) -> None:
    """籌碼 tab:近 7 日法人 stacked + 近 12 週千張戶。"""
    st.markdown("#### 近 7 日法人買賣超")
    inst = db.get_inst_history(sid, days=7)
    if not inst:
        st.info(
            "🔍 此股近期無法人籌碼資料。"
            "(覆蓋率有限,主要是高市值 / 關注清單)"
        )
    else:
        st.plotly_chart(_make_inst_stacked_bar(inst), use_container_width=True)

    st.markdown("#### 千張戶趨勢(近 12 週)")
    sh = db.get_shareholder_history(sid, weeks=12)
    if not sh:
        st.info(
            "📭 無千張戶資料。TDCC 週快照覆蓋限上市股,新上市或上櫃常無。"
        )
    else:
        st.plotly_chart(_make_shareholder_chart(sh), use_container_width=True)


def _render_detail_ml_tab(sid: str) -> None:
    """ML 解釋 tab:命中策略列表 + ML 分數 + SHAP top features + 歷史命中。"""
    st.markdown("#### 過去策略命中(近 30 筆)")
    hist = db.get_pick_history_for_sid(sid, limit=30)
    if not hist:
        st.info("📭 此股近期未被任一策略命中。")
    else:
        rows_df = pd.DataFrame([
            {
                "命中日": r["pick_date"],
                "策略": STRATEGY_LABELS.get(r["strategy"], r["strategy"]),
                "ML 機率": r["ml_prob"],
                "進場價": r["entry_close"],
                "D5 報酬 %": (
                    r["return_d5"] * 100 if r["return_d5"] is not None else None
                ),
                "達標": (
                    "✅" if r["hit_target"] == 1 else
                    "❌" if r["stopped_out"] == 1 else
                    ("—" if r["return_d5"] is None else "")
                ),
            }
            for r in hist
        ])
        st.dataframe(
            rows_df, use_container_width=True, hide_index=True,
            column_config={
                "ML 機率": st.column_config.NumberColumn(format="%.2f"),
                "進場價": st.column_config.NumberColumn(format="%.2f"),
                "D5 報酬 %": st.column_config.NumberColumn(format="%+.2f"),
            },
        )

    st.markdown("#### 最新 ML 解釋(SHAP top features)")
    shap = db.get_shap_for_sid_latest(sid)
    if not shap or not shap.get("top_features"):
        st.info("📭 該 sid 還沒有 SHAP 解釋(尚未進過 daily_picks 或未生成)。")
        return
    label = STRATEGY_LABELS.get(shap["strategy"], shap["strategy"])
    st.caption(
        f"取自 {shap['pick_date']} · 策略路由:{label}"
    )
    top_df = pd.DataFrame([
        {
            "feature": f.get("feature"),
            "value": f.get("value"),
            "contribution": f.get("contribution"),
            "contribution_pct": f.get("contribution_pct"),
            "direction": f.get("direction"),
        }
        for f in shap["top_features"][:8]
    ])
    st.dataframe(
        top_df, use_container_width=True, hide_index=True,
        column_config={
            "value": st.column_config.NumberColumn(format="%.3f"),
            "contribution": st.column_config.NumberColumn(format="%+.3f"),
            "contribution_pct": st.column_config.NumberColumn(format="%.2%"),
        },
    )


def _render_detail_warnings_section(sid: str) -> None:
    """⚠️ 警示紀錄 section(2026-05-15 主公拍板,違約交割教訓 root cause)。

    顯示該 sid 過去 90 天所有 stock_warnings(含已解除),時間軸排序
    (announced_date desc)。沒紀錄 → graceful skip 不顯整 section
    (避免乾淨股出現一行「無警示」雜訊)。

    Mobile-first:單欄 st.markdown,無 st.columns。已解除 vs 仍生效用色塊區分,
    iPhone 窄屏直接看到。
    """
    try:
        history = db.get_warning_history_for_sid(sid, days=90)
    except Exception:  # noqa: BLE001
        history = []
    if not history:
        return  # 乾淨股不顯 section,避免雜訊
    from src.warnings_filter import WARNING_TYPE_LABELS
    st.markdown("---")
    n_active = sum(1 for w in history if w.get("is_active"))
    title = f"#### ⚠️ 警示紀錄(近 90 天 · {len(history)} 筆"
    if n_active:
        title += f",仍生效 {n_active}"
    title += ")"
    st.markdown(title)
    if n_active:
        st.warning(
            f"⚠️ 此股目前有 {n_active} 筆警示生效中,進場前請評估風險"
        )
    for w in history:
        wt = str(w.get("warning_type", ""))
        label = WARNING_TYPE_LABELS.get(wt, wt)
        announced = w.get("announced_date") or "—"
        eff_from = w.get("effective_from")
        eff_to = w.get("effective_to")
        reason = w.get("reason") or ""
        is_active = bool(w.get("is_active"))
        # 仍生效紅色 / 已解除灰色
        badge_bg = "#d62728" if is_active else "#888"
        status = "生效中" if is_active else "已解除"
        period_str = ""
        if eff_from:
            period_str = f"{eff_from}"
            if eff_to:
                period_str += f" → {eff_to}"
            else:
                period_str += " → 持續"
        st.markdown(
            f"<div style='border-left:4px solid {badge_bg};"
            f"padding:6px 10px;margin:6px 0;background:rgba(128,128,128,0.05)'>"
            f"<div style='font-size:13px;font-weight:500'>"
            f"<span style='background:{badge_bg};color:#fff;padding:1px 6px;"
            f"border-radius:3px;font-size:11px;margin-right:6px'>{status}</span>"
            f"{label}</div>"
            f"<div style='font-size:12px;color:#888'>公告 {announced}"
            + (f" · 處置 {period_str}" if period_str else "")
            + "</div>"
            + (
                f"<div style='font-size:12px;margin-top:4px'>{reason[:200]}</div>"
                if reason else ""
            )
            + "</div>",
            unsafe_allow_html=True,
        )


def _render_detail_news_tab(sid: str) -> None:
    """新聞 tab:近 7 日該 sid 重大訊息列表。"""
    news = db.get_news_for_sid(sid, days=7)
    if not news:
        st.info("📭 近 7 日無重大訊息。")
        return
    st.caption(f"近 7 日共 {len(news)} 則重大訊息(最新先)")
    for n in news[:30]:  # 限 30 則避免爆 page
        # 時間 + 標題 一行;description 摺進 expander
        time_str = (n["publish_time"] or "")[:6]
        time_pretty = (
            f"{time_str[:2]}:{time_str[2:4]}" if len(time_str) >= 4 else ""
        )
        head = f"**{n['publish_date']}** {time_pretty}　{n['subject']}"
        with st.expander(head, expanded=False):
            if n.get("article_no"):
                st.caption(f"條款:{n['article_no']}")
            if n.get("fact_date"):
                st.caption(f"事實日:{n['fact_date']}")
            desc = n.get("description") or ""
            if desc:
                # 限 1200 字,新聞 raw 偶爾很長,避免 mobile 滾不完
                st.markdown(desc[:1200] + ("..." if len(desc) > 1200 else ""))


def _page_stock_detail() -> None:
    """📊 個股深度頁(C 計畫最後一件)。

    從任何 page 點 sid → set session_state['detail_sid'] + pending_nav
    → 本頁讀 detail_sid 渲染:
      Header → 4 個 tab(K 線 / 籌碼 / ML / 新聞)→ paper_trade 狀態。

    Mobile-first:無多欄 st.columns,改 st.tabs 4 個 tab,單屏切換。
    """
    st.header("📊 個股深度")
    db.init_db()

    # query param ?sid=2330 / session_state['detail_sid'] / 預設輸入
    default_sid = "2330"
    try:
        qp = st.query_params.get("sid")
    except Exception:  # noqa: BLE001
        qp = None
    if qp:
        default_sid = str(qp)
    if "detail_sid" in st.session_state and st.session_state["detail_sid"]:
        default_sid = str(st.session_state["detail_sid"])

    sid = st.text_input(
        "股號",
        value=default_sid,
        key="detail_sid_input",
        help="輸入 4 碼股號(例 2330)— 上方可從各 page 點「📊 看細節」直接跳入",
    ).strip()
    if not sid:
        st.info("請輸入股號。")
        return
    # 同步寫回 session_state(下次返回保留)
    st.session_state["detail_sid"] = sid

    _render_detail_header(sid)

    # 🎯 軍師判讀 — 主公看一眼就知道結論(2026-05-18)。優先於 tabs,
    # 整合 K 線形態 + 警示 + 大盤 + ML + 共識 + 題材 + 持倉 給 verdict。
    try:
        from src import individual_stock_verdict as _isv
        if _isv.is_enabled():
            st.markdown("---")
            _isv.render_stock_verdict(sid)
            st.markdown("---")
    except Exception as e:  # noqa: BLE001
        print(f"[VERDICT] render 失敗 sid={sid}: {type(e).__name__}: {e}", flush=True)

    tab_k, tab_chip, tab_ml, tab_news, tab_warn = st.tabs([
        "📈 K 線", "🚦 籌碼", "🧠 ML 解釋", "📰 新聞", "⚠️ 警示",
    ])
    with tab_k:
        _render_detail_kline_tab(sid)
    with tab_chip:
        _render_detail_chip_tab(sid)
    with tab_ml:
        _render_detail_ml_tab(sid)
    with tab_news:
        _render_detail_news_tab(sid)
    with tab_warn:
        # graceful skip 仍守在 _render_detail_warnings_section 內(乾淨股 early
        # return)。tab 內若空,補一個正向訊息避免空白 tab。
        try:
            _has_warnings = bool(
                db.get_warning_history_for_sid(sid, days=90)
            )
        except Exception:  # noqa: BLE001
            _has_warnings = False
        if _has_warnings:
            _render_detail_warnings_section(sid)
        else:
            st.success("✅ 此股近 90 天無警示紀錄")

    # K 線形態 section(B 進場時機強化,2026-05-17)— 近 30 日各形態統計。
    _render_detail_patterns_section(sid)

    # 💬 問軍師 — Gemini 對該 sid 做綜合判讀(C,2026-05-17)
    _render_detail_ask_ai_section(sid)

    st.markdown("---")
    st.markdown("#### 🧪 實測倉位")
    _render_detail_paper_trade_status(sid)


def _render_detail_ask_ai_section(sid: str) -> None:
    """個股深度頁:💬 問軍師(Gemini 對該 sid 綜合判讀)。"""
    from src import ai_assistant
    st.markdown("---")
    st.markdown("#### 💬 問軍師(AI)")
    if not ai_assistant.is_enabled():
        st.caption("💤 軍師目前停用(env AI_ASSISTANT_ENABLED=false)")
        return
    if not config.GEMINI_API_KEY:
        st.caption("⚠️ 缺 GEMINI_API_KEY,軍師無法工作")
        return
    q = st.text_input(
        "問題(留空 → 自動綜合判讀)",
        value="",
        key=f"detail_ai_q_{sid}",
        placeholder="例:技術面如何?要不要進場?",
    )
    if st.button(
        "🧙 問軍師",
        key=f"detail_ai_btn_{sid}",
        help="拉該股全部資料給 Gemini 做綜合判讀",
    ):
        with st.spinner("軍師思考中..."):
            res = ai_assistant.ask_about_stock(sid, q.strip())
        if not res.get("ok"):
            st.warning(res.get("answer") or "(無回應)")
        else:
            st.markdown(res["answer"])
            if res.get("context_summary"):
                st.caption(f"📊 軍師用的資料:{res['context_summary']}")


def _render_detail_patterns_section(sid: str) -> None:
    """個股深度頁:K 線形態 section — 近 30 日掃描各形態出現次數 + 最近一根命中。

    PATTERN_DETECTION_ENABLED=false → 顯停用提示。
    """
    from src import candlestick_patterns as _cp
    st.markdown("---")
    st.markdown("#### 📊 K 線形態(近 30 日)")
    if not _cp.is_enabled():
        st.caption("⚠️ K 線形態偵測已停用(PATTERN_DETECTION_ENABLED=false)")
        return
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT date, open, high, low, close FROM daily_prices "
                "WHERE stock_id=? AND open IS NOT NULL AND high IS NOT NULL "
                "AND low IS NOT NULL AND close IS NOT NULL "
                "ORDER BY date DESC LIMIT 30",
                (sid,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        st.warning("⚠️ 撈 daily_prices 失敗")
        return
    if not rows or len(rows) < 3:
        st.caption("⚠️ 歷史不足 3 日,無法判讀形態")
        return
    bars = [dict(r) for r in reversed(rows)]
    df_bars = pd.DataFrame(bars)
    # 逐根掃描(滑窗 last-3 ~ last-1)— 統計近 30 日各形態出現次數
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for end in range(3, len(df_bars) + 1):
        window = df_bars.iloc[:end]
        hits = _cp.detect_all_patterns(sid, window)
        for h in hits:
            n = h.get("name", "")
            counts[n] = counts.get(n, 0) + 1
            labels[n] = h.get("label", n)
    # 最近一根的命中形態 — 用白話呈現(主公看不懂術語)
    from src import individual_stock_verdict as _isv
    last_hits = _cp.detect_all_patterns(sid, df_bars)
    phrase = _isv.latest_pattern_phrase(last_hits or [])
    if last_hits:
        st.success(f"**最近一根**:{phrase}")
    else:
        st.info(f"**最近一根**:{phrase}")
    if counts:
        # 白話表(每形態附主公看懂的一句解釋)
        rows_view: list[dict] = []
        for n, c in sorted(counts.items(), key=lambda kv: -kv[1]):
            meaning = _isv.PATTERN_MEANINGS.get(n)
            if meaning:
                emoji_label, color_emoji, phrase = meaning
                row_label = f"{color_emoji} {emoji_label}"
            else:
                row_label = labels.get(n, n)
                phrase = ""
            rows_view.append({
                "形態": row_label,
                "白話解釋": phrase,
                "近 30 日次數": c,
            })
        df_counts = pd.DataFrame(rows_view)
        st.dataframe(df_counts, use_container_width=True, hide_index=True)
    else:
        st.caption("近 30 日無任何形態命中")


# === 回測頁 ===

def _page_backtest() -> None:
    st.header("📈 簡易回測")
    st.caption("把目前的短線策略套到歷史資料,看勝率與累積報酬。**不含交易成本/滑價/資金管理**。")

    # sidebar 參數(與短線頁同名,共用值;預設收起)
    with st.sidebar.expander("⚙️ 進階參數(回測)", expanded=False):
        vol_mult = st.number_input(
            "均量倍數", min_value=1.0, max_value=5.0,
            value=float(DEFAULT_SHORT_PARAMS["volume_multiplier"]), step=0.1,
            key="bt_vol_mult",
        )
        kd_low = st.number_input(
            "KD 門檻 K_low", min_value=0.0, max_value=80.0,
            value=float(DEFAULT_SHORT_PARAMS["kd_threshold_low"]), step=5.0,
            key="bt_kd_low",
        )
        inst_days = st.number_input(
            "法人連續買超天數", min_value=1, max_value=10,
            value=int(DEFAULT_SHORT_PARAMS["inst_buy_days"]), step=1,
            key="bt_inst_days",
        )
        hold_days = st.selectbox(
            "持有天數", [1, 3, 5, 10, 20], index=2,
            key="bt_hold_days",
            help="每筆入選收盤買進,持有 N 個交易日後收盤賣出",
        )

    # === 策略多選 ===
    from src.strategies import ALL_STRATEGIES, STRATEGY_LABELS
    strategy_keys = list(ALL_STRATEGIES.keys())
    st.markdown("##### 🧪 選股策略(多選 = OR,任一命中即買進)")
    enabled = st.multiselect(
        "策略",
        strategy_keys,
        default=["volume_kd"],
        format_func=lambda k: STRATEGY_LABELS.get(k, k),
        key="bt_strategies",
        label_visibility="collapsed",
    )
    if not enabled:
        st.warning("請至少選一套策略")

    # === 期間 preset 快選 ===
    # 用「最新交易日」當錨點(週末/假日不會選到沒資料的 today)
    latest = _get_default_screen_date()
    preset_label_to_days = {
        "近 30 日": 30, "近 60 日": 60, "近 90 日": 90,
        "近 180 日": 180, "自訂": None,
    }
    preset = st.radio(
        "回測期間",
        list(preset_label_to_days.keys()),
        index=2,  # default 近 90 日
        horizontal=True, key="bt_period_preset",
    )
    cols = st.columns([2, 2, 1])
    if preset == "自訂":
        start = cols[0].date_input(
            "回測起始", value=latest - timedelta(days=180), key="bt_start"
        )
        end = cols[1].date_input("回測結束", value=latest, key="bt_end")
    else:
        days = preset_label_to_days[preset]
        start = latest - timedelta(days=days)
        end = latest
        cols[0].metric("起始", start.isoformat())
        cols[1].metric("結束", end.isoformat())
    cols[2].markdown("&nbsp;", unsafe_allow_html=True)
    submit = cols[2].button(
        "執行回測",
        type="primary",
        use_container_width=True,
        disabled=not enabled,
    )

    if not submit:
        st.info(
            "選好參數與期間後按「執行回測」。\n\n"
            "**建議先按 sidebar『更新 50 檔大型股』補足歷史資料**(尤其雲端容器睡眠醒來後 cache 是空的)。"
            "首次跑半年期間約需 1–2 分鐘。"
        )
        return

    if start > end:
        st.error("回測起始不能晚於結束。")
        return

    universe = TW_TOP_50
    params = {
        "volume_multiplier": float(vol_mult),
        "kd_threshold_low": float(kd_low),
        "inst_buy_days": int(inst_days),
    }

    progress = st.progress(0.0, text="準備...")

    def cb(idx: int, total: int, d: str) -> None:
        progress.progress(idx / total, text=f"回測中 {idx}/{total}: {d}")

    with st.spinner("執行回測..."):
        try:
            from src.backtester import backtest_short  # lazy(此 module ~290ms cold)
            # 單一 volume_kd 走舊路徑(向下相容);多選 / 非預設走聚合路徑
            use_multi = (
                len(enabled) > 1 or (len(enabled) == 1 and enabled[0] != "volume_kd")
            )
            result = backtest_short(
                start.isoformat(), end.isoformat(),
                params=params,
                hold_days=int(hold_days),
                universe=universe,
                on_progress=cb,
                enabled_strategies=enabled if use_multi else None,
            )
        except Exception as e:  # noqa: BLE001
            progress.empty()
            st.error(f"回測失敗:{type(e).__name__}: {e}")
            return
    progress.empty()

    summary = result["summary"]
    if summary["trades"] == 0:
        st.info(
            "📭 期間內無入選個股(或無歷史資料)。可放寬參數、拉長期間,"
            "或先按 sidebar『更新 50 檔大型股』補資料。"
        )
        return

    # 8 格 metric (兩列 × 四欄)
    # 用 .get() 防禦式取值,避免後端 cache 落後或新增欄位時 UI 炸 KeyError
    g = summary.get
    hd = int(g("hold_days", 5) or 5)
    row1 = st.columns(4)
    row1[0].metric("交易次數", f"{g('trades', 0)}")
    row1[1].metric("勝率", f"{g('win_rate', 0.0):.1f}%")
    row1[2].metric("平均報酬/筆", f"{g('avg_return', 0.0):.2f}%")
    row1[3].metric("總報酬(複利)", f"{g('total_return', 0.0):.2f}%")

    row2 = st.columns(4)
    row2[0].metric("年化報酬", f"{g('annual_return', 0.0):.2f}%")
    row2[1].metric(
        "夏普比率",
        f"{g('sharpe', 0.0):.2f}",
        help=f"年化基準 √(252/{hd}) = {(252 / max(hd, 1)) ** 0.5:.2f}",
    )
    row2[2].metric(
        "最大回撤",
        f"-{g('max_drawdown', 0.0):.2f}%",
        help="累積報酬曲線從歷史峰值跌至谷底的最大跌幅",
    )
    row2[3].metric(
        "最大單筆",
        f"{g('max_win', 0.0):.2f}%",
        delta=f"最差 {g('max_loss', 0.0):.2f}%",
        delta_color="off",
    )

    st.caption(
        f"策略:{' + '.join(STRATEGY_LABELS.get(k, k) for k in enabled)} | "
        f"期間:{start.isoformat()} ~ {end.isoformat()} | "
        f"年化波動率:{g('annual_volatility', 0.0):.2f}%"
    )

    # 累積報酬曲線(含 0050 對比,失敗就不畫)
    st.markdown("### 📈 累積報酬曲線")
    st.plotly_chart(
        _make_equity_chart(result["equity_curve"], start.isoformat(), end.isoformat()),
        use_container_width=True,
    )

    # 單筆報酬分佈直方圖
    st.markdown("### 📊 單筆報酬分佈")
    st.plotly_chart(
        _make_returns_histogram(result["trades"]),
        use_container_width=True,
    )
    st.caption(
        "看分佈是否有極端值(肥尾)拉高 sharpe / 平均報酬。"
        "理想分佈是右偏(賺多賠少)。"
    )

    # 交易明細
    st.markdown(f"### 📋 交易明細({len(result['trades'])} 筆)")
    trades_df = result["trades"].sort_values("buy_date", ascending=False)
    view_mode = view_mode_toggle("backtest_view_mode")
    if view_mode == "🃏 卡片":
        # trade row 欄位:stock_id, buy_date, sell_date, buy_price, sell_price,
        #                  return_pct, holding_days
        for _, t in trades_df.iterrows():
            ret = t.get("return_pct", 0)
            with st.container(border=True):
                col1, col2 = st.columns([3, 2])
                with col1:
                    st.markdown(
                        f"**{t.get('stock_id', '?')}** "
                        f"{t.get('buy_date', '')} → {t.get('sell_date', '')}"
                    )
                    st.caption(
                        f"持有 {int(t.get('holding_days', 0))} 天 | "
                        f"買 {float(t.get('buy_price', 0)):.2f} → "
                        f"賣 {float(t.get('sell_price', 0)):.2f}"
                    )
                with col2:
                    # 台股慣例:正報酬紅 / 負報酬綠 — 一律 inverse
                    from src.ui_format import arrow_for
                    st.metric(
                        "報酬", f"{ret:+.2f}%",
                        delta=arrow_for(ret),
                        delta_color="inverse",
                    )
    else:
        st.dataframe(
            trades_df, use_container_width=True, hide_index=True,
        )


def _make_equity_chart(
    equity_curve: pd.Series,
    start: str,
    end: str,
) -> go.Figure:
    """累積報酬曲線。

    用 date 軸(不是 category),避免兩條線(策略 vs 0050)x 集合不同時
    plotly 把策略線壓到 0050 範圍內導致看不到。
    """
    fig = go.Figure()

    # 策略線 — index 從 'YYYY-MM-DD' str 轉 datetime
    if not equity_curve.empty:
        x_strategy = pd.to_datetime(list(equity_curve.index))
        fig.add_trace(go.Scatter(
            x=x_strategy, y=equity_curve.values,
            name="策略累積報酬",
            line=dict(color="#d62728", width=2.5),
            mode="lines",
        ))

    # 0050 大盤對比(失敗就不畫)
    try:
        bench = fetch_daily_price("0050", start, end)
        if not bench.empty and len(bench) >= 2:
            base = float(bench["close"].iloc[0])
            if base > 0:
                bench_returns = (bench["close"] / base - 1) * 100
                fig.add_trace(go.Scatter(
                    x=pd.to_datetime(bench["date"].tolist()),
                    y=bench_returns.tolist(),
                    name="0050 台灣 50",
                    line=dict(color="#1f77b4", width=2, dash="dot"),
                    mode="lines",
                ))
    except Exception:  # noqa: BLE001 — 抓不到大盤就不畫
        pass

    fig.add_hline(y=0, line_color="gray", opacity=0.5)
    fig.update_layout(
        height=400,
        margin=dict(t=20, b=20, l=20, r=20),
        legend=dict(orientation="h", y=1.02, x=0, font=dict(size=16)),
        font=dict(size=16),
        xaxis=dict(
            tickfont=dict(size=14),
            # 跳週末讓圖看起來連續(台股無交易)
            rangebreaks=[dict(bounds=["sat", "mon"])],
        ),
        yaxis=dict(tickfont=dict(size=14), title="累積報酬 (%)"),
    )
    return fig


def _make_returns_histogram(trades_df: pd.DataFrame) -> go.Figure:
    """單筆報酬分佈直方圖,用來肉眼判斷有沒有肥尾拉高 sharpe。"""
    fig = go.Figure()
    if trades_df.empty:
        return fig
    returns = trades_df["return_pct"]
    fig.add_trace(go.Histogram(
        x=returns,
        nbinsx=20,
        marker=dict(color="#1f77b4", line=dict(color="white", width=1)),
        name="次數",
    ))
    fig.add_vline(
        x=0, line_color="gray", line_dash="dash", opacity=0.7,
    )
    fig.add_vline(
        x=float(returns.mean()),
        line_color="#d62728", line_dash="dot",
        annotation_text=f"平均 {returns.mean():.2f}%",
        annotation_font=dict(size=14),
    )
    fig.update_layout(
        height=300,
        margin=dict(t=20, b=20, l=20, r=20),
        font=dict(size=16),
        xaxis=dict(tickfont=dict(size=14), title="單筆報酬率 (%)"),
        yaxis=dict(tickfont=dict(size=14), title="次數"),
        showlegend=False,
    )
    return fig


# === ⭐ 我的關注頁 ===

def _count_missing_history(sids: list[str], min_required: int = 15) -> int:
    """回有幾檔個股 daily_prices < min_required 筆(需 backfill)。"""
    if not sids:
        return 0
    placeholders = ",".join(["?"] * len(sids))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT stock_id, COUNT(*) AS c FROM daily_prices "
            f"WHERE stock_id IN ({placeholders}) GROUP BY stock_id",
            sids,
        ).fetchall()
    cnt_map = {r["stock_id"]: r["c"] for r in rows}
    return sum(1 for s in sids if cnt_map.get(s, 0) < min_required)


def _backfill_watchlist_history(sids: list[str], min_required: int = 15) -> int:
    """對 watchlist 中 daily_prices 不足 min_required 筆的個股自動補 90 天。

    回實際補了幾檔。預期 cache 命中時是 no-op。
    """
    if not sids:
        return 0
    placeholders = ",".join(["?"] * len(sids))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT stock_id, COUNT(*) AS c FROM daily_prices "
            f"WHERE stock_id IN ({placeholders}) GROUP BY stock_id",
            sids,
        ).fetchall()
    cnt_map = {r["stock_id"]: r["c"] for r in rows}
    need_fetch = [s for s in sids if cnt_map.get(s, 0) < min_required]
    if not need_fetch:
        return 0

    today_iso = date.today().isoformat()
    start_iso = (date.today() - timedelta(days=90)).isoformat()
    success = 0
    for sid in need_fetch:
        try:
            fetch_daily_price(sid, start_iso, today_iso)
            success += 1
        except Exception:  # noqa: BLE001
            pass
    return success


def _bulk_add_form(key_prefix: str = "wl_bulk") -> None:
    """渲染「批量加入 watchlist」表單(textarea + 按鈕),處理結果並 toast。"""
    with st.expander("➕ 批量加入(貼上多檔代號)", expanded=False):
        raw = st.text_area(
            "代號(逗號 / 空白 / 換行分隔皆可)",
            key=f"{key_prefix}_textarea",
            placeholder="2330, 2317, 3680\n2454 0050\n00878",
            height=100,
        )
        if st.button(
            "🚀 批量加入", key=f"{key_prefix}_btn", use_container_width=True,
        ):
            tokens = re.split(r"[\s,;]+", raw.strip()) if raw else []
            if not tokens:
                st.warning("請先貼上至少一個代號")
                return
            result = db.bulk_add_to_watchlist(tokens)
            ok = result["ok"]
            dup = result["dup"]
            invalid = result["invalid"]
            msgs: list[str] = []
            if ok:
                msgs.append(f"✅ 新加 {ok} 檔")
            if dup:
                msgs.append(f"⚠️ 重複 {dup} 檔(已在清單)")
            if invalid:
                inv_sample = ", ".join(result["invalid_ids"][:5])
                more = "..." if invalid > 5 else ""
                msgs.append(f"❌ 無效 {invalid} 檔 ({inv_sample}{more})")
            st.toast(" / ".join(msgs) if msgs else "什麼都沒處理", icon="📋")
            if ok:
                st.rerun()


def _remove_watchlist_row(sid: str) -> None:
    """表格列「🗑️ 移除」 button 的 callback — 移除 + 提示。

    rerun 由 Streamlit on_click 機制自動觸發,不用手動 st.rerun()。
    """
    db.remove_from_watchlist(sid)
    st.toast(f"已移除 {sid}", icon="🗑️")


def _on_watchlist_back_to_table() -> None:
    """關注頁「← 返回關注列表」 on_click callback。

    清掉 wl_detail_sid + bump wl_table_version → dataframe key 改變
    → Streamlit 視為新 widget,selection 自動清空(避免回列表後又被誤判
    成新點擊重新進 detail mode)。
    """
    st.session_state["wl_detail_sid"] = None
    v = st.session_state.get("wl_table_version", 0)
    st.session_state["wl_table_version"] = v + 1


def _on_inline_table_back(state_prefix: str) -> None:
    """Generic 「← 返回列表」 callback for _render_table_with_inline_detail。

    清掉 detail_sid + bump table_version,跟 watchlist 同 pattern。
    """
    st.session_state[f"{state_prefix}_detail_sid"] = None
    v = st.session_state.get(f"{state_prefix}_table_version", 0)
    st.session_state[f"{state_prefix}_table_version"] = v + 1


def _render_table_with_inline_detail(
    df: pd.DataFrame,
    state_prefix: str,
    column_config: dict,
    back_label: str = "← 返回列表",
    table_caption: str = "💡 點任一行 → 在本區展開該檔完整卡片",
    sid_column: str = "編號",
    name_column: str = "名稱",
    detail_button_prefix: str | None = None,
) -> bool:
    """五欄 dataframe + 點行 inline 展開完整卡片(reuse pattern from watchlist)。

    Caller 負責 build df with columns matching column_config keys,以及保留 sid/name
    欄(default 編號 / 名稱)。session_state 用 state_prefix 隔離不同 caller:
    - {prefix}_detail_sid: 當前展開的 sid(None = table mode)
    - {prefix}_table_version: dataframe widget key 版本號(返回時 +1 清 selection)

    detail_button_prefix:render_pick_card 的 button_key_prefix。預設 = state_prefix。

    踩過的坑(已內建):
    - dataframe selection 跨 mode 切換殘留 → 用 table_version bump 清
    - 卡片詳細 lazy expander 預設關閉 → 強制設 flag_key=True 讓全部 section 展開
    - render_pick_card 內部用 button_key_prefix + sid 組 widget key,prefix 必須跨 caller 唯一
    """
    if df is None or df.empty:
        st.caption("📭 沒有資料")
        return False

    if detail_button_prefix is None:
        detail_button_prefix = state_prefix

    detail_sid = st.session_state.get(f"{state_prefix}_detail_sid")
    valid_sids = {str(s) for s in df[sid_column].astype(str).tolist()}
    detail_valid = detail_sid and detail_sid in valid_sids

    if detail_valid:
        # === Detail mode:返回 button + 完整卡片 ===
        st.button(
            back_label,
            key=f"{state_prefix}_back_to_table",
            on_click=_on_inline_table_back,
            args=(state_prefix,),
        )
        sid = detail_sid
        # 📊 跳到「個股深度」頁(reuse pending_nav pattern;sid 已選好直接帶入)
        if st.button(
            "📊 看完整深度頁",
            key=f"{state_prefix}_jump_detail_{sid}",
            help="跳到「📊 個股深度」頁看 K 線 + 籌碼 + ML + 新聞",
        ):
            st.session_state["detail_sid"] = sid
            st.session_state["pending_nav"] = "📊 個股深度"
            st.rerun()
        # 從 df 拿該 row 算 close + change_pct(reuse 已 build 好的資料避免重複 SQL)
        row = df[df[sid_column].astype(str) == sid].iloc[0]
        close = row.get("目前股價")
        chg = row.get("漲幅")
        try:
            close = float(close) if close is not None and not pd.isna(close) else None
        except (TypeError, ValueError):
            close = None
        try:
            chg = float(chg) if chg is not None and not pd.isna(chg) else None
        except (TypeError, ValueError):
            chg = None
        # 補目標價 / 法人共識(detail 才需要,table 不顯)
        from src.analyst_targets import get_analyst_target
        tp = compute_target_prices(sid)
        card = {
            "stock_id": sid,
            "name": str(row.get(name_column, "") or ""),
            "close": close,
            "change_pct": chg,
        }
        if tp is not None:
            card.update({
                "target_low": tp.get("target_low"),
                "target_high": tp.get("target_high"),
                "stop_loss": tp.get("stop_loss"),
                "risk_reward": tp.get("risk_reward"),
            })
        at_row = get_analyst_target(sid)
        if at_row:
            card.update({
                "analyst_target_mean": at_row.get("target_mean"),
                "analyst_num": at_row.get("num_analysts"),
            })
        # U3 進場區間(ATR/BB based)— 算不出(< 20 天歷史)→ 不注入,卡片 graceful skip
        if close is not None:
            try:
                from src.notifier import compute_entry_range
                with db.get_conn() as _conn:
                    rng = compute_entry_range(sid, close, _conn)
                if rng is not None:
                    card["entry_low"], card["entry_high"] = rng
            except Exception:  # noqa: BLE001
                pass  # 任何錯誤 silent skip,不擋整張卡片
        cards_for_inject = _inject_intraday_quotes([card], [sid])
        # 強制展開 lazy detail section
        flag_key = f"card_exp_{detail_button_prefix}_{sid}"
        st.session_state[flag_key] = True
        render_picks_cards(
            cards_for_inject,
            show_signal=False, show_targets=True, show_change=True,
            show_add_button=True,
            button_key_prefix=detail_button_prefix,
        )
        return True   # detail 已 render → caller 通常 return 不再顯其他 section

    # === Table mode:dataframe with single-row select ===
    table_version = st.session_state.get(f"{state_prefix}_table_version", 0)
    selection = st.dataframe(
        df,
        use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row",
        key=f"{state_prefix}_table_v{table_version}",
        column_config=column_config,
    )
    st.caption(table_caption)

    if selection and selection.selection.rows:
        idx = selection.selection.rows[0]
        if 0 <= idx < len(df):
            clicked_sid = str(df.iloc[idx][sid_column])
            if st.session_state.get(f"{state_prefix}_detail_sid") != clicked_sid:
                st.session_state[f"{state_prefix}_detail_sid"] = clicked_sid
                st.rerun()
    return False


def _page_watchlist() -> None:
    st.header("⭐ 我的關注")
    db.init_db()

    # 雲端持久化警語(短線是 cloud reboot 容器會清光 SQLite,只 watchlist.csv 會被讀回)
    with st.expander("ℹ️ 關於關注清單持久化(雲端使用者注意)", expanded=False):
        st.markdown(
            "**雲端關注清單儲存方式**:\n"
            "- 雲端 SQLite 在容器重啟時會被清光 → 只有 "
            "`data/twse_snapshot/watchlist.csv` (commit 進 repo) 會被保留\n"
            "- 雲端 UI 加的關注是**暫時的**,直到下次 `daily_market_update` "
            "或 `backfill_history` workflow 跑完(會 dump 到 CSV 並 commit)\n"
            "- **永久保存的方法**:\n"
            "  1. 本機 git pull → 跑 streamlit → 加股票 → 跑 "
            "`python scripts/daily_market_update.py` → push commit\n"
            "  2. 或直接在 GitHub 網頁編 "
            "`data/twse_snapshot/watchlist.csv`(stock_id 一欄即可)"
        )

    items = db.get_watchlist()

    # 批量加入(空清單時也能用)
    _bulk_add_form()

    if not items:
        st.info(
            "目前沒有關注的個股。\n\n"
            "用上方「批量加入」貼代號,或到「🔍 個股」頁查詢後按 ☆ 圖示。"
        )
        return

    # === 雲端 export:把當前 watchlist 下載成 CSV,使用者自行 commit 永久化 ===
    # 雲端容器沒 git push 權限,在雲端 ☆ 加的東西重啟就沒;這顆按鈕讓使用者把當下狀態
    # 下載 → 覆蓋進 repo 的 data/twse_snapshot/watchlist.csv → push,讓下次 boot 還原。
    from src import watchlist_snapshot
    exp_cols = st.columns([1, 4])
    exp_cols[0].download_button(
        "📤 匯出 watchlist.csv",
        data=watchlist_snapshot.dump_to_string(),
        file_name=f"watchlist_{date.today().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        use_container_width=True,
    )
    exp_cols[1].caption(
        "下載後請 commit 進 repo (`data/twse_snapshot/watchlist.csv`) 才能跨容器永久保存。"
    )

    # 對歷史不足 15 日的個股(算不出 ATR)自動補 90 天 — 第一次會 spinner
    sids_in_wl = [it["stock_id"] for it in items]
    need_fetch_count = _count_missing_history(sids_in_wl, min_required=15)
    if need_fetch_count > 0:
        with st.spinner(f"補 {need_fetch_count} 檔歷史中(首次較慢)..."):
            backfilled = _backfill_watchlist_history(sids_in_wl, min_required=15)
        if backfilled > 0:
            st.toast(f"已補 {backfilled} 檔 90 日歷史", icon="📅")

    # 把關注清單組成 DataFrame:代號 / 名稱 / 收盤 / 漲跌% / MA5 / 備註
    rows = []
    sids = [it["stock_id"] for it in items]
    placeholders = ",".join(["?"] * len(sids))
    with db.get_conn() as conn:
        # 名稱
        name_rows = conn.execute(
            f"SELECT stock_id, name FROM stocks WHERE stock_id IN ({placeholders})",
            sids,
        ).fetchall()
        name_map = {r["stock_id"]: r["name"] for r in name_rows}

    # 對 name 為 None / 空 / 不存在的股票自動抓 FinMind 補
    # (例如上櫃股 3680 家登,初始不在 TW_TOP_50)
    for sid in sids:
        if not name_map.get(sid):
            info = ensure_stock_info(sid)
            if info and info.get("name"):
                name_map[sid] = info["name"]

    # 算「分析建議」欄(技術綜合評估 summary,例「多頭整理」/ 「盤整待方向」)
    # 走 _compute_technical_summary cache,同 sid 60s 內只算一次。
    # 歷史不足 / NaN → 顯「資料不足」(比「—」明顯,主公不會誤判成 bug)。
    from src.individual_sections import _compute_technical_summary

    target_prices: dict[str, dict | None] = {}  # 留給下方目標價區塊用
    for it in items:
        sid = it["stock_id"]
        # 取最新兩日 close 算漲跌
        with db.get_conn() as conn:
            recent = conn.execute(
                "SELECT date, close FROM daily_prices "
                "WHERE stock_id=? ORDER BY date DESC LIMIT 2",
                (sid,),
            ).fetchall()
        if recent:
            close = float(recent[0]["close"]) if recent[0]["close"] else None
            prev_close = (
                float(recent[1]["close"])
                if len(recent) > 1 and recent[1]["close"] else None
            )
            change_pct = (
                (close - prev_close) / prev_close * 100.0
                if (close and prev_close) else None
            )
        else:
            close = prev_close = change_pct = None
        target_prices[sid] = compute_target_prices(sid)

        # 分析建議(歷史不足 / NaN → 顯「資料不足」;exception → 顯「分析失敗」)
        try:
            tech = _compute_technical_summary(sid)
            advice = tech.get("summary")
            if not advice:
                advice = "資料不足"
        except Exception:  # noqa: BLE001
            advice = "分析失敗"

        rows.append({
            "編號": sid,
            "名稱": name_map.get(sid, "—"),
            "目前股價": close,        # float | None
            "漲幅": change_pct,       # float | None
            "分析建議": advice,       # str
        })

    df = pd.DataFrame(rows)

    # 主公拍板:點擊行 → inline 展開完整卡片(不跳頁)
    # reuse _render_table_with_inline_detail helper(state_prefix="wl"),內部
    # 自動處理 detail / table mode 切換 + table_version bump 清 selection 殘留。
    # helper 回 True = detail mode 已 render(skip 後面的 目標價參考 / 編輯)。
    showed_detail = _render_table_with_inline_detail(
        df,
        state_prefix="wl",
        column_config={
            "編號": st.column_config.TextColumn("編號", width="small"),
            "名稱": st.column_config.TextColumn("名稱", width="medium"),
            "目前股價": st.column_config.NumberColumn(
                "目前股價", format="%.2f", width="small",
            ),
            "漲幅": st.column_config.NumberColumn(
                "漲幅 %", format="%+.2f%%", width="small",
            ),
            "分析建議": st.column_config.TextColumn("分析建議", width="medium"),
        },
        back_label="← 返回關注列表",
        table_caption="💡 點任一行 → 在本頁展開該檔完整卡片(K 線、技術、法人目標價)",
        detail_button_prefix="wl_detail",
    )
    if showed_detail:
        return

    # === 🎯 目標價參考(每檔一行 markdown bullet,跟 Telegram 推播格式一致) ===
    st.markdown("### 🎯 目標價參考")
    bullet_lines: list[str] = []
    for it in items:
        sid = it["stock_id"]
        name = name_map.get(sid, "—")
        tp = target_prices.get(sid)
        if tp is None:
            bullet_lines.append(
                f"- **{sid} {name}**:(cache 缺資料,請按 sidebar 更新)"
            )
        else:
            rr_str = (
                f" (R:R {tp['risk_reward']:.1f}:1)"
                if tp.get("risk_reward") else ""
            )
            bullet_lines.append(
                f"- **{sid} {name}**(收 {tp['close']:.2f}):"
                f"🎯 保守 {tp['target_low']:.2f} / "
                f"🚀 積極 {tp['target_high']:.2f} / "
                f"🛑 停損 {tp['stop_loss']:.2f}{rr_str}"
            )
    st.markdown("\n".join(bullet_lines))
    st.caption(
        "⚠️ 目標價為 ATR(14) 統計參考,**非實際預測**。"
        "公式:🎯 = close + 1.5×ATR / 🚀 = close + 3×ATR / 🛑 = close − 1.5×ATR"
    )

    st.markdown("---")
    st.markdown("### 編輯")
    c1, c2 = st.columns(2)
    edit_sid = c1.selectbox(
        "選個股", [it["stock_id"] for it in items], key="wl_edit_sid",
    )
    new_note = c2.text_input("新備註", key="wl_new_note")
    bcols = st.columns(3)
    if bcols[0].button("💾 更新備註", use_container_width=True):
        db.add_to_watchlist(edit_sid, note=new_note)
        st.toast(f"{edit_sid} 備註已更新", icon="💾")
        st.rerun()
    if bcols[1].button("🗑️ 從關注移除", use_container_width=True):
        if db.remove_from_watchlist(edit_sid):
            st.toast(f"已移除 {edit_sid}", icon="🗑️")
            st.rerun()


# === 🌡️ 市場熱度頁 ===

def _page_market_heat() -> None:
    """市場熱度三 tab:🔥 熱門 / 🚀 漲停 / 💥 跌停反轉。

    純走 SQLite daily_prices(src.limit_movers),~ 毫秒。每 tab 用
    _render_table_with_inline_detail 渲染:list mode 5 欄表格 + 點行 inline
    展開完整卡片(不跳頁,跟關注頁同 pattern)。

    跌停反轉 = 當日 ret ≤ -9.95% AND 前 5 日內 ≥ 1 日 ret ≥ +9.95%
    (「飆完反轉」/ 逃命波警訊),跟普通跌停區別開。
    """
    st.header("🌡️ 市場熱度")
    from src.limit_movers import (
        get_hot_stocks, get_limit_up, get_limit_down_after_up,
    )
    tab_hot, tab_up, tab_down = st.tabs(
        ["🔥 熱門", "🚀 漲停", "💥 跌停反轉"],
    )
    with tab_hot:
        df_hot = get_hot_stocks(n=30)
        if df_hot.empty:
            st.caption("📭 沒抓到熱門股(daily_prices 沒今日資料?)")
        else:
            _render_table_with_inline_detail(
                df_hot,
                state_prefix="dash_hot",
                column_config={
                    "編號": st.column_config.TextColumn("編號", width="small"),
                    "名稱": st.column_config.TextColumn("名稱", width="medium"),
                    "目前股價": st.column_config.NumberColumn(
                        "目前股價", format="%.2f", width="small",
                    ),
                    "漲幅": st.column_config.NumberColumn(
                        "漲幅 %", format="%+.2f%%", width="small",
                    ),
                    "成交金額(億)": st.column_config.NumberColumn(
                        "成交金額 (億)", format="%.1f", width="small",
                    ),
                },
                back_label="← 返回熱門股列表",
                table_caption=(
                    "🔥 當日成交金額 Top 30。點任一行 → 展開完整卡片"
                ),
                detail_button_prefix="dash_hot_detail",
            )
    with tab_up:
        df_up = get_limit_up()
        if df_up.empty:
            st.caption("📭 今日無漲停股(或資料尚未更新)")
        else:
            _render_table_with_inline_detail(
                df_up,
                state_prefix="dash_up",
                column_config={
                    "編號": st.column_config.TextColumn("編號", width="small"),
                    "名稱": st.column_config.TextColumn("名稱", width="medium"),
                    "目前股價": st.column_config.NumberColumn(
                        "目前股價", format="%.2f", width="small",
                    ),
                    "漲幅": st.column_config.NumberColumn(
                        "漲幅 %", format="%+.2f%%", width="small",
                    ),
                    "成交金額(億)": st.column_config.NumberColumn(
                        "成交金額 (億)", format="%.1f", width="small",
                    ),
                },
                back_label="← 返回漲停股列表",
                table_caption=(
                    "🚀 當日 ret ≥ +9.95%。點任一行 → 展開完整卡片"
                ),
                detail_button_prefix="dash_up_detail",
            )
    with tab_down:
        df_down = get_limit_down_after_up(window=5)
        if df_down.empty:
            st.caption("📭 今日無「跌停反轉」股(逃命波警訊條件未觸發)")
        else:
            _render_table_with_inline_detail(
                df_down,
                state_prefix="dash_down",
                column_config={
                    "編號": st.column_config.TextColumn("編號", width="small"),
                    "名稱": st.column_config.TextColumn("名稱", width="medium"),
                    "目前股價": st.column_config.NumberColumn(
                        "目前股價", format="%.2f", width="small",
                    ),
                    "漲幅": st.column_config.NumberColumn(
                        "漲幅 %", format="%+.2f%%", width="small",
                    ),
                    "前 N 日漲停日": st.column_config.TextColumn(
                        "前 5 日漲停日", width="medium",
                    ),
                },
                back_label="← 返回跌停反轉列表",
                table_caption=(
                    "💥 當日 ret ≤ -9.95% AND 前 5 日內 ≥ 1 日漲停。"
                    "「飆完反轉」/ 逃命波警訊。點任一行 → 展開完整卡片"
                ),
                detail_button_prefix="dash_down_detail",
            )


# === 👥 大戶入場頁 ===

def _page_big_buyer() -> None:
    """大戶入場三 tab:🚀 本週暴增 / 📈 連續增加 / 🏰 絕對占比。

    純走 SQLite shareholder_concentration JOIN stocks JOIN daily_prices,
    全部 helpers 已 LEFT JOIN ML 分數 / 收盤,App 端直接攤平成 DataFrame
    + _render_table_with_inline_detail 渲染(同關注 / 市場熱度頁 pattern)。

    Mobile-first:純 st.dataframe,不用 st.columns 分區塊
    (參考 src/ui_cards.py:469 已定的桌面 + iPhone narrow 兩種版型準則)。

    第一次跑時資料只一週 → 「連續增加」tab 顯 st.info「資料累積中」提示。
    """
    st.header("👥 大戶入場")

    db.init_db()  # 雲端容器重啟 / 第一次 boot 時保險

    tab_movers, tab_streak, tab_top = st.tabs(
        ["🚀 本週暴增", "📈 連續增加", "🏰 絕對占比"],
    )

    # 共用 column_config:7 欄(代號 / 名稱 / 收盤 / 千張戶 / 週變 / 占比 / ML 分數)
    _col_cfg = {
        "編號": st.column_config.TextColumn("編號", width="small"),
        "名稱": st.column_config.TextColumn("名稱", width="medium"),
        "目前股價": st.column_config.NumberColumn(
            "收盤", format="%.2f", width="small",
        ),
        "千張戶": st.column_config.NumberColumn(
            "千張戶", format="%d", width="small",
        ),
        "週變": st.column_config.TextColumn("週變", width="small"),
        "占比": st.column_config.TextColumn("占比", width="small"),
        "ML 分數": st.column_config.TextColumn("ML 分數", width="small"),
        # 漲幅(隱藏欄位)是給 inline detail 卡 reuse 算 change_pct 用,
        # NumberColumn 必須 keep 否則 _render_table_with_inline_detail 拿不到。
        "漲幅": None,
    }

    def _to_df(rows: list[dict]) -> "pd.DataFrame":
        """helper rows → UI DataFrame。週變 / 占比 / ML 缺資料顯「—」/「N/A」。"""
        if not rows:
            return pd.DataFrame(columns=[
                "編號", "名稱", "目前股價", "千張戶", "週變",
                "占比", "ML 分數", "漲幅",
            ])
        out: list[dict] = []
        for r in rows:
            dw = r.get("holders_delta_w")
            pct = r.get("holders_pct")
            ml = r.get("ml_prob")
            out.append({
                "編號": r["sid"],
                "名稱": r.get("name") or "—",
                "目前股價": r.get("close"),
                "千張戶": (
                    int(r["holders_1000up_count"])
                    if r.get("holders_1000up_count") is not None else None
                ),
                "週變": (
                    f"{dw:+d}" if dw is not None else "—"
                ),
                "占比": (
                    f"{pct * 100:.2f}%" if pct is not None else "—"
                ),
                "ML 分數": (
                    f"{ml:.2f}" if ml is not None else "N/A"
                ),
                "漲幅": None,   # 暫不算每日漲幅(週快照 cross-sid 排行不需要)
            })
        return pd.DataFrame(out)

    with tab_movers:
        rows = db.get_top_shareholder_movers(limit=30)
        df = _to_df(rows)
        if df.empty:
            st.caption("📭 沒抓到大戶暴增股(shareholder_concentration 沒資料?)")
        else:
            _render_table_with_inline_detail(
                df,
                state_prefix="bb_mover",
                column_config=_col_cfg,
                back_label="← 返回暴增列表",
                table_caption=(
                    "🚀 本週千張戶增加最多 Top 30(delta_w > 0)。"
                    "點任一行 → 展開完整卡片"
                ),
                detail_button_prefix="bb_mover_detail",
            )

    with tab_streak:
        # 資料只一週時這 tab 永遠回空 — 顯資料累積中提示
        st.info(
            "📅 資料累積中,下週六起『週變』欄位會有完整數據"
        )
        rows = db.get_consecutive_shareholder_increases(weeks=2, limit=30)
        df = _to_df(rows)
        if df.empty:
            st.caption(
                "📭 目前沒有「連 2 週千張戶增加」的個股"
                "(需等資料累積至少兩週)"
            )
        else:
            _render_table_with_inline_detail(
                df,
                state_prefix="bb_streak",
                column_config=_col_cfg,
                back_label="← 返回連續增加列表",
                table_caption=(
                    "📈 連續 ≥ 2 週千張戶增加。點任一行 → 展開完整卡片"
                ),
                detail_button_prefix="bb_streak_detail",
            )

    with tab_top:
        rows = db.get_top_shareholder_concentration(limit=30)
        df = _to_df(rows)
        if df.empty:
            st.caption("📭 沒抓到大戶占比資料")
        else:
            _render_table_with_inline_detail(
                df,
                state_prefix="bb_top",
                column_config=_col_cfg,
                back_label="← 返回占比列表",
                table_caption=(
                    "🏰 千張戶占比最高 Top 30。點任一行 → 展開完整卡片"
                ),
                detail_button_prefix="bb_top_detail",
            )


def _render_theme_heat_section() -> None:
    """渲染「📡 題材熱度排行」section — 9 大題材近 5 日 heat_score + multiplier badge。

    Mobile-first:純 dataframe 單欄,不用 st.columns 分區塊。給「📋 系統結論」
    頁 + 「📊 強者跟蹤」高信心精選 tab 共用,點兩處進來看到一樣。

    Kill-switch:env THEME_HEAT_ENABLED=false → caption 提示已關閉。
    DB / yaml 任何例外 → graceful info,不擋整頁渲染。
    """
    from src import theme_heat as _th

    st.markdown("### 📡 題材熱度排行(近 5 日)")
    if not _th._is_enabled():
        st.info(
            "🚫 題材熱度動態權重已關閉 "
            "(環境變數 THEME_HEAT_ENABLED=false)。"
        )
        return

    db.init_db()
    try:
        with db.get_conn() as conn:
            heat = _th.compute_theme_heat(conn)
    except Exception as e:  # noqa: BLE001
        st.warning(f"題材熱度計算失敗:{type(e).__name__}: {e}")
        return

    if not heat:
        st.info("📭 無題材設定檔(data/themes/*.yaml)或全題材無 daily_prices")
        return

    rows = []
    for theme_key, info in heat.items():
        m = info.get("multiplier")
        # None = cold = hard exclude;UI 顯示「🚫 擋」取代「×0.7」
        if m is None:
            weight_str = "🚫 擋"
        else:
            weight_str = f"×{float(m):.2f}"
        rows.append({
            "題材": info.get("display_name") or theme_key,
            "成分股": f"{info.get('n_valid', 0)} / {info.get('n_total', 0)}",
            "5日均漲幅": f"{info.get('avg_return', 0.0):+.2f}%",
            "勝率": f"{info.get('win_rate', 0.0) * 100:.0f}%",
            "熱度分": f"{info.get('heat_score', 0.0):+.2f}",
            "權重": weight_str,
            "判定": info.get("badge", "➖"),
        })
    rows.sort(
        key=lambda r: -float(r["熱度分"].replace("+", "").rstrip()),
    )
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(
        "🔥 熱題材(權重 ×1.3 加分)= 5 日均漲 > 3% 且勝率 > 50%  · "
        "🚫 冷題材(整題材成分股不推播)= 均漲 < -2% 或勝率 < 30%  · "
        "➖ 中性(×1.0 照常)。"
        "跨題材 sid 取最熱(熱不被冷稀釋);只在冷題材的 sid 才會被擋。"
        "不在任何題材的 sid 照常推薦(沒題材 ≠ 冷)。"
    )


# === 📊 強者跟蹤頁(報告 docs/dage-feature-scope.md 方案 D)===

def _page_strong_follower() -> None:
    """強者跟蹤三 tab:🏛️ 法人共識榜 / 🐋 千張大戶進場榜 / 🎯 綜合排行。

    報告 docs/dage-feature-scope.md 方案 D:用既有訊號(三大法人連買 +
    千張大戶人數變化)組合,**不**抓新資料源 / **不**做分點。

    法律警示(報告第 7.2 節):中性語言 / 不點名分點 / 常駐 disclaimer。

    Mobile-first:純 st.dataframe + _render_table_with_inline_detail 渲染,
    不用 st.columns 分區塊(同 _page_big_buyer pattern)。

    各 tab 資料源:
    - 法人共識榜 → db.get_top_inst_consensus(min_days=2)
    - 千張大戶進場榜 → db.get_top_shareholder_movers(reuse 大戶入場頁)
    - 綜合排行 → db.get_strong_follower_composite(交集 + rank-normalize)
    - 高信心精選 → db.get_strong_follower_premium(三維交集 + 推薦理由)
    """
    st.header("📊 強者跟蹤")
    st.caption(
        "🔍 籌碼異動綜合視角:三大法人連買 + 千張大戶進場 + 兩者交集。"
        "資料源 = 既有 institutional / shareholder_concentration,無新爬蟲。"
    )
    # 法律 disclaimer(報告 7.2 強烈建議,常駐顯示)
    st.warning(
        "⚠️ **資訊僅供個人投資決策參考,非投資建議。**"
        "本工具不對使用結果負任何責任,投資請自行評估。"
    )

    db.init_db()  # 雲端容器重啟 / 第一次 boot 時保險(同 _page_big_buyer)

    # 大盤 regime banner — 把 5 種(bull / weak_bull / sideways / bear / unknown)
    # 收斂成 3 桶顯示,熊市給強烈視覺警示。bear 時把 premium top_n 從 10 降到 5
    # (主公拍板:逆風期收緊精選張數,避免追高)。
    from src.market_regime import compute_regime
    regime_info = compute_regime()
    sf_regime = regime_info["regime"]
    if sf_regime == "bull":
        st.success("🟢 **大盤偏多** — 可較積極配置")
    elif sf_regime == "bear":
        st.error(
            "🔴 **大盤偏空,小心追高** — "
            "高信心精選自動收緊至 Top 5,建議分批佈局"
        )
    elif sf_regime in ("weak_bull", "sideways"):
        st.warning("🟡 **大盤盤整** — 籌碼共識為主,觀察突破方向")
    else:
        # unknown(資料不足 60 天)→ 不顯 banner,避免誤導
        pass

    # 熊市自動降 top_n:premium tab 的查詢 top_n 由 regime 決定
    sf_premium_top_n = 5 if sf_regime == "bear" else 10

    tab_inst, tab_holders, tab_combined, tab_premium = st.tabs(
        ["🏛️ 法人共識榜", "🐋 千張大戶進場榜", "🎯 綜合排行", "✨ 高信心精選"],
    )

    # 共用 column_config(對齊 _page_big_buyer 的 7 欄但欄位語意調整):
    #   編號 / 名稱 / 收盤 / 法人 net / 千張週變 / ML 分數 / 漲幅(隱藏)
    def _to_df_inst(rows: list[dict]) -> "pd.DataFrame":
        """法人共識榜 rows → UI DataFrame。"""
        if not rows:
            return pd.DataFrame(columns=[
                "編號", "名稱", "目前股價", "法人 net (張)",
                "共識天數", "ML 分數", "漲幅",
            ])
        out: list[dict] = []
        for r in rows:
            net = r.get("inst_net_total")
            cd = r.get("consensus_days")
            ml = r.get("ml_prob")
            out.append({
                "編號": r["sid"],
                "名稱": r.get("name") or "—",
                "目前股價": r.get("close"),
                # institutional 單位 = 張(已是)。報告 / TWSE 慣例:正 = 買超
                "法人 net (張)": (
                    f"+{net:,}" if net is not None and net > 0
                    else (str(net) if net is not None else "—")
                ),
                "共識天數": (f"{cd} 日" if cd is not None else "—"),
                "ML 分數": (
                    f"{ml:.2f}" if ml is not None else "N/A"
                ),
                "漲幅": None,
            })
        return pd.DataFrame(out)

    def _to_df_holders(rows: list[dict]) -> "pd.DataFrame":
        """千張大戶進場榜 rows → UI DataFrame(對齊 _page_big_buyer 的 _to_df)。"""
        if not rows:
            return pd.DataFrame(columns=[
                "編號", "名稱", "目前股價", "千張戶", "週變", "占比",
                "ML 分數", "漲幅",
            ])
        out: list[dict] = []
        for r in rows:
            dw = r.get("holders_delta_w")
            pct = r.get("holders_pct")
            ml = r.get("ml_prob")
            out.append({
                "編號": r["sid"],
                "名稱": r.get("name") or "—",
                "目前股價": r.get("close"),
                "千張戶": (
                    int(r["holders_1000up_count"])
                    if r.get("holders_1000up_count") is not None else None
                ),
                "週變": (f"{dw:+d}" if dw is not None else "—"),
                "占比": (f"{pct * 100:.2f}%" if pct is not None else "—"),
                "ML 分數": (f"{ml:.2f}" if ml is not None else "N/A"),
                "漲幅": None,
            })
        return pd.DataFrame(out)

    def _to_df_combined(rows: list[dict]) -> "pd.DataFrame":
        """綜合排行 rows → UI DataFrame。"""
        if not rows:
            return pd.DataFrame(columns=[
                "編號", "名稱", "目前股價", "綜合分數", "法人 net (張)",
                "千張週變", "ML 分數", "漲幅",
            ])
        out: list[dict] = []
        for r in rows:
            score = r.get("composite_score")
            net = r.get("inst_net_total")
            dw = r.get("holders_delta_w")
            ml = r.get("ml_prob")
            out.append({
                "編號": r["sid"],
                "名稱": r.get("name") or "—",
                "目前股價": r.get("close"),
                "綜合分數": (
                    f"{score:.2f}" if score is not None else "—"
                ),
                "法人 net (張)": (
                    f"+{net:,}" if net is not None and net > 0
                    else (str(net) if net is not None else "—")
                ),
                "千張週變": (f"{dw:+d}" if dw is not None else "—"),
                "ML 分數": (f"{ml:.2f}" if ml is not None else "N/A"),
                "漲幅": None,
            })
        return pd.DataFrame(out)

    def _to_df_premium(rows: list[dict]) -> "pd.DataFrame":
        """高信心精選 rows → UI DataFrame(三維交集 + composite score)。"""
        if not rows:
            return pd.DataFrame(columns=[
                "編號", "名稱", "目前股價", "法人天數", "千張戶", "週變",
                "ML分數", "綜合分", "漲幅",
            ])
        out: list[dict] = []
        for r in rows:
            cd = r.get("consensus_days")
            h1k = r.get("holders_1000up_count")
            dw = r.get("holders_delta_w")
            ml = r.get("ml_prob")
            score = r.get("composite_score")
            out.append({
                "編號": r["sid"],
                "名稱": r.get("name") or "—",
                "目前股價": r.get("close"),
                "法人天數": (f"{cd} 日" if cd is not None else "—"),
                "千張戶": (int(h1k) if h1k is not None else None),
                "週變": (f"{dw:+d}" if dw is not None else "—"),
                "ML分數": (f"{ml:.2f}" if ml is not None else "N/A"),
                "綜合分": (f"{score:.2f}" if score is not None else "—"),
                "漲幅": None,
            })
        return pd.DataFrame(out)

    with tab_inst:
        rows = db.get_top_inst_consensus(min_days=2, limit=30)
        df = _to_df_inst(rows)
        if df.empty:
            st.caption(
                "📭 暫無「三大法人連 2 日同時買超」個股"
                "(institutional 資料不足或市場無共識)"
            )
        else:
            _render_table_with_inline_detail(
                df,
                state_prefix="sf_inst",
                column_config={
                    "編號": st.column_config.TextColumn("編號", width="small"),
                    "名稱": st.column_config.TextColumn("名稱", width="medium"),
                    "目前股價": st.column_config.NumberColumn(
                        "收盤", format="%.2f", width="small",
                    ),
                    "法人 net (張)": st.column_config.TextColumn(
                        "法人 net", width="small",
                    ),
                    "共識天數": st.column_config.TextColumn(
                        "連買", width="small",
                    ),
                    "ML 分數": st.column_config.TextColumn(
                        "ML 分數", width="small",
                    ),
                    "漲幅": None,
                },
                back_label="← 返回法人共識榜",
                table_caption=(
                    "🏛️ 三大法人(外資 / 投信 / 自營商)連 2 日同時買超 Top 30,"
                    "按期間 net 加總 desc。點任一行 → 展開完整卡片"
                ),
                detail_button_prefix="sf_inst_detail",
            )

    with tab_holders:
        rows = db.get_top_shareholder_movers(limit=30)
        df = _to_df_holders(rows)
        if df.empty:
            st.caption(
                "📭 暫無千張大戶進場個股"
                "(shareholder_concentration 資料尚未到位)"
            )
        else:
            _render_table_with_inline_detail(
                df,
                state_prefix="sf_holders",
                column_config={
                    "編號": st.column_config.TextColumn("編號", width="small"),
                    "名稱": st.column_config.TextColumn("名稱", width="medium"),
                    "目前股價": st.column_config.NumberColumn(
                        "收盤", format="%.2f", width="small",
                    ),
                    "千張戶": st.column_config.NumberColumn(
                        "千張戶", format="%d", width="small",
                    ),
                    "週變": st.column_config.TextColumn("週變", width="small"),
                    "占比": st.column_config.TextColumn("占比", width="small"),
                    "ML 分數": st.column_config.TextColumn(
                        "ML 分數", width="small",
                    ),
                    "漲幅": None,
                },
                back_label="← 返回大戶進場榜",
                table_caption=(
                    "🐋 本週千張戶人數增加最多 Top 30(本土主力進場跡象)。"
                    "點任一行 → 展開完整卡片"
                ),
                detail_button_prefix="sf_holders_detail",
            )

    with tab_combined:
        st.caption(
            "🔮 雙重訊號交集:**法人連 2 日共識買超**且**最新一週千張大戶人數增加**。"
            "綜合分數 = 兩個訊號各自 rank 歸一化後加總,範圍 (0, 2]。"
        )
        st.caption(
            "📌 註:融資融券熱度(per-stock)資料源規劃中,目前綜合僅含法人 + 千張兩條訊號。"
        )
        rows = db.get_strong_follower_composite(min_inst_days=2, limit=30)
        df = _to_df_combined(rows)
        if df.empty:
            st.caption(
                "📭 暫無「法人共識 ∩ 千張大戶進場」交集個股"
                "(兩個訊號獨立資料中,等下一波籌碼共識)"
            )
        else:
            _render_table_with_inline_detail(
                df,
                state_prefix="sf_combined",
                column_config={
                    "編號": st.column_config.TextColumn("編號", width="small"),
                    "名稱": st.column_config.TextColumn("名稱", width="medium"),
                    "目前股價": st.column_config.NumberColumn(
                        "收盤", format="%.2f", width="small",
                    ),
                    "綜合分數": st.column_config.TextColumn(
                        "分數", width="small",
                    ),
                    "法人 net (張)": st.column_config.TextColumn(
                        "法人 net", width="small",
                    ),
                    "千張週變": st.column_config.TextColumn(
                        "千張", width="small",
                    ),
                    "ML 分數": st.column_config.TextColumn(
                        "ML 分數", width="small",
                    ),
                    "漲幅": None,
                },
                back_label="← 返回綜合排行",
                table_caption=(
                    "🎯 兩個籌碼訊號交集,綜合分數 desc。點任一行 → 展開完整卡片"
                ),
                detail_button_prefix="sf_combined_detail",
            )

    with tab_premium:
        # 題材熱度排行(主公 2026-05-15 加)— 高信心精選 tab 上方先看題材輪動,
        # 再看精選個股是否屬熱題材。
        _render_theme_heat_section()
        st.markdown("---")

        st.caption(
            "✨ **三維交集精選**:法人連買 ≥ 3 天 + 千張戶週增 + ML 高信心。"
            "綜合分 = 法人 0.4 + 千張 0.4 + ML 0.2 rank-normalize 加權。"
        )
        st.caption(
            "📌 註:DB 無 ML cache 時自動 fallback 用前 2 維(0.5 / 0.5),"
            "推薦理由會省略 ML 段。"
        )
        rows = db.get_strong_follower_premium(
            min_inst_days=3, min_delta_w=1, top_n=sf_premium_top_n,
        )
        df = _to_df_premium(rows)
        if df.empty:
            st.info(
                "📋 今日無三維交集標的,"
                "可去 🎯 綜合排行 看放寬條件版本"
            )
        else:
            showed_detail = _render_table_with_inline_detail(
                df,
                state_prefix="sf_premium",
                column_config={
                    "編號": st.column_config.TextColumn("編號", width="small"),
                    "名稱": st.column_config.TextColumn("名稱", width="medium"),
                    "目前股價": st.column_config.NumberColumn(
                        "收盤", format="%.2f", width="small",
                    ),
                    "法人天數": st.column_config.TextColumn(
                        "法人", width="small",
                    ),
                    "千張戶": st.column_config.NumberColumn(
                        "千張戶", format="%d", width="small",
                    ),
                    "週變": st.column_config.TextColumn("週變", width="small"),
                    "ML分數": st.column_config.TextColumn(
                        "ML", width="small",
                    ),
                    "綜合分": st.column_config.TextColumn(
                        "綜合", width="small",
                    ),
                    "漲幅": None,
                },
                back_label="← 返回高信心精選",
                table_caption=(
                    f"✨ 三維交集 Top {sf_premium_top_n},綜合分數 desc。"
                    "點任一行 → 展開完整卡片"
                ),
                detail_button_prefix="sf_premium_detail",
            )
            # 表格模式才顯示推薦理由(detail mode 不重複)
            if not showed_detail:
                st.caption("**🎯 推薦理由**")
                for r in rows:
                    name = r.get("name") or "—"
                    st.caption(
                        f"`{r['sid']}` {name} — {r.get('reason_text', '')}"
                    )


# === 📊 大盤情緒頁 ===

def _page_market_sentiment() -> None:
    st.header("📊 大盤情緒")
    st.caption("資料 60 秒 cache;失敗區塊不影響其他指標。")

    # === 1. TAIEX K 線(升級成 candlestick + BB + MA,reuse 個股 _make_candlestick) ===
    st.markdown("### 加權指數 (TAIEX)")
    # 抓 120 天:夠 BB(20) + MA60 計算(SQLite 應有 130 天 from taiex.csv preload)
    taiex_df = fetch_taiex(days=120)
    if taiex_df.empty:
        st.warning("加權指數抓取失敗(可能 FinMind 限流或 SQLite 沒 TAIEX)")
    else:
        last_close = float(taiex_df["close"].iloc[-1])
        prev_close = (
            float(taiex_df["close"].iloc[-2]) if len(taiex_df) >= 2 else last_close
        )
        delta = last_close - prev_close
        delta_pct = delta / prev_close * 100 if prev_close else 0
        # 台股慣例 — 漲紅跌綠
        st.metric(
            f"當日收盤 {taiex_df['date'].iloc[-1]}",
            f"{last_close:,.2f}",
            f"{delta:+.2f} ({delta_pct:+.2f}%)",
            delta_color="inverse",
        )
        # 算 MA + BB(reuse src.indicators)
        try:
            taiex_df = taiex_df.copy()
            taiex_df["MA5"] = ind.sma(taiex_df, 5)
            taiex_df["MA20"] = ind.sma(taiex_df, 20)
            taiex_df["MA60"] = ind.sma(taiex_df, 60)
            bb = ind.bollinger(taiex_df, period=20, num_std=2.0)
            st.plotly_chart(
                _make_candlestick(taiex_df, bb), use_container_width=True,
            )
        except Exception as e:  # noqa: BLE001
            st.warning(f"K 線繪製失敗(歷史可能不足):{type(e).__name__}: {e}")
        # 短評
        short = "短期偏多" if delta > 0 else "短期偏空"
        st.caption(f"💬 當日 {short}")

    # === 2. TAIEX 技術分析總覽(reuse 個股 helper) ===
    _render_technical_summary("TAIEX")

    # === 3. TAIEX 多週期分析(日 K + 週 K, reuse 個股 helper) ===
    _render_multi_timeframe("TAIEX")

    # === 4. VIX 恐慌指數(原樣保留,從 c2 拆出獨立全寬) ===
    st.markdown("### VIX 恐慌指數 (美股)")
    df = fetch_vix(days=90)
    if df.empty:
        st.warning("VIX 抓取失敗")
    else:
        last = float(df["Close"].iloc[-1])
        st.metric("當前 VIX", f"{last:.2f}",
                  help="高於 25 = 市場恐慌;低於 15 = 樂觀")
        fig = go.Figure(go.Scatter(
            x=df["Date"], y=df["Close"],
            mode="lines", line=dict(color="#9467bd", width=2),
        ))
        fig.add_hline(y=25, line_dash="dash", line_color="#d62728",
                      annotation_text="恐慌 25")
        fig.add_hline(y=15, line_dash="dash", line_color="#2ca02c",
                      annotation_text="樂觀 15")
        fig.update_layout(
            height=250, margin=dict(t=10, b=10, l=10, r=10),
            font=dict(size=14),
            xaxis=dict(tickfont=dict(size=12)),
            yaxis=dict(tickfont=dict(size=12)),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)
        zone = (
            "市場恐慌" if last > 25
            else "市場樂觀" if last < 15
            else "中性"
        )
        st.caption(f"💬 {zone}")

    # === 5. 三大法人 + 5/10/20 日累計 metric(NEW) ===
    st.markdown("### 三大法人合計買賣超(全市場)")
    df = fetch_institutional_total(days=30)
    if df.empty:
        st.warning("法人總額抓取失敗(FinMind 限流或 dataset 受限)")
    else:
        # 用 helper 抽「每日 total 那筆」,避免把 5 法人 + 'total' 加成 2× 真值
        agg = compute_total_net_per_day(df)
        if agg.empty:
            st.dataframe(df.head(20), use_container_width=True, hide_index=True)
            st.caption(f"💬 dataset 欄位:{list(df.columns)} — 與預期不同")
        else:
            # 5/10/20 日累計 metric(在 bar chart 上方)
            n5 = float(agg["net"].tail(5).sum())
            n10 = float(agg["net"].tail(10).sum())
            n20 = float(agg["net"].tail(20).sum())

            def _color_arrow(v: float) -> str:
                return "🔴" if v > 0 else ("🟢" if v < 0 else "⚪")

            cols = st.columns(3)
            cols[0].metric(
                f"近 5 日累計 {_color_arrow(n5)}",
                f"{n5:+.1f} 億",
                help="近 5 個交易日法人合計 net 買賣超(正 = 買超,利多)",
            )
            cols[1].metric(
                f"近 10 日累計 {_color_arrow(n10)}",
                f"{n10:+.1f} 億",
            )
            cols[2].metric(
                f"近 20 日累計 {_color_arrow(n20)}",
                f"{n20:+.1f} 億",
            )

            # 紅買綠賣(買超是利多 → 紅)
            colors = ["#d62728" if v > 0 else "#2ca02c" for v in agg["net"]]
            fig = go.Figure(go.Bar(
                x=agg["date"], y=agg["net"],
                marker_color=colors, name="買賣超(億元)",
            ))
            fig.add_hline(y=0, line_color="gray", opacity=0.5)
            fig.update_layout(
                height=300, margin=dict(t=10, b=10, l=10, r=10),
                font=dict(size=14),
                xaxis=dict(tickfont=dict(size=12)),
                yaxis=dict(tickfont=dict(size=12), title="億元"),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)
            recent_buy = int((agg["net"].tail(5) > 0).sum())
            latest_net = float(agg["net"].iloc[-1])
            st.caption(
                f"💬 最新一日 ({agg['date'].iloc[-1]}):"
                f"{'買超' if latest_net > 0 else '賣超'} "
                f"{abs(latest_net):.1f} 億 | "
                f"近 5 日買超天數:**{recent_buy}/5** "
                f"({'偏多' if recent_buy >= 3 else '偏空'})"
            )

    # 融資融券
    st.markdown("### 融資 / 融券餘額(全市場,近 30 日)")
    df = fetch_margin_balance(days=30)
    if df.empty:
        st.warning("融資券抓取失敗")
    else:
        margin_df, short_df = _split_margin_dataset(df)
        if not margin_df.empty and not short_df.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=margin_df["date"], y=margin_df["balance_billion"],
                name="融資餘額(億)", line=dict(color="#d62728"),
            ))
            fig.add_trace(go.Scatter(
                x=short_df["date"], y=short_df["balance_billion"],
                name="融券餘額(億)", line=dict(color="#2ca02c"),
                yaxis="y2",
            ))
            fig.update_layout(
                height=300, margin=dict(t=10, b=10, l=10, r=10),
                font=dict(size=14),
                xaxis=dict(tickfont=dict(size=12)),
                yaxis=dict(tickfont=dict(size=12), title="融資(億)"),
                yaxis2=dict(
                    tickfont=dict(size=12), title="融券(億)",
                    overlaying="y", side="right",
                ),
                legend=dict(font=dict(size=14), orientation="h"),
            )
            st.plotly_chart(fig, use_container_width=True)

            # 最新一日數值短評
            latest_margin = float(margin_df["balance_billion"].iloc[-1])
            latest_short = float(short_df["balance_billion"].iloc[-1])
            st.caption(
                f"💬 最新 ({margin_df['date'].iloc[-1]}):"
                f"融資 **{latest_margin:.0f} 億** / "
                f"融券 **{latest_short:.0f} 億**"
            )
        else:
            st.warning("融資券 dataset schema 暫不支援,無法繪製。")


def _split_margin_dataset(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """把 FinMind margin/short 資料切成融資 + 融券兩個 DataFrame,並換算成「億」。

    支援兩種 schema(FinMind 不同版本回的格式):
    1. **Long format**(目前主流):每日兩列,`name` 欄區分 'MarginPurchase' /
       'ShortSale','TodayBalance' 是當日餘額(股數)
    2. **Wide format**(舊版):一列含 `MarginPurchaseTodayBalance` /
       `ShortSaleTodayBalance` 兩個欄位

    都不符 → 回兩個空 DataFrame。caller 看到空就走 fallback warning。

    回值各 DataFrame 都帶 `date` + `balance_billion`(餘額億元)兩欄。
    """
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    cols = set(df.columns)

    # Long format(FinMind 主流)
    if {"name", "TodayBalance", "date"} <= cols:
        margin_df = df[df["name"].astype(str).str.contains(
            "MarginPurchase|融資", case=False, na=False, regex=True,
        )].copy()
        short_df = df[df["name"].astype(str).str.contains(
            "ShortSale|融券", case=False, na=False, regex=True,
        )].copy()
        for d in (margin_df, short_df):
            if not d.empty:
                d.sort_values("date", inplace=True)
                d["balance_billion"] = pd.to_numeric(
                    d["TodayBalance"], errors="coerce"
                ) / 1e8
        return margin_df, short_df

    # Wide format(舊版)— 留著兼容
    margin_col = next(
        (c for c in df.columns if "Margin" in c and "Balance" in c), None,
    )
    short_col = next(
        (c for c in df.columns if "Short" in c and "Balance" in c), None,
    )
    if margin_col and short_col and "date" in cols:
        m = df[["date", margin_col]].rename(
            columns={margin_col: "balance_raw"}
        ).copy()
        s = df[["date", short_col]].rename(
            columns={short_col: "balance_raw"}
        ).copy()
        m["balance_billion"] = pd.to_numeric(m["balance_raw"], errors="coerce") / 1e8
        s["balance_billion"] = pd.to_numeric(s["balance_raw"], errors="coerce") / 1e8
        return m, s

    return pd.DataFrame(), pd.DataFrame()


# === 💼 交易紀錄頁 ===

def _page_trades() -> None:
    """個人交易紀錄 + P&L 追蹤頁。"""
    st.header("💼 交易紀錄")
    st.caption(
        "記錄你的買賣 → 自動算 weighted average 成本 + 已實現 / 未實現 P&L。"
        "雲端容器重啟會清光,本地 CSV snapshot 永久化(GitHub auto-push 待後續 commit)。"
    )
    db.init_db()

    # === 新增交易表單 ===
    with st.expander("✚ 新增交易", expanded=True):
        with st.form("add_trade_form", clear_on_submit=True):
            cols = st.columns([2, 1, 2, 1, 2])
            sid = cols[0].text_input("股票代號", placeholder="2330")
            direction_label = cols[1].radio(
                "方向", ["買進", "賣出"], horizontal=True, key="trade_dir",
            )
            price = cols[2].number_input(
                "價格(每張)", min_value=0.01, value=100.0, step=0.5,
                format="%.2f", key="trade_price",
            )
            qty = cols[3].number_input(
                "張數", min_value=1, value=1, step=1, key="trade_qty",
            )
            trade_date = cols[4].date_input(
                "交易日期", value=_get_default_screen_date(), key="trade_date",
            )
            note = st.text_input("備註(選填)", key="trade_note")
            submitted = st.form_submit_button("✚ 新增", type="primary")
            if submitted:
                sid_clean = sid.strip()
                if not sid_clean:
                    st.error("請輸入股票代號")
                else:
                    d = "buy" if direction_label == "買進" else "sell"
                    try:
                        tid = db.add_trade(
                            sid_clean, d, float(price), int(qty),
                            trade_date.isoformat(),
                            note=note.strip() if note.strip() else None,
                        )
                        # 同步 dump 進 CSV(本地 snapshot,讓使用者下次 boot 還原)
                        try:
                            from src import portfolio_snapshot
                            portfolio_snapshot.dump_to_csv()
                        except Exception:  # noqa: BLE001
                            pass
                        st.success(
                            f"✅ #{tid}: {sid_clean} {direction_label} "
                            f"{qty} 張 @ {price}"
                        )
                    except ValueError as e:
                        st.error(f"❌ 輸入錯誤:{e}")

    # === 列表 + 統計 ===
    all_trades = db.get_trades()
    if not all_trades:
        st.info("尚無交易紀錄。從上方表單新增第一筆。")
        return

    # 計算所有 stock_id 的 position + P&L(統計用)
    stock_ids = sorted(set(t["stock_id"] for t in all_trades))
    total_realized = 0.0
    total_unrealized = 0.0
    total_buy_amount = 0.0
    positions: list[dict] = []
    with db.get_conn() as conn:
        for sid_iter in stock_ids:
            pos = db.get_position(sid_iter)
            total_realized += pos["realized_pnl"]
            total_buy_amount += pos["total_buy_amount"]
            if pos["quantity"] > 0:
                row = conn.execute(
                    "SELECT close FROM daily_prices WHERE stock_id=? "
                    "ORDER BY date DESC LIMIT 1",
                    (sid_iter,),
                ).fetchone()
                if row and row["close"]:
                    cur_price = float(row["close"])
                    unrealized = (cur_price - pos["avg_cost"]) * pos["quantity"]
                    total_unrealized += unrealized
                    pct = (cur_price - pos["avg_cost"]) / pos["avg_cost"] * 100
                    positions.append({
                        "代號": sid_iter,
                        "張數": pos["quantity"],
                        "均價": round(pos["avg_cost"], 2),
                        "現價": round(cur_price, 2),
                        "未實現損益": round(unrealized),
                        "未實現%": round(pct, 2),
                        "已實現損益": round(pos["realized_pnl"]),
                    })
                else:
                    positions.append({
                        "代號": sid_iter,
                        "張數": pos["quantity"],
                        "均價": round(pos["avg_cost"], 2),
                        "現價": None,
                        "未實現損益": None,
                        "未實現%": None,
                        "已實現損益": round(pos["realized_pnl"]),
                    })

    st.markdown("### 📊 投資組合總覽")
    cols = st.columns(3)
    cols[0].metric("總投入金額", f"{total_buy_amount:,.0f}")
    cols[1].metric(
        "總實現損益", f"{total_realized:+,.0f}",
        help="(賣出價 − 賣出當下均價) × 賣出張數,累計",
    )
    cols[2].metric(
        "總未實現損益", f"{total_unrealized:+,.0f}",
        help="(現價 − 均價) × 持有張數,僅算有持倉個股",
    )

    if positions:
        st.markdown("### 🎯 當前持倉")
        st.dataframe(
            pd.DataFrame(positions),
            use_container_width=True, hide_index=True,
        )

    st.markdown("### 📋 完整交易紀錄")
    for t in all_trades:
        cols = st.columns([1.5, 1, 1.2, 1, 1.5, 2, 1])
        cols[0].markdown(f"**{t['stock_id']}**")
        cols[1].write("買進" if t["direction"] == "buy" else "賣出")
        cols[2].write(f"{t['price']:.2f}")
        cols[3].write(f"{t['quantity']} 張")
        cols[4].write(t["trade_date"])
        cols[5].write(t.get("note") or "—")
        if cols[6].button("🗑️", key=f"del_trade_{t['id']}", help="刪除此筆"):
            db.delete_trade(t["id"])
            try:
                from src import portfolio_snapshot
                portfolio_snapshot.dump_to_csv()
            except Exception:  # noqa: BLE001
                pass
            st.toast(f"已刪除 #{t['id']}", icon="🗑️")
            st.rerun()


# === 持倉管理(風險控制)頁 ===

def _page_position_management() -> None:
    """🛡️ 持倉管理頁 — 主公手動建倉 + drawdown / 集中度告警。

    跟「💼 交易紀錄」不同:這頁是「真倉」+ 停損停利 + 部位建議,
    交易紀錄是純歷史 ledger。
    """
    from src import position_sizing as _ps
    from src import risk_management as _rm
    st.header("🛡️ 持倉管理")
    st.caption(
        "記錄當前持倉 + 自動算 ATR 停損停利 + 整體 drawdown 監控。"
        "**這頁是給「真倉」用,『💼 交易紀錄』記完整買賣 ledger。**"
    )
    db.init_db()

    rm_on = _rm.is_enabled()
    ps_on = _ps.is_enabled()
    if not rm_on:
        st.warning("⚠️ 風險管理已停用(RISK_MGMT_ENABLED=false),停損停利不會自動算。")
    if not ps_on:
        st.warning("⚠️ 部位建議已停用(POSITION_SIZING_ENABLED=false),Kelly 建議不顯。")

    # === 新增持倉表單 ===
    with st.expander("✚ 新增持倉", expanded=True):
        with st.form("add_position_form", clear_on_submit=True):
            cols = st.columns([2, 1.5, 1.5, 1.5])
            sid_input = cols[0].text_input(
                "股票代號", placeholder="2330", key="pos_sid",
            )
            entry_price = cols[1].number_input(
                "進場價", min_value=0.01, value=100.0, step=0.5,
                format="%.2f", key="pos_entry_price",
            )
            shares = cols[2].number_input(
                "股數(整股)", min_value=1, value=1000, step=1000,
                key="pos_shares",
                help="1 張 = 1000 股。預設 1 張(1000 股)。",
            )
            entry_date = cols[3].date_input(
                "進場日", value=date.today(), key="pos_entry_date",
            )

            cols2 = st.columns([2, 2, 2])
            atr_days = cols2[0].slider(
                "ATR 平滑天數", 5, 30, 14, key="pos_atr_days",
            )
            stop_mult = cols2[1].number_input(
                "停損 ATR 倍數", 0.5, 5.0, 2.0, 0.5,
                key="pos_stop_mult",
                help="停損 = entry − ATR × 倍數。預設 2.0。",
            )
            tp_mult = cols2[2].number_input(
                "停利 ATR 倍數", 0.5, 10.0, 4.0, 0.5,
                key="pos_tp_mult",
                help="停利 = entry + ATR × 倍數。預設 4.0(2:1 風報)。",
            )

            stop_manual = st.number_input(
                "手動停損(0 = 自動 ATR 算)",
                min_value=0.0, value=0.0, step=0.5, format="%.2f",
                key="pos_stop_manual",
            )
            tp_manual = st.number_input(
                "手動停利(0 = 自動 ATR 算)",
                min_value=0.0, value=0.0, step=0.5, format="%.2f",
                key="pos_tp_manual",
            )
            notes = st.text_input("備註(選填)", key="pos_notes")

            submitted = st.form_submit_button("✚ 新增", type="primary")
            if submitted:
                sid_clean = sid_input.strip()
                if not sid_clean:
                    st.error("請輸入股票代號")
                else:
                    # 算 ATR 停損停利(若 user 沒手動填)
                    sl = stop_manual if stop_manual > 0 else None
                    tp = tp_manual if tp_manual > 0 else None
                    if sl is None and rm_on:
                        try:
                            sl_res = _rm.compute_atr_stop_loss(
                                sid_clean, float(entry_price),
                                days=int(atr_days),
                                atr_multiplier=float(stop_mult),
                            )
                            if sl_res:
                                sl = sl_res["stop_loss"]
                        except Exception:  # noqa: BLE001
                            sl = None
                    if tp is None and rm_on:
                        try:
                            tp_res = _rm.compute_atr_take_profit(
                                sid_clean, float(entry_price),
                                days=int(atr_days),
                                atr_multiplier=float(tp_mult),
                            )
                            if tp_res:
                                tp = tp_res["take_profit"]
                        except Exception:  # noqa: BLE001
                            tp = None
                    try:
                        pid = db.add_position(
                            sid_clean,
                            entry_date.isoformat(),
                            float(entry_price),
                            int(shares),
                            stop_loss=sl,
                            take_profit=tp,
                            notes=notes.strip() if notes.strip() else None,
                        )
                        if sl and tp:
                            msg = (
                                f"✅ #{pid} {sid_clean} @ {entry_price:.2f} × "
                                f"{shares} 股 (停損 {sl:.2f} / 停利 {tp:.2f})"
                            )
                        else:
                            msg = (
                                f"✅ #{pid} {sid_clean} @ {entry_price:.2f} × "
                                f"{shares} 股"
                            )
                        st.success(msg)
                        st.rerun()
                    except ValueError as e:
                        st.error(f"❌ 輸入錯誤:{e}")

    # === 持倉列表 + 整體統計 ===
    positions = db.get_all_positions(include_closed=False)
    if not positions:
        st.info("尚無持倉。從上方表單新增第一筆。")
        return

    # 撈每檔的 current_price
    enriched: list[dict] = []
    with db.get_conn() as conn:
        for p in positions:
            sid = p["stock_id"]
            row = conn.execute(
                "SELECT close FROM daily_prices WHERE stock_id=? "
                "ORDER BY date DESC LIMIT 1",
                (sid,),
            ).fetchone()
            cur = float(row["close"]) if row and row["close"] is not None else None
            entry = float(p["entry_price"])
            shares_n = int(p["shares"])
            sign = 1.0 if (p.get("side") or "long") == "long" else -1.0
            pnl = (cur - entry) * shares_n * sign if cur else None
            pnl_pct = (cur - entry) / entry * 100.0 * sign if cur else None
            enriched.append({
                **p,
                "current_price": cur,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            })

    # === Trailing Stop 控制(B 進場時機強化,2026-05-17)===
    from src import trailing_stop as _ts
    from src import take_profit_alerts as _tp_alerts
    ts_on = _ts.is_enabled()
    with st.expander("📈 動態停損(Trailing Stop)設定", expanded=False):
        if not ts_on:
            st.warning(
                "⚠️ Trailing Stop 已停用(TRAILING_STOP_ENABLED=false),"
                "停損不會自動上移。"
            )
        else:
            st.caption(
                "Trailing Stop 規則:當價格漲超 entry+ATR,停損自動上移到 "
                "(HWM − ATR × 倍數)。永遠 only-up,不會鬆動。"
            )
        ts_auto = st.checkbox(
            "🔄 每次開頁自動更新 trailing stop",
            value=False, key="ts_auto_update",
            help="勾選後每次切回本頁會跑一次 batch update。"
                 "未勾選可手動按下方按鈕。",
        )
        if ts_on and (ts_auto or st.button("📈 立即更新所有持倉 trailing stop")):
            with st.spinner("更新中..."):
                summary = _ts.batch_update_trailing_stops()
            n_up = summary.get("updated", 0)
            n_skip = summary.get("skipped_no_data", 0)
            if n_up > 0:
                st.success(
                    f"✅ {n_up} 筆停損已上移 "
                    f"(checked={summary.get('checked', 0)}, "
                    f"skipped={n_skip})"
                )
                for r in summary.get("raised_positions") or []:
                    st.info(
                        f"• #{r['position_id']} {r['sid']}: "
                        f"{r['old_stop']:.2f} → {r['new_stop']:.2f}"
                        f"(HWM {r['hwm']:.2f})"
                    )
                st.rerun()
            else:
                st.caption(
                    f"無持倉達上移條件 "
                    f"(checked={summary.get('checked', 0)}, skipped={n_skip})"
                )

    # === TP/SL 達標警報(B 進場時機強化,2026-05-17)===
    if _tp_alerts.is_enabled():
        alerts = _tp_alerts.check_take_profit_hit()
        if alerts:
            st.markdown("### 🚨 達標警報")
            for a in alerts:
                sev = a.get("severity", "info")
                msg = a.get("message", "")
                action = a.get("suggested_action", "")
                full = f"{msg}\n→ {action}" if action else msg
                if sev == "danger":
                    st.error(full)
                elif sev == "warn":
                    st.warning(full)
                else:
                    st.info(full)

    # 整體 drawdown
    dd_input = [
        {**e, "current_price": e["current_price"]}
        for e in enriched
    ]
    # 把已平倉的也撈進來(get_all_positions(include_closed=True))算 realized
    closed = [
        p for p in db.get_all_positions(include_closed=True)
        if int(p.get("is_open", 1) or 0) == 0
    ]
    dd_summary = _rm.drawdown_pct(dd_input + closed)

    st.markdown("### 📊 整體統計")
    mcols = st.columns(4)
    mcols[0].metric("總投入", f"${dd_summary['total_invested']:,.0f}")
    mcols[1].metric("當前市值", f"${dd_summary['current_value']:,.0f}")
    mcols[2].metric(
        "未實現損益",
        f"${dd_summary['unrealized_pnl']:+,.0f}",
        delta=f"{dd_summary['drawdown_pct']:+.2f}%"
        if dd_summary['total_invested'] > 0 else None,
    )
    mcols[3].metric(
        "已實現損益",
        f"${dd_summary['realized_pnl']:+,.0f}",
        help=f"持倉筆數: {dd_summary['n_open']} open / {dd_summary['n_closed']} closed",
    )

    sev = dd_summary["severity"]
    dd_pct = dd_summary["drawdown_pct"]
    if sev == "danger":
        st.error(
            f"🚨 整體 drawdown {dd_pct:+.2f}%(loss ≥ {_rm.DRAWDOWN_DANGER_PCT}%)"
            f" — 軍師建議停手 + 全面檢視策略"
        )
    elif sev == "warn":
        st.warning(
            f"⚠️ 整體 drawdown {dd_pct:+.2f}%(loss ≥ {_rm.DRAWDOWN_WARN_PCT}%)"
            f" — 建議暫停加碼,檢視持倉"
        )

    # 單檔集中度警報
    over_concentrated = _rm.check_single_concentration(
        [{**e, "sid": e["stock_id"]} for e in enriched],
        max_single_pct=0.20,
    )
    if over_concentrated:
        for o in over_concentrated:
            st.warning(
                f"⚠️ {o['sid']} 占整體部位 {o['position_pct']*100:.1f}%"
                f"(> 20%),風險集中"
            )

    # === 持倉表格 ===
    st.markdown("### 🎯 當前持倉")
    table_rows = []
    for e in enriched:
        cur = e.get("current_price")
        sl = e.get("stop_loss")
        tp = e.get("take_profit")
        trail = e.get("trailing_stop")
        sl_hit = _rm.should_stop_loss(e["entry_price"], cur, sl) if cur else False
        tp_hit = _rm.should_take_profit(e["entry_price"], cur, tp) if cur else False
        status = ""
        if sl_hit:
            status = "🔴 達停損"
        elif tp_hit:
            status = "🟢 達停利"
        table_rows.append({
            "ID": e["id"],
            "代號": e["stock_id"],
            "進場日": e["entry_date"],
            "進場價": round(e["entry_price"], 2),
            "股數": e["shares"],
            "現價": round(cur, 2) if cur else "—",
            "損益$": round(e["pnl"]) if e["pnl"] is not None else "—",
            "損益%": round(e["pnl_pct"], 2) if e["pnl_pct"] is not None else "—",
            "停損": round(sl, 2) if sl else "—",
            "停利": round(tp, 2) if tp else "—",
            "Trail": round(trail, 2) if trail else "—",
            "狀態": status or "持有",
        })
    st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True, hide_index=True,
    )

    # === 平倉操作 ===
    st.markdown("### 🚪 平倉")
    with st.expander("選擇要平倉的部位", expanded=False):
        sids_open = [
            f"#{e['id']} {e['stock_id']} @ {e['entry_price']:.2f} × {e['shares']}"
            for e in enriched
        ]
        if sids_open:
            idx = st.selectbox(
                "持倉", range(len(sids_open)),
                format_func=lambda i: sids_open[i],
                key="close_pos_idx",
            )
            cols = st.columns([2, 2, 1])
            exit_price = cols[0].number_input(
                "出場價", min_value=0.01,
                value=enriched[idx].get("current_price") or enriched[idx]["entry_price"],
                step=0.5, format="%.2f", key="close_exit_price",
            )
            exit_date_input = cols[1].date_input(
                "出場日", value=date.today(), key="close_exit_date",
            )
            if cols[2].button("✅ 平倉", type="primary", key="btn_close_pos"):
                pid = int(enriched[idx]["id"])
                ok = db.close_position(
                    pid, float(exit_price),
                    exit_date=exit_date_input.isoformat(),
                )
                if ok:
                    st.success(f"✅ #{pid} 已平倉 @ {exit_price:.2f}")
                    st.rerun()
                else:
                    st.error("❌ 平倉失敗(已平倉或 id 不存在)")

    # === 部位建議區(若 ML / 勝率有可用資料)===
    if ps_on:
        st.markdown("### 🧮 軍師部位建議(根據近 30 天歷史)")
        stats = _ps.get_recent_win_stats()
        col1, col2, col3 = st.columns(3)
        col1.metric("樣本數", stats["n"])
        col2.metric("歷史勝率", f"{stats['win_rate']*100:.0f}%")
        col3.metric("贏輸比", f"{stats['win_loss_ratio']:.2f}")
        if stats["is_fallback"]:
            st.caption("⚠️ 樣本不足,使用 fallback win_rate=50% / R:R=1.5(保守值)")


# === 系統健康監控頁 ===

def _render_system_health() -> None:
    """系統頁:資料覆蓋率 / 更新時間 / backfill workflow / API token / SQLite 統計。"""
    import os
    from pathlib import Path

    db.init_db()

    # === 🏥 Snapshot CSV 健康(最先顯,error 時頁頂醒目警告)===
    # 防呆:2026-05-06 主公踩 daily_prices.csv 漏 dump 坑後加。
    from src.snapshot_health import (
        get_snapshot_health, overall_status, get_last_update_text,
    )
    health_rows = get_snapshot_health()
    overall = overall_status(health_rows)
    if overall == "error":
        st.error(
            "🚨 **資料健康異常**:有 CSV 落後超過警戒線。"
            "下方表格紅色行為主要問題。"
        )
    elif overall == "warn":
        st.warning(
            "⚠️ **資料健康警告**:部分 CSV 略微落後,通常 cron 跑下輪會修復。"
        )

    st.markdown("### 🏥 Snapshot CSV 健康")
    df_health = pd.DataFrame(health_rows)
    if not df_health.empty:
        # 顯示用欄位 + 中文 header
        df_show = df_health.rename(columns={
            "table": "CSV",
            "last_date": "最新資料日期",
            "days_lag": "落後天數",
            "row_count": "行數",
            "expected_freq": "預期頻率",
            "status": "狀態",
            "note": "備註",
        })[
            ["CSV", "最新資料日期", "落後天數", "行數",
             "預期頻率", "狀態", "備註"]
        ]
        # status emoji 染色
        _status_emoji = {
            "ok": "🟢 正常",
            "warn": "🟡 警告",
            "error": "🔴 異常",
            "missing": "⚪ 缺失",
            "unknown": "❔ 未知",
        }
        df_show["狀態"] = df_show["狀態"].map(_status_emoji).fillna(df_show["狀態"])
        # row 數加千分位
        df_show["行數"] = df_show["行數"].apply(
            lambda v: f"{int(v):,}" if pd.notna(v) and int(v) > 0 else "—"
        )
        st.dataframe(df_show, use_container_width=True, hide_index=True)
    last_update_text = get_last_update_text()
    if last_update_text:
        with st.expander("📝 last_update.txt(daily-notify 寫入的 timestamp + run_id)"):
            st.code(last_update_text, language="ini")

    # === 📊 資料覆蓋率 ===
    st.markdown("### 📊 資料覆蓋率")
    health = db.cache_health_summary()
    b = health["buckets"]
    ge_20 = b["60+"] + b["20-59"]  # 20+ 合計(短線 selectbox 用同一定義)
    cols = st.columns(5)
    cols[0].metric("全市場股票", f"{health['total_stocks']:,}")
    cols[1].metric("有價量歷史", f"{health['with_prices']:,}")
    cols[2].metric("≥60 天(可跑全策略)", f"{b['60+']:,}")
    cols[3].metric("≥20 天(可跑量價KD/乖離)", f"{ge_20:,}")
    cols[4].metric("<14 天(新)", f"{b['<14']:,}")
    st.caption(
        f"分桶:60+ {b['60+']:,} / 20-59 {b['20-59']:,} / "
        f"14-19 {b['14-19']:,} / <14 {b['<14']:,}。"
        "20+ 天可跑量價KD 與乖離率策略;60+ 才能跑 ma_alignment(因為 MA60 需要)。"
    )

    # === 🔄 上次更新(各表 max date + count) ===
    st.markdown("### 🔄 上次更新")
    update_rows: list[dict] = []
    with db.get_conn() as conn:
        # daily_prices / institutional / daily_metrics 用 date 欄
        for table, label in [
            ("daily_prices", "daily_prices"),
            ("institutional", "institutional"),
            ("daily_metrics", "daily_metrics"),
        ]:
            try:
                row = conn.execute(
                    f"SELECT MAX(date) AS d, COUNT(*) AS n FROM {table}"
                ).fetchone()
                last = row["d"] if row else None
                count = row["n"] if row else 0
            except sqlite3.OperationalError:
                last, count = None, 0
            update_rows.append({
                "表": label,
                "最新日期": last or "—",
                "行數": f"{count:,}" if count else "0",
            })
        # financials.quarterly 用 period 欄
        try:
            row = conn.execute(
                "SELECT MAX(period) AS d, COUNT(*) AS n FROM financials "
                "WHERE period_type='quarterly'"
            ).fetchone()
            update_rows.append({
                "表": "financials.quarterly",
                "最新日期": (row["d"] if row else None) or "—",
                "行數": f"{(row['n'] if row else 0):,}",
            })
        except sqlite3.OperationalError:
            update_rows.append({
                "表": "financials.quarterly",
                "最新日期": "—", "行數": "0",
            })
    st.dataframe(
        pd.DataFrame(update_rows),
        use_container_width=True, hide_index=True,
    )

    # === 🚦 Backfill workflow(讀 last_backfill.txt) ===
    st.markdown("### 🚦 Backfill workflow(上次跑況)")
    backfill_path = (
        config.PROJECT_ROOT / "data" / "twse_snapshot" / "last_backfill.txt"
    )
    if backfill_path.exists():
        info: dict[str, str] = {}
        for line in backfill_path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        cols = st.columns(3)
        cols[0].metric(
            "Run ID",
            info.get("run_id", "—"),
            help=f"GitHub Actions Run ID(commit {info.get('git_sha','—')[:7]})",
        )
        cols[1].metric(
            "Price 成功率",
            f"{info.get('price_success_rate_pct', '—')}%",
            help=(
                f"price_ok={info.get('price_ok','—')} / "
                f"price_fail={info.get('price_fail','—')} / "
                f"todo={info.get('todo','—')}"
            ),
        )
        cols[2].metric(
            "Shards 完成",
            info.get("shards_completed", "—"),
        )
        st.caption(
            f"上次跑於 {info.get('backfilled_at', '—')}, "
            f"耗時 {info.get('elapsed_min_max', '—')} 分鐘"
        )
    else:
        st.info(
            "🔍 `data/twse_snapshot/last_backfill.txt` 不存在 — "
            "backfill workflow 還沒跑過(或 cloud snapshot 沒灌進來)"
        )

    # === 📤 API token / 推播狀態(只看 env var 是否非空,不顯示 token 數值) ===
    st.markdown("### 📤 API Token / 推播狀態")
    secrets_status = []
    for env, label in [
        ("FINMIND_TOKEN", "FinMind API"),
        ("TELEGRAM_BOT_TOKEN", "Telegram Bot"),
        ("TELEGRAM_CHAT_ID", "Telegram Chat ID"),
        ("DISCORD_WEBHOOK_URL", "Discord webhook"),
        ("GITHUB_PAT", "GitHub PAT(雲端 push 快取)"),
    ]:
        val = os.environ.get(env, "")
        secrets_status.append({
            "服務": label,
            "Env Var": env,
            "狀態": "✅ 已設定" if val else "❌ 未設定",
        })
    st.dataframe(
        pd.DataFrame(secrets_status),
        use_container_width=True, hide_index=True,
    )
    st.caption("Token 數值不顯示,只看 env var 是否非空(避免外洩)。")

    # === 💰 投資組合損益(從 trades 表算) ===
    st.markdown("### 💰 投資組合損益")
    trades_in_db = db.get_trades()
    if not trades_in_db:
        st.caption("尚無交易紀錄。到「💼 交易紀錄」頁新增。")
    else:
        sids_with_trade = sorted(set(t["stock_id"] for t in trades_in_db))
        sys_total_realized = 0.0
        sys_total_unrealized = 0.0
        with db.get_conn() as _conn:
            for _sid in sids_with_trade:
                _pos = db.get_position(_sid)
                sys_total_realized += _pos["realized_pnl"]
                if _pos["quantity"] > 0:
                    _row = _conn.execute(
                        "SELECT close FROM daily_prices WHERE stock_id=? "
                        "ORDER BY date DESC LIMIT 1",
                        (_sid,),
                    ).fetchone()
                    if _row and _row["close"]:
                        sys_total_unrealized += (
                            float(_row["close"]) - _pos["avg_cost"]
                        ) * _pos["quantity"]
        cols = st.columns(3)
        cols[0].metric("實現損益", f"{sys_total_realized:+,.0f}")
        cols[1].metric("未實現損益", f"{sys_total_unrealized:+,.0f}")
        cols[2].metric(
            "合計",
            f"{sys_total_realized + sys_total_unrealized:+,.0f}",
        )
        st.caption(
            f"來自 trades 表 {len(trades_in_db)} 筆,涵蓋 "
            f"{len(sids_with_trade)} 檔個股"
        )

    # === 🤖 AI 模型(短線勝率預測) ===
    st.markdown("### 🤖 AI 模型(短線勝率預測)")
    ml_pkl = config.PROJECT_ROOT / "models" / "short_pick.pkl"
    try:
        from src.ml_predictor import load_model_meta
        meta = load_model_meta(ml_pkl)
    except Exception:  # noqa: BLE001
        meta = None

    if not ml_pkl.exists():
        st.warning(
            "🤖 模型尚未訓練。本機跑 `python scripts/train_ml_model.py` 生成"
            " `models/short_pick.pkl` + `.meta.json`,commit 後推送即可。"
        )
    elif meta is None:
        st.warning(
            "⚠️ `short_pick.meta.json` 不存在。pkl 檔在但缺 metadata,"
            "重訓一次:`python scripts/train_ml_model.py`"
        )
    else:
        m = meta.get("metrics", {})
        cols = st.columns(5)
        cols[0].metric(
            "訓練樣本", f"{meta.get('samples', 0):,}",
            help=f"train={meta.get('n_train',0)} / test={meta.get('n_test',0)}",
        )
        cols[1].metric(
            "Accuracy", f"{m.get('accuracy', 0) * 100:.1f}%",
            help=f"基準勝率(隨機猜):{m.get('base_win_rate', 0) * 100:.1f}%",
        )
        cols[2].metric("Precision", f"{m.get('precision', 0) * 100:.1f}%")
        cols[3].metric("Recall", f"{m.get('recall', 0) * 100:.1f}%")
        cols[4].metric("F1", f"{m.get('f1', 0) * 100:.1f}%")

        # 副資訊
        try:
            pkl_size_kb = ml_pkl.stat().st_size / 1024
        except Exception:  # noqa: BLE001
            pkl_size_kb = 0
        feat_names = meta.get("feature_names", [])
        st.markdown(
            f"- **模型類型**:{meta.get('model_type', '—')}\n"
            f"- **版本**:{meta.get('version', '—')}\n"
            f"- **特徵維度**:{meta.get('features_count', 0)} 個 "
            f"({', '.join(feat_names[:5])}...)\n"
            f"- **最低歷史**:{meta.get('min_history_days', 0)} trading days\n"
            f"- **上次訓練**:{meta.get('trained_at', '—')}\n"
            f"- **模型檔大小**:{pkl_size_kb:.1f} KB"
        )
        st.caption(
            "模型 metrics 來自上次訓練的 hold-out test set(8:2 train/test split)。"
            "**預測結果僅供參考,不構成投資建議。** 重訓:"
            "`python scripts/train_ml_model.py`"
        )

    # === 🗄️ SQLite 資料庫(各表行數 + 檔案大小) ===
    st.markdown("### 🗄️ SQLite 資料庫")
    db_path_str = str(config.DATABASE_PATH)
    db_path = Path(db_path_str)
    db_size_mb = (
        db_path.stat().st_size / 1024 / 1024 if db_path.exists() else 0.0
    )
    st.metric(
        "DB 大小", f"{db_size_mb:.2f} MB",
        help=f"path: {db_path_str}",
    )

    table_rows = []
    with db.get_conn() as conn:
        try:
            tables = [
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            tables = []
        for t in tables:
            try:
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
                table_rows.append({"表名": t, "行數": f"{row['n']:,}"})
            except sqlite3.OperationalError:
                table_rows.append({"表名": t, "行數": "?"})
    if table_rows:
        st.dataframe(
            pd.DataFrame(table_rows),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("SQLite 還沒有任何 table。")


@st.cache_data(ttl=60, show_spinner=False)
def _evaluate_active_paper_trades_cached(cache_buster: int = 0) -> int:
    """TTL 60s + buster — 避免每 rerun(尤其是 button click)都重跑 DB scan。

    cache_buster 由 _add_paper_trade_callback 在加入新 trade 後 +1,讓下次
    render 強制 cache miss 重 evaluate(因為 active list 變了)。
    """
    from src import paper_trading as pt
    return pt.evaluate_active_trades()


def _add_paper_trade_callback(
    sid: str, name: str, entry_date: str, entry_price: float,
    matched_strategies: list[str], ml_prob: float | None,
) -> None:
    """on_click callback — Streamlit 自動 rerun,不用 explicit st.rerun()。

    callback 模式比 `if st.button():` 少一輪 rerun,點擊到結果回顯體感快很多。
    錯誤經 toast 顯示。
    """
    from src import paper_trading as pt
    try:
        new_id = pt.add_paper_trade(
            sid=sid, name=name, entry_date=entry_date,
            entry_price=entry_price,
            matched_strategies=matched_strategies, ml_prob=ml_prob,
        )
        if new_id:
            st.toast(
                f"✅ #{new_id}: {sid} 加入追蹤 @ {entry_price:.2f}",
                icon="🧪",
            )
            # bump 讓下個 render evaluate cache miss
            st.session_state["_paper_evaluate_buster"] = (
                st.session_state.get("_paper_evaluate_buster", 0) + 1
            )
        else:
            st.toast(f"⚠ {sid} 同日已加入過,跳過", icon="⚠️")
    except ValueError as e:  # noqa: BLE001
        st.toast(f"❌ {e}", icon="❌")


def _bulk_add_paper_trades_callback(
    rows: list[dict], entry_date: str,
) -> None:
    """on_click callback — 批量加入今日所有未追蹤 picks。

    UNIQUE 衝突自動算 skipped(不報錯),callback 結束 toast 印 added/skipped。
    """
    from src import paper_trading as pt
    result = pt.bulk_add_paper_trades(rows, entry_date=entry_date)
    n_added = result["added"]
    n_skipped = result["skipped"]
    n_errors = result["errors"]
    if n_added > 0:
        msg = f"✅ 一鍵加入 {n_added} 張"
        if n_skipped > 0:
            msg += f"({n_skipped} 張已追蹤跳過)"
        st.toast(msg, icon="🧪")
        st.session_state["_paper_evaluate_buster"] = (
            st.session_state.get("_paper_evaluate_buster", 0) + 1
        )
    elif n_skipped > 0:
        st.toast(f"📭 全部 {n_skipped} 張都已追蹤過", icon="ℹ️")
    if n_errors > 0:
        st.toast(f"⚠ {n_errors} 張無效資料跳過", icon="⚠️")


def _page_price_alerts() -> None:
    """🚨 警報設定 — 主公手動設定 G 個股價格警報(price_above / price_below /
    pct_change / ex_dividend)+ 看已觸發歷史。

    跑 intraday_alerts cron(每 30 分鐘)時 src.price_alerts 會比對當前 daily
    close 跟設定門檻,觸發後寫 alert_dedup + Telegram / Discord + mark_triggered
    (一次性,要再用須重設)。
    """
    from src import price_alerts as _pa

    st.header("🚨 警報設定")
    st.caption(
        "設定價位 ≥ / ≤ / 漲跌幅 % / 除權息提醒。"
        "盤中 cron(每 30 分鐘)自動檢查 + 推 Telegram + Discord。"
    )
    db.init_db()

    if not _pa.is_enabled():
        st.warning(
            "⚠️ 警報已停用(`PRICE_ALERT_ENABLED=false`),設定還可寫,但不會推播。"
        )

    # === 新增警報表單 ===
    with st.expander("✚ 新增警報", expanded=True):
        with st.form("add_price_alert_form", clear_on_submit=True):
            cols = st.columns([2, 2, 2])
            sid_input = cols[0].text_input(
                "股票代號", placeholder="2330", key="alert_sid",
            )
            alert_type_label = cols[1].selectbox(
                "警報類型",
                options=[
                    "價位 ≥ (price_above)",
                    "價位 ≤ (price_below)",
                    "漲跌幅 % (pct_change)",
                    "除權息提醒 (ex_dividend)",
                ],
                key="alert_type_label",
            )
            target_value = cols[2].number_input(
                "目標值",
                value=0.0, step=0.5, format="%.2f",
                key="alert_target",
                help=(
                    "price_above/below 填價位;pct_change 填 % 門檻(例 5.0);"
                    "ex_dividend 填提醒天數(例 3)。"
                ),
            )
            notes_input = st.text_input(
                "備註(選填,pct_change 可寫 base=600 當基準價)",
                key="alert_notes",
            )

            submitted = st.form_submit_button("✚ 新增警報", type="primary")
            if submitted:
                sid_clean = sid_input.strip().upper()
                if not sid_clean:
                    st.error("請輸入股票代號")
                else:
                    label_to_type = {
                        "價位 ≥ (price_above)": "price_above",
                        "價位 ≤ (price_below)": "price_below",
                        "漲跌幅 % (pct_change)": "pct_change",
                        "除權息提醒 (ex_dividend)": "ex_dividend",
                    }
                    atype = label_to_type[alert_type_label]
                    try:
                        aid = db.add_alert(
                            sid_clean, atype,
                            target_value=float(target_value),
                            notes=notes_input.strip() if notes_input.strip() else None,
                        )
                        st.success(
                            f"✅ #{aid} {sid_clean} {atype} target={target_value}"
                        )
                        st.rerun()
                    except ValueError as e:
                        st.error(f"❌ 輸入錯誤:{e}")

    # === 兩個 tab:active + history ===
    tab_active, tab_history = st.tabs(["🟢 進行中", "📜 已觸發歷史"])

    with tab_active:
        active_alerts = db.list_alerts(active_only=True)
        if not active_alerts:
            st.info("目前沒有 active 警報。上方新增一筆開始。")
        else:
            st.caption(f"共 {len(active_alerts)} 筆 active。")
            for a in active_alerts:
                cols = st.columns([1, 2, 2, 2, 3, 1])
                cols[0].text(f"#{a['id']}")
                cols[1].text(a["stock_id"])
                cols[2].text(a["alert_type"])
                tv = a.get("target_value")
                cols[3].text(f"{tv:.2f}" if tv is not None else "—")
                cols[4].text((a.get("notes") or "")[:40])
                if cols[5].button("🗑", key=f"del_alert_{a['id']}"):
                    if db.delete_alert(int(a["id"])):
                        st.toast(f"已刪除 #{a['id']}", icon="🗑")
                        st.rerun()

    with tab_history:
        all_alerts = db.list_alerts(active_only=False, limit=200)
        triggered = [a for a in all_alerts if a.get("triggered_at")]
        if not triggered:
            st.info("尚無已觸發紀錄。")
        else:
            st.caption(f"共 {len(triggered)} 筆已觸發。")
            for a in triggered:
                cols = st.columns([1, 2, 2, 2, 3])
                cols[0].text(f"#{a['id']}")
                cols[1].text(a["stock_id"])
                cols[2].text(a["alert_type"])
                cols[3].text(a.get("triggered_at") or "—")
                cols[4].text((a.get("notes") or "")[:40])


def _page_paper_tracking() -> None:
    """🧪 實測追蹤 — paper trading 驗證 Stage 2B v2 ML 過濾在實盤是否吻合 backtest 預測。

    3 sections:
    1. 今日 picks 一鍵加入(reuse 現有 `_run_all_strategies_cached` + 高信心模式
       toggle)
    2. 進行中 paper trades(每次 render 自動 evaluate_active_trades)
    3. 已結算統計(WR / avg_return / max_loss_streak / 依策略拆分 / vs backtest)
    """
    from src import paper_trading as pt
    from src.ui_format import format_change

    st.header("🧪 實測追蹤")
    st.caption(
        "**Paper trading**(不是真實交易) — 驗證 Stage 2B v2 ML 過濾後的"
        "勝率在實盤是否吻合 backtest 預測。每次打開頁面自動結算可結算的 trades。"
    )
    db.init_db()

    # evaluate cached(TTL 60s + add 後 buster bump)— 不每 rerun 都重跑 DB scan
    cache_buster = st.session_state.get("_paper_evaluate_buster", 0)
    _evaluate_active_paper_trades_cached(cache_buster)

    # === Section 1: 今日 picks 一鍵加入 ===
    st.subheader("① 今日 picks 一鍵加入")
    eligible_sids = pure_stock_universe(min_history=20)
    if not eligible_sids:
        st.caption("📭 cache 內沒有任何個股累積 20 天歷史。請先 backfill。")
    else:
        target_date = _get_default_screen_date()
        today_iso = target_date.isoformat()
        loaded_key = "_paper_picks_loaded"
        if not st.session_state.get(loaded_key):
            st.button(
                f"🔍 掃描今日 picks(~{len(eligible_sids)} 檔)",
                on_click=_set_session_flag, args=(loaded_key,),
                key="paper_load_btn", use_container_width=True,
            )
        else:
            with st.spinner(f"掃描 {len(eligible_sids)} 檔..."):
                try:
                    agg = _run_all_strategies_cached(
                        today_iso,
                        tuple(eligible_sids),
                        None,
                        None,
                    )
                    df_full = _enrich_df_with_consensus(
                        _enrich_df_with_matched_strategies(
                            _enrich_df_with_ml_prob(
                                _enrich_df_with_win_rate(
                                    aggregated_to_dataframe(agg), agg,
                                ),
                                trade_date=today_iso,
                                agg=agg,
                            ),
                            agg,
                        ),
                        agg,
                    )
                    rows_full = df_full.to_dict("records")
                    rows_filtered, total = _apply_confidence_filter(rows_full)
                except Exception as e:  # noqa: BLE001
                    st.error(f"❌ 掃描失敗:{type(e).__name__}: {e}")
                    rows_filtered = []
                    total = 0

            high_confidence_on = st.session_state.get("high_confidence_mode", True)
            mode_label = "高信心模式 ON" if high_confidence_on else "全部 picks"
            st.caption(
                f"📊 {mode_label} → 顯示 {len(rows_filtered)}/{total} 檔"
                f"(交易日 {today_iso})"
            )

            if not rows_filtered:
                st.info("📭 今日無符合條件的 picks。")
            else:
                # 批量查 today 已追蹤的 sid 集合(避免 N×SQL,改 1 次 SQL + set lookup)
                with db.get_conn() as conn:
                    tracked_today = {
                        r["sid"] for r in conn.execute(
                            "SELECT sid FROM paper_trades WHERE entry_date=?",
                            (today_iso,),
                        ).fetchall()
                    }

                # 一鍵加入全部 picks(只算未追蹤的;callback 內仍經 UNIQUE 防雙加)
                pending_count = sum(
                    1 for r in rows_filtered
                    if str(r.get("stock_id", "") or "") not in tracked_today
                    and r.get("close") and float(r.get("close") or 0) > 0
                )
                if pending_count > 0:
                    st.button(
                        f"🧪 一鍵加入全部 picks({pending_count} 張未追蹤)",
                        key=f"paper_bulk_add_{today_iso}",
                        on_click=_bulk_add_paper_trades_callback,
                        args=(rows_filtered, today_iso),
                        type="primary", use_container_width=True,
                    )
                else:
                    st.caption("✅ 今日所有 picks 都已加入追蹤")

                st.markdown(
                    "<small>或逐張選擇:</small>", unsafe_allow_html=True,
                )

                # 表格 + 每行旁邊「加入追蹤」button(on_click callback,不用 if-button 模式)
                for r in rows_filtered:
                    sid = str(r.get("stock_id", ""))
                    name = str(r.get("name", "") or "")
                    close = r.get("close")
                    matched = r.get("matched_strategies") or []
                    ml_prob = r.get("ml_prob")
                    if not sid or not close or float(close) <= 0:
                        continue

                    cols = st.columns([1, 2, 1, 2, 1, 1])
                    cols[0].markdown(f"**{sid}**")
                    cols[1].markdown(name or "—")
                    cols[2].markdown(f"{float(close):.2f}")
                    matched_label = "・".join(
                        STRATEGY_LABELS.get(s, s) for s in matched
                    ) if matched else "—"
                    cols[3].markdown(f"<small>{matched_label}</small>", unsafe_allow_html=True)
                    if ml_prob is not None:
                        cols[4].markdown(f"🤖 {float(ml_prob) * 100:.0f}%")
                    else:
                        cols[4].markdown("🤖 —")

                    if sid in tracked_today:
                        cols[5].button(
                            "✅ 已加入", key=f"paper_add_{sid}_{today_iso}",
                            disabled=True, use_container_width=True,
                        )
                    else:
                        ml_prob_val = (
                            float(ml_prob) if ml_prob is not None else None
                        )
                        cols[5].button(
                            "🧪 加入追蹤",
                            key=f"paper_add_{sid}_{today_iso}",
                            on_click=_add_paper_trade_callback,
                            args=(sid, name, today_iso, float(close),
                                  list(matched), ml_prob_val),
                            type="primary", use_container_width=True,
                        )

    st.markdown("---")

    # === Section 2: 進行中 picks ===
    st.subheader("② 進行中 paper trades")
    active_df = pt.list_active_trades()
    if active_df.empty:
        st.caption("📭 目前沒有進行中的 paper trades。從 ① 加入第一筆。")
    else:
        # 取每張 active trade 的最新收盤(跟 entry_date 之後的最新交易日)
        with db.get_conn() as conn:
            latest_prices: dict[str, tuple[str, float]] = {}
            for sid_iter in active_df["sid"].unique():
                row = conn.execute(
                    "SELECT date, close FROM daily_prices "
                    "WHERE stock_id=? ORDER BY date DESC LIMIT 1",
                    (sid_iter,),
                ).fetchone()
                if row and row["close"]:
                    latest_prices[sid_iter] = (row["date"], float(row["close"]))

        # 動態停損 trailing level → 中文 + 顏色(主公拍板 2026-05-06)
        _trailing_labels = {
            0: ("", ""),
            1: ("🛡️ 保本中", "#888888"),
            2: ("🔒 鎖 2%", "#1f77b4"),
            3: ("🔒 鎖 5%", "#2ca02c"),
        }
        st.caption(f"進行中 {len(active_df)} 筆 — 系統每次打開自動 evaluate")
        for _, row in active_df.iterrows():
            sid = row["sid"]
            entry = float(row["entry_price"])
            target = float(row["target_price"])
            init_stop = float(row["stop_price"])
            # current_stop 可能 None(舊資料未 migrate),fallback 用 stop_price
            current_stop_raw = row.get("current_stop")
            current_stop = (
                float(current_stop_raw)
                if current_stop_raw is not None
                else init_stop
            )
            tl = int(row.get("trailing_level") or 0)
            entry_date = row["entry_date"]
            expected_exit = row.get("expected_exit_date") or "—"
            matched = row.get("matched_strategies") or []
            matched_label = "・".join(
                STRATEGY_LABELS.get(s, s) for s in matched
            ) if matched else "—"

            cur_date, cur_price = latest_prices.get(sid, ("—", entry))
            float_pct = (cur_price - entry) / entry * 100 if entry > 0 else 0.0
            # current_stop vs entry %(level 0 = -3%、level 1 = 0%、level 2 = +2%、level 3 = +5%)
            stop_pct_vs_entry = (
                (current_stop - entry) / entry * 100 if entry > 0 else 0.0
            )
            stop_pct_str = (
                f"{stop_pct_vs_entry:+.1f}%" if stop_pct_vs_entry != 0
                else "0%"
            )
            badge_text, badge_color = _trailing_labels.get(tl, ("", ""))

            with st.container(border=True):
                cols = st.columns([2, 2, 2, 2, 2])
                cols[0].markdown(
                    f"**{sid} {row.get('name') or ''}**<br>"
                    f"<small>{matched_label}</small>",
                    unsafe_allow_html=True,
                )
                cols[1].markdown(
                    f"進場 {entry_date}<br>@ {entry:.2f}",
                    unsafe_allow_html=True,
                )
                cols[2].markdown(
                    f"當前 {cur_date}<br>@ {cur_price:.2f}",
                    unsafe_allow_html=True,
                )
                cols[3].markdown(
                    f"浮動<br>{format_change(float_pct)}",
                    unsafe_allow_html=True,
                )
                # cols[4]:目標 + 動態停損 + trailing badge
                badge_html = (
                    f"<br><span style='color:{badge_color};font-size:0.85rem'>"
                    f"{badge_text}</span>"
                    if tl > 0 else ""
                )
                cols[4].markdown(
                    f"📅 到期 {expected_exit}<br>"
                    f"<small>+{(target / entry - 1) * 100:.1f}% / "
                    f"停損 {current_stop:.2f} ({stop_pct_str})</small>"
                    f"{badge_html}",
                    unsafe_allow_html=True,
                )

    st.markdown("---")

    # === Section 3: 已結算統計 ===
    st.subheader("③ 已結算統計 vs backtest")
    settled_df = pt.list_settled_trades()
    stats = pt.compute_stats(settled_df)

    cols = st.columns(4)
    cols[0].metric("總追蹤", len(active_df) + stats["n_settled"])
    cols[1].metric("已結算", stats["n_settled"])
    cols[2].metric("進行中", len(active_df))
    cols[3].metric(
        "勝率",
        f"{stats['win_rate'] * 100:.1f}%" if stats["n_settled"] > 0 else "—",
    )

    if stats["n_settled"] > 0:
        cols2 = st.columns(3)
        cols2[0].metric("平均報酬", f"{stats['avg_return'] * 100:+.2f}%")
        cols2[1].metric("最大連敗", stats["max_loss_streak"])
        # 對比 backtest 預測(取 strategy_backtest 表全策略加權平均當基準)
        try:
            rates = db.load_latest_strategy_backtest()
            if rates:
                bt_avg = sum(rates.values()) / len(rates)
                diff_pp = (stats["win_rate"] - bt_avg) * 100
                cols2[2].metric(
                    "vs backtest 預測",
                    f"{bt_avg * 100:.1f}%",
                    delta=f"{diff_pp:+.1f} pp",
                )
        except Exception:  # noqa: BLE001
            pass

        # 依策略拆分 table
        by_strategy = stats["by_strategy"]
        if by_strategy:
            st.markdown("**依策略拆分**")
            rows_table = []
            for s, st_stat in sorted(
                by_strategy.items(), key=lambda kv: -kv[1]["n"],
            ):
                rows_table.append({
                    "策略": STRATEGY_LABELS.get(s, s),
                    "命中數": st_stat["n"],
                    "勝": st_stat["wins"],
                    "勝率": f"{st_stat['win_rate'] * 100:.1f}%",
                    "平均報酬": f"{st_stat['avg_return'] * 100:+.2f}%",
                })
            st.dataframe(
                pd.DataFrame(rows_table),
                use_container_width=True, hide_index=True,
            )
    else:
        st.info(
            "📭 還沒任何結算紀錄。等 ① 加入 picks 後過 5 個交易日,系統自動結算。"
        )


def _page_strategy_history() -> None:
    """📊 策略歷史命中 — 把 M4 weekly backtest 落在 pick_outcomes 的後向資料 UI 化。

    跟「🧪 實測追蹤」(前向 paper-trade,當下 picks 進場後 hold N 天結算)是
    兩條獨立 pipeline,但放隔壁讓主公一眼能對比:
      - 🧪 實測追蹤:今天 picks 進場 → 5 天後算戰績(主動進場驗證)
      - 📊 策略歷史:過去所有 daily_picks 跑後向 D1/D3/D5/D10 報酬(回溯期勝率)

    3 個 sub-tab:
      📈 by-strategy:各策略累積 N、平均報酬、命中率、停損率
      📅 by-date:近 30 天每日推播當天的整體 D1/D5 表現
      📦 明細:全部 pick_outcomes raw rows(可篩 strategy / 日期下限)
    """
    st.header("📊 策略歷史命中")
    st.caption(
        "**後向 backtest** — 過去所有 daily_picks 命中後實際 1/3/5/10 日報酬。"
        "資料來源:`pick_outcomes`(weekly backtest_picks.py 跑出)。"
    )
    db.init_db()

    last_eval = db.get_last_evaluated_pick_date()
    if not last_eval:
        st.info(
            "📭 還沒有任何 pick_outcomes 資料。等 weekly backtest_picks "
            "workflow 跑過後再來看。"
        )
        return
    st.caption(f"📅 最新 evaluate 日:**{last_eval}**")

    tab_strat, tab_date, tab_raw, tab_vbt = st.tabs([
        "📈 by-strategy", "📅 by-date", "📦 全部結算明細", "🎲 參數最佳化",
    ])

    # === Tab 1: by-strategy ===
    with tab_strat:
        stats = db.get_strategy_history_stats()
        if not stats:
            st.info("📭 尚無聚合資料")
        else:
            # pick_outcomes.return_d* 已用 percent 寫入(1.0 = +1%,由
            # scripts/backtest_picks.py line 169 `* 100.0` 決定);UI 直接顯
            # 不用再乘 100。hit_target / stopped_out 0/1 → * 100 變百分比。
            rows_view = []
            for s in stats:
                key = s["strategy"]
                rows_view.append({
                    "策略": STRATEGY_LABELS.get(key, key),
                    "命中數 N": int(s["n"] or 0),
                    "D1 平均": f"{(s['avg_d1'] or 0):+.2f}%",
                    "D3 平均": f"{(s['avg_d3'] or 0):+.2f}%",
                    "D5 平均": f"{(s['avg_d5'] or 0):+.2f}%",
                    "D10 平均": f"{(s['avg_d10'] or 0):+.2f}%",
                    "命中率(+3%)": f"{(s['hit_rate'] or 0) * 100:.1f}%",
                    "停損率(-3%)": f"{(s['stop_rate'] or 0) * 100:.1f}%",
                })
            st.dataframe(
                pd.DataFrame(rows_view),
                use_container_width=True, hide_index=True,
            )
            st.caption(
                "↑ 依 D5 平均報酬由高到低排序。命中率 / 停損率為 D1~D10 內"
                "high 達 +3% / low 觸 -3% 的比例(可雙重 1)。"
            )

    # === Tab 2: by-date ===
    with tab_date:
        by_date = db.get_pick_outcomes_by_date(days=30)
        if not by_date:
            st.info("📭 近 30 日尚無資料")
        else:
            rows_view = []
            for r in by_date:
                rows_view.append({
                    "推播日": r["pick_date"],
                    "命中數 N": int(r["n"] or 0),
                    "D1 平均": f"{(r['avg_d1'] or 0):+.2f}%",
                    "D5 平均": f"{(r['avg_d5'] or 0):+.2f}%",
                    "命中率": f"{(r['hit_rate'] or 0) * 100:.1f}%",
                    "停損率": f"{(r['stop_rate'] or 0) * 100:.1f}%",
                })
            st.dataframe(
                pd.DataFrame(rows_view),
                use_container_width=True, hide_index=True,
            )

    # === Tab 3: 明細 ===
    with tab_raw:
        strat_options = ["(全部)"] + [
            STRATEGY_LABELS.get(s["strategy"], s["strategy"])
            for s in db.get_strategy_history_stats()
        ]
        label_to_key = {
            STRATEGY_LABELS.get(s["strategy"], s["strategy"]): s["strategy"]
            for s in db.get_strategy_history_stats()
        }
        chosen_label = st.selectbox(
            "篩選策略", strat_options, index=0,
            key="strategy_history_filter",
        )
        chosen_strategy = (
            label_to_key.get(chosen_label) if chosen_label != "(全部)" else None
        )
        since = st.text_input(
            "起始日(YYYY-MM-DD,留空 = 全部)", value="",
            key="strategy_history_since",
        ).strip() or None
        raw = db.get_pick_outcomes_raw(
            since=since, strategy=chosen_strategy, limit=2000,
        )
        if not raw:
            st.info("📭 沒有符合條件的資料")
        else:
            rows_view = []
            for r in raw:
                key = r["strategy"]
                rows_view.append({
                    "推播日": r["pick_date"],
                    "股號": r["sid"],
                    "策略": STRATEGY_LABELS.get(key, key),
                    "進場價": f"{(r['entry_close'] or 0):.2f}",
                    "D1": f"{(r['return_d1'] or 0):+.2f}%",
                    "D5": f"{(r['return_d5'] or 0):+.2f}%",
                    "D10": f"{(r['return_d10'] or 0):+.2f}%",
                    "達標": "✅" if (r["hit_target"] or 0) >= 1 else "—",
                    "停損": "🛑" if (r["stopped_out"] or 0) >= 1 else "—",
                })
            st.caption(f"共 {len(rows_view)} 筆")
            st.dataframe(
                pd.DataFrame(rows_view),
                use_container_width=True, hide_index=True,
            )

            # 跳「📊 個股深度」頁 — 從本頁的 raw 結算明細選一檔 sid 直接看 K/籌碼/ML
            sid_options = sorted({r["sid"] for r in raw})
            jump_cols = st.columns([2, 1])
            chosen_jump = jump_cols[0].selectbox(
                "從結算明細選股號",
                options=sid_options,
                key="strategy_history_jump_sid",
                index=0,
            )
            if jump_cols[1].button(
                "📊 看細節", key="strategy_history_jump_btn",
                use_container_width=True,
                help="跳到「📊 個股深度」頁",
            ):
                st.session_state["detail_sid"] = chosen_jump
                st.session_state["pending_nav"] = "📊 個股深度"
                st.rerun()

    # === Tab 4: vectorbt grid search 結果 ===
    with tab_vbt:
        _render_vbt_grid_tab()


# 各策略 production default params(給「軍師判讀」比對 grid winner 用)
# 只列目前 grid 範圍內有對接的策略;未在此 dict 內 → 不下判讀
_STRATEGY_DEFAULT_PARAMS: dict[str, dict] = {
    "volume_breakout": DEFAULT_VOL_BREAKOUT_PARAMS,
    "bias_convergence": DEFAULT_BIAS_PARAMS,
    "macd_golden": DEFAULT_MACD_PARAMS,
    "ma_alignment": DEFAULT_MA_PARAMS,
}


def _vbt_sort_key_col(grid_df: pd.DataFrame) -> str:
    """挑排序欄:有 sharpe_daily 且至少一筆非 NaN → 用 sharpe_daily(新指標,
    抗 N 膨脹);否則 fallback 到 sharpe(舊指標,trade-level)。

    2026-05-15:trade-level sharpe = mean/std × sqrt(N),N=6000+ 時 sqrt(N)
    把 Sharpe 放大近百倍,跨策略比較失真 → 新增 sharpe_daily(daily-aggregated
    annualized)欄,UI 用此排序。
    """
    if "sharpe_daily" in grid_df.columns and grid_df["sharpe_daily"].notna().any():
        return "sharpe_daily"
    return "sharpe"


def _vbt_default_strategy(grid_df: pd.DataFrame, strategies: list[str]) -> str:
    """挑「樣本最多 + 排序指標最高」的策略當 selectbox 預設值。

    對每策略找該策略內排序指標最高 row,排序鍵 (n_trades DESC, metric DESC),
    取頭一個。樣本越多代表 grid 收得越扎實。
    """
    sort_col = _vbt_sort_key_col(grid_df)
    best_by_strat: list[tuple[str, int, float]] = []
    for s in strategies:
        sub = grid_df[grid_df["strategy"] == s]
        if sub.empty:
            continue
        # NaN 用 -inf 排在最後,不會被 idxmax 選中
        metric = sub[sort_col].fillna(float("-inf"))
        top = sub.loc[metric.idxmax()]
        top_metric = top[sort_col]
        if pd.isna(top_metric):
            top_metric = 0.0
        best_by_strat.append((s, int(top["n_trades"]), float(top_metric)))
    if not best_by_strat:
        return strategies[0]
    best_by_strat.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return best_by_strat[0][0]


def _render_vbt_grid_tab() -> None:
    """🎲 參數最佳化 — 顯示 vbt_grid_results 表內各策略 grid search 結果。

    資料來源:`vbt_grid_results`(scripts/vbt_grid_search.py 跑出)。
    多策略支援:用 selectbox 切策略,預設「樣本最多 + sharpe 最高」的策略。
    每策略下方加軍師判讀(比較 production default 與 grid winner)。
    結果**不自動覆蓋既有 production default**,僅供主公手動採用參考。
    """
    import json as _json

    grid_df = db.load_vbt_grid_results()
    if grid_df is None or grid_df.empty:
        st.info(
            "📭 還沒有 vectorbt grid search 結果。\n\n"
            "跑指令:`python scripts/vbt_grid_search.py --strategy volume_breakout --months 6`"
        )
        return

    st.caption(
        "**vectorbt 策略級 grid search** — 對每策略多組參數一次掃,找 Sharpe / 報酬最佳組合。"
        "結果**不自動覆蓋既有 production default**,僅供主公手動採用參考。"
        "\n\n📐 排序指標 **Sharpe(日)** = daily-aggregated annualized Sharpe"
        "(2026-05-15 修;trade-level Sharpe 在大樣本 sqrt(N) 膨脹,跨策略比較失真)。"
        "舊 trade-level Sharpe 並列「Sharpe(trade,舊)」供對照。"
    )

    strategies = sorted(grid_df["strategy"].unique())
    if not strategies:
        st.info("📭 grid 表內沒有任何策略 row")
        return

    sort_col = _vbt_sort_key_col(grid_df)
    default_strat = _vbt_default_strategy(grid_df, strategies)
    option_labels = [f"{STRATEGY_LABELS.get(s, s)}（{s}）" for s in strategies]
    default_idx = strategies.index(default_strat)
    chosen_label = st.selectbox(
        "選策略", option_labels, index=default_idx,
        key="vbt_grid_strategy_selector",
        help="預設為樣本最多 + Sharpe(日)最高的策略",
    )
    strat = strategies[option_labels.index(chosen_label)]
    sub = grid_df[grid_df["strategy"] == strat].copy()
    # NaN 排到尾,排序穩定
    sub["_sort_metric"] = sub[sort_col].fillna(float("-inf"))
    sub = sub.sort_values("_sort_metric", ascending=False).drop(
        columns=["_sort_metric"]
    ).reset_index(drop=True)
    if sub.empty:
        st.info("此策略無 grid 結果")
        return

    label = STRATEGY_LABELS.get(strat, strat)
    period_start = sub.iloc[0].get("period_start", "")
    period_end = sub.iloc[0].get("period_end", "")
    st.subheader(f"{label}（`{strat}`）")
    st.caption(
        f"區間:{period_start} ~ {period_end}　共 {len(sub)} 組合"
    )

    best = sub.iloc[0]
    sharpe_daily_val = best.get("sharpe_daily")
    sharpe_daily_str = (
        f"{float(sharpe_daily_val):.2f}"
        if pd.notna(sharpe_daily_val) else "—(舊資料)"
    )
    st.success(
        f"💡 **最佳組合**:`{best['params_json']}`　"
        f"Sharpe(日) **{sharpe_daily_str}**　"
        f"Sharpe(trade,舊) {float(best['sharpe']):.2f}　"
        f"報酬 **{float(best['total_return']):+.2f}%**　"
        f"勝率 **{float(best['win_rate']):.1f}%**　"
        f"({int(best['n_trades'])} trades)　"
        "→ 建議用此參數替換 default（主公手動採用）"
    )

    # 軍師判讀:比對 production default 與 grid winner
    def _fmt_sd(v) -> str:
        return f"{float(v):.2f}" if pd.notna(v) else "—"

    default_params = _STRATEGY_DEFAULT_PARAMS.get(strat)
    if default_params:
        default_json = _json.dumps(default_params, sort_keys=True, default=str)
        default_match = sub[sub["params_json"] == default_json]
        if not default_match.empty:
            dr = default_match.iloc[0]
            if dr["params_hash"] == best["params_hash"]:
                st.info(
                    f"🎖️ 軍師判讀:現有 default 已是 grid 最佳組合,維持即可 "
                    f"(Sharpe 日 {_fmt_sd(best.get('sharpe_daily'))}, "
                    f"{int(best['n_trades'])} trades)"
                )
            else:
                best_params = _json.loads(best["params_json"])
                diffs = []
                for k, v in best_params.items():
                    if k in default_params and default_params[k] != v:
                        diffs.append(f"`{k}` {default_params[k]} → {v}")
                diff_str = "、".join(diffs) if diffs else "(整組參數不同)"
                st.info(
                    f"🎖️ 軍師判讀:建議 {diff_str}　"
                    f"(n_trades {int(dr['n_trades'])} → {int(best['n_trades'])}, "
                    f"Sharpe 日 {_fmt_sd(dr.get('sharpe_daily'))} → "
                    f"{_fmt_sd(best.get('sharpe_daily'))})"
                )
        else:
            st.info(
                "🎖️ 軍師判讀:既有 default 不在本次 grid 範圍內,"
                "無法直接 A/B 比較;若要採用 winner 請主公手動評估。"
            )

    rows_view = []
    for _, r in sub.iterrows():
        rows_view.append({
            "參數": r["params_json"],
            # 新指標放第一欄(預設排序鍵),舊 trade-level 放後面對照
            "Sharpe(日)": (
                f"{float(r['sharpe_daily']):.3f}"
                if pd.notna(r.get("sharpe_daily")) else "—"
            ),
            "Sharpe(trade,舊)": f"{float(r['sharpe']):.3f}",
            "報酬 %": f"{float(r['total_return']):+.2f}",
            "MaxDD %": f"{float(r['max_drawdown']):.2f}",
            "勝率 %": f"{float(r['win_rate']):.1f}",
            "Trades": int(r["n_trades"]),
        })
    st.dataframe(
        pd.DataFrame(rows_view),
        use_container_width=True, hide_index=True,
    )


def _render_reliability_diagram(strategy: str) -> None:
    """畫某策略的 reliability diagram(holdout 內 10 bins 預測 vs 實際命中率)。

    從 calibrator pkl + 對應 base model 不再重算 raw probs(訓練時 dataset
    沒持久化),改成 reconstruct calibrator 的 input grid:show 校正曲線從
    0→1 的形狀(model → calibrator 的轉換),搭配對角線當「完美校準」基線。

    沒 calibrator → 顯示空狀態提示。
    """
    try:
        from src import ml_calibration

        cal = ml_calibration.load_calibrator(strategy)
        if cal is None:
            st.info(f"找不到 calibrator(`models/calibrators/{strategy}.pkl`)")
            return
        # 在 [0, 1] 上取 11 個點看映射形狀
        grid = np.linspace(0.0, 1.0, 11)
        mapped = cal.transform(grid)
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=[0, 1], y=[0, 1],
            mode="lines",
            line=dict(dash="dash", color="gray"),
            name="完美校準(對角線)",
        ))
        fig.add_trace(go.Scatter(
            x=grid, y=mapped,
            mode="lines+markers",
            name=f"{strategy} 校正曲線 ({cal.method})",
            line=dict(color="#1f77b4", width=2),
        ))
        fig.update_layout(
            xaxis_title="模型 raw probability",
            yaxis_title="calibrator 轉換後 prob",
            xaxis=dict(range=[0, 1]),
            yaxis=dict(range=[0, 1]),
            height=320,
            margin=dict(l=40, r=20, t=20, b=40),
            showlegend=True,
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "曲線在對角線上方 = 模型低估(校正後拉高);"
            "下方 = 模型高估(校正後壓低)。轉折越明顯,raw RF 越偏離真實機率。"
        )
    except Exception as e:  # noqa: BLE001
        st.warning(f"Reliability diagram 渲染失敗:{type(e).__name__}: {e}")


def _page_performance() -> None:
    """📈 績效分析 — 主公真實平倉損益 + 策略歸因 + 組合回測 + 相關性。

    四個 tab:
      1. 真實交易:user_positions 已平倉 P&L 表 + equity curve + drawdown + metrics
      2. 策略 attribution:把每筆平倉歸因到當時 daily_picks 命中的策略
      3. 策略組合回測:對選定策略(交集/聯集)從 daily_picks 撈歷史 + holding_days
      4. 策略相關性:Jaccard heatmap

    Kill-switch:PERFORMANCE_ENABLED=false 時 page 顯 warning 但仍 render
    結構讓 test 過(empty data),不擋畫面。
    """
    from src import performance_analysis as _pa
    from src import strategy_backtest as _sb
    from src.strategies import STRATEGY_LABELS

    st.header("📈 績效分析")
    st.caption(
        "看主公真實買賣的損益、勝率、Sharpe、drawdown,並把每筆平倉歸因到"
        "當時觸發的策略。**全部讀 SQLite,不打 API。**"
    )

    if not _pa.is_enabled():
        st.warning(
            "⚠️ 績效分析已停用(`PERFORMANCE_ENABLED=false`),"
            "下方顯示空白(kill-switch)。"
        )

    db.init_db()

    tabs = st.tabs([
        "💰 真實交易", "🎯 策略 attribution",
        "🔬 策略組合回測", "🧭 策略相關性",
    ])

    # === Tab 1: 真實交易 ===
    with tabs[0]:
        with db.get_conn() as conn:
            pnl_df = _pa.compute_user_pnl(conn)
            summary = _pa.compute_summary_metrics(conn)
            dd_df = _pa.compute_drawdown_curve(conn)

        if pnl_df.empty:
            st.info(
                "尚無已平倉交易,先到🛡️ 持倉管理新增/平倉。"
                "(系統結算後此頁會自動顯示損益曲線)"
            )
        else:
            cols = st.columns(4)
            total_pnl = summary.get("total_pnl") or 0.0
            cols[0].metric(
                "總損益(NTD)",
                f"{total_pnl:+,.0f}",
                help=f"共 {summary.get('n_trades', 0)} 筆已平倉",
            )
            wr = summary.get("win_rate")
            cols[1].metric(
                "勝率",
                f"{wr * 100:.1f}%" if wr is not None else "—",
            )
            sh = summary.get("sharpe")
            cols[2].metric(
                "Sharpe",
                f"{sh:.2f}" if sh is not None else "—",
                help="年化(× √252),P&L 序列",
            )
            max_dd = summary.get("max_drawdown") or 0.0
            cols[3].metric(
                "Max Drawdown(NTD)",
                f"{max_dd:+,.0f}",
                help=(
                    f"百分比 {summary.get('max_drawdown_pct'):.2f}%"
                    if summary.get("max_drawdown_pct") is not None else "—"
                ),
            )

            # equity curve
            if not dd_df.empty:
                st.markdown("#### 📈 累積損益(equity curve)")
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=dd_df["date"], y=dd_df["equity"],
                    mode="lines+markers",
                    name="Equity (NTD)",
                    line=dict(color="#1f77b4", width=2),
                ))
                fig.update_layout(
                    height=320,
                    margin=dict(l=40, r=20, t=20, b=40),
                    xaxis_title="日期", yaxis_title="累積 P&L (NTD)",
                    showlegend=False,
                )
                st.plotly_chart(fig, use_container_width=True)

                st.markdown("#### 📉 Drawdown")
                fig_dd = go.Figure()
                fig_dd.add_trace(go.Scatter(
                    x=dd_df["date"], y=dd_df["drawdown_pct"],
                    mode="lines",
                    fill="tozeroy",
                    name="Drawdown %",
                    line=dict(color="#d62728", width=2),
                ))
                fig_dd.update_layout(
                    height=240,
                    margin=dict(l=40, r=20, t=20, b=40),
                    xaxis_title="日期", yaxis_title="Drawdown %",
                    showlegend=False,
                )
                st.plotly_chart(fig_dd, use_container_width=True)

            st.markdown("#### 📋 每筆平倉")
            disp = pnl_df[[
                "exit_date", "sid", "entry_date", "entry_price",
                "exit_price", "shares", "pnl", "pnl_pct", "holding_days",
            ]].rename(columns={
                "exit_date": "出場日", "sid": "代號",
                "entry_date": "進場日", "entry_price": "進場價",
                "exit_price": "出場價", "shares": "股數",
                "pnl": "P&L(NTD)", "pnl_pct": "P&L %",
                "holding_days": "持有天數",
            })
            st.dataframe(disp, use_container_width=True, hide_index=True)

    # === Tab 2: 策略 attribution ===
    with tabs[1]:
        with db.get_conn() as conn:
            attr = _pa.compute_attribution(conn)

        if not attr:
            st.info(
                "尚無歸因資料(需要已平倉部位 + 對應 daily_picks)。"
                "先到🛡️ 持倉管理新增/平倉,或等 nightly daily_picks 累積。"
            )
        else:
            df_attr = pd.DataFrame([
                {
                    "策略": STRATEGY_LABELS.get(k, k),
                    "key": k,
                    "總 P&L (NTD)": v.get("total_pnl") or 0.0,
                    "筆數": v.get("count", 0),
                    "勝率": (
                        f"{v['win_rate'] * 100:.1f}%"
                        if v.get("win_rate") is not None else "—"
                    ),
                    "平均報酬 %": (
                        round(v["avg_return_pct"], 2)
                        if v.get("avg_return_pct") is not None else None
                    ),
                    "平均 P&L (NTD)": v.get("avg_pnl") or 0.0,
                }
                for k, v in attr.items()
            ])
            df_attr = df_attr.sort_values("總 P&L (NTD)", ascending=False)

            st.markdown("#### 🎯 各策略貢獻 P&L")
            chart_df = df_attr[df_attr["key"] != "_unknown"].copy()
            if not chart_df.empty:
                fig = go.Figure()
                fig.add_trace(go.Bar(
                    x=chart_df["策略"], y=chart_df["總 P&L (NTD)"],
                    marker_color=[
                        "#2ca02c" if v >= 0 else "#d62728"
                        for v in chart_df["總 P&L (NTD)"]
                    ],
                ))
                fig.update_layout(
                    height=360,
                    margin=dict(l=40, r=20, t=20, b=80),
                    yaxis_title="總 P&L (NTD)",
                    xaxis_tickangle=-30,
                )
                st.plotly_chart(fig, use_container_width=True)

            st.dataframe(
                df_attr.drop(columns=["key"]),
                use_container_width=True, hide_index=True,
            )

            # 軍師判讀
            best = _pa.best_strategy_by_pnl(attr, min_count=1)
            if best:
                k, v = best
                label = STRATEGY_LABELS.get(k, k)
                wr_str = (
                    f"{v['win_rate'] * 100:.0f}%"
                    if v.get("win_rate") is not None else "—"
                )
                st.success(
                    f"🎖️ 軍師判讀:主公真實表現最好的策略是 **{label}**"
                    f"(累計 +{v['total_pnl']:,.0f} NTD,勝率 {wr_str},"
                    f"共 {v['count']} 筆),建議多看 {label} 類推薦。"
                )

    # === Tab 3: 策略組合回測 ===
    with tabs[2]:
        st.caption(
            "從 daily_picks 撈歷史命中,模擬「N 個策略**交集/聯集**」"
            "推薦後持有 holding_days 個交易日的實際報酬。"
        )

        all_keys = list(STRATEGY_LABELS.keys())
        label_map = {STRATEGY_LABELS[k]: k for k in all_keys}
        chosen_labels = st.multiselect(
            "策略(可複選)",
            options=list(label_map.keys()),
            default=[STRATEGY_LABELS["ma_alignment"]],
            key="perf_backtest_strategies",
        )
        cols_bt = st.columns([1.5, 1.5, 1.5])
        mode_label = cols_bt[0].radio(
            "組合模式",
            options=["聯集(任一命中)", "交集(全部命中)"],
            horizontal=True,
            key="perf_backtest_mode",
        )
        mode = "union" if "聯集" in mode_label else "intersect"
        holding_days = cols_bt[1].slider(
            "持有交易日(holding_days)",
            min_value=1, max_value=30, value=5, step=1,
            key="perf_backtest_hold",
        )
        date_from = cols_bt[2].date_input(
            "開始日",
            value=date.today() - timedelta(days=180),
            key="perf_backtest_from",
        )
        date_to = st.date_input(
            "結束日",
            value=date.today(),
            key="perf_backtest_to",
        )

        if st.button("▶️ 執行回測", type="primary", key="perf_backtest_run"):
            chosen = [label_map[lbl] for lbl in chosen_labels]
            if not chosen:
                st.error("請至少選一個策略")
            else:
                with db.get_conn() as conn:
                    result = _sb.backtest_combination(
                        conn,
                        strategies=chosen,
                        start_date=date_from.isoformat(),
                        end_date=date_to.isoformat(),
                        holding_days=int(holding_days),
                        mode=mode,
                    )
                cols_m = st.columns(5)
                cols_m[0].metric("樣本數", result.get("n_trades", 0))
                wr = result.get("win_rate")
                cols_m[1].metric(
                    "勝率",
                    f"{wr * 100:.1f}%" if wr is not None else "—",
                )
                avg = result.get("avg_return_pct")
                cols_m[2].metric(
                    "平均報酬",
                    f"{avg:+.2f}%" if avg is not None else "—",
                )
                tot = result.get("total_return_pct") or 0.0
                cols_m[3].metric("累積報酬", f"{tot:+.2f}%")
                sh = result.get("sharpe")
                cols_m[4].metric("Sharpe", f"{sh:.2f}" if sh is not None else "—")

                trades = result.get("trades") or []
                if trades:
                    tr_df = pd.DataFrame(trades)
                    tr_df["matched"] = tr_df["matched"].apply(
                        lambda lst: "/".join(
                            STRATEGY_LABELS.get(s, s) for s in (lst or [])
                        )
                    )
                    st.dataframe(
                        tr_df, use_container_width=True, hide_index=True,
                    )
                else:
                    st.info("此區間 / 模式下沒有命中。")

    # === Tab 4: 策略相關性 ===
    with tabs[3]:
        st.caption(
            "Jaccard 相關性 = |A ∩ B| / |A ∪ B|(同 (date, sid) 命中集合)。"
            "高 = 兩策略常同時推同股,低 = 互補。預設過去 180 日。"
        )
        corr_days = st.slider(
            "區間天數", min_value=30, max_value=365, value=180, step=30,
            key="perf_corr_days",
        )
        with db.get_conn() as conn:
            corr_df = _sb.compute_strategy_correlation(conn, days=int(corr_days))

        if corr_df.empty:
            st.info("daily_picks 表內無資料,無法算相關性。")
        else:
            # 用 STRATEGY_LABELS 把 key 換中文標籤
            renamed = corr_df.rename(
                index=lambda k: STRATEGY_LABELS.get(k, k),
                columns=lambda k: STRATEGY_LABELS.get(k, k),
            )
            fig = go.Figure(data=go.Heatmap(
                z=renamed.values,
                x=list(renamed.columns),
                y=list(renamed.index),
                colorscale="Viridis",
                zmin=0, zmax=1,
                colorbar=dict(title="Jaccard"),
            ))
            fig.update_layout(
                height=max(360, 30 * len(renamed)),
                margin=dict(l=120, r=20, t=20, b=120),
                xaxis_tickangle=-45,
            )
            st.plotly_chart(fig, use_container_width=True)


def _page_system_brief() -> None:
    """📋 系統結論頁 — 軍師主觀統整 DB + 策略表現 + 市場狀態 + 建議。

    呼叫 src.system_brief.build_system_brief(conn) 拿 dict,分 6 個 section render:
      1. 系統健康(metric 大數字 + warnings)
      2. 策略表現(dataframe 排序)
      3. ML 表現(meta + 校準)
      4. 市場狀態(regime / 法人共識 / 千張戶)
      5. 觀察清單 Top 5(三維交集 / 千張突破)
      6. 軍師建議(顏色標 severity)

    Mobile-first:metric 用 st.columns(3) 並排（主公手機 + 桌面都看），其餘無多欄。
    """
    from src.system_brief import build_system_brief

    st.header("📋 系統結論")
    st.caption(
        "軍師統整 DB + 策略歷史 + ML 校準 + 市場狀態,基於規則主動下結論。"
        "**不會 fetch 任何 API**,純讀 SQLite。"
    )

    # 大盤 regime gating badge — 系統結論頁標題下方,讓主公一眼看到當前 gating 狀態
    _render_regime_gating_badge(location="page")

    # === 0. 軍師判讀 banner(三段 Top — 2026-05-18 主公拍板)===
    trade_date = _get_latest_data_date()
    if trade_date:
        st.markdown("### 🎯 今日軍師判讀")
        try:
            summary = _get_verdict_summary_cached(trade_date)
            _render_verdict_banner_full(summary)
        except Exception as e:  # noqa: BLE001
            st.caption(f"📊 軍師判讀 banner 暫不可用:{type(e).__name__}")

    db.init_db()
    with db.get_conn() as conn:
        brief = build_system_brief(conn)

    # === 軍師建議（最重要,放最前）===
    st.markdown("### 🎖️ 軍師建議")
    recs = brief.get("recommendations") or []
    if not recs:
        st.info("尚無足夠資料下結論。")
    else:
        for r in recs:
            if r.startswith("🚨"):
                st.error(r)
            elif r.startswith(("🔴", "🥶", "📉")):
                st.warning(r)
            elif r.startswith(("🔥", "💰", "📈")):
                st.success(r)
            else:
                st.info(r)

    # === 系統健康 ===
    st.markdown("### 🏥 系統健康")
    health = brief["health"]
    cols = st.columns(3)
    cols[0].metric(
        "整體狀態",
        "🟢 健康" if health.get("is_healthy") else "🔴 異常",
    )
    cols[1].metric(
        "daily_prices 落後",
        (
            f"{health.get('daily_prices_stale_days')} 天"
            if health.get("daily_prices_stale_days") is not None else "—"
        ),
        help=f"最新日 {health.get('daily_prices_max_date') or '—'}",
    )
    cols[2].metric(
        "institutional 落後",
        (
            f"{health.get('institutional_stale_days')} 天"
            if health.get("institutional_stale_days") is not None else "—"
        ),
        help=f"最新日 {health.get('institutional_max_date') or '—'}",
    )
    warns = health.get("warnings") or []
    if warns:
        st.warning("⚠️ " + " / ".join(warns))

    # === 市場狀態 ===
    st.markdown("### 🌡️ 市場狀態")
    ms = brief["market_state"]
    cols = st.columns(3)
    cols[0].metric(
        "大盤 regime",
        f"{ms.get('regime_emoji', '❔')} {ms.get('regime_label', '未知')}",
    )
    cols[1].metric(
        "法人共識(今日)",
        f"{ms.get('inst_consensus_count_today', 0)} 檔",
        help=(
            f"7 天前 {ms.get('inst_consensus_count_7d_ago', 0)} 檔 · "
            f"趨勢：{ms.get('inst_consensus_trend_7d', '持平')}"
        ),
    )
    cols[2].metric(
        "千張戶進場",
        f"{ms.get('shareholder_movers_count', 0)} 檔",
    )
    pp = ms.get("premium_picks_count", 0)
    if pp > 0:
        st.success(f"✨ 三維精選 {pp} 檔（法人連買 ∩ 千張戶 ∩ ML 高信心）")

    # === 策略表現 ===
    st.markdown("### 📊 策略表現(過去 30 天)")
    perf = brief.get("strategy_performance") or []
    if not perf:
        st.info("尚無策略樣本(pick_outcomes 還沒結算)")
    else:
        df_perf = pd.DataFrame([
            {
                "策略": s["name"],
                "樣本數": s["n"],
                "命中率": (
                    f"{s['wr'] * 100:.1f}%" if s["wr"] is not None else "—"
                ),
                "平均 D5 報酬": (
                    f"{s['avg_d5']:+.2f}%" if s["avg_d5"] is not None else "—"
                ),
                "軍師判定": s["verdict"],
            }
            for s in perf
        ])
        st.dataframe(df_perf, use_container_width=True, hide_index=True)
        st.caption(
            "🔥 發燙 = WR ≥ 60% 且 N ≥ 30  · 🥶 該休息 = WR ≤ 40% 且 N ≥ 30  · "
            "🌱 觀察中 = N < 30(樣本太小不下結論)"
        )

    # === 動態權重明細(daily-notify 推播排序加權生效中) ===
    with st.expander("⚖️ 動態權重明細", expanded=False):
        from src.strategy_weighting import get_strategy_weight_details
        with db.get_conn() as conn:
            weight_rows = get_strategy_weight_details(conn)
        if not weight_rows:
            st.info("尚無策略樣本(pick_outcomes 還沒結算)")
        else:
            df_w = pd.DataFrame([
                {
                    "策略": r["strategy"],
                    "樣本數 (N)": r["n"],
                    "命中率 (WR)": (
                        f"{r['wr'] * 100:.1f}%"
                        if r["wr"] is not None else "—"
                    ),
                    "權重": f"{r['weight']:.2f}",
                    "判定": r["verdict"],
                }
                for r in weight_rows
            ])
            st.dataframe(df_w, use_container_width=True, hide_index=True)
        st.caption(
            "權重 = clip(WR / 0.5, 0.5, 1.5),套到 daily-notify 推播排序的 ml_prob 上。"
            "當前生效中,可在 `src/notifier.py` 改 `STRATEGY_DYNAMIC_WEIGHT_ENABLED = False` 關掉。"
        )

    # === 題材熱度排行(主公 2026-05-15 加,動態權重的題材維度) ===
    _render_theme_heat_section()

    # === ML 表現 ===
    st.markdown("### 🤖 ML 模型表現")
    ml = brief.get("ml_performance") or {}
    cols = st.columns(3)
    auc = ml.get("short_pick_roc_auc")
    cols[0].metric(
        "Short pick 準確率",
        f"{auc:.3f}" if auc is not None else "—",
        help="meta.json metrics.accuracy(roc_auc 還沒落地時 fallback)",
    )
    cal = ml.get("calibration_7d")
    n_cal = ml.get("calibration_sample_n", 0)
    cols[1].metric(
        "近 7 天高信心命中率",
        f"{cal * 100:.1f}%" if cal is not None else "—",
        help=f"ml_prob > 0.6 的 picks 實際命中率(N={n_cal})",
    )
    trained_at = ml.get("model_trained_at") or "—"
    cols[2].metric("模型訓練時間", trained_at[:10] if trained_at != "—" else "—")
    feats = ml.get("top_features") or []
    if feats:
        st.caption("**Top 5 features**: " + " · ".join(feats))

    # === ML 機率校準健康度(2026-05-15 加) ===
    cal_health = ml.get("calibration_health") or []
    if cal_health:
        st.markdown("#### 🎯 ML 機率校準 (Brier score)")
        df_cal = pd.DataFrame([
            {
                "策略": c["strategy"],
                "method": c.get("method") or "—",
                "holdout N": c.get("n_holdout") or 0,
                "Raw Brier": f"{c['raw_brier']:.3f}" if c["raw_brier"] == c["raw_brier"] else "—",
                "校正後 Brier": f"{c['calibrated_brier']:.3f}" if c["calibrated_brier"] == c["calibrated_brier"] else "—",
                "Δ (改善)": (
                    f"{c['raw_brier'] - c['calibrated_brier']:+.3f}"
                    if c["raw_brier"] == c["raw_brier"] and c["calibrated_brier"] == c["calibrated_brier"]
                    else "—"
                ),
                "健康": "✅" if c["is_healthy"] else "⚠️",
            }
            for c in cal_health
        ])
        st.dataframe(df_cal, use_container_width=True, hide_index=True)
        st.caption(
            "Brier score 越低越好(perfect=0, random≈0.25, > 0.3 偏離校準)。"
            "✅ = 校正後 < 0.25,信心度可作決策依據;⚠️ = 校正失效,UI 預測請打折看。"
        )
        # Reliability diagram 折疊區(取第一個有 calibration block 的 strategy 畫)
        first_health = cal_health[0]
        with st.expander(
            f"📊 Reliability diagram — {first_health['strategy']}", expanded=False,
        ):
            _render_reliability_diagram(first_health["strategy"])

    # === 觀察清單 ===
    st.markdown("### 🎯 今日觀察清單")
    wl = brief.get("watchlist_today") or []
    if not wl:
        st.info("今日無符合條件的觀察標的")
    else:
        df_wl = pd.DataFrame([
            {
                "代號": item["sid"],
                "名稱": item.get("name", "—"),
                "推薦理由": item.get("reason", ""),
            }
            for item in wl
        ])
        st.dataframe(df_wl, use_container_width=True, hide_index=True)

    st.caption(f"產生時間：{brief.get('generated_at', '—')}")


def _page_ai_assistant() -> None:
    """C 問軍師頁(2026-05-17 加):Gemini 對「今天所有資料」做綜合判讀。

    主公在這頁可:
    - 問「2330 怎麼樣」/「今天大盤怎麼樣」之類自然語言問題
    - 看軍師用什麼資料下結論(context_summary)
    - 受 env AI_ASSISTANT_ENABLED kill-switch 管制
    """
    st.header("💬 問軍師")
    st.caption(
        "Gemini AI 幫你綜合 picks / 法人 / 千張戶 / 警示 / 新聞 / SHAP / 持倉 → "
        "給結論 + 風險提示。**僅供研究,不做投資決策。**"
    )

    from src import ai_assistant

    if not ai_assistant.is_enabled():
        st.warning(
            "💤 軍師目前停用 — 設 env `AI_ASSISTANT_ENABLED=true` 並提供 "
            "`GEMINI_API_KEY` 後重啟。"
        )
        return

    if not config.GEMINI_API_KEY:
        st.error(
            "❌ 缺 `GEMINI_API_KEY`。請至 https://aistudio.google.com 申請 "
            "(免費 tier 夠用),然後寫進 .env 或 Streamlit Secrets。"
        )
        return

    # 兩種問法:個股 / 大盤
    mode = st.radio(
        "問什麼?",
        ["個股", "大盤"],
        horizontal=True,
        key="ai_assistant_mode",
    )

    if mode == "個股":
        col1, col2 = st.columns([1, 3])
        with col1:
            sid = st.text_input("股票代號", value="2330", key="ai_assistant_sid")
        with col2:
            q = st.text_input(
                "你的問題(可留空 → 軍師會自己判讀)",
                value="",
                placeholder="例:技術面如何?要不要進場?",
                key="ai_assistant_q_stock",
            )
        if st.button("🧙 問軍師", type="primary", key="ai_assistant_btn_stock"):
            with st.spinner("軍師思考中..."):
                res = ai_assistant.ask_about_stock(sid.strip(), q.strip())
            _render_ai_answer(res)
    else:
        q = st.text_input(
            "你的問題",
            value="",
            placeholder="例:今天適合進場嗎?要不要減碼?",
            key="ai_assistant_q_market",
        )
        if st.button("🧙 問軍師", type="primary", key="ai_assistant_btn_market"):
            with st.spinner("軍師思考中..."):
                res = ai_assistant.ask_about_market(q.strip())
            _render_ai_answer(res)


def _render_ai_answer(res: dict) -> None:
    """渲染 ai_assistant.ask_about_* 回的 dict。"""
    if not res.get("ok"):
        st.warning(res.get("answer") or "(無回應)")
        if res.get("error"):
            st.caption(f"debug: {res['error']}")
        return
    st.markdown(res["answer"])
    if res.get("context_summary"):
        st.caption(f"📊 軍師用的資料:{res['context_summary']}")
    st.caption(f"模型:{res.get('model', '?')}")


def _page_system() -> None:
    """系統健康監控頁(看資料覆蓋率 / 更新時間 / backfill / 推播狀態 / SQLite)。"""
    st.header("⚙️ 系統健康")
    st.caption(
        "看資料管線當前狀態、各表覆蓋率、上次 backfill workflow 結果、API "
        "token 是否設定。**不會 fetch 任何 API**,純讀 SQLite + 本地檔案。"
    )
    _render_system_health()


# === 設定頁 ===

def _page_settings() -> None:
    st.header("⚙️ 設定")

    st.markdown("### 環境變數 (.env / Streamlit Secrets)")
    cols = st.columns(2)
    cols[0].metric(
        "FinMind Token",
        "●●● (有 token)" if config.FINMIND_TOKEN else "(無 token,使用免費模式)",
    )
    cols[1].metric(
        "Telegram Bot",
        "●●● (已設定)"
        if (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
        else "(未設定)",
    )

    st.markdown("### 系統")
    cols = st.columns(2)
    cols[0].metric("SQLite 路徑", config.DATABASE_PATH)
    cols[1].metric("預設市場", config.DEFAULT_MARKET)

    # 資料新鮮度:cache 內 daily_prices 最新日 + snapshot timestamp
    st.markdown("### 📅 資料新鮮度")
    fresh_cols = st.columns(2)
    latest_date = _get_latest_data_date()
    fresh_cols[0].metric(
        "daily_prices 最新日",
        latest_date or "(無)",
        help="cache 內最新一筆 daily_prices 的日期",
    )
    snapshot_info = _get_snapshot_last_update()
    fresh_cols[1].metric(
        "TWSE snapshot",
        snapshot_info or "(無)",
        help="data/twse_snapshot/last_update.txt 的 updated_at",
    )

    st.markdown("### 目前 cache 內容")
    counts = _get_table_counts()
    df = pd.DataFrame(
        [{"資料表": t, "筆數": c} for t, c in counts.items()]
    )
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.caption(
        "註:此頁僅唯讀。要修改設定請編輯專案根目錄的 `.env`(本機)"
        "或 Streamlit Cloud Settings → Secrets(雲端)後重啟。"
    )

    # === Telegram 設定教學 ===
    st.markdown("---")
    st.markdown("### 📲 Telegram 推播設定")
    with st.expander("怎麼設定 Telegram Bot?", expanded=False):
        st.markdown(
            """
**1. 建立 Bot 拿 token**
1. 在 Telegram 找 [@BotFather](https://t.me/BotFather)
2. 傳 `/newbot`,依指示取名
3. BotFather 回一段 token,類似 `1234567890:AAH...........`

**2. 拿你的 chat_id**
1. 先傳「任何訊息」給你新建的 bot(讓他看得到你)
2. 在瀏覽器開 `https://api.telegram.org/bot<你的 token>/getUpdates`
3. 在 JSON 內找 `result[0].message.chat.id`,例如 `123456789`

**3. 寫進 Secrets**
- 雲端:Streamlit Cloud → Settings → Secrets,加兩行:
    ```
    TELEGRAM_BOT_TOKEN = "1234567890:AAH..."
    TELEGRAM_CHAT_ID = "123456789"
    ```
- 本機:編 `.env`(同樣兩行,值不要加引號)
- Reboot app 後 sidebar 會自動出現「📲 測試 Telegram」按鈕

**4. 排程每日推播**
Streamlit Cloud 沒有定時任務功能,要靠:
- GitHub Actions(推薦,免費)— 範本看 README「Telegram 推播」章節
- 自家 Linux 主機 cron / Windows 工作排程器
            """
        )

    with st.expander("怎麼設定 Discord Webhook(備援推播)?", expanded=False):
        st.markdown(
            """
**Telegram 偶爾會被 GFW / Cloudflare 擋,Discord webhook 是另一個推播管道。**

**1. 建 Discord server(已有可跳過)**
- Discord 桌機 / 手機 App 左下「+」→ 建立伺服器 → 自訂

**2. 建頻道 webhook(免帳號 / 免 OAuth)**
- 找一個你想接收推播的頻道(例 `#stock-alerts`)
- 頻道名稱右邊齒輪 → **整合 / Integrations** → **Webhooks** → **New Webhook**
- 取名(隨意)→ **複製 Webhook URL**(類似 `https://discord.com/api/webhooks/12345/abcdef...`)

**3. 寫進 Secrets**
- 雲端:Streamlit Cloud → Settings → Secrets 加一行:
    ```
    DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
    ```
- 本機:`.env` 加同樣內容(值不加引號)
- Reboot app → sidebar 出現「💬 測試 Discord」

**4. 每日排程**
和 Telegram 同 workflow(`daily-notify.yml`),env 加 `DISCORD_WEBHOOK_URL`,
GitHub repo Settings → Secrets 也補一份。Telegram + Discord 並行送,
任一個成功就算 OK,GitHub Actions 不會紅。
            """
        )


def _get_latest_data_date() -> str | None:
    """SQLite daily_prices 內最新一筆日期。"""
    db.init_db()
    with db.get_conn() as conn:
        try:
            row = conn.execute(
                "SELECT MAX(date) AS latest FROM daily_prices"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return row["latest"] if row and row["latest"] else None


def _get_default_screen_date() -> date:
    """date_input 預設值:取 daily_prices 最新交易日,沒資料才退 today()。

    避免週末/假日打開短線頁時 default = date.today()(非交易日)→ SELECT
    WHERE date=今天 永遠 0 picks。
    """
    latest = _get_latest_data_date()
    if not latest:
        return date.today()
    try:
        return date.fromisoformat(latest)
    except ValueError:
        return date.today()


def _get_snapshot_last_update() -> str | None:
    """讀 data/twse_snapshot/last_update.txt 的 updated_at 行。"""
    path = config.PROJECT_ROOT / "data" / "twse_snapshot" / "last_update.txt"
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("updated_at="):
                return line.split("=", 1)[1].strip()[:19]  # 取 ISO 前 19 字
    except Exception:  # noqa: BLE001
        return None
    return None


def _get_table_counts() -> dict[str, object]:
    db.init_db()
    counts: dict[str, object] = {}
    with db.get_conn() as conn:
        for t in _CACHE_TABLES:
            try:
                row = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()
                counts[t] = row["c"]
            except sqlite3.OperationalError:
                counts[t] = "—"
    return counts


# === sidebar 資料更新按鈕(每頁都看得到) ===

def _render_sidebar_update() -> None:
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🔄 資料更新")
    if st.sidebar.button("🔄 更新全市場價量", use_container_width=True):
        _run_update_full_market()
    st.sidebar.caption(
        "📅 全市場 ~2360 檔(twse + tpex)bulk endpoint,30 秒內。"
    )
    if st.sidebar.button("更新 50 檔大型股 (1 年)", use_container_width=True):
        _run_update_top_50()
    st.sidebar.caption("📅 每檔抓過去 1 年(供回測用),約 4–6 分鐘。")
    if st.sidebar.button("📊 更新財報資料(免費版)", use_container_width=True):
        _run_update_long_term_free()
    st.sidebar.caption(
        "📊 用 TWSE OpenAPI + FinMind 免費版,**不需 token**;"
        "PE/PB/殖利率/EPS 來自證交所,ROE 用 PB 反推(Du Pont 簡化)。"
    )

    # === 一次性 backfill(觸發 GH Actions workflow) ===
    st.sidebar.markdown("---")
    with st.sidebar.expander("⏳ 一次性回補 90 日歷史"):
        st.markdown(
            "**用途**:全市場個股短線策略需要 14-60 天歷史,而日常 TWSE bulk "
            "endpoint 每天只回當日 1 筆 → 累積要等 1-2 個月。\n\n"
            "**做法**:在 GitHub Actions 跑 backfill 一次抓全市場 90 天 daily_price"
            "(via FinMind),dump 成 CSV commit 進 repo,Streamlit Cloud 啟動時"
            "讀進 SQLite。\n\n"
            "**時間**:預估 30-45 分鐘(視 FinMind token 限額)。"
        )
        st.markdown(
            "**手動觸發**:\n"
            "1. 進 GitHub repo → Actions tab\n"
            "2. 找『Backfill 90-day history』workflow\n"
            "3. 點 **Run workflow** → main branch → Run\n"
            "4. 等 30-45 分鐘 → 自動 commit CSV → "
            "Streamlit Cloud 自動 redeploy"
        )

    # 只在 token 有設定時顯示測試按鈕
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        st.sidebar.markdown("---")
        if st.sidebar.button("📲 測試 Telegram", use_container_width=True):
            from src.notifier import send_telegram_message
            ok = send_telegram_message(
                "👋 Hello from Stock Screener!設定成功。"
            )
            if ok:
                st.toast("✅ Telegram 已通", icon="✅")
            else:
                st.toast("❌ Telegram 發送失敗,看 console 日誌", icon="❌")

    if config.DISCORD_WEBHOOK_URL:
        if st.sidebar.button("💬 測試 Discord", use_container_width=True):
            from src.discord_notifier import send_discord_message
            ok = send_discord_message("📊 Discord 通了!")
            if ok:
                st.toast("✅ Discord 已通", icon="✅")
            else:
                st.toast("❌ Discord 發送失敗,看 console 日誌", icon="❌")


# 50 檔大型股抓取的回望天數(過去 1 年,讓回測有足夠歷史)
_TOP_50_LOOKBACK_DAYS = 365


def _run_update_full_market() -> None:
    """全市場 bulk 抓 OHLCV(TWSE + TPEx),約 30 秒。

    順手 init universe(把全市場 stock_id 寫入 stocks 表)。
    """
    with st.spinner("Init universe(TaiwanStockInfo)..."):
        sids = get_full_universe()
    st.toast(f"universe = {len(sids)} 檔", icon="📊")

    progress = st.progress(0.0, text="bulk 抓全市場 OHLCV...")
    bulk_df = fetch_all_daily_prices_bulk()
    progress.progress(0.7, text=f"寫入 SQLite ({len(bulk_df)} 筆)...")
    if not bulk_df.empty:
        n = db.upsert_daily_prices(bulk_df.to_dict("records"))
        progress.progress(1.0)
        progress.empty()
        st.toast(f"✅ 全市場 {n} 筆寫入", icon="✅")
    else:
        progress.empty()
        st.error("❌ TWSE + TPEx 都失敗 — 雲端 IP 被擋?看 console logs。")


def _run_update_top_50() -> None:
    """對 TW_TOP_50 跑一次增量抓取(已有的不重抓)。

    抓過去 _TOP_50_LOOKBACK_DAYS 天的 daily_price + institutional,
    讓「📈 簡易回測」有足夠歷史資料。
    """
    today = date.today()
    today_iso = today.isoformat()
    start_iso = (today - timedelta(days=_TOP_50_LOOKBACK_DAYS)).isoformat()

    progress = st.progress(0.0, text="準備抓取 50 檔過去 1 年的資料...")
    n = len(TW_TOP_50)
    success = 0
    for i, (sid, name) in enumerate(TW_TOP_50):
        # 進度條:每檔顯示兩個子步驟(price → institutional),會更新兩次
        progress.progress(
            (i + 0.3) / n,
            text=f"[{i + 1}/{n}] 抓取 {sid} {name} daily_price...",
        )
        ok_any = False
        try:
            db.upsert_stocks([{"stock_id": sid, "name": name, "market": "TW"}])
            fetch_daily_price(sid, start_iso, today_iso)
            ok_any = True
        except Exception:  # noqa: BLE001
            pass
        progress.progress(
            (i + 0.7) / n,
            text=f"[{i + 1}/{n}] 抓取 {sid} {name} institutional...",
        )
        try:
            fetch_institutional(sid, start_iso, today_iso)
            ok_any = True
        except Exception:  # noqa: BLE001
            pass
        if ok_any:
            success += 1
        progress.progress((i + 1) / n)
    progress.empty()
    st.toast(f"已更新 {success} / {n} 檔(過去 1 年)", icon="✅")


def _run_update_long_term_free() -> None:
    """對 TW_TOP_50 跑免費版長線資料(TWSE OpenAPI + FinMind dividend)。"""
    sids = [sid for sid, _ in TW_TOP_50]
    db.upsert_stocks(
        [{"stock_id": sid, "name": name, "market": "TW"} for sid, name in TW_TOP_50]
    )

    progress = st.progress(0.0, text="抓 TWSE 全市場 PE / EPS...")
    n = len(sids)

    def on_progress(idx: int, total: int, sid: str, err: Exception | None) -> None:
        suffix = " (失敗)" if err else ""
        progress.progress(
            idx / total, text=f"[{idx}/{total}] {sid}{suffix}"
        )

    # Step 1: TWSE OpenAPI(PE/PB/殖利率/EPS + PB 反推 ROE)
    from src.financial_fetcher_free import update_long_term_data_free  # lazy
    result = update_long_term_data_free(sids, on_progress=on_progress)
    n_metrics = len(result["success_metrics"])
    n_eps = len(result["success_eps"])

    # Step 2: FinMind dividend(無 token 也能用)
    progress.progress(0.0, text="抓 FinMind 歷年配息...")
    div_ok = 0
    for i, sid in enumerate(sids):
        progress.progress(
            (i + 1) / n, text=f"[{i + 1}/{n}] {sid} 配息..."
        )
        try:
            df = fetch_dividend(sid)
            if not df.empty:
                div_ok += 1
        except Exception:  # noqa: BLE001
            continue
    progress.empty()

    msg = (
        f"免費版完成:daily_metrics {n_metrics}/{n}、"
        f"EPS {n_eps}/{n}、配息 {div_ok}/{n}"
    )
    st.toast(msg, icon="📊")
    if n_metrics == 0:
        err = result.get("error")
        err_detail = (
            f"({type(err).__name__}): {str(err)[:200]}" if err is not None
            else "(無具體 error 訊息)"
        )
        st.error(
            f"❌ TWSE OpenAPI 在雲端被擋 {err_detail}\n\n"
            "💡 **這是已知問題** — Streamlit Cloud 的 IP 會被 TWSE 拒絕,"
            "**你按按鈕沒辦法成功**。\n\n"
            "🤖 我已設定 GitHub Actions **每週六早上 07:00 台北時間**自動更新,"
            "把 TWSE 資料 commit 進 repo,Cloud 重啟後會自動讀進 SQLite。\n"
            "→ **下次拉到的資料約 1 週新,通常足夠長線選股使用。**\n\n"
            "急用的話:\n"
            "- 在 **本機** 跑 sidebar 按鈕(本機 IP 不被擋)\n"
            "- 在 GitHub Actions 手動觸發 `weekly-market-update` workflow"
        )


# === 頁尾 ===

def _render_footer() -> None:
    st.markdown("---")
    st.markdown(
        '<p class="footer-warning">⚠️ 本工具僅供個人研究使用,不構成任何投資建議。'
        "投資請自行評估風險。</p>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
