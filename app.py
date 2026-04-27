"""
Stock Screener — Streamlit 入口。

T4-A 完成:sidebar 路由 + 個股查詢頁 + 設定頁
T4-B 完成:短線推薦頁 + 長線占位頁 + sidebar「更新今日資料」按鈕
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src import config, database as db, indicators as ind
from src.backtester import backtest_short
from src.data_fetcher import (
    FinMindAPIError,
    fetch_daily_price,
    fetch_dividend,
    fetch_institutional,
)
from src.financial_fetcher_free import update_long_term_data_free
from src.market_sentiment import (
    fetch_institutional_total,
    fetch_margin_balance,
    fetch_taiex,
    fetch_vix,
)
from src.screener_long import screen_long
from src.screener_short import DEFAULT_SHORT_PARAMS, screen_short
from src.strategies import (
    ALL_STRATEGIES, STRATEGY_LABELS,
    aggregated_to_dataframe, run_all_strategies,
)
from src.universe import TW_TOP_50, WATCHLIST_PATH, load_watchlist


PAGES = [
    "短線推薦", "長線口袋名單", "📈 簡易回測",
    "個股查詢", "⭐ 我的關注", "📊 大盤情緒", "設定",
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
</style>
"""


def _inject_global_css() -> None:
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)


# === 主程式 ===

def main() -> None:
    st.set_page_config(
        page_title="個人選股工具",
        page_icon="📈",
        layout="wide",
    )
    _inject_global_css()

    st.sidebar.title("📈 個人選股工具")
    st.sidebar.caption("台股 · 短線 + 長線")
    page = st.sidebar.radio("選擇功能", PAGES, index=3)

    if page == "短線推薦":
        _page_short()
    elif page == "長線口袋名單":
        _page_long()
    elif page == "📈 簡易回測":
        _page_backtest()
    elif page == "個股查詢":
        _page_stock_query()
    elif page == "⭐ 我的關注":
        _page_watchlist()
    elif page == "📊 大盤情緒":
        _page_market_sentiment()
    elif page == "設定":
        _page_settings()

    _render_sidebar_update()
    st.sidebar.markdown("---")
    st.sidebar.caption(
        f"市場:{config.DEFAULT_MARKET}　·　"
        f"FinMind:{'有 token' if config.FINMIND_TOKEN else '無 token'}"
    )

    _render_footer()


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

    # sidebar 參數區
    st.sidebar.markdown("### 短線參數(策略 1)")
    vol_mult = st.sidebar.number_input(
        "均量倍數", min_value=1.0, max_value=5.0,
        value=float(DEFAULT_SHORT_PARAMS["volume_multiplier"]), step=0.1,
        help="當日量 > 過去 5 日均量 × 此倍數",
    )
    kd_low = st.sidebar.number_input(
        "KD 門檻 K_low", min_value=0.0, max_value=80.0,
        value=float(DEFAULT_SHORT_PARAMS["kd_threshold_low"]), step=5.0,
        help="K 黃金交叉 D 後,K 至少要超過此值才入選",
    )
    inst_days = st.sidebar.number_input(
        "法人連續買超天數", min_value=1, max_value=10,
        value=int(DEFAULT_SHORT_PARAMS["inst_buy_days"]), step=1,
    )

    # 上方控制列
    cols = st.columns([2, 3, 1])
    target_date = cols[0].date_input("選股日期", value=date.today())
    universe_choice = cols[1].selectbox(
        "選股範圍",
        ["快速:50 檔大型股", "我的關注清單", "全部台股 (慢)"],
        help="無 token 模式抓全台股容易被 FinMind 頻率限制,建議先用 50 檔",
    )
    cols[2].markdown("&nbsp;", unsafe_allow_html=True)
    submit = cols[2].button("執行選股", type="primary", use_container_width=True)

    if not submit:
        st.info(
            "選好參數後按「執行選股」。\n\n"
            "首次執行會逐檔抓取最新資料(已在 cache 的不重抓),"
            "建議先用「快速:50 檔大型股」測試。"
        )
        return

    # 取選股範圍
    universe = _resolve_universe(universe_choice)
    if universe is None:
        return

    # 抓資料(進度條)
    today_iso = target_date.isoformat()
    start_iso = (target_date - timedelta(days=90)).isoformat()
    progress = st.progress(0.0, text="準備資料...")
    status = st.empty()
    n = len(universe)
    failures: list[str] = []
    for i, (sid, name) in enumerate(universe):
        progress.progress(
            (i + 1) / n,
            text=f"抓取 {i + 1}/{n}: {sid} {name}",
        )
        try:
            db.upsert_stocks([{"stock_id": sid, "name": name, "market": "TW"}])
            fetch_daily_price(sid, start_iso, today_iso)
            fetch_institutional(sid, start_iso, today_iso)
        except FinMindAPIError as e:
            failures.append(f"{sid}({e})")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{sid}({type(e).__name__}: {e})")
    progress.empty()
    status.empty()

    if failures:
        st.warning(f"⚠️ {len(failures)} 檔抓取失敗(可能無 token 被限制):{', '.join(failures[:5])}{'...' if len(failures) > 5 else ''}")

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
    with st.spinner(f"執行 {len(enabled_keys)} 套策略並行..."):
        agg = run_all_strategies(
            today_iso, enabled=enabled_keys, params=params,
            stock_ids=sids_only,
        )

    if not agg:
        st.info(
            "📭 任一啟用的策略都無入選。可放寬參數或加開更多策略。"
        )
        return

    st.success(
        f"✅ 共 {len(agg)} 檔被選中"
        f"({len(enabled_keys)} 套策略並行,按信號數排序)"
    )

    df = aggregated_to_dataframe(agg)
    selection = st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )

    if selection and selection.selection.rows:
        idx = selection.selection.rows[0]
        sid = df.iloc[idx]["stock_id"]
        st.session_state["query_stock_id"] = sid
        st.info(f"已選 **{sid}**。請點 sidebar 切到「個股查詢」頁查看詳細圖表。")


