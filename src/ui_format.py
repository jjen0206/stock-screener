"""漲跌顏色 / 箭頭格式化(台股慣例:漲紅跌綠)。

跟歐美顛倒 — Bloomberg / Yahoo Finance 等系統 positive 是綠色,但台灣 / 亞洲
市場習慣紅色為漲、綠色為跌。本 module 集中所有漲跌相關顯示邏輯,避免散落
各處改不齊。

統一用法:
    >>> from src.ui_format import format_change, color_for
    >>> st.markdown(format_change(2.34), unsafe_allow_html=True)  # 紅色 ↑ 2.34%
    >>> color_for(-1.5)  # '#2ca02c'(綠)

st.metric 的 delta_color 應一律用 "inverse"(streamlit 預設 "normal" 是
positive 綠 / negative 紅,跟台股慣例顛倒)。"normal" 適合非市場場合(例如
「停損」目標 — 數字越負越「好」反向)。
"""
from __future__ import annotations

import math


# 台股慣例顏色(對齊 plotly candlestick increasing/decreasing color)
COLOR_UP = "#d62728"      # 漲 — 紅
COLOR_DOWN = "#2ca02c"    # 跌 — 綠
COLOR_FLAT = "#888888"    # 平盤 / N/A — 灰

ARROW_UP = "↑"
ARROW_DOWN = "↓"
ARROW_FLAT = "—"


def _is_nan(value) -> bool:
    """判斷 value 是否為 NaN(避免 import pandas)。"""
    if value is None:
        return True
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def color_for(value) -> str:
    """回傳漲跌對應的 hex 色:正=紅、負=綠、零/None/NaN=灰。

    給 plotly traces / Styler / 自訂 HTML 用(不含箭頭、不含格式化)。
    """
    if _is_nan(value):
        return COLOR_FLAT
    v = float(value)
    if v > 0:
        return COLOR_UP
    if v < 0:
        return COLOR_DOWN
    return COLOR_FLAT


def arrow_for(value) -> str:
    """回傳漲跌箭頭:正=↑、負=↓、零/None/NaN=—。"""
    if _is_nan(value):
        return ARROW_FLAT
    v = float(value)
    if v > 0:
        return ARROW_UP
    if v < 0:
        return ARROW_DOWN
    return ARROW_FLAT


def format_change(
    value,
    *,
    percent: bool = True,
    decimals: int = 2,
    show_arrow: bool = True,
    show_sign: bool = False,
) -> str:
    """格式化漲跌數字成帶顏色 + 箭頭的 HTML markdown。

    Args:
        value: 漲跌數值(例如 +2.34 或 -1.5;None / NaN 回「—」)
        percent: True 加 % 後綴(default);False 純數字
        decimals: 小數位數
        show_arrow: True 在前面加 ↑/↓/— 箭頭
        show_sign: True 在數字前加 + 號(箭頭已有方向感,通常 False 即可)

    Returns:
        '<span style="color:#d62728">↑ 2.34%</span>' 之類字串。
        必須用 `st.markdown(..., unsafe_allow_html=True)` 渲染。

    None / NaN → '<span style="color:#888888">—</span>'。
    """
    if _is_nan(value):
        return f"<span style='color:{COLOR_FLAT}'>{ARROW_FLAT}</span>"

    v = float(value)
    color = color_for(v)
    arrow = arrow_for(v) if show_arrow else ""

    fmt = f"{{:+.{decimals}f}}" if show_sign else f"{{:.{decimals}f}}"
    body = fmt.format(abs(v) if show_arrow else v)
    suffix = "%" if percent else ""

    if show_arrow:
        return f"<span style='color:{color}'>{arrow} {body}{suffix}</span>"
    return f"<span style='color:{color}'>{body}{suffix}</span>"


def format_pnl(
    value,
    *,
    decimals: int = 0,
) -> str:
    """格式化損益金額(整數或小位數,加 + 號 + 顏色,不加 %)。

    給 P&L 顯示用 — 例如 +50,000 / -30,000。
    """
    if _is_nan(value):
        return f"<span style='color:{COLOR_FLAT}'>{ARROW_FLAT}</span>"
    v = float(value)
    color = color_for(v)
    arrow = arrow_for(v)
    body = f"{abs(v):,.{decimals}f}"
    sign = "+" if v > 0 else ("-" if v < 0 else "")
    return f"<span style='color:{color}'>{arrow} {sign}{body}</span>"


__all__ = [
    "COLOR_UP", "COLOR_DOWN", "COLOR_FLAT",
    "ARROW_UP", "ARROW_DOWN", "ARROW_FLAT",
    "color_for", "arrow_for",
    "format_change", "format_pnl",
]
