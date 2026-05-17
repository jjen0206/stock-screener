"""個股深度頁 📈 K 線 tab 用的互動 plotly chart renderer。

設計重點:
- 主圖 OHLC + MA20/MA60(+ 可選 Bollinger)
- 副圖動態 row 配置:Volume / RSI / MACD / KD / Stoch — 哪幾個有就疊幾個
- 主圖約 60% 高,副圖均分剩餘 ~40%(iPhone 看 OK)
- shared_xaxes + category x-axis 對齊
- hover 模式:x unified(同一個時間點所有指標一起看)
- mobile-first:tickfont 縮小、margin 縮緊、rangeslider 關閉

對外:
- render_candlestick_chart(sid, days, indicators, *, db_path=None, df=None) -> Figure
- mark_pick_dates(fig, sid, *, conn=None, db_path=None) -> Figure
- mark_pattern_signals(fig, sid, days, *, db_path=None) -> Figure   (graceful)
- mark_position_levels(fig, sid, *, conn=None, db_path=None) -> Figure
- compute_bollinger / compute_kd / compute_stoch   (薄 wrapper / Stoch 額外實作)

對 candlestick_patterns 模組走 try/except 軟相依(B task 還沒 merge 時不炸)。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src import database as db, indicators as ind


# === Indicator helpers ===============================================

def compute_bollinger(
    close: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    """Bollinger Bands wrapper — 接受 close Series,回 [mid, upper, lower]。"""
    df = pd.DataFrame({"close": close.astype(float)})
    return ind.bollinger(df, period=period, num_std=num_std)


def compute_kd(df: pd.DataFrame, period: int = 9) -> pd.DataFrame:
    """KD 9-3-3 wrapper(台股傳統算法)。回 DataFrame[K, D]。"""
    return ind.kd(df, n=period)


def compute_stoch(
    df: pd.DataFrame,
    period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> pd.DataFrame:
    """Stochastic Oscillator(%K / %D,美股慣例)。

    跟 KD 不同的是:
    - %K 用 SMA(raw_k, smooth_k) 平滑(KD 是 EMA-like 2/3·prev + 1/3·raw)
    - %D 用 SMA(%K, smooth_d) 平滑

    回 DataFrame[K, D]。
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    rolling_high = high.rolling(window=period, min_periods=period).max()
    rolling_low = low.rolling(window=period, min_periods=period).min()
    denom = rolling_high - rolling_low
    # 安全除法:denom > 0 算 (close - low)/denom × 100;denom == 0(無波動)→ 0;
    # denom NaN(rolling 窗口未滿)→ NaN(保留)。
    raw_k = pd.Series(np.nan, index=close.index, dtype=float)
    valid = denom.notna()
    nonzero = valid & (denom > 0)
    raw_k.loc[nonzero] = (
        (close - rolling_low) / denom * 100.0
    ).loc[nonzero]
    raw_k.loc[valid & (denom == 0)] = 0.0
    k = raw_k.rolling(window=smooth_k, min_periods=smooth_k).mean()
    d = k.rolling(window=smooth_d, min_periods=smooth_d).mean()
    return pd.DataFrame({"K": k, "D": d}, index=close.index)


# === Main render =====================================================

# 支援的指標 key(大小寫不敏感),歸類:
#   main-row: MA20 / MA60 / Bollinger(BB)
#   subplot : Volume / RSI / MACD / KD / Stoch
_MAIN_INDICATORS = {"ma20", "ma60", "bollinger", "bb"}
_SUBPLOT_INDICATORS = ("volume", "rsi", "macd", "kd", "stoch")


