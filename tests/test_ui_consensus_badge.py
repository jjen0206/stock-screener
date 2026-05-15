"""UI consensus badge 渲染結構測試。

純單元 — 直接呼叫 _build_consensus_badge_html / _build_card_html,不啟動
streamlit,不開 browser。

守住:
1. _build_consensus_badge_html 對各 tier 產出正確的 badge 文字 + 顏色
2. None / 單策略 → 回空字串(不噴 HTML)
3. title attr 含具體策略 label(mobile 長按可看)
4. _build_card_html 接 consensus kwarg
5. render_pick_card 傳 row['consensus'] 進 helper
6. 強共識用紅色、共識用橙色、同類用灰色
7. kill switch off → badge 一律空
"""
from __future__ import annotations

import inspect

from src import ui_cards


# === _build_consensus_badge_html ===

def test_badge_html_none_returns_empty():
    assert ui_cards._build_consensus_badge_html(None) == ""
    assert ui_cards._build_consensus_badge_html({}) == ""


def test_badge_html_single_strategy_empty():
    """單策略 → 不渲 badge。"""
    meta = {
        "strategy_count": 1,
        "category_count": 1,
        "strategies": ["macd_golden"],
        "categories": ["趨勢"],
    }
    assert ui_cards._build_consensus_badge_html(meta) == ""


def test_badge_html_cross_2_renders_orange():
    """跨類 2 票 → ⭐⭐ 共識,橙色。"""
    meta = {
        "strategy_count": 2,
        "category_count": 2,
        "strategies": ["macd_golden", "inst_consensus"],
        "categories": ["趨勢", "籌碼"],
    }
    out = ui_cards._build_consensus_badge_html(meta)
    assert "⭐⭐" in out
    assert "共識" in out
    assert "#ff7f0e" in out  # 橙色
    # tooltip 應該含中文策略 label(不是 raw key)
    assert "MACD" in out or "macd" in out or "趨勢" in out


def test_badge_html_cross_3_renders_red_strong():
    """跨類 3+ 票 → ⭐⭐⭐ 強共識,紅色 + 粗體 600。"""
    meta = {
        "strategy_count": 3,
        "category_count": 3,
        "strategies": ["macd_golden", "inst_consensus", "volume_breakout"],
        "categories": ["趨勢", "籌碼", "動能"],
    }
    out = ui_cards._build_consensus_badge_html(meta)
    assert "⭐⭐⭐" in out
    assert "強共識" in out
    assert "#d62728" in out  # 紅色
    assert "font-weight:600" in out


def test_badge_html_same_category_renders_gray_star():
    """同類 2 票 → ⭐,灰色,不加「共識」字。"""
    meta = {
        "strategy_count": 2,
        "category_count": 1,
        "strategies": ["macd_golden", "ma_alignment"],
        "categories": ["趨勢"],
    }
    out = ui_cards._build_consensus_badge_html(meta)
    assert "⭐" in out
    assert "⭐⭐" not in out  # 同類別只給單顆星
    assert "#888" in out  # 灰色
    # 同類 2 票 badge 不加「共識」字眼 — 視覺上不該跟跨類同等
    assert "共識" not in out


def test_badge_html_title_attr_escaped():
    """tooltip 字串(title attr)要 HTML-escape 避免 < / > / " 切斷。"""
    meta = {
        "strategy_count": 2,
        "category_count": 2,
        "strategies": ['<script>', 'inst_consensus'],
        "categories": ["趨勢", "籌碼"],
    }
    out = ui_cards._build_consensus_badge_html(meta)
    # < > 必須被 escape
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_badge_html_kill_switch(monkeypatch):
    """STRATEGY_CONSENSUS_ENABLED=false → badge 一律空。"""
    monkeypatch.setenv("STRATEGY_CONSENSUS_ENABLED", "false")
    meta = {
        "strategy_count": 3,
        "category_count": 3,
        "strategies": ["macd_golden", "inst_consensus", "volume_breakout"],
        "categories": ["趨勢", "籌碼", "動能"],
    }
    assert ui_cards._build_consensus_badge_html(meta) == ""


# === _build_card_html 接 consensus ===

def test_build_card_html_accepts_consensus():
    """_build_card_html 必須接 consensus kwarg。"""
    sig = inspect.signature(ui_cards._build_card_html)
    assert "consensus" in sig.parameters, (
        f"_build_card_html 缺 consensus 參數: {list(sig.parameters)}"
    )


def test_build_card_html_renders_badge_in_header():
    """卡片整體 HTML 應該含 ⭐⭐ badge(跨類 picks)。"""
    meta = {
        "strategy_count": 2,
        "category_count": 2,
        "strategies": ["macd_golden", "inst_consensus"],
        "categories": ["趨勢", "籌碼"],
    }
    out = ui_cards._build_card_html(
        sid="2330",
        name="台積電",
        close=779.0,
        change_pct=1.5,
        signals_label="MACD + 法人",
        target_low=800,
        target_high=850,
        stop_loss=750,
        win_rate=0.6,
        risk_reward=2.0,
        n_signals=2,
        consensus=meta,
    )
    assert "⭐⭐" in out
    assert "共識" in out


def test_build_card_html_no_badge_when_consensus_none():
    """consensus=None / 單策略 → 卡片 HTML 不該含任何 ⭐。"""
    out = ui_cards._build_card_html(
        sid="2330", name="台積電", close=779.0, change_pct=None,
        signals_label="MACD", target_low=None, target_high=None,
        stop_loss=None, win_rate=None, risk_reward=None,
        n_signals=1, consensus=None,
    )
    assert "⭐" not in out


# === render_pick_card 傳 row['consensus'] ===

def test_render_pick_card_reads_consensus_from_row():
    """render_pick_card source 必須讀 row.get('consensus') 並傳進 _build_card_html。"""
    src = inspect.getsource(ui_cards.render_pick_card)
    assert "consensus" in src, (
        "render_pick_card 沒從 row dict 拿 consensus 欄"
    )
    assert "consensus_meta" in src or "consensus=" in src, (
        "render_pick_card 沒傳 consensus 進 _build_card_html"
    )
