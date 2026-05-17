"""Guard notifier → candlestick_patterns / trailing_stop / take_profit_alerts wires."""
from __future__ import annotations

import inspect

import src.notifier as notifier_mod
from src import notifier


def test_enrich_picks_with_patterns_exists():
    assert hasattr(notifier_mod, "_enrich_picks_with_patterns")
    assert callable(notifier_mod._enrich_picks_with_patterns)


def test_notify_top_picks_wires_pattern_enrich():
    """notify_top_picks 必須 call _enrich_picks_with_patterns。"""
    src = inspect.getsource(notifier_mod.notify_top_picks)
    assert "_enrich_picks_with_patterns(" in src, (
        "notify_top_picks 必須 wire pattern enrich"
    )


def test_format_pick_block_renders_pattern_line():
    """pick 帶 patterns(bull bias)→ format_pick_block 輸出含「形態」標籤。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 600, "matched_labels": [], "ml_prob": 0.64,
        "patterns": [
            {
                "name": "three_white_soldiers", "label": "三紅兵",
                "bias": "bull", "confidence": 2,
            },
        ],
    }
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "形態" in block
    assert "三紅兵" in block
    assert "★" in block


def test_format_pick_block_skips_neutral_bias():
    """neutral bias(doji)→ 不顯。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 600, "matched_labels": [], "ml_prob": 0.64,
        "patterns": [
            {"name": "doji", "label": "十字星", "bias": "neutral", "confidence": 2},
        ],
    }
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "形態" not in block  # 只有 neutral 不顯


def test_format_pick_block_no_patterns_field_graceful():
    """pick 沒 patterns 欄 → graceful skip。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 600, "matched_labels": [], "ml_prob": 0.64,
    }
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "2330" in block


def test_format_pick_block_multiple_patterns_top_2():
    """多個形態命中 → 取前 2 強。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 600, "matched_labels": [], "ml_prob": 0.64,
        "patterns": [
            {"name": "p1", "label": "形態1", "bias": "bull", "confidence": 3},
            {"name": "p2", "label": "形態2", "bias": "bull", "confidence": 2},
            {"name": "p3", "label": "形態3", "bias": "bull", "confidence": 1},
        ],
    }
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "形態1" in block
    assert "形態2" in block
    assert "形態3" not in block  # 只取前 2


def test_enrich_patterns_kill_switch(monkeypatch):
    monkeypatch.setenv("PATTERN_DETECTION_ENABLED", "false")
    picks = [{"sid": "2330"}]
    notifier_mod._enrich_picks_with_patterns(picks)
    assert picks[0]["patterns"] == []


def test_format_trailing_stop_update_block_with_data():
    summary = {
        "checked": 2, "updated": 1, "skipped_no_data": 0, "errors": 0,
        "raised_positions": [
            {"position_id": 1, "sid": "2330", "old_stop": 588.0,
             "new_stop": 648.0, "hwm": 660.0, "entry_price": 600.0,
             "current_price": 660.0},
        ],
    }
    block = notifier_mod._format_trailing_stop_update(summary, channel="telegram")
    assert "停損" in block
    assert "2330" in block
    assert "648.00" in block


def test_format_trailing_stop_update_empty_returns_empty():
    summary = {"checked": 0, "updated": 0, "skipped_no_data": 0,
               "errors": 0, "raised_positions": []}
    assert notifier_mod._format_trailing_stop_update(summary, channel="telegram") == ""


def test_format_trailing_stop_kill_switch(monkeypatch):
    monkeypatch.setenv("TRAILING_STOP_ENABLED", "false")
    summary = {
        "raised_positions": [
            {"position_id": 1, "sid": "2330", "old_stop": 588.0,
             "new_stop": 648.0, "hwm": 660.0},
        ],
    }
    assert notifier_mod._format_trailing_stop_update(summary, channel="telegram") == ""


def test_format_take_profit_alerts_with_data():
    alerts = [
        {
            "position_id": 1, "sid": "2330",
            "entry_price": 600.0, "current_price": 550.0,
            "pnl_pct": -8.3, "kind": "stop_loss", "severity": "danger",
            "message": "🚨 2330 達停損(550.00 ≤ 560.00),pnl -8.3%",
            "suggested_action": "全平倉 — 停損紀律",
        },
    ]
    block = notifier_mod._format_take_profit_alerts(alerts, channel="telegram")
    assert "持倉警報" in block
    assert "2330" in block
    assert "停損" in block


def test_format_take_profit_alerts_empty_returns_empty():
    assert notifier_mod._format_take_profit_alerts([], channel="telegram") == ""


def test_format_take_profit_alerts_kill_switch(monkeypatch):
    monkeypatch.setenv("TAKE_PROFIT_ALERT_ENABLED", "false")
    alerts = [{"message": "x", "suggested_action": "x", "severity": "danger"}]
    assert notifier_mod._format_take_profit_alerts(alerts, channel="telegram") == ""


# === Structural: daily_notify 結尾必須含 trailing stop update + tp alerts ===

def test_format_top_picks_message_wires_trailing_stop_section():
    src = inspect.getsource(notifier_mod.format_top_picks_message)
    assert "_format_trailing_stop_update(" in src, (
        "format_top_picks_message 結尾必須 call _format_trailing_stop_update"
    )


def test_format_top_picks_message_wires_take_profit_alerts():
    src = inspect.getsource(notifier_mod.format_top_picks_message)
    assert "_format_take_profit_alerts(" in src, (
        "format_top_picks_message 結尾必須 call _format_take_profit_alerts"
    )


def test_morning_brief_wires_take_profit_alerts():
    """scripts/morning_brief.py 開頭必須 call _build_take_profit_alert_lines。"""
    import scripts.morning_brief as mb_mod
    src = inspect.getsource(mb_mod._format_full_telegram)
    assert "_build_take_profit_alert_lines(" in src
    src_d = inspect.getsource(mb_mod._format_full_discord)
    assert "_build_take_profit_alert_lines(" in src_d
