"""src/regime_gating.py 單元測試。

測試 _classify_regime 純邏輯 + get_regime_gating_params 從 SQLite TAIEX 算
gating params + env kill-switch + edge cases(資料不足、TAIEX 缺值)。

不打網路 — 用 tmp_db 灌假 TAIEX daily_prices。
"""
from __future__ import annotations

from datetime import date as _d, timedelta as _td

import pytest

from src import database as db, regime_gating as rg

# tmp_db fixture 共用 tests/conftest.py


def _seed_taiex(closes: list[float]) -> str:
    """灌 TAIEX daily_prices。closes 順序 = 由舊到新。回最新一天 ISO date。"""
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


# === _classify_regime 純邏輯 ===

def test_classify_bull():
    """5MA > 20MA > 60MA + 60MA 斜率向上 → bull。"""
    # 60MA 從 100 → 105:變化率 +5% > +0.5% threshold
    assert rg._classify_regime(ma5=115, ma20=110, ma60=105, ma60_prev=100) == "bull"


def test_classify_bear():
    """5MA < 20MA < 60MA + 60MA 斜率向下 → bear。"""
    # 60MA 從 100 → 95:變化率 -5% < -0.5% threshold
    assert rg._classify_regime(ma5=85, ma20=90, ma60=95, ma60_prev=100) == "bear"


def test_classify_range_when_ma_crossed():
    """MA 不嚴格單調 → range(即使斜率向上 / 向下)。"""
    # 5MA < 20MA 但 20MA < 60MA 不滿足(20MA > 60MA) → 不算 bull
    assert rg._classify_regime(ma5=98, ma20=110, ma60=105, ma60_prev=100) == "range"


def test_classify_range_when_slope_flat():
    """MA 單調但 60MA 斜率平 → range(correction / 整理階段)。"""
    # 5MA > 20MA > 60MA 但 60MA 沒明顯向上(變化率 0.1% < 0.5%)
    assert rg._classify_regime(
        ma5=115, ma20=110, ma60=105, ma60_prev=104.9
    ) == "range"


def test_classify_correction_bull_pullback_to_range():
    """多頭中短期跌破 5MA/20MA 但 60MA 仍向上 → 暫歸 range(spec 要求)。"""
    # 5MA < 20MA(短期跌破), 60MA 仍向上 → range
    assert rg._classify_regime(
        ma5=102, ma20=108, ma60=100, ma60_prev=95
    ) == "range"


# === get_regime_gating_params 從 SQLite ===

def test_gating_bull_params(tmp_db):
    """線性上漲 80+ 天 → bull regime + 對應 params。"""
    closes = [100.0 + i * 0.5 for i in range(85)]  # 100..142
    last_d = _seed_taiex(closes)
    with db.get_conn() as conn:
        params = rg.get_regime_gating_params(conn, as_of=last_d)
    assert params["regime"] == "bull"
    assert params["short_pick_max_count"] == 10
    assert params["long_pick_max_count"] == 10
    assert params["confidence_threshold_uplift"] == 0.0
    assert "📈" in params["caption"]


def test_gating_bear_params_with_warning(tmp_db):
    """線性下跌 → bear regime + 警語 caption。"""
    closes = [200.0 - i * 0.5 for i in range(85)]  # 200..158
    last_d = _seed_taiex(closes)
    with db.get_conn() as conn:
        params = rg.get_regime_gating_params(conn, as_of=last_d)
    assert params["regime"] == "bear"
    assert params["short_pick_max_count"] == 2
    assert params["long_pick_max_count"] == 5
    assert params["confidence_threshold_uplift"] == pytest.approx(0.15)
    assert "📉" in params["caption"]
    assert "保守操作" in params["caption"], (
        "bear caption 應含警語『保守操作』"
    )


def test_gating_range_params(tmp_db):
    """橫盤(close 在 100 附近震盪)→ range regime + 中性 params。"""
    closes = [100.0 + (1 if i % 2 == 0 else -1) for i in range(85)]
    last_d = _seed_taiex(closes)
    with db.get_conn() as conn:
        params = rg.get_regime_gating_params(conn, as_of=last_d)
    assert params["regime"] == "range"
    assert params["short_pick_max_count"] == 5
    assert params["long_pick_max_count"] == 7
    assert params["confidence_threshold_uplift"] == pytest.approx(0.05)
    assert "📊" in params["caption"]


