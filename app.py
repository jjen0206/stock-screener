"""
Stock Screener — Streamlit 入口。

T4-A 階段實作:
- sidebar 四項分頁(短線推薦 / 長線口袋名單 / 個股查詢 / 設定)
- 「個股查詢」與「設定」兩頁完整實作
- 「短線推薦」「長線口袋名單」放佔位畫面,留給 T4-B
- 風險警語每頁底部都看得到
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src import config, database as db, indicators as ind
from src.data_fetcher import fetch_daily_price


# === 頁面定義 ===

PAGES = ["短線推薦", "長線口袋名單", "個股查詢", "設定"]

# 設定頁要列出的資料表
_CACHE_TABLES = [
    "stocks",
    "daily_prices",
    "institutional",
    "financials",
    "dividend",
    "sync_log",
]


# === 主程式 ===

def main() -> None:
    st.set_page_config(
        page_title="個人選股工具",
        page_icon="📈",
        layout="wide",
    )

    st.sidebar.title("📈 個人選股工具")
    st.sidebar.caption("台股 · 短線 + 長線")
    page = st.sidebar.radio("選擇功能", PAGES, index=2)
    st.sidebar.markdown("---")
    st.sidebar.caption(
        f"市場:{config.DEFAULT_MARKET}　·　"
        f"FinMind:{'有 token' if config.FINMIND_TOKEN else '無 token'}"
    )

    if page == "短線推薦":
        _page_short()
    elif page == "長線口袋名單":
        _page_long()
    elif page == "個股查詢":
        _page_stock_query()
    elif page == "設定":
        _page_settings()

    _render_footer()


# === 個股查詢頁 ===

def _page_stock_query() -> None:
    st.header("🔍 個股查詢")

    cols = st.columns([2, 2, 2, 1])
    stock_id = cols[0].text_input("股票代碼", value="2330", help="例:2330(台積電)")
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
    except Exception as e:  # noqa: BLE001 — UI 層面要吃掉所有錯誤,不能爆
        st.error(f"資料抓取失敗:{type(e).__name__}: {e}")
        st.caption(
            "可能原因:股票代碼錯誤、無 token 模式被頻率限制、網路問題。"
        )
        return

    if df.empty:
        st.warning(
            f"找不到 **{sid}** 在 {start} ~ {end} 的資料。\n\n"
            "可能股票代碼錯誤,或該區間沒有交易日。"
        )
        return

    # 算指標
    df = df.copy()
    df["MA5"] = ind.sma(df, 5)
    df["MA20"] = ind.sma(df, 20)
    df["MA60"] = ind.sma(df, 60)
    bb = ind.bollinger(df, period=20, num_std=2.0)
    kd_df = ind.kd(df, n=9)
    rsi14 = ind.rsi(df, period=14)
    macd_df = ind.macd(df, fast=12, slow=26, signal=9)

    st.caption(f"取得 {len(df)} 筆,日期 {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")

    # K 線圖
    st.plotly_chart(_make_candlestick(df, bb), use_container_width=True)

    # 指標分頁
    tab_kd, tab_macd, tab_rsi = st.tabs(["KD (9, 3, 3)", "MACD (12, 26, 9)", "RSI (14)"])
    with tab_kd:
        st.plotly_chart(_make_kd_chart(df, kd_df), use_container_width=True)
    with tab_macd:
        st.plotly_chart(_make_macd_chart(df, macd_df), use_container_width=True)
    with tab_rsi:
        st.plotly_chart(_make_rsi_chart(df, rsi14), use_container_width=True)

    # 摘要
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
    """K 線 + 均線 + 布林通道 + 量。"""
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.72, 0.28],
    )
    # K 線(台股慣例:紅漲綠跌)
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
    # 均線
    for col, color in [("MA5", "#1f77b4"), ("MA20", "#ff7f0e"), ("MA60", "#9467bd")]:
        if col in df.columns and df[col].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=df["date"], y=df[col],
                    name=col, line=dict(width=1.2, color=color),
                ),
                row=1, col=1,
            )
    # 布林通道(上下軌帶填色)
    if not bb.empty and bb["upper"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=bb["upper"],
                name="BB 上", line=dict(width=1, dash="dot", color="rgba(120,120,120,0.7)"),
            ),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=bb["lower"],
                name="BB 下", line=dict(width=1, dash="dot", color="rgba(120,120,120,0.7)"),
                fill="tonexty", fillcolor="rgba(120,120,120,0.08)",
            ),
            row=1, col=1,
        )
    # 量(漲紅 / 跌綠)
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
        legend=dict(orientation="h", y=1.02, x=0),
    )
    fig.update_xaxes(type="category", row=1, col=1)
    fig.update_xaxes(type="category", row=2, col=1)
    fig.update_yaxes(title_text="股價", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)
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
        yaxis=dict(range=[0, 100]),
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
    fig.update_layout(height=320, margin=dict(t=20, b=20, l=20, r=20))
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
        yaxis=dict(range=[0, 100]),
    )
    fig.update_xaxes(type="category")
    return fig


# === 設定頁 ===

def _page_settings() -> None:
    st.header("⚙️ 設定")

    st.markdown("### 環境變數 (.env)")
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
        "註:此頁僅唯讀。要修改設定請編輯專案根目錄的 `.env` 後重啟 Streamlit。"
    )


def _get_table_counts() -> dict[str, object]:
    """各表 row 數;表不存在記為 '—'。"""
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


# === 佔位頁 ===

def _page_short() -> None:
    st.header("🔥 短線推薦")
    st.info("📌 T4-B 階段實作。屆時會列出當日符合「量價突破 + 法人買超 + KD 黃金交叉」的個股。")


def _page_long() -> None:
    st.header("💎 長線口袋名單")
    st.info("📌 T4-B 階段實作。屆時會列出符合「高 ROE + 低 PE + 連續配息 + 殖利率」的個股。")


# === 頁尾 ===

def _render_footer() -> None:
    st.markdown("---")
    st.caption(
        "⚠️ 本工具僅供個人研究使用,不構成任何投資建議。投資請自行評估風險。"
    )


if __name__ == "__main__":
    main()
