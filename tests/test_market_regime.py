"""src/market_regime.py 單元測試。

測試 _classify 純邏輯 + compute_regime 從 SQLite TAIEX 算 regime + filter helper。
不打網路 — 用 tmp_db 灌假 TAIEX daily_prices。
"""
from __future__ import annotations

import pytest

from src import config, database as db, market_regime as mr


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "regime.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


def _seed_taiex(closes: list[float]) -> str:
    """灌 TAIEX daily_prices。closes 順序 = 由舊到新。回最新一天 ISO date。"""
    from datetime import date as _d, timedelta as _td
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


# === _classify 純邏輯 ===

def test_classify_bull():
    """close > MA20 > MA60 → bull。"""
    assert mr._classify(close=110, ma20=105, ma60=100) == "bull"


def test_classify_weak_bull():
    """close > MA20, close < MA60 → weak_bull。"""
    assert mr._classify(close=102, ma20=100, ma60=110) == "weak_bull"


def test_classify_sideways():
    """close < MA20, close > MA60 → sideways(短期下,但長期還在均線上)。"""
    assert mr._classify(close=98, ma20=100, ma60=95) == "sideways"


def test_classify_bear():
    """close < MA20 < MA60 → bear。"""
    assert mr._classify(close=90, ma20=95, ma60=100) == "bear"


# === compute_regime 從 SQLite ===

def test_compute_regime_bull_from_uptrend(tmp_db):
    """線性上漲 60+ 天 → close 必然 > MA20 > MA60 → bull。"""
    closes = [100.0 + i * 0.5 for i in range(70)]  # 100..134.5
    last_d = _seed_taiex(closes)
    info = mr.compute_regime(target_date=last_d)
    assert info["regime"] == "bull"
    assert info["badge_emoji"] == "📈"
    assert info["close"] > info["ma20"] > info["ma60"]


def test_compute_regime_bear_from_downtrend(tmp_db):
    """線性下跌 → close < MA20 < MA60 → bear。"""
    closes = [200.0 - i * 0.5 for i in range(70)]  # 200..165.5
    last_d = _seed_taiex(closes)
    info = mr.compute_regime(target_date=last_d)
    assert info["regime"] == "bear"
    assert info["badge_emoji"] == "🔴"


def test_compute_regime_unknown_when_data_insufficient(tmp_db):
    """< 60 天 TAIEX → unknown。"""
    closes = [100.0] * 30  # 只 30 天
    last_d = _seed_taiex(closes)
    info = mr.compute_regime(target_date=last_d)
    assert info["regime"] == "unknown"
    assert info["close"] is None


# === filter_strategies_by_regime ===

def test_filter_bull_passes_all_strategies():
    """bull regime 的 filter set = None → 全開。"""
    keys = ["a", "b", "c"]
    cat = {"a": "趨勢", "b": "反轉", "c": "籌碼"}
    rfilt = {"bull": None}
    out = mr.filter_strategies_by_regime(keys, "bull", cat, rfilt)
    assert sorted(out) == sorted(keys)


def test_filter_sideways_keeps_only_reversal_and_chip():
    """sideways → 只留反轉/籌碼 categories。"""
    keys = ["mom1", "rev1", "chip1", "trend1"]
    cat = {"mom1": "動能", "rev1": "反轉", "chip1": "籌碼", "trend1": "趨勢"}
    rfilt = {"sideways": {"反轉", "籌碼"}}
    out = mr.filter_strategies_by_regime(keys, "sideways", cat, rfilt)
    assert sorted(out) == ["chip1", "rev1"]


def test_filter_unknown_regime_falls_back_to_all():
    """regime 不在 filter dict 內 → 全開(保守)。"""
    keys = ["a", "b"]
    cat = {"a": "趨勢", "b": "反轉"}
    rfilt = {"bull": None}
    out = mr.filter_strategies_by_regime(keys, "unknown", cat, rfilt)
    assert sorted(out) == sorted(keys)


# === 整合 STRATEGY_CATEGORY + STRATEGY_REGIME_FILTER ===

def test_real_strategy_category_covers_all_strategies():
    """src/strategies.py 的 STRATEGY_CATEGORY 該覆蓋 ALL_STRATEGIES 全部 keys。"""
    from src.strategies import ALL_STRATEGIES, STRATEGY_CATEGORY
    missing = set(ALL_STRATEGIES.keys()) - set(STRATEGY_CATEGORY.keys())
    assert not missing, f"STRATEGY_CATEGORY 漏了:{missing}"


def test_real_strategy_regime_filter_categories_match_strategy_categories():
    """STRATEGY_REGIME_FILTER 用到的 category 字串該全在 STRATEGY_CATEGORY 值裡。"""
    from src.strategies import STRATEGY_CATEGORY, STRATEGY_REGIME_FILTER
    valid_cats = set(STRATEGY_CATEGORY.values())
    for regime, cats in STRATEGY_REGIME_FILTER.items():
        if cats is None:
            continue
        bad = cats - valid_cats
        assert not bad, f"{regime} 含未知 category {bad}(valid={valid_cats})"
