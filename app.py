"""
Stock Screener — Streamlit 入口。

T4-A 完成:sidebar 路由 + 個股查詢頁 + 設定頁
T4-B 完成:短線推薦頁 + 長線占位頁 + sidebar「更新今日資料」按鈕
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src import config, database as db, indicators as ind
from src.backtester import backtest_short
from src.cache_utils import clear_all_caches
from src.data_fetcher import (
    FinMindAPIError,
    ensure_stock_info,
    fetch_all_daily_prices_bulk,
    fetch_daily_price,
    fetch_dividend,
    fetch_institutional,
)
from src.financial_fetcher_free import update_long_term_data_free
from src.individual_sections import (
    _compute_key_levels,
    _compute_main_force_signal,  # noqa: F401  個股頁不直接用,e2e test 從 app namespace 拿
    _compute_technical_summary,
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
from src.screener_long import screen_long
from src.screener_short import DEFAULT_SHORT_PARAMS
from src.ui_cards import render_picks_cards, view_mode_toggle
from src.strategies import (
    STRATEGY_LABELS,
    aggregated_to_dataframe, compute_target_prices, run_all_strategies,
)
from src.universe import (
    TW_TOP_50, WATCHLIST_PATH, get_full_universe, is_pure_stock,
    load_watchlist, pure_stock_universe,
)


PAGES = [
    "🏠 首頁", "🔥 短線", "💎 長線", "📈 回測",
    "🔍 個股", "⭐ 關注", "📊 大盤", "⚙️ 系統", "⚙️ 設定",
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

_snapshot_loaded = False


def _load_snapshot_if_needed() -> None:
    """雲端 startup 時:若 SQLite 內 financials/daily_metrics 為空,
    讀 data/twse_snapshot/*.csv 灌進去。

    解決 Streamlit Cloud IP 被 TWSE OpenAPI 擋的問題,改用每週六 GitHub Actions
    自動抓資料 commit CSV → Cloud 容器 git pull 時自動拿到 → app 啟動時讀進 SQLite。

    用 module-level flag 避免每次 page rerun 都重灌(streamlit hot-reload 不會重 import module)。
    """
    global _snapshot_loaded
    if _snapshot_loaded:
        return

    snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    if not snapshot_dir.exists():
        _snapshot_loaded = True
        return

    db.init_db()

    # 一律 load CSV — 所有 upsert_* 都是 idempotent (PRIMARY KEY ON CONFLICT
    # DO UPDATE),即使 SQLite 已有資料也安全。之前用 prices_cnt < 1000 等門檻
    # 會擋住:雲端 daily_fetch 累積幾天 prices_cnt 早就 > 1000,結果 backfill
    # snapshot 的 90 天歷史完全灌不進去。

    # 灌 stocks (含 industry)
    stocks_csv = snapshot_dir / "stocks.csv"
    if stocks_csv.exists():
        df = pd.read_csv(stocks_csv, dtype={"stock_id": str})
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "stock_id": str(r["stock_id"]),
                "name": str(r["name"]) if pd.notna(r.get("name")) else "",
                "industry": str(r["industry"]) if pd.notna(r.get("industry")) else None,
                "market": "TW",
            })
        if rows:
            db.upsert_stocks(rows)

    # 灌 daily_metrics
    metrics_csv = snapshot_dir / "daily_metrics.csv"
    if metrics_csv.exists():
        df = pd.read_csv(metrics_csv, dtype={"stock_id": str})
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "stock_id": str(r["stock_id"]),
                "date": str(r["date"]),
                "close": float(r["close"]) if pd.notna(r.get("close")) else None,
                "pe": float(r["pe"]) if pd.notna(r.get("pe")) else None,
                "pb": float(r["pb"]) if pd.notna(r.get("pb")) else None,
                "dividend_yield": float(r["dividend_yield"]) if pd.notna(r.get("dividend_yield")) else None,
            })
        if rows:
            db.upsert_daily_metrics(rows)

    # 灌 financials.quarterly
    fin_csv = snapshot_dir / "financials_quarterly.csv"
    if fin_csv.exists():
        df = pd.read_csv(fin_csv, dtype={"stock_id": str})
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "stock_id": str(r["stock_id"]),
                "period_type": "quarterly",
                "period": str(r["period"]),
                "revenue": float(r["revenue"]) if pd.notna(r.get("revenue")) else None,
                "revenue_yoy": float(r["revenue_yoy"]) if pd.notna(r.get("revenue_yoy")) else None,
                "eps": float(r["eps"]) if pd.notna(r.get("eps")) else None,
                "roe": float(r["roe"]) if pd.notna(r.get("roe")) else None,
            })
        if rows:
            db.upsert_financials(rows)

    # 灌 daily_prices(從 backfill_history.py 產生的 snapshot)
    prices_csv = snapshot_dir / "daily_prices.csv"
    if prices_csv.exists():
        df = pd.read_csv(prices_csv, dtype={"stock_id": str})
        # 用 to_dict('records') 比 iterrows 快 30-50x(180K 行差很大)
        records = df.to_dict("records")
        # NaN → None(SQLite 不接受 NaN)
        for r in records:
            for k, v in list(r.items()):
                if pd.isna(v):
                    r[k] = None
        if records:
            db.upsert_daily_prices(records)

    # 灌 institutional(同樣從 backfill snapshot)
    inst_csv = snapshot_dir / "institutional.csv"
    if inst_csv.exists():
        df = pd.read_csv(inst_csv, dtype={"stock_id": str})
        records = df.to_dict("records")
        for r in records:
            for k, v in list(r.items()):
                if pd.isna(v):
                    r[k] = None
        if records:
            db.upsert_institutional(records)

    # 灌 watchlist(避免雲端 reboot 後 user 關注清單丟光)
    # 走 safe_boot_load:會優先嘗試 watchlist-sync 遠端,任何錯誤(ImportError、
    # 認證失敗、parse error 等)一律 silent fallback 到本機 seed CSV,絕不 raise
    # — boot 路徑禁止 crash。
    from src import watchlist_snapshot
    watchlist_snapshot.safe_boot_load()

    _snapshot_loaded = True


# === 主程式 ===

def main() -> None:
    st.set_page_config(
        page_title="個人選股工具",
        page_icon="📈",
        layout="wide",
    )
    _inject_global_css()
    _inject_pwa()
    _load_snapshot_if_needed()

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

    # 上方水平 tabs 取代 sidebar radio(手機優先)
    # 註:用 segmented_control 而非 st.tabs — st.tabs 會把所有頁渲染,
    # 觸發不必要的選股 + 大盤情緒抓取。segmented_control 走 session_state
    # 單頁路由,行為等同舊版 radio 但放在主區頂端。
    if "active_page" not in st.session_state:
        st.session_state["active_page"] = PAGES[0]  # 預設首頁 Dashboard
    page = st.segmented_control(
        "頁面", PAGES,
        default=st.session_state["active_page"],
        key="nav_segmented",
        label_visibility="collapsed",
    ) or st.session_state["active_page"]
    st.session_state["active_page"] = page

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
    elif page == "⭐ 關注":
        _page_watchlist()
    elif page == "📊 大盤":
        _page_market_sentiment()
    elif page == "⚙️ 系統":
        _page_system()
    elif page == "⚙️ 設定":
        _page_settings()

    _render_sidebar_update()
    st.sidebar.markdown("---")
    st.sidebar.caption(
        f"市場:{config.DEFAULT_MARKET}　·　"
        f"FinMind:{'有 token' if config.FINMIND_TOKEN else '無 token'}"
    )

    _render_footer()


# === 首頁 Dashboard ===

def _page_dashboard() -> None:
    """4 區塊速覽:大盤 / 今日推薦 Top 3 / 關注 Top 3 / 系統狀態。"""
    st.header("🏠 今日速覽")
    st.caption("一頁看完台股最新狀況。詳細分析請切上方 tabs。")

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
            st.metric(
                f"加權指數 ({taiex_df['date'].iloc[-1]})",
                f"{last_close:,.2f}",
                f"{delta:+.2f} ({delta_pct:+.2f}%)",
                delta_color="normal" if delta >= 0 else "inverse",
            )

    # === 2. 今日短線推薦 Top 3 ===
    st.markdown("### 🔥 今日短線推薦 (Top 3)")
    # 20 天足夠跑量價KD + 乖離率(60+ 天靠 backfill 抓 90 calendar days
    # 只能換到 ~54 個交易日,湊不到 60 桶);ETF/債券過濾(短線是個股取向)
    eligible_sids = pure_stock_universe(min_history=20)
    if not eligible_sids:
        st.caption(
            "📭 cache 內沒有任何個股累積 20 天歷史。"
            "請按 sidebar『⏳ 一次性回補 90 日歷史』。"
        )
    else:
        with st.spinner(f"掃描 {len(eligible_sids)} 檔(20+ 天 / 純股票)..."):
            try:
                today_iso = date.today().isoformat()
                agg = run_all_strategies(today_iso, stock_ids=eligible_sids)
                df_picks = aggregated_to_dataframe(agg).head(3)
            except Exception as e:  # noqa: BLE001
                st.caption(f"掃描失敗:{type(e).__name__}: {e}")
                df_picks = pd.DataFrame()
        if df_picks.empty:
            st.caption("📭 今日無入選。可切「🔥 短線」放寬參數試試。")
        else:
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
        f"上限 7 檔 / 30 秒內只能推一次 / footer 會標『雲端 App 手動推播』。"
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

def _page_short() -> None:
    st.header("🔥 短線推薦")

    # sidebar 多策略選擇
    st.sidebar.markdown("### 啟用策略")
    label_to_key = {v: k for k, v in STRATEGY_LABELS.items()}
    selected_labels = st.sidebar.multiselect(
        "策略",
        list(STRATEGY_LABELS.values()),
        default=list(STRATEGY_LABELS.values()),
        help="多選 = 多策略並行,信號越多 = 越多策略同時看好",
    )
    enabled_keys = [label_to_key[lbl] for lbl in selected_labels]

    # sidebar 參數區(預設收起,進階使用者再展開)
    with st.sidebar.expander("⚙️ 進階參數(策略 1)", expanded=False):
        vol_mult = st.number_input(
            "均量倍數", min_value=1.0, max_value=5.0,
            value=float(DEFAULT_SHORT_PARAMS["volume_multiplier"]), step=0.1,
            help="當日量 > 過去 5 日均量 × 此倍數",
        )
        kd_low = st.number_input(
            "KD 門檻 K_low", min_value=0.0, max_value=80.0,
            value=float(DEFAULT_SHORT_PARAMS["kd_threshold_low"]), step=5.0,
            help="K 黃金交叉 D 後,K 至少要超過此值才入選",
        )
        inst_days = st.number_input(
            "法人連續買超天數", min_value=1, max_value=10,
            value=int(DEFAULT_SHORT_PARAMS["inst_buy_days"]), step=1,
        )

    # cache 健康度(供下方 selectbox + caption 用)
    health = db.cache_health_summary()
    # 20+ 天 = 60+ 桶 + 20-59 桶之和(20 天足夠跑量價KD + 乖離率;
    # ma_alignment 需 MA60 但 backfill 90 calendar days 只能換 ~54 交易日)
    eligible_stocks = health["buckets"]["60+"] + health["buckets"]["20-59"]

    # 上方控制列
    cols = st.columns([2, 3, 1])
    target_date = cols[0].date_input("選股日期", value=_get_default_screen_date())
    universe_options = [
        f"🎯 充足歷史的純股票 ({eligible_stocks} 檔, 20+ 天 / 不含 ETF & 債券)",
        f"📊 充足歷史的股 ({eligible_stocks} 檔, 20+ 天 / 含 ETF & 債券)",
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
        f"60+ 天 {b['60+']}・20-59 天 {b['20-59']}・"
        f"14-19 天 {b['14-19']}・<14 天 {b['<14']}"
        + (
            "  ⚠️ 多數個股歷史不足,請按 sidebar『⏳ 一次性回補 90 日歷史』"
            if eligible_stocks < 100 else ""
        )
    )

    if not submit:
        st.info(
            "選好參數後按「執行選股」。\n\n"
            "全市場 / TOP 50 走 GH Actions 每日更新的 SQLite 快取,秒級回應;"
            "我的關注清單會即時補抓最新資料。"
        )
        return

    import time as _time
    t0 = _time.perf_counter()

    # 取選股範圍
    universe = _resolve_universe(universe_choice)
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

    # 跑多策略並行
    params = {
        "volume_multiplier": float(vol_mult),
        "kd_threshold_low": float(kd_low),
        "inst_buy_days": int(inst_days),
    }
    if not enabled_keys:
        st.warning("⚠️ 至少要選一套策略")
        return
    sids_only = [s for s, _ in universe]
    with st.spinner(
        f"掃描 {len(sids_only)} 檔 × {len(enabled_keys)} 套策略 "
        f"(bulk SQL load)..."
    ):
        agg = run_all_strategies(
            today_iso, enabled=enabled_keys, params=params,
            stock_ids=sids_only,
        )

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

    df = aggregated_to_dataframe(agg)
    t3 = _time.perf_counter()

    # 顯示模式切換(手機預設卡片,桌機可切表格)
    view_mode = view_mode_toggle("short_view_mode")

    if view_mode == "🃏 卡片":
        render_picks_cards(
            df.to_dict("records"),
            show_add_button=True, button_key_prefix="short",
        )
        selection = None  # 卡片模式不支援 row selection
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

    t4 = _time.perf_counter()
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
        bcols = st.columns([1, 1, 3])
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
                render_picks_cards(
                    result.to_dict("records"),
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

def _page_stock_query() -> None:
    st.header("🔍 個股查詢")
    db.init_db()

    # 短線 / 關注頁可以 push stock_id 進來當預設
    default_stock = st.session_state.pop("query_stock_id", "2330")

    # 輸入區先(input 在 toggle 之前,toggle 拿到的就是最新值)
    cols = st.columns([2, 2, 2, 1])
    stock_id = cols[0].text_input(
        "股票代碼", value=default_stock, help="例:2330(台積電)",
    )
    today = date.today()
    start = cols[1].date_input("起始日", value=today - timedelta(days=90))
    end = cols[2].date_input("結束日", value=today)
    cols[3].markdown("&nbsp;", unsafe_allow_html=True)
    submit = cols[3].button("查詢", use_container_width=True, type="primary")

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

    st.caption(f"取得 {len(df)} 筆,日期 {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")

    st.plotly_chart(_make_candlestick(df, bb), use_container_width=True)

    tab_kd, tab_macd, tab_rsi = st.tabs(
        ["KD (9, 3, 3)", "MACD (12, 26, 9)", "RSI (14)"]
    )
    with tab_kd:
        st.plotly_chart(_make_kd_chart(df, kd_df), use_container_width=True)
    with tab_macd:
        st.plotly_chart(_make_macd_chart(df, macd_df), use_container_width=True)
    with tab_rsi:
        st.plotly_chart(_make_rsi_chart(df, rsi14), use_container_width=True)

    _render_summary(df, kd_df, rsi14, macd_df)
    _render_institutional_table(sid)
    _render_institutional_cumulative_table(sid)
    _render_multi_timeframe(sid)
    _render_pattern_analysis(sid)
    _render_main_force_signal(sid)
    _render_technical_summary(sid)
    _render_key_levels(sid)
    _render_action_suggestion(sid)


def _render_summary(
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
    cols[0].metric(
        "收盤", f"{close:.2f}",
        f"{delta:+.2f}" if delta is not None else None,
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
        cols[0].metric(
            "🎯 保守目標",
            f"{target_low:.2f}",
            f"+{(target_low - close) / close * 100:.1f}%",
        )
        cols[1].metric(
            "🚀 積極目標",
            f"{target_high:.2f}",
            f"+{(target_high - close) / close * 100:.1f}%",
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
                    arrow = "▲" if ret >= 0 else "▼"
                    st.metric(
                        "報酬", f"{ret:+.2f}%",
                        delta=f"{arrow}",
                        delta_color="normal" if ret >= 0 else "inverse",
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


def _page_watchlist() -> None:
    st.header("⭐ 我的關注")
    db.init_db()

    # 雲端持久化警語(短線是 cloud reboot 容器會清光 SQLite,只 watchlist.csv 會被讀回)
    with st.expander("ℹ️ 關於關注清單持久化(雲端使用者注意)", expanded=False):
        st.markdown(
            "**雲端關注清單儲存方式**:\n"
            "- 雲端 SQLite 在容器重啟時會被清光 → 只有 "
            "`data/twse_snapshot/watchlist.csv` (commit 進 repo) 會被保留\n"
            "- 雲端 UI 加的關注是**暫時的**,直到下次 `weekly_market_update` "
            "或 `backfill_history` workflow 跑完(會 dump 到 CSV 並 commit)\n"
            "- **永久保存的方法**:\n"
            "  1. 本機 git pull → 跑 streamlit → 加股票 → 跑 "
            "`python scripts/weekly_market_update.py` → push commit\n"
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

    target_prices: dict[str, dict | None] = {}  # 留給下方目標價區塊用
    for it in items:
        sid = it["stock_id"]
        # 取最新兩日 close 算漲跌 + 最近 5 日 MA5
        with db.get_conn() as conn:
            recent = conn.execute(
                "SELECT date, close FROM daily_prices "
                "WHERE stock_id=? ORDER BY date DESC LIMIT 5",
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
            ma5 = (
                sum(float(r["close"]) for r in recent if r["close"]) / len(recent)
                if recent else None
            )
        else:
            close = prev_close = change_pct = ma5 = None
        # 順便算目標價,留給下方區塊用(不塞進表格,保持原樣)
        target_prices[sid] = compute_target_prices(sid)
        rows.append({
            "代號": sid,
            "名稱": name_map.get(sid, "—"),
            "收盤": f"{close:.2f}" if close else "—",
            "漲跌%": f"{change_pct:+.2f}%" if change_pct is not None else "—",
            "MA5": f"{ma5:.2f}" if ma5 else "—",
            "備註": it.get("note") or "",
            "加入時間": it["added_at"][:10] if it["added_at"] else "—",
        })
    df = pd.DataFrame(rows)

    view_mode = view_mode_toggle("watchlist_view_mode")
    if view_mode == "🃏 卡片":
        # 把 watchlist row 轉成 ui_cards 認得的 schema
        def _to_float(s: str) -> float | None:
            if not s or s == "—":
                return None
            try:
                return float(str(s).rstrip("%").rstrip("+"))
            except (TypeError, ValueError):
                return None

        cards = []
        for it, r in zip(items, rows):
            sid = it["stock_id"]
            tp = target_prices.get(sid)
            card = {
                "stock_id": sid,
                "name": r["名稱"],
                "close": _to_float(r["收盤"]),
                "change_pct": _to_float(r["漲跌%"]),
            }
            if tp is not None:
                card.update({
                    "target_low": tp.get("target_low"),
                    "target_high": tp.get("target_high"),
                    "stop_loss": tp.get("stop_loss"),
                    "risk_reward": tp.get("risk_reward"),
                })
            cards.append(card)
        render_picks_cards(
            cards, show_signal=False, show_targets=True, show_change=True,
        )
    else:
        selection = st.dataframe(
            df, use_container_width=True, hide_index=True,
            on_select="rerun", selection_mode="single-row",
        )
        if selection and selection.selection.rows:
            idx = selection.selection.rows[0]
            sid = items[idx]["stock_id"]
            st.session_state["query_stock_id"] = sid
            st.info(f"已選 **{sid}**。請點 sidebar 切到「個股查詢」頁查看詳細圖表。")

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


# === 📊 大盤情緒頁 ===

def _page_market_sentiment() -> None:
    st.header("📊 大盤情緒")
    st.caption("資料 60 秒 cache;失敗區塊不影響其他指標。")

    c1, c2 = st.columns(2)

    # 加權指數
    with c1:
        st.markdown("### 加權指數 (TAIEX)")
        df = fetch_taiex(days=90)
        if df.empty:
            st.warning("加權指數抓取失敗(可能 FinMind 限流)")
        else:
            last_close = float(df["close"].iloc[-1])
            prev_close = (
                float(df["close"].iloc[-2]) if len(df) >= 2 else last_close
            )
            delta = last_close - prev_close
            delta_pct = delta / prev_close * 100 if prev_close else 0
            st.metric(
                f"當日收盤 {df['date'].iloc[-1]}",
                f"{last_close:,.2f}",
                f"{delta:+.2f} ({delta_pct:+.2f}%)",
            )
            fig = go.Figure(go.Scatter(
                x=df["date"], y=df["close"],
                mode="lines", line=dict(color="#1f77b4", width=2),
            ))
            fig.update_layout(
                height=250, margin=dict(t=10, b=10, l=10, r=10),
                font=dict(size=14),
                xaxis=dict(tickfont=dict(size=12)),
                yaxis=dict(tickfont=dict(size=12)),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)
            # 短評
            short = "短期偏多" if delta > 0 else "短期偏空"
            st.caption(f"💬 當日 {short}")

    # VIX
    with c2:
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

    # 三大法人
    st.markdown("### 三大法人合計買賣超(全市場,近 30 日)")
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
        # FinMind 通常給 MarginPurchaseTodayBalance / ShortSaleTodayBalance
        # 欄位名隨版本可能不同,defensive 處理
        margin_col = next(
            (c for c in df.columns if "Margin" in c and "Balance" in c),
            None,
        )
        short_col = next(
            (c for c in df.columns if "Short" in c and "Balance" in c),
            None,
        )
        if margin_col and short_col:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=df["date"], y=df[margin_col] / 1e8,
                name="融資餘額(億)", line=dict(color="#d62728"),
            ))
            fig.add_trace(go.Scatter(
                x=df["date"], y=df[short_col] / 1e8,
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
        else:
            st.dataframe(df.head(20), use_container_width=True, hide_index=True)
            st.caption(f"💬 dataset 欄位:{list(df.columns)}")


# === 系統健康監控頁 ===

def _render_system_health() -> None:
    """系統頁:資料覆蓋率 / 更新時間 / backfill workflow / API token / SQLite 統計。"""
    import os
    from pathlib import Path

    db.init_db()

    # === 📊 資料覆蓋率 ===
    st.markdown("### 📊 資料覆蓋率")
    health = db.cache_health_summary()
    cols = st.columns(4)
    cols[0].metric("全市場股票", f"{health['total_stocks']:,}")
    cols[1].metric("有價量歷史", f"{health['with_prices']:,}")
    b = health["buckets"]
    cols[2].metric("≥60 天歷史", f"{b['60+']:,}")
    cols[3].metric("<14 天 (新)", f"{b['<14']:,}")
    st.caption(
        f"分桶:60+ {b['60+']:,} / 20-59 {b['20-59']:,} / "
        f"14-19 {b['14-19']:,} / <14 {b['<14']:,}"
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
