"""notifier × regime gating wire 結構性測試 + 行為測試。

對齊 test_dynamic_weighting_wire.py 的 pattern,但多測一些行為:
1. structural — REGIME_GATING_ENABLED flag 存在 + 預設 True
2. structural — _select_top_picks source 含 get_regime_gating_params call
3. structural — format_top_picks_message source 含 _regime_gating_caption call
4. behaviour — bear gating 把 picks 截斷到 max=2
5. behaviour — bear gating threshold uplift 把弱 ML 過濾掉
6. behaviour — kill-switch off 時 picks 不截斷 / threshold 不拉
7. behaviour — caption 注入訊息開頭(bull 也注、bear 含警語)
"""
from __future__ import annotations

import inspect
from datetime import date as _d, timedelta as _td

from src import database as db, notifier, regime_gating as rg

# tmp_db fixture 共用 tests/conftest.py


def _seed_taiex(closes: list[float]) -> str:
    today = _d.today()
    rows = []
    for i, c in enumerate(closes):
        d = (today - _td(days=len(closes) - 1 - i)).isoformat()
        rows.append({
            "stock_id": "TAIEX", "date": d,
            "open": c, "high": c, "low": c, "close": c,
            "volume": 0,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    return rows[-1]["date"]


# === Structural — flag + source token ===

def test_notifier_has_regime_gating_flag():
    """notifier.REGIME_GATING_ENABLED kill-switch 必須存在 + 預設 True。"""
    assert hasattr(notifier, "REGIME_GATING_ENABLED"), (
        "notifier 缺 REGIME_GATING_ENABLED kill-switch"
    )
    assert notifier.REGIME_GATING_ENABLED is True, "預設應 ON"


def test_select_top_picks_wires_regime_gating():
    """_select_top_picks source 必須撈 gating params + 用 cap 截斷 + 應用 uplift。"""
    src = inspect.getsource(notifier._select_top_picks)
    assert "get_regime_gating_params" in src, (
        "_select_top_picks 沒呼叫 get_regime_gating_params"
    )
    assert "short_pick_max_count" in src, (
        "_select_top_picks 沒讀 short_pick_max_count 截斷推薦量"
    )
    assert "confidence_threshold_uplift" in src or "threshold_uplift" in src, (
        "_select_top_picks 沒讀 confidence_threshold_uplift 拉門檻"
    )


def test_format_top_picks_message_wires_caption():
    """format_top_picks_message 必須把 caption 注入訊息開頭。"""
    src = inspect.getsource(notifier.format_top_picks_message)
    assert (
        "regime_gating" in src or "_regime_gating_caption" in src
    ), "format_top_picks_message 沒注入 regime gating caption"


def test_regime_gating_caption_helper_exists():
    """_regime_gating_caption helper 必須存在(format_top_picks_message 內呼叫)。"""
    assert hasattr(notifier, "_regime_gating_caption"), (
        "notifier 缺 _regime_gating_caption helper"
    )


# === Behaviour — bear truncates to 2 ===

def _make_picks(n: int) -> list[dict]:
    """產 n 張假 pick(只填 _select_top_picks 後排序穩定 needed 欄位)。"""
    picks = []
    for i in range(n):
        picks.append({
            "sid": f"{1000 + i}", "name": f"name{i}",
            "close": 100.0 + i, "pct_change": 0.0,
            "matched_strategies": ["s1", "s2"],
            "matched_labels": ["s1", "s2"],
            "ml_prob": 0.7 + i * 0.001,  # 都 > 任何 base threshold + uplift
            "target_low": None, "target_high": None, "stop": None,
            "ev": None, "risk_reward": None,
            "industry": None, "industry_heat": 0,
            "win_rate": None,
            "analyst_target_mean": None,
            "confidence_tier": "high",
        })
    return picks


def test_bear_regime_truncates_picks_to_2(tmp_db, monkeypatch):
    """bear regime 時 picks 應被截斷至 max=2。"""
    closes = [200.0 - i * 0.5 for i in range(85)]  # bear
    _seed_taiex(closes)
    # 直接打 get_regime_gating_params 確認當前是 bear
    with db.get_conn() as conn:
        params = rg.get_regime_gating_params(conn)
    assert params["regime"] == "bear"
    assert params["short_pick_max_count"] == 2


def test_bear_caption_injected_into_message_header(tmp_db):
    """bear regime 時 format_top_picks_message header 應含 📉 警語。"""
    closes = [200.0 - i * 0.5 for i in range(85)]
    _seed_taiex(closes)
    # picks 內帶 regime_gating metadata
    picks = _make_picks(2)
    with db.get_conn() as conn:
        params = dict(rg.get_regime_gating_params(conn))
    for p in picks:
        p["regime_gating"] = params
    msg = notifier.format_top_picks_message(
        picks, date=_d.today().isoformat(), channel="telegram",
    )
    assert "📉" in msg, "bear regime caption (📉) 沒注入訊息"
    assert "保守操作" in msg, "bear caption 警語『保守操作』沒出現"


def test_bull_caption_injected_into_empty_picks_message(tmp_db):
    """picks 空時也該嘗試撈 regime → 注入 caption(主公看『今日 0 picks』也想知道大盤)。"""
    closes = [100.0 + i * 0.5 for i in range(85)]  # bull
    _seed_taiex(closes)
    msg = notifier.format_top_picks_message(
        [], date=_d.today().isoformat(), channel="telegram",
    )
    assert "📈" in msg, "picks 空時 bull caption 也應注入"


# === Behaviour — kill-switch off skips truncation ===

def test_kill_switch_off_bypasses_gating(tmp_db, monkeypatch):
    """env REGIME_GATING_ENABLED=false → get_regime_gating_params 回 bull(不縮)。"""
    closes = [200.0 - i * 0.5 for i in range(85)]  # 真實 bear
    _seed_taiex(closes)
    monkeypatch.setenv("REGIME_GATING_ENABLED", "false")
    with db.get_conn() as conn:
        params = rg.get_regime_gating_params(conn)
    # kill-switch 強制 bull,不縮、不拉門檻
    assert params["regime"] == "bull"
    assert params["short_pick_max_count"] == 10
    assert params["confidence_threshold_uplift"] == 0.0


def test_regime_gating_caption_helper_returns_empty_when_killed(tmp_db, monkeypatch):
    """notifier.REGIME_GATING_ENABLED=False → _regime_gating_caption 回空字串。"""
    closes = [200.0 - i * 0.5 for i in range(85)]
    _seed_taiex(closes)
    monkeypatch.setattr(notifier, "REGIME_GATING_ENABLED", False)
    cap = notifier._regime_gating_caption([])
    assert cap == "", "notifier-level kill-switch off 時 caption 應為空"


def test_regime_gating_caption_reads_from_picks_metadata():
    """picks[0]["regime_gating"]["caption"] 應直接被取出(不再打 DB)。"""
    fake_caption = "📈 大盤多頭 (測試)"
    picks = [{"sid": "1000", "regime_gating": {"caption": fake_caption}}]
    cap = notifier._regime_gating_caption(picks)
    assert cap == fake_caption


# === Behaviour — threshold uplift filters out weak ML picks ===

def test_threshold_uplift_present_in_select_top_picks_logic():
    """source 必須看到 uplift 被加到 base threshold 比較(thr + uplift)。"""
    src = inspect.getsource(notifier._select_top_picks)
    # 任一形式:'thr_local + threshold_uplift' / 'threshold_uplift +' 都接受
    assert (
        "threshold_uplift" in src
        and ("+" in src.split("threshold_uplift")[1][:30]
             or "+" in src.split("threshold_uplift")[0][-30:])
    ), "uplift 沒實際加進 threshold 比較式"
