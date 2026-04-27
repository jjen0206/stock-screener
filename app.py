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
    fetch_institutional,
    fetch_long_term_data,
)
from src.screener_long import screen_long
from src.screener_short import DEFAULT_SHORT_PARAMS, screen_short
from src.universe import TW_TOP_50, WATCHLIST_PATH, load_watchlist


PAGES = ["短線推薦", "長線口袋名單", "📈 簡易回測", "個股查詢", "設定"]

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

    # sidebar 參數區
    st.sidebar.markdown("### 短線參數")
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

    # 跑選股
    params = {
        "volume_multiplier": float(vol_mult),
        "kd_threshold_low": float(kd_low),
        "inst_buy_days": int(inst_days),
    }
    with st.spinner("執行短線選股邏輯..."):
        result = screen_short(today_iso, params=params)

    if result.empty:
        st.info(
            "📭 無符合條件的個股。可放寬參數:降低均量倍數、降低 KD 門檻、減少法人買超天數。"
        )
        return

    st.success(f"✅ 共 {len(result)} 檔符合條件(三條件:量價突破 + KD 黃金交叉 + 法人連買)")

    # 加「符合理由」欄位
    result_show = result.copy()
    result_show["符合理由"] = "量價突破 + KD 黃金交叉 + 法人連買"

    selection = st.dataframe(
        result_show,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )

    if selection and selection.selection.rows:
        idx = selection.selection.rows[0]
        sid = result.iloc[idx]["stock_id"]
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

    st.warning(
        "⚠️ 此功能需 **FinMind 升級會員 token** 才能用,需要季財報、ROE、配息資料。\n\n"
        "目前版本走無 token 模式,長線選股會回空清單。"
    )

    st.markdown("### 預設策略")
    st.markdown(
        """
- **高 ROE**:近 3 年平均季 ROE > 15%
- **低 PE**:當日本益比 < 20,或 < 該股票所屬產業平均 PE
- **連續配息**:近 5 年每年都有現金股利
- **殖利率**:近 1 年現金殖利率 > 4%
        """
    )

    st.markdown("---")
    cols = st.columns(2)
    cols[0].link_button(
        "📝 申請 FinMind Token",
        "https://finmindtrade.com/",
        use_container_width=True,
    )
    test_btn = cols[1].button(
        "🧪 我已升級 token,立即試用",
        type="primary",
        use_container_width=True,
    )

    if test_btn:
        if not _has_long_data():
            st.warning(
                "📊 還沒抓任何財報 / 配息資料。\n\n"
                "請點 **sidebar → 「📊 更新財報資料」**,等抓完(約 2–3 分鐘)後再回此頁試用。"
            )
        else:
            with st.spinner("執行長線選股..."):
                result = screen_long()
            if result.empty:
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

    # 短線頁可以 push stock_id 進來
    default_stock = st.session_state.pop("query_stock_id", "2330")

    cols = st.columns([2, 2, 2, 1])
    stock_id = cols[0].text_input("股票代碼", value=default_stock, help="例:2330(台積電)")
    start = cols[1].date_input("起始日", value=date(2024, 1, 1))
    end = cols[2].date_input("結束日", value=date(2024, 3, 31))
    cols[3].markdown("&nbsp;", unsafe_allow_html=True)
    submit = cols[3].button("查詢", use_container_width=True, type="primary")

    if not submit:
        st.info(
            "輸入股票代碼與日期區間後按「查詢」。\n\n"
            "**首次查詢的區間若不在 cache 內**,會打 FinMind API 抓取(無 token 模式較慢);"
            "之後同樣區間都直接從 SQLite 取。\n\n"
            "預設區間 2024-01-01 ~ 2024-03-31(2330 已在 cache)可立即顯示。"
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

    # 6 格 metric
    cols = st.columns(6)
    cols[0].metric("交易次數", f"{summary['trades']}")
    cols[1].metric("勝率", f"{summary['win_rate']:.1f}%")
    cols[2].metric("平均報酬", f"{summary['avg_return']:.2f}%")
    cols[3].metric(
        "總報酬(複利)", f"{summary['total_return']:.2f}%",
        delta=f"vs 0050 待算" if False else None,
    )
    cols[4].metric(
        "最大單筆",
        f"{summary['max_win']:.2f}%",
        delta=f"最差 {summary['max_loss']:.2f}%",
        delta_color="off",
    )
    cols[5].metric("夏普比率", f"{summary['sharpe']:.2f}")

    # 累積報酬曲線(含 0050 對比,失敗就不畫)
    st.markdown("### 📈 累積報酬曲線")
    st.plotly_chart(
        _make_equity_chart(result["equity_curve"], start.isoformat(), end.isoformat()),
        use_container_width=True,
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
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(equity_curve.index), y=equity_curve.values,
        name="策略累積報酬",
        line=dict(color="#d62728", width=2.5),
    ))

    # 嘗試疊上 0050 大盤對比(抓不到優雅降級)
    try:
        bench = fetch_daily_price("0050", start, end)
        if not bench.empty and len(bench) >= 2:
            base = float(bench["close"].iloc[0])
            if base > 0:
                bench_returns = (bench["close"] / base - 1) * 100
                fig.add_trace(go.Scatter(
                    x=bench["date"].tolist(),
                    y=bench_returns.tolist(),
                    name="0050 台灣 50",
                    line=dict(color="#1f77b4", width=2, dash="dot"),
                ))
    except Exception:  # noqa: BLE001 — 抓不到大盤就不畫
        pass

    fig.add_hline(y=0, line_color="gray", opacity=0.5)
    fig.update_layout(
        height=400,
        margin=dict(t=20, b=20, l=20, r=20),
        legend=dict(orientation="h", y=1.02, x=0, font=dict(size=16)),
        font=dict(size=16),
        xaxis=dict(tickfont=dict(size=14), type="category"),
        yaxis=dict(tickfont=dict(size=14), title="累積報酬 (%)"),
    )
    return fig


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
    if st.sidebar.button("📊 更新財報資料", use_container_width=True):
        _run_update_long_term()
    st.sidebar.caption(
        "📊 財報需要 FinMind token,無 token 模式可能拿不到。"
    )


