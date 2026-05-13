"""Structural guards for U3 進場價區間建議 wire-up。

純 inspect.getsource + regex — 不 mock streamlit、不跑 strategy 真邏輯,
只守住:
  1. notifier.compute_entry_range callable 存在
  2. _select_top_picks source 含 compute_entry_range 呼叫(top picks enrich)
  3. format_pick_block source 含「💰 進場區間」字串(Telegram 訊息行)
  4. ui_cards.py 含 _render_entry_range_inline 定義
  5. render_pick_card source 含 _render_entry_range_inline 呼叫
  6. app._render_table_with_inline_detail source 含 compute_entry_range 呼叫
     (強者跟蹤 + 大戶入場 + 關注 inline detail 共用)
"""
from __future__ import annotations

import inspect
import re

import app
from src import notifier
from src import ui_cards


# ============================================================================
# 1. helper callable 存在
# ============================================================================

def test_compute_entry_range_callable_exists():
    fn = getattr(notifier, "compute_entry_range", None)
    assert fn is not None, "src.notifier 缺 compute_entry_range"
    assert callable(fn), "compute_entry_range 不是 callable"


# ============================================================================
# 2. _select_top_picks 對 top picks 呼叫 compute_entry_range
# ============================================================================

def test_select_top_picks_calls_compute_entry_range():
    src = inspect.getsource(notifier._select_top_picks)
    assert "compute_entry_range" in src, (
        "_select_top_picks 沒呼叫 compute_entry_range — top picks 不會帶 entry_range"
    )
    # 還要寫入 pick dict 才算正確 wire(避免 import 但沒用)
    assert re.search(r'entry_low.*entry_high|entry_high.*entry_low', src), (
        "_select_top_picks 沒把 entry_low/entry_high 寫進 pick dict"
    )


# ============================================================================
# 3. format_pick_block 顯「💰 進場區間」
# ============================================================================

def test_format_pick_block_shows_entry_range_line():
    src = inspect.getsource(notifier.format_pick_block)
    assert "💰" in src and "進場區間" in src, (
        "format_pick_block source 沒有「💰 進場區間」字串 — Telegram 訊息看不到"
    )
    # 應該檢查兩個欄位都存在才印
    assert "entry_low" in src and "entry_high" in src, (
        "format_pick_block 沒讀 pick['entry_low'/'entry_high']"
    )


# ============================================================================
# 4. ui_cards._render_entry_range_inline 定義
# ============================================================================

def test_render_entry_range_inline_callable_exists():
    fn = getattr(ui_cards, "_render_entry_range_inline", None)
    assert fn is not None, "src.ui_cards 缺 _render_entry_range_inline"
    assert callable(fn), "_render_entry_range_inline 不是 callable"


def test_render_entry_range_inline_source_contains_emoji():
    """source 有 💰 + 進場區間 才算正確 render。"""
    src = inspect.getsource(ui_cards._render_entry_range_inline)
    assert "💰" in src, "_render_entry_range_inline 缺 💰 emoji"
    assert "進場區間" in src, "_render_entry_range_inline 缺『進場區間』文字"


# ============================================================================
# 5. render_pick_card 呼叫 _render_entry_range_inline
# ============================================================================

def test_render_pick_card_calls_entry_range_inline():
    src = inspect.getsource(ui_cards.render_pick_card)
    assert "_render_entry_range_inline" in src, (
        "render_pick_card 沒呼叫 _render_entry_range_inline — 卡片看不到進場區間"
    )


# ============================================================================
# 6. app._render_table_with_inline_detail 呼叫 compute_entry_range
#    (強者跟蹤 + 大戶入場 + 關注 etc. inline detail card 共用)
# ============================================================================

def test_app_inline_detail_calls_compute_entry_range():
    src = inspect.getsource(app._render_table_with_inline_detail)
    assert "compute_entry_range" in src, (
        "_render_table_with_inline_detail 沒 enrich entry_range,"
        "強者跟蹤 inline detail 卡片不會帶進場區間"
    )
    # 注入到 card dict 才算正確
    assert re.search(r'entry_low.*entry_high|entry_high.*entry_low', src), (
        "_render_table_with_inline_detail 沒把 entry_low/entry_high 寫進 card dict"
    )