# === Edge cases ===

def test_gating_insufficient_data_falls_back_to_range(tmp_db):
    """TAIEX 不足 80 天 → fallback range。"""
    closes = [100.0] * 30  # 只 30 天
    last_d = _seed_taiex(closes)
    with db.get_conn() as conn:
        params = rg.get_regime_gating_params(conn, as_of=last_d)
    assert params["regime"] == "range", (
        "資料不足應 fallback range,不該因 DB 缺值就盲目壓 bear 或放 bull"
    )


def test_gating_no_taiex_at_all_falls_back_to_range(tmp_db):
    """TAIEX 完全沒資料 → fallback range。"""
    # 沒灌任何 TAIEX
    with db.get_conn() as conn:
        params = rg.get_regime_gating_params(conn)
    assert params["regime"] == "range"
    assert params["short_pick_max_count"] == 5


def test_gating_as_of_filters_to_earlier_date(tmp_db):
    """as_of 指定到歷史某一天 → 只看那天之前的資料判 regime。"""
    # 前 80 天線性上漲(bull),後 5 天暴跌
    up = [100.0 + i * 0.5 for i in range(80)]
    down = [140.0 - i * 5 for i in range(5)]
    last_d = _seed_taiex(up + down)
    # as_of = 暴跌前最後一天 → 應該還是 bull
    target_date = (_d.fromisoformat(last_d) - _td(days=5)).isoformat()
    with db.get_conn() as conn:
        params = rg.get_regime_gating_params(conn, as_of=target_date)
    assert params["regime"] == "bull"


# === Kill-switch (env REGIME_GATING_ENABLED) ===

def test_kill_switch_off_returns_bull_params(tmp_db, monkeypatch):
    """env REGIME_GATING_ENABLED=false → 永遠回 bull params(等同關 gating)。"""
    # 灌一份線性下跌 = 真實 bear data
    closes = [200.0 - i * 0.5 for i in range(85)]
    last_d = _seed_taiex(closes)
    monkeypatch.setenv("REGIME_GATING_ENABLED", "false")
    with db.get_conn() as conn:
        params = rg.get_regime_gating_params(conn, as_of=last_d)
    # 即使真實 bear,kill-switch 強制 bull params
    assert params["regime"] == "bull"
    assert params["short_pick_max_count"] == 10
    assert params["confidence_threshold_uplift"] == 0.0


def test_kill_switch_default_on(monkeypatch):
    """未設 env → is_enabled 預設 True。"""
    monkeypatch.delenv("REGIME_GATING_ENABLED", raising=False)
    assert rg.is_enabled() is True


def test_kill_switch_explicit_true(monkeypatch):
    """REGIME_GATING_ENABLED=true → is_enabled True。"""
    monkeypatch.setenv("REGIME_GATING_ENABLED", "true")
    assert rg.is_enabled() is True


@pytest.mark.parametrize("v", ["false", "0", "no", "off", "FALSE", "False"])
def test_kill_switch_off_strings(monkeypatch, v):
    """REGIME_GATING_ENABLED=false/0/no/off(大小寫無所謂)→ is_enabled False。"""
    monkeypatch.setenv("REGIME_GATING_ENABLED", v)
    assert rg.is_enabled() is False


# === Params 合理性檢查 ===

def test_params_max_counts_monotonic():
    """short_pick_max_count: bull 10 > range 5 > bear 2。長線同方向但縮幅較小。"""
    short = {
        k: v["short_pick_max_count"] for k, v in rg._REGIME_PARAMS.items()
    }
    long = {
        k: v["long_pick_max_count"] for k, v in rg._REGIME_PARAMS.items()
    }
    assert short["bull"] > short["range"] > short["bear"]
    assert long["bull"] >= long["range"] > long["bear"]


def test_params_threshold_uplift_monotonic():
    """confidence_threshold_uplift: bull 0 < range +0.05 < bear +0.15。"""
    uplifts = {
        k: v["confidence_threshold_uplift"]
        for k, v in rg._REGIME_PARAMS.items()
    }
    assert uplifts["bull"] < uplifts["range"] < uplifts["bear"]
    assert uplifts["bear"] >= 0.10, "bear uplift 太小擋不住爛 picks"