def render_candlestick_chart(
    sid: str,
    days: int = 120,
    indicators: Iterable[str] | None = None,
    *,
    db_path: str | Path | None = None,
    df: pd.DataFrame | None = None,
) -> go.Figure:
    """渲染互動 K 線圖。

    Args:
        sid: 股號(只用於 fallback message 顯示)。
        days: lookback 天數(60/120/180/360)。
        indicators: 要疊的指標 list,大小寫不敏感。
            支援 'MA20'/'MA60'/'Bollinger'/'Volume'/'RSI'/'MACD'/'KD'/'Stoch'。
            None → default ['MA20','MA60','Volume','RSI','MACD']。
        db_path: 給 test 灌 isolated DB。None → 走 production cache.db。
        df: 已預先 load 的 DataFrame(避免重複 query)。None → 從 DB 撈。

    Returns:
        plotly.graph_objects.Figure。沒資料 → 帶「找不到」annotation 的空圖。
    """
    indicators_set = {
        s.lower() for s in (indicators if indicators is not None
                            else ["MA20", "MA60", "Volume", "RSI", "MACD"])
    }

    if df is None:
        df = db.get_stock_kline_with_indicators(sid, days=days, db_path=db_path)

    if df is None or df.empty:
        fig = go.Figure()
        fig.add_annotation(
            text=f"📭 找不到 {sid} 歷史 K 線",
            xref="paper", yref="paper",
            x=0.5, y=0.5, showarrow=False,
            font=dict(size=16, color="#888"),
        )
        fig.update_layout(height=320, margin=dict(t=20, b=20, l=20, r=20))
        return fig

    # 副圖 row plan
    sub_rows = [k for k in _SUBPLOT_INDICATORS if k in indicators_set]
    total_rows = 1 + len(sub_rows)
    if total_rows == 1:
        row_heights: list[float] = [1.0]
    else:
        sub_h = 0.40 / len(sub_rows)
        row_heights = [0.60] + [sub_h] * len(sub_rows)

    fig = make_subplots(
        rows=total_rows, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights,
    )

    # === 主圖 ===
    fig.add_trace(
        go.Candlestick(
            x=df["date"],
            open=df["open"], high=df["high"],
            low=df["low"], close=df["close"],
            increasing_line_color="#d62728",  # 台股慣例:紅漲綠跌
            decreasing_line_color="#2ca02c",
            name="K 線",
            showlegend=False,
        ),
        row=1, col=1,
    )

    if "ma20" in indicators_set and "ma20" in df.columns and df["ma20"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=df["ma20"], name="MA20",
                line=dict(width=1.4, color="#1f77b4"),
                hovertemplate="MA20 %{y:.2f}<extra></extra>",
            ),
            row=1, col=1,
        )
    if "ma60" in indicators_set and "ma60" in df.columns and df["ma60"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=df["ma60"], name="MA60",
                line=dict(width=1.4, color="#ff7f0e"),
                hovertemplate="MA60 %{y:.2f}<extra></extra>",
            ),
            row=1, col=1,
        )

    want_bb = ("bollinger" in indicators_set) or ("bb" in indicators_set)
    if want_bb and "bb_upper" in df.columns and df["bb_upper"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=df["bb_upper"], name="BB 上",
                line=dict(width=1, dash="dot",
                          color="rgba(120,120,120,0.7)"),
                hovertemplate="BB 上 %{y:.2f}<extra></extra>",
            ),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=df["date"], y=df["bb_lower"], name="BB 下",
                line=dict(width=1, dash="dot",
                          color="rgba(120,120,120,0.7)"),
                fill="tonexty", fillcolor="rgba(120,120,120,0.08)",
                hovertemplate="BB 下 %{y:.2f}<extra></extra>",
            ),
            row=1, col=1,
        )

    # === 副圖 ===
    for r_idx, kind in enumerate(sub_rows, start=2):
        if kind == "volume":
            vol_colors = [
                "#d62728" if c >= o else "#2ca02c"
                for c, o in zip(df["close"], df["open"])
            ]
            fig.add_trace(
                go.Bar(
                    x=df["date"], y=df["volume"],
                    name="量", marker_color=vol_colors, showlegend=False,
                    hovertemplate="量 %{y}<extra></extra>",
                ),
                row=r_idx, col=1,
            )
            fig.update_yaxes(
                title_text="量", tickfont=dict(size=10),
                title_font=dict(size=11), row=r_idx, col=1,
            )
        elif kind == "rsi":
            rsi_series = ind.rsi(df, period=14)
            fig.add_trace(
                go.Scatter(
                    x=df["date"], y=rsi_series, name="RSI(14)",
                    line=dict(width=1.3, color="#9467bd"),
                    hovertemplate="RSI %{y:.1f}<extra></extra>",
                ),
                row=r_idx, col=1,
            )
            fig.add_hline(
                y=70, line_dash="dash",
                line_color="rgba(214,39,40,0.5)", row=r_idx, col=1,
            )
            fig.add_hline(
                y=30, line_dash="dash",
                line_color="rgba(44,160,44,0.5)", row=r_idx, col=1,
            )
            fig.update_yaxes(
                title_text="RSI", range=[0, 100], tickfont=dict(size=10),
                title_font=dict(size=11), row=r_idx, col=1,
            )
        elif kind == "macd":
            macd_df = ind.macd(df)
            fig.add_trace(
                go.Scatter(
                    x=df["date"], y=macd_df["DIF"], name="DIF",
                    line=dict(width=1.2, color="#d62728"),
                    hovertemplate="DIF %{y:.3f}<extra></extra>",
                ),
                row=r_idx, col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=df["date"], y=macd_df["DEA"], name="DEA",
                    line=dict(width=1.2, color="#2ca02c"),
                    hovertemplate="DEA %{y:.3f}<extra></extra>",
                ),
                row=r_idx, col=1,
            )
            hist_colors = [
                "#d62728" if (v is not None and v >= 0) else "#2ca02c"
                for v in macd_df["HIST"]
            ]
            fig.add_trace(
                go.Bar(
                    x=df["date"], y=macd_df["HIST"], name="HIST",
                    marker_color=hist_colors, showlegend=False,
                    hovertemplate="HIST %{y:.3f}<extra></extra>",
                ),
                row=r_idx, col=1,
            )
            fig.update_yaxes(
                title_text="MACD", tickfont=dict(size=10),
                title_font=dict(size=11), row=r_idx, col=1,
            )
        elif kind == "kd":
            kd_df = compute_kd(df, period=9)
            fig.add_trace(
                go.Scatter(
                    x=df["date"], y=kd_df["K"], name="K",
                    line=dict(width=1.2, color="#1f77b4"),
                    hovertemplate="K %{y:.1f}<extra></extra>",
                ),
                row=r_idx, col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=df["date"], y=kd_df["D"], name="D",
                    line=dict(width=1.2, color="#ff7f0e"),
                    hovertemplate="D %{y:.1f}<extra></extra>",
                ),
                row=r_idx, col=1,
            )
            fig.update_yaxes(
                title_text="KD", range=[0, 100], tickfont=dict(size=10),
                title_font=dict(size=11), row=r_idx, col=1,
            )
        elif kind == "stoch":
            st_df = compute_stoch(df, period=14)
            fig.add_trace(
                go.Scatter(
                    x=df["date"], y=st_df["K"], name="%K",
                    line=dict(width=1.2, color="#17becf"),
                    hovertemplate="%K {y:.1f}<extra></extra>",
                ),
                row=r_idx, col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=df["date"], y=st_df["D"], name="%D",
                    line=dict(width=1.2, color="#bcbd22"),
                    hovertemplate="%D {y:.1f}<extra></extra>",
                ),
                row=r_idx, col=1,
            )
            fig.update_yaxes(
                title_text="Stoch", range=[0, 100], tickfont=dict(size=10),
                title_font=dict(size=11), row=r_idx, col=1,
            )

    # 整體 layout — 主圖固定 ~280,每副圖 +130(iPhone 看 OK,桌機也夠)
    main_h = 320
    sub_h_px = 130
    height = main_h + len(sub_rows) * sub_h_px
    fig.update_layout(
        height=height,
        xaxis_rangeslider_visible=False,
        showlegend=True,
        margin=dict(t=20, b=20, l=20, r=20),
        legend=dict(
            orientation="h", y=1.02, x=0, font=dict(size=11),
            yanchor="bottom",
        ),
        font=dict(size=12),
        hovermode="x unified",
        dragmode="pan",
    )
    for r in range(1, total_rows + 1):
        fig.update_xaxes(
            type="category", tickfont=dict(size=9),
            showticklabels=(r == total_rows),  # 只最後一 row 顯日期
            row=r, col=1,
        )
    fig.update_yaxes(
        title_text="股價", tickfont=dict(size=10),
        title_font=dict(size=11), row=1, col=1,
    )
    return fig


