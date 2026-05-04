"""src/ui_format.py 漲跌顏色 / 箭頭格式化測試(台股慣例:漲紅跌綠)。"""
from __future__ import annotations

import math

import pytest

from src.ui_format import (
    ARROW_DOWN, ARROW_FLAT, ARROW_UP,
    COLOR_DOWN, COLOR_FLAT, COLOR_UP,
    arrow_for, color_for, format_change, format_pnl,
)


# === color_for ===

def test_color_for_positive_returns_red():
    """正值 → 紅色(台股慣例)。"""
    assert color_for(1.0) == COLOR_UP == "#d62728"
    assert color_for(0.01) == COLOR_UP


def test_color_for_negative_returns_green():
    """負值 → 綠色(台股慣例)。"""
    assert color_for(-1.0) == COLOR_DOWN == "#2ca02c"
    assert color_for(-0.01) == COLOR_DOWN


def test_color_for_zero_returns_gray():
    """0 / None / NaN → 灰色。"""
    assert color_for(0) == COLOR_FLAT == "#888888"
    assert color_for(None) == COLOR_FLAT
    assert color_for(float("nan")) == COLOR_FLAT


# === arrow_for ===

def test_arrow_for_positive_is_up():
    assert arrow_for(1) == ARROW_UP == "↑"


def test_arrow_for_negative_is_down():
    assert arrow_for(-1) == ARROW_DOWN == "↓"


def test_arrow_for_zero_or_none_is_dash():
    assert arrow_for(0) == ARROW_FLAT == "—"
    assert arrow_for(None) == ARROW_FLAT
    assert arrow_for(float("nan")) == ARROW_FLAT


# === format_change ===

def test_format_change_positive_is_red_with_up_arrow():
    """正值 → 紅色 HTML span + ↑ 箭頭 + 絕對值。"""
    out = format_change(2.34)
    assert "#d62728" in out
    assert "↑" in out
    assert "2.34%" in out
    # 不該含負號(用箭頭表示方向,絕對值顯示)
    assert "-" not in out


def test_format_change_negative_is_green_with_down_arrow():
    """負值 → 綠色 + ↓ 箭頭 + 絕對值(沒負號)。"""
    out = format_change(-1.5)
    assert "#2ca02c" in out
    assert "↓" in out
    assert "1.50%" in out
    assert "-" not in out  # 用箭頭表示方向


def test_format_change_zero_is_gray_with_dash():
    """0 → 灰色 + — 箭頭。"""
    out = format_change(0)
    assert "#888888" in out
    assert "—" in out


def test_format_change_none_returns_dash_only():
    """None → 灰色 — (沒數字)。"""
    out = format_change(None)
    assert "#888888" in out
    assert "—" in out
    # 沒數字 / 沒 %
    assert "0.00" not in out


def test_format_change_nan_returns_dash_only():
    """NaN → 同 None,灰色 —。"""
    out = format_change(float("nan"))
    assert "#888888" in out
    assert "—" in out


def test_format_change_no_percent_no_suffix():
    """percent=False → 不加 % 後綴。"""
    out = format_change(2.34, percent=False)
    assert "2.34" in out
    assert "%" not in out


def test_format_change_decimals_param():
    """decimals 參數控制小數位數。"""
    assert "2.3" in format_change(2.34, decimals=1)
    assert "2.340" in format_change(2.34, decimals=3)


def test_format_change_no_arrow():
    """show_arrow=False → 沒箭頭(只有顏色 + 數字)。"""
    out = format_change(2.34, show_arrow=False)
    assert "↑" not in out
    assert "↓" not in out
    assert "—" not in out
    assert "2.34" in out
    assert "#d62728" in out  # 顏色仍在


# === format_pnl ===

def test_format_pnl_positive_with_thousand_separator():
    """正 P&L → 紅色 + ↑ + +50,000(千分位 + 正號)。"""
    out = format_pnl(50000)
    assert "#d62728" in out
    assert "↑" in out
    assert "+50,000" in out


def test_format_pnl_negative():
    """負 P&L → 綠色 + ↓ + -30,000。"""
    out = format_pnl(-30000)
    assert "#2ca02c" in out
    assert "↓" in out
    assert "-30,000" in out


def test_format_pnl_zero():
    out = format_pnl(0)
    assert "#888888" in out
    assert "—" in out


def test_format_pnl_none():
    out = format_pnl(None)
    assert "#888888" in out
    assert "—" in out


# === 整合驗證:跟 plotly candlestick 顏色一致 ===

def test_colors_match_plotly_candlestick_convention():
    """台股慣例:plotly increasing.line.color=#d62728(紅), decreasing=#2ca02c(綠)。
    ui_format 的 COLOR_UP/COLOR_DOWN 必須一致避免混淆。"""
    assert COLOR_UP == "#d62728"
    assert COLOR_DOWN == "#2ca02c"
