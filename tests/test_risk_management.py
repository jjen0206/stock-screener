"""src/risk_management.py 單元測試:ATR stop / take profit / drawdown / S/R / concentration。"""
from __future__ import annotations

import pytest

from src import database as db, risk_management as rm

# tmp_db fixture 共用 tests/conftest.py


def _seed_bars(
    sid: str,
    n: int = 30,
    base: float = 600.0,
    range_pct: float = 0.01,
) -> None:
    """灌 n 筆 daily_prices,簡單線性上升 + 固定 high-low range。"""
    from datetime import date, timedelta
    start = date(2026, 4, 1)
    with db.get_conn() as conn:
        for i in range(n):
            d = (start + timedelta(days=i)).isoformat()
            close = base + i * 0.5
            high = close * (1 + range_pct)
            low = close * (1 - range_pct)
            conn.execute(
                "INSERT OR REPLACE INTO daily_prices "
                "(stock_id, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, d, close, high, low, close, 1000),
            )


# === is_enabled ===

def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("RISK_MGMT_ENABLED", raising=False)
    assert rm.is_enabled() is True


def test_is_enabled_false_via_env(monkeypatch):
    monkeypatch.setenv("RISK_MGMT_ENABLED", "false")
    assert rm.is_enabled() is False


# === compute_atr_stop_loss ===

def test_atr_stop_loss_basic(tmp_db):
    """灌 30 筆 ~1% 區間波動的 bar → ATR > 0,停損 < entry。"""
    _seed_bars("2330", n=50, base=600.0, range_pct=0.01)
    res = rm.compute_atr_stop_loss("2330", entry_price=600.0,
                                   days=14, atr_multiplier=2.0)
    assert res is not None
    assert res["atr"] > 0
    assert res["stop_loss"] < 600.0
    assert res["stop_loss_pct"] < 0
    assert "ATR(14)" in res["rationale"]


def test_atr_stop_loss_insufficient_data(tmp_db):
    """資料不足 → None。"""
    _seed_bars("2330", n=5)
    res = rm.compute_atr_stop_loss("2330", entry_price=600.0, days=14)
    assert res is None


def test_atr_stop_loss_bad_inputs(tmp_db):
    with pytest.raises(ValueError):
        rm.compute_atr_stop_loss("2330", entry_price=0)
    with pytest.raises(ValueError):
        rm.compute_atr_stop_loss("2330", entry_price=100, atr_multiplier=0)


# === compute_atr_take_profit ===

def test_atr_take_profit_basic(tmp_db):
    """停利 > entry,× 4 倍 ATR > 停損 × 2 倍 (預設配置 2:1 R:R)。"""
    _seed_bars("2330", n=50, base=600.0, range_pct=0.01)
    sl = rm.compute_atr_stop_loss("2330", 600.0, atr_multiplier=2.0)
    tp = rm.compute_atr_take_profit("2330", 600.0, atr_multiplier=4.0)
    assert sl is not None and tp is not None
    assert tp["take_profit"] > 600.0
    risk = 600.0 - sl["stop_loss"]
    reward = tp["take_profit"] - 600.0
    # 2:1 R:R(在小數誤差內)
    assert abs(reward / risk - 2.0) < 0.05


# === compute_support_resistance ===

def test_support_resistance_basic(tmp_db):
    _seed_bars("2330", n=20, base=600.0)
    res = rm.compute_support_resistance("2330", lookback=20)
    assert res is not None
    assert res["support"] < res["resistance"]
    assert res["n_bars"] > 0


def test_support_resistance_no_data(tmp_db):
    """無 daily_prices → None。"""
    assert rm.compute_support_resistance("9999", lookback=20) is None


def test_support_resistance_bad_lookback():
    with pytest.raises(ValueError):
        rm.compute_support_resistance("2330", lookback=3)


# === should_take_profit / should_stop_loss ===

def test_should_take_profit_hit():
    assert rm.should_take_profit(600, 632, take_profit=630) is True


def test_should_take_profit_not_yet():
    assert rm.should_take_profit(600, 620, take_profit=630) is False


def test_should_take_profit_none():
    """take_profit=None → False。"""
    assert rm.should_take_profit(600, 1000, take_profit=None) is False


def test_should_stop_loss_hit():
    assert rm.should_stop_loss(600, 585, stop_loss=588) is True


def test_should_stop_loss_not_yet():
    assert rm.should_stop_loss(600, 595, stop_loss=588) is False


# === drawdown_pct ===

def test_drawdown_empty():
    res = rm.drawdown_pct([])
    assert res["total_invested"] == 0
    assert res["drawdown_pct"] == 0
    assert res["severity"] == "ok"


def test_drawdown_ok_when_profit():
    """賺錢 → severity = ok(警報只看虧)。"""
    positions = [
        {"entry_price": 100, "shares": 1000, "current_price": 110,
         "is_open": 1, "side": "long"},
    ]
    res = rm.drawdown_pct(positions)
    assert res["unrealized_pnl"] == 10000
    assert res["drawdown_pct"] > 0  # 正 = 賺
    assert res["severity"] == "ok"


def test_drawdown_warn():
    """虧 -10% 到 -20% → warn。"""
    positions = [
        {"entry_price": 100, "shares": 1000, "current_price": 88,
         "is_open": 1, "side": "long"},
    ]
    res = rm.drawdown_pct(positions)
    # (88-100)*1000 = -12000;dd = -12000/100000 = -12%
    assert res["drawdown_pct"] < -10
    assert res["severity"] == "warn"


def test_drawdown_danger():
    """虧 ≥ 20% → danger。"""
    positions = [
        {"entry_price": 100, "shares": 1000, "current_price": 75,
         "is_open": 1, "side": "long"},
    ]
    res = rm.drawdown_pct(positions)
    assert res["drawdown_pct"] <= -20
    assert res["severity"] == "danger"


def test_drawdown_includes_realized():
    """closed 部位用 exit_price 算 realized。"""
    positions = [
        {"entry_price": 100, "shares": 1000, "exit_price": 90,
         "is_open": 0, "side": "long"},
        {"entry_price": 100, "shares": 1000, "current_price": 100,
         "is_open": 1, "side": "long"},
    ]
    res = rm.drawdown_pct(positions)
    assert res["realized_pnl"] == -10000
    assert res["n_open"] == 1
    assert res["n_closed"] == 1


# === check_single_concentration ===

def test_concentration_no_overconcentration():
    """兩檔各 50k(50/50),max=20% → 兩個都超(50% > 20%)。"""
    positions = [
        {"stock_id": "A", "entry_price": 100, "shares": 500, "is_open": 1},
        {"stock_id": "B", "entry_price": 100, "shares": 500, "is_open": 1},
    ]
    over = rm.check_single_concentration(positions, max_single_pct=0.20)
    assert len(over) == 2


def test_concentration_balanced():
    """10 檔均分,每檔 10% → 都 < 20% → 空。"""
    positions = [
        {"stock_id": f"S{i}", "entry_price": 100, "shares": 100, "is_open": 1}
        for i in range(10)
    ]
    over = rm.check_single_concentration(positions, max_single_pct=0.20)
    assert over == []


def test_concentration_closed_excluded():
    """closed 部位不算進集中度。"""
    positions = [
        {"stock_id": "A", "entry_price": 100, "shares": 1000,
         "is_open": 0, "exit_price": 90},
        {"stock_id": "B", "entry_price": 100, "shares": 500, "is_open": 1},
    ]
    over = rm.check_single_concentration(positions, max_single_pct=0.20)
    # 只剩 B = 100% > 20%
    assert len(over) == 1
    assert over[0]["sid"] == "B"