# === Markers ==========================================================

def _chart_x_dates(fig: go.Figure) -> list[str]:
    """取主圖 Candlestick trace 的 x(日期 list),用來篩 markers 是否在範圍內。"""
    for tr in fig.data:
        if isinstance(tr, go.Candlestick):
            return list(tr.x) if tr.x is not None else []
    return []


def mark_pick_dates(
    fig: go.Figure,
    sid: str,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
) -> go.Figure:
    """在 K 線上標 ⭐ — 該 sid 過去被 daily_picks 命中過的日期。

    Dedup by date(同日多策略只標一次 ⭐)。
    只標 chart x 範圍內的日期,範圍外的 silently skip。
    """
    chart_dates = _chart_x_dates(fig)
    if not chart_dates:
        return fig
    chart_set = set(chart_dates)

    pick_dates: list[str] = []
    try:
        if conn is None:
            with db.get_conn(db_path) as c:
                rows = c.execute(
                    "SELECT DISTINCT trade_date FROM daily_picks "
                    "WHERE sid=? ORDER BY trade_date DESC LIMIT 500",
                    (sid,),
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM daily_picks "
                "WHERE sid=? ORDER BY trade_date DESC LIMIT 500",
                (sid,),
            ).fetchall()
        for r in rows:
            d = r["trade_date"] if hasattr(r, "keys") else r[0]
            if d in chart_set:
                pick_dates.append(d)
    except sqlite3.OperationalError:
        return fig

    for d in pick_dates:
        fig.add_annotation(
            x=d, y=1.0,
            xref="x", yref="y domain",
            text="⭐", showarrow=False,
            yanchor="bottom",
            font=dict(size=13),
        )
    return fig