def _resolve_universe(choice: str) -> list[tuple[str, str]] | None:
    """根據使用者選擇回傳個股清單;回 None 表示已 render 提示、外層直接 return。"""
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
    # 全部台股
    st.warning(
        "⚠️ 無 token 模式抓全部台股會被 FinMind 頻率限制(可能 5–10 分鐘且容易失敗)。"
        "建議先升級 FinMind token,或改選「快速:50 檔大型股」。"
    )
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id, name FROM stocks WHERE market='TW'"
        ).fetchall()
    universe = [(r["stock_id"], r["name"]) for r in rows]
    if not universe:
        st.error(
            "stocks 表為空。請先在 sidebar 點「🔄 更新今日資料」初始化 50 檔大型股,"
            "或執行一次「快速:50 檔大型股」選股。"
        )
        return None
    return universe


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
            st.dataframe(
                result, use_container_width=True, hide_index=True
            )


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

    # 短線 / 關注頁可以 push stock_id 進來
    default_stock = st.session_state.pop("query_stock_id", "2330")

    # ⭐ Toggle 關注按鈕(放標題下,以「上次查詢的」或預設股號為對象)
    db.init_db()
    starred = db.is_in_watchlist(default_stock.strip())
    star_col, info_col = st.columns([1, 5])
    if star_col.button(
        f"{'⭐' if starred else '☆'} {'已關注' if starred else '加入關注'}",
        key="star_toggle",
        help=f"切換 {default_stock} 是否在關注清單",
    ):
        if starred:
            db.remove_from_watchlist(default_stock.strip())
            st.toast(f"已從關注移除 {default_stock}", icon="☆")
        else:
            db.add_to_watchlist(default_stock.strip())
            st.toast(f"已加入關注 {default_stock}", icon="⭐")
        st.rerun()
    info_col.caption(
        f"目前關注對象:**{default_stock}**(改下方代號後再切換才會對到新代號)"
    )

    cols = st.columns([2, 2, 2, 1])
    stock_id = cols[0].text_input("股票代碼", value=default_stock, help="例:2330(台積電)")
    today = date.today()
    start = cols[1].date_input("起始日", value=today - timedelta(days=90))
    end = cols[2].date_input("結束日", value=today)
    cols[3].markdown("&nbsp;", unsafe_allow_html=True)
    submit = cols[3].button("查詢", use_container_width=True, type="primary")

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