def _run_update_top_50() -> None:
    """對 TW_TOP_50 跑一次增量抓取(已有的不重抓)。"""
    today = date.today()
    today_iso = today.isoformat()
    start_iso = (today - timedelta(days=90)).isoformat()

    progress = st.progress(0.0, text="準備...")
    n = len(TW_TOP_50)
    success = 0
    for i, (sid, name) in enumerate(TW_TOP_50):
        progress.progress(
            (i + 1) / n, text=f"更新 {i + 1}/{n}: {sid} {name}",
        )
        try:
            db.upsert_stocks([{"stock_id": sid, "name": name, "market": "TW"}])
            fetch_daily_price(sid, start_iso, today_iso)
            fetch_institutional(sid, start_iso, today_iso)
            success += 1
        except Exception:  # noqa: BLE001
            continue
    progress.empty()
    st.toast(f"已更新 {success} / {n} 檔資料", icon="✅")


def _run_update_long_term() -> None:
    """對 TW_TOP_50 跑季財報 + 配息抓取。"""
    sids = [sid for sid, _ in TW_TOP_50]
    # 確保 stocks 表有這 50 檔(產業欄位為空也沒關係,有就更新)
    db.upsert_stocks(
        [{"stock_id": sid, "name": name, "market": "TW"} for sid, name in TW_TOP_50]
    )

    progress = st.progress(0.0, text="準備...")
    n = len(sids)

    def on_progress(idx: int, total: int, sid: str, err: Exception | None) -> None:
        suffix = " (失敗)" if err else ""
        progress.progress(idx / total, text=f"更新財報 {idx}/{total}: {sid}{suffix}")

    result = fetch_long_term_data(sids, on_progress=on_progress)
    progress.empty()

    n_fin = len(result["success_financials"])
    n_div = len(result["success_dividend"])
    n_fail = len(result["failed"])
    msg = f"季財報 {n_fin} 檔、配息 {n_div} 檔已更新;{n_fail} 檔無資料"
    st.toast(msg, icon="📊")
    if n_fin == 0 and n_div == 0:
        st.warning(
            "⚠️ 全部 50 檔都沒拿到資料 — FinMind token 可能未設定,"
            "或免費版不支援這兩個 dataset。"
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