def mark_pattern_signals(
    fig: go.Figure,
    sid: str,
    days: int = 120,
    *,
    db_path: str | Path | None = None,
) -> go.Figure:
    """在 K 線上標 K 線形態(三紅兵 / 槌子 / 吞噬 / 旗形 etc)。

    軟相依:`src.candlestick_patterns` 模組還沒 merge 時走 try/except 直接跳過,
    不炸 chart(B task PR 還沒進來 / 老 branch checkout 時最常見)。
    """
    try:
        from src import candlestick_patterns  # type: ignore  # noqa: PLC0415
    except ImportError:
        return fig

    # 嘗試呼叫常見 API 名稱(detect_patterns / detect)
    fn = getattr(candlestick_patterns, "detect_patterns", None) or \
        getattr(candlestick_patterns, "detect", None)
    if fn is None:
        return fig

    try:
        signals = fn(sid, days=days, db_path=db_path)
    except Exception:  # noqa: BLE001  # graceful — 圖不應該因為形態抓不到就崩
        return fig

    if not signals:
        return fig

    chart_dates = set(_chart_x_dates(fig))
    for sig in signals:
        d = sig.get("date") if isinstance(sig, dict) else None
        if not d or d not in chart_dates:
            continue
        label = (sig.get("label") or sig.get("name")
                 or sig.get("pattern") or "🕯️")
        fig.add_annotation(
            x=d, y=0.0,
            xref="x", yref="y domain",
            text=str(label), showarrow=False,
            yanchor="top",
            font=dict(size=10, color="#333"),
            bgcolor="rgba(255,255,200,0.85)",
            bordercolor="#999", borderwidth=1, borderpad=2,
        )
    return fig


def mark_position_levels(
    fig: go.Figure,
    sid: str,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
) -> go.Figure:
    """在主圖標主公該 sid 的 open user_positions 進場價 / 停損 / 停利。

    多筆 open(同 sid 加碼)→ 每筆都標(進場價可能不同)。
    顏色:進場 #2ca02c(綠)/ 停損 #d62728(紅)/ 停利 #1f77b4(藍)。
    annotation_position='right' — 線右端顯數值。
    """
    positions: list[dict] = []
    try:
        if conn is None:
            all_open = db.get_open_positions(db_path=db_path)
        else:
            rows = conn.execute(
                "SELECT * FROM user_positions WHERE is_open=1 AND stock_id=?",
                (str(sid),),
            ).fetchall()
            all_open = [dict(r) for r in rows]
        positions = [p for p in all_open if str(p.get("stock_id")) == str(sid)]
    except sqlite3.OperationalError:
        return fig

    if not positions:
        return fig

    for p in positions:
        entry = p.get("entry_price")
        sl = p.get("stop_loss")
        tp = p.get("take_profit")
        if entry is not None:
            fig.add_hline(
                y=float(entry), line_dash="dot",
                line_color="#2ca02c", line_width=1.5,
                row=1, col=1,
                annotation_text=f"進場 {float(entry):.2f}",
                annotation_position="right",
                annotation_font=dict(size=10, color="#2ca02c"),
            )
        if sl is not None:
            fig.add_hline(
                y=float(sl), line_dash="dash",
                line_color="#d62728", line_width=1.5,
                row=1, col=1,
                annotation_text=f"停損 {float(sl):.2f}",
                annotation_position="right",
                annotation_font=dict(size=10, color="#d62728"),
            )
        if tp is not None:
            fig.add_hline(
                y=float(tp), line_dash="dash",
                line_color="#1f77b4", line_width=1.5,
                row=1, col=1,
                annotation_text=f"停利 {float(tp):.2f}",
                annotation_position="right",
                annotation_font=dict(size=10, color="#1f77b4"),
            )
    return fig


__all__ = [
    "render_candlestick_chart",
    "mark_pick_dates",
    "mark_pattern_signals",
    "mark_position_levels",
    "compute_bollinger",
    "compute_kd",
    "compute_stoch",
]