def _render_summary(
    df: pd.DataFrame,
    kd_df: pd.DataFrame,
    rsi14: pd.Series,
    macd_df: pd.DataFrame,
) -> None:
    st.markdown("### 📊 最新指標摘要")
    last = df.iloc[-1]
    prev_close = df["close"].iloc[-2] if len(df) >= 2 else None
    delta = (last["close"] - prev_close) if prev_close is not None else None

    cols = st.columns(4)
    cols[0].metric(
        "收盤", f"{last['close']:.2f}",
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

    # sidebar 參數(與短線頁同名,共用值)
    st.sidebar.markdown("### 短線參數(回測共用)")
    vol_mult = st.sidebar.number_input(
        "均量倍數", min_value=1.0, max_value=5.0,
        value=float(DEFAULT_SHORT_PARAMS["volume_multiplier"]), step=0.1,
        key="bt_vol_mult",
    )
    kd_low = st.sidebar.number_input(
        "KD 門檻 K_low", min_value=0.0, max_value=80.0,
        value=float(DEFAULT_SHORT_PARAMS["kd_threshold_low"]), step=5.0,
        key="bt_kd_low",
    )
    inst_days = st.sidebar.number_input(
        "法人連續買超天數", min_value=1, max_value=10,
        value=int(DEFAULT_SHORT_PARAMS["inst_buy_days"]), step=1,
        key="bt_inst_days",
    )

    today = date.today()
    cols = st.columns([2, 2, 2, 1])
    start = cols[0].date_input(
        "回測起始", value=today - timedelta(days=180), key="bt_start"
    )
    end = cols[1].date_input("回測結束", value=today, key="bt_end")
    hold_days = cols[2].selectbox(
        "持有天數", [1, 3, 5, 10, 20], index=2,
        help="每筆入選收盤買進,持有 N 個交易日後收盤賣出",
    )
    cols[3].markdown("&nbsp;", unsafe_allow_html=True)
    submit = cols[3].button(
        "執行回測", type="primary", use_container_width=True
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
            result = backtest_short(
                start.isoformat(), end.isoformat(),
                params=params,
                hold_days=int(hold_days),
                universe=universe,
                on_progress=cb,
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
    row2[1].metric("年化波動率", f"{g('annual_volatility', 0.0):.2f}%")
    row2[2].metric(
        "夏普比率",
        f"{g('sharpe', 0.0):.2f}",
        help=f"年化基準 √(252/{hd}) = {(252 / max(hd, 1)) ** 0.5:.2f}",
    )
    row2[3].metric(
        "最大單筆",
        f"{g('max_win', 0.0):.2f}%",
        delta=f"最差 {g('max_loss', 0.0):.2f}%",
        delta_color="off",
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
    st.dataframe(
        result["trades"].sort_values("buy_date", ascending=False),
        use_container_width=True, hide_index=True,
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

def _page_watchlist() -> None:
    st.header("⭐ 我的關注")
    db.init_db()

    items = db.get_watchlist()
    if not items:
        st.info(
            "目前沒有關注的個股。\n\n"
            "到「個股查詢」頁,輸入代號查詢後按 ☆ 圖示即可加入關注。"
        )
        return

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

    selection = st.dataframe(
        df, use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row",
    )
    if selection and selection.selection.rows:
        idx = selection.selection.rows[0]
        sid = items[idx]["stock_id"]
        st.session_state["query_stock_id"] = sid
        st.info(f"已選 **{sid}**。請點 sidebar 切到「個股查詢」頁查看詳細圖表。")

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
        # FinMind 此 dataset 每天每法人別一筆,需 pivot 取 buy-sell
        if "buy" in df.columns and "sell" in df.columns:
            df["net"] = (df["buy"].fillna(0) - df["sell"].fillna(0)) / 1e8  # 億元
            agg = df.groupby("date")["net"].sum().reset_index()
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
            recent_buy = (agg["net"].tail(5) > 0).sum()
            st.caption(
                f"💬 近 5 日法人合計買超天數:**{recent_buy}/5** "
                f"({'偏多' if recent_buy >= 3 else '偏空'})"
            )
        else:
            st.dataframe(df.head(20), use_container_width=True, hide_index=True)
            st.caption(
                f"💬 dataset 欄位:{list(df.columns)} "
                "— 與預期不同,請看 raw data"
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
    if st.sidebar.button("更新 50 檔大型股", use_container_width=True):
        _run_update_top_50()
    st.sidebar.caption("📅 每檔抓過去 1 年(供回測用),首次約 4–6 分鐘。")
    if st.sidebar.button("📊 更新財報資料(免費版)", use_container_width=True):
        _run_update_long_term_free()
    st.sidebar.caption(
        "📊 用 TWSE OpenAPI + FinMind 免費版,**不需 token**;"
        "PE/PB/殖利率/EPS 來自證交所,ROE 用 PB 反推(Du Pont 簡化)。"
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
        if err is not None:
            st.error(
                f"❌ TWSE OpenAPI 全失敗 ({type(err).__name__}): "
                f"{str(err)[:300]}\n\n"
                "雲端環境的 SSL / cipher / UA 不相容?"
                "右下 **Manage app → Logs** 可看完整 traceback。"
            )
        else:
            st.error(
                "❌ TWSE OpenAPI 全失敗(無具體 error 訊息),"
                "請看 Manage app → Logs。"
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
