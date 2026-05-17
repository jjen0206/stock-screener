"""Guard the notifier → position_sizing / risk_management wire path.

防止有人改 notifier.py 時把部位建議行靜默刪掉。
"""
from __future__ import annotations

import inspect

import src.notifier as notifier_mod
from src import database as db, notifier


def test_notifier_imports_position_sizing_lazy():
    """_enrich_picks_with_position_advice 內部 lazy import position_sizing。"""
    fn_src = inspect.getsource(notifier_mod._enrich_picks_with_position_advice)
    assert "position_sizing" in fn_src
    assert "risk_management" in fn_src


def test_enrich_picks_with_position_advice_exists():
    assert hasattr(notifier_mod, "_enrich_picks_with_position_advice")
    assert callable(notifier_mod._enrich_picks_with_position_advice)


def test_notify_top_picks_wires_position_advice():
    """notify_top_picks 一定要 call _enrich_picks_with_position_advice。"""
    src = inspect.getsource(notifier_mod.notify_top_picks)
    assert "_enrich_picks_with_position_advice(" in src, (
        "notify_top_picks 必須 wire 部位建議 enrich"
    )


def test_format_pick_block_uses_position_advice():
    """format_pick_block 必須讀 pick['position_advice'] 然後渲染。"""
    src = inspect.getsource(notifier_mod.format_pick_block)
    assert 'pick.get("position_advice")' in src or "position_advice" in src
    assert "軍師建議" in src, "format_pick_block 缺中文軍師標籤"


def test_enrich_skip_when_kill_switch_off(tmp_db, monkeypatch):
    """POSITION_SIZING_ENABLED=false → pick["position_advice"] = None。"""
    monkeypatch.setenv("POSITION_SIZING_ENABLED", "false")
    picks = [{"sid": "2330", "ml_prob": 0.7, "close": 600}]
    notifier_mod._enrich_picks_with_position_advice(picks)
    assert picks[0]["position_advice"] is None


def test_enrich_populates_advice_when_enabled(tmp_db, monkeypatch):
    """正常 on → pick["position_advice"] dict 帶 position_pct。"""
    monkeypatch.setenv("POSITION_SIZING_ENABLED", "true")
    monkeypatch.setenv("RISK_MGMT_ENABLED", "true")
    # 灌一些 daily_prices 給 ATR 算
    from datetime import date, timedelta
    start = date(2026, 4, 1)
    with db.get_conn() as conn:
        for i in range(50):
            d = (start + timedelta(days=i)).isoformat()
            close = 600 + i * 0.5
            conn.execute(
                "INSERT OR REPLACE INTO daily_prices "
                "(stock_id, date, open, high, low, close, volume) "
                "VALUES ('2330', ?, ?, ?, ?, ?, 1000)",
                (d, close, close * 1.01, close * 0.99, close),
            )
    picks = [{"sid": "2330", "ml_prob": 0.7, "close": 600.0}]
    notifier_mod._enrich_picks_with_position_advice(picks)
    advice = picks[0]["position_advice"]
    assert advice is not None
    assert advice["position_pct"] > 0
    assert "stop_loss" in advice  # ATR 算得出來
    assert "take_profit" in advice


def test_format_pick_block_renders_advice_line(tmp_db):
    """pick 帶 position_advice → format_pick_block 輸出含「軍師建議」。"""
    pick = {
        "rank": 1,
        "sid": "2330",
        "name": "台積電",
        "close": 600,
        "matched_labels": [],
        "ml_prob": 0.64,
        "position_advice": {
            "position_pct": 0.08,
            "suggested_lots": 50,
            "suggested_shares": 50000,
            "stop_loss": 588.0,
            "take_profit": 632.0,
            "stop_loss_pct": -2.0,
            "take_profit_pct": 5.3,
            "max_single_pct": 0.20,
        },
    }
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "軍師建議" in block
    assert "停損" in block and "停利" in block
    assert "20%" in block  # 單檔上限


def test_format_pick_block_skip_advice_when_zero_pct():
    """position_pct=0 → 不渲染軍師行(劣勢場景軍師沉默)。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 600, "matched_labels": [], "ml_prob": 0.3,
        "position_advice": {"position_pct": 0.0},
    }
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "軍師建議" not in block


def test_format_pick_block_no_advice_field_graceful():
    """pick 沒 position_advice 欄 → 整段 skip,不擋其他行。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 600, "matched_labels": [], "ml_prob": 0.64,
    }
    block = notifier.format_pick_block(pick, channel="telegram")
    # 不該爆;股號仍要顯
    assert "2330" in block
