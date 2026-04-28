"""src/strategies.py 多策略測試。

策略 1 (volume_kd) 已在 test_screener.py 充分覆蓋,這裡聚焦:
- 策略 2 (ma_alignment): 多頭排列邏輯
- 策略 3 (bias_convergence): 乖離率收斂邏輯
- run_all_strategies 聚合邏輯
"""
from __future__ import annotations


import pandas as pd
import pytest

from src import config, database as db
from src import strategies as strat


_DATES = [
    "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08",
    "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12", "2024-01-15",
    "2024-01-16", "2024-01-17", "2024-01-18", "2024-01-19", "2024-01-22",
    "2024-01-23", "2024-01-24", "2024-01-25", "2024-01-26", "2024-01-29",
    "2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02", "2024-02-05",
    "2024-02-06", "2024-02-07", "2024-02-08", "2024-02-09", "2024-02-12",
    "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16", "2024-02-19",
    "2024-02-20", "2024-02-21", "2024-02-22", "2024-02-23", "2024-02-26",
    "2024-02-27", "2024-02-28", "2024-02-29", "2024-03-01", "2024-03-04",
    "2024-03-05", "2024-03-06", "2024-03-07", "2024-03-08", "2024-03-11",
    "2024-03-12", "2024-03-13", "2024-03-14", "2024-03-15", "2024-03-18",
    "2024-03-19", "2024-03-20", "2024-03-21", "2024-03-22", "2024-03-25",
    "2024-03-26", "2024-03-27", "2024-03-28", "2024-03-29", "2024-04-01",
    "2024-04-02", "2024-04-03",  # 共 67 個
]


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "strat.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def _seed_uptrend(stock_id: str, name: str, n: int = 67):
    """構造線性遞增收盤,讓 MA5/10/20/60 全部上揚 + 多頭排列。"""
    db.upsert_stocks([{"stock_id": stock_id, "name": name, "market": "TW"}])
    closes = [100.0 + i * 1.0 for i in range(n)]
    rows = []
    for i in range(n):
        rows.append({
            "stock_id": stock_id, "date": _DATES[i],
            "open": closes[i], "high": closes[i] + 1, "low": closes[i] - 1,
            "close": closes[i], "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    return _DATES[n - 1]


def _seed_flat(stock_id: str, name: str, n: int = 30):
    """構造平盤(MA20 = close,乖離 ~0)+ 量比 1:1。"""
    db.upsert_stocks([{"stock_id": stock_id, "name": name, "market": "TW"}])
    rows = []
    for i in range(n):
        rows.append({
            "stock_id": stock_id, "date": _DATES[i],
            "open": 100.0, "high": 101, "low": 99,
            "close": 100.0, "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    return _DATES[n - 1]


# === 策略 2:ma_alignment ===

def test_ma_alignment_uptrend_passes(tmp_db):
    """線性上漲 → 多頭排列成立。"""
    last = _seed_uptrend("UP", "上升股")
    df = strat.screen_ma_alignment(last, stock_ids=["UP"])
    assert len(df) == 1
    row = df.iloc[0]
    assert row["stock_id"] == "UP"
    # MA5 > MA10 > MA20 > MA60
    assert row["ma5"] > row["ma10"] > row["ma20"] > row["ma60"]


def test_ma_alignment_flat_fails(tmp_db):
    """平盤 → 不滿足上揚條件。"""
    last = _seed_flat("FLAT", "平盤股", n=67)
    df = strat.screen_ma_alignment(last, stock_ids=["FLAT"])
    assert df.empty


def test_ma_alignment_insufficient_data(tmp_db):
    """資料 < 65 日 → 跳過該檔。"""
    last = _seed_uptrend("SHORT", "短期", n=30)
    df = strat.screen_ma_alignment(last, stock_ids=["SHORT"])
    assert df.empty


# === 策略 3:bias_convergence ===

def test_bias_convergence_near_ma20_with_volume(tmp_db):
    """收盤接近 MA20 + 今日量 > 1.2 倍前 5 日均量 → 入選。"""
    db.upsert_stocks([{"stock_id": "BIAS", "name": "乖離股", "market": "TW"}])
    closes = [100.0] * 25
    closes[-1] = 99.5  # 微跌 -0.5%(在 [-5, +1] 區間)
    volumes = [1000] * 25
    volumes[-1] = 2000  # 量比 = 2000 / 1000 = 2.0 > 1.2
    rows = []
    for i in range(25):
        rows.append({
            "stock_id": "BIAS", "date": _DATES[i],
            "open": closes[i], "high": closes[i] + 1, "low": closes[i] - 1,
            "close": closes[i], "volume": volumes[i],
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    df = strat.screen_bias_convergence(_DATES[24], stock_ids=["BIAS"])
    assert len(df) == 1
    row = df.iloc[0]
    assert row["stock_id"] == "BIAS"
    assert -5.0 <= row["bias_pct"] <= 1.0
    assert row["vol_ratio"] > 1.2


def test_bias_convergence_too_far_from_ma_fails(tmp_db):
    """乖離 > +5% → 不入選。"""
    db.upsert_stocks([{"stock_id": "FAR", "name": "乖離大", "market": "TW"}])
    closes = [100.0] * 24 + [120.0]  # 最後一天 +20%
    rows = []
    for i in range(25):
        rows.append({
            "stock_id": "FAR", "date": _DATES[i],
            "open": closes[i], "high": closes[i] + 1, "low": closes[i] - 1,
            "close": closes[i], "volume": 2000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    df = strat.screen_bias_convergence(_DATES[24], stock_ids=["FAR"])
    assert df.empty


def test_bias_convergence_low_volume_fails(tmp_db):
    """乖離 OK 但量比 < 1.2 → 不入選。"""
    db.upsert_stocks([{"stock_id": "DRY", "name": "量縮", "market": "TW"}])
    rows = []
    for i in range(25):
        rows.append({
            "stock_id": "DRY", "date": _DATES[i],
            "open": 100, "high": 101, "low": 99,
            "close": 100, "volume": 1000,  # 量都一樣 1000,量比=1.0
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    df = strat.screen_bias_convergence(_DATES[24], stock_ids=["DRY"])
    assert df.empty


# === 目標價 enrich ===

def test_enrich_adds_target_columns(tmp_db):
    """ATR 算得出來時,該加 5 個欄位。"""
    last = _seed_uptrend("UP", "上升", n=67)
    df = strat.screen_ma_alignment(last, stock_ids=["UP"])
    assert not df.empty
    for col in ["atr14", "target_low", "target_high", "stop_loss", "risk_reward"]:
        assert col in df.columns, f"missing {col}"
    # 線性遞增 close,ATR 該 > 0
    row = df.iloc[0]
    assert row["atr14"] > 0
    # 公式:target_low = close + 1.5·ATR
    expected_low = row["close"] + 1.5 * row["atr14"]
    assert row["target_low"] == pytest.approx(expected_low)
    expected_high = row["close"] + 3.0 * row["atr14"]
    assert row["target_high"] == pytest.approx(expected_high)
    expected_stop = row["close"] - 1.5 * row["atr14"]
    assert row["stop_loss"] == pytest.approx(expected_stop)
    # R:R = 3 / 1.5 = 2.0
    assert row["risk_reward"] == pytest.approx(2.0, abs=0.01)


def test_enrich_empty_df_keeps_schema(tmp_db):
    """空 input → 5 個欄位仍存在(下游不會 KeyError)。"""
    df = strat.screen_ma_alignment("2024-01-01", stock_ids=["NONEXIST"])
    for col in ["atr14", "target_low", "target_high", "stop_loss", "risk_reward"]:
        assert col in df.columns


def test_enrich_insufficient_data_fills_none(tmp_db, monkeypatch):
    """資料不足算 ATR → 5 個欄位是 None。"""
    # 模擬有入選但 daily_prices 不足 15 日
    db.upsert_stocks([{"stock_id": "X", "name": "X", "market": "TW"}])
    db.upsert_daily_prices([{
        "stock_id": "X", "date": "2024-01-01",
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    }])
    fake_picks = pd.DataFrame([{
        "stock_id": "X", "name": "X", "close": 100,
        "ma5": 99, "ma10": 98, "ma20": 95, "ma60": 90,
        "matched_at": "2024-01-01",
    }])
    enriched = strat._enrich_with_targets(fake_picks, "2024-01-01")
    assert enriched.iloc[0]["atr14"] is None
    assert enriched.iloc[0]["target_low"] is None
    assert enriched.iloc[0]["stop_loss"] is None


# === compute_target_prices(對單檔算 ATR + 目標價) ===

def test_compute_target_prices_returns_full_dict(tmp_db):
    """有足夠資料 → 回完整 dict 含 6 個 key。"""
    last = _seed_uptrend("UP", "上升", n=67)
    tp = strat.compute_target_prices("UP", target_date=last)
    assert tp is not None
    for key in ["close", "atr14", "target_low", "target_high",
                "stop_loss", "risk_reward"]:
        assert key in tp
    assert tp["atr14"] > 0
    # 公式驗算
    assert tp["target_low"] == pytest.approx(
        tp["close"] + 1.5 * tp["atr14"]
    )
    assert tp["target_high"] == pytest.approx(
        tp["close"] + 3.0 * tp["atr14"]
    )
    assert tp["stop_loss"] == pytest.approx(
        tp["close"] - 1.5 * tp["atr14"]
    )
    # R:R = 3 / 1.5 = 2.0
    assert tp["risk_reward"] == pytest.approx(2.0, abs=0.01)


def test_compute_target_prices_no_data_returns_none(tmp_db):
    """SQLite 沒該股資料 → None。"""
    tp = strat.compute_target_prices("NONEXIST")
    assert tp is None


def test_compute_target_prices_insufficient_data_returns_none(tmp_db):
    """資料 < 15 日 → ATR 算不出來 → None。"""
    db.upsert_stocks([{"stock_id": "X", "name": "X", "market": "TW"}])
    db.upsert_daily_prices([
        {
            "stock_id": "X", "date": _DATES[i],
            "open": 100, "high": 101, "low": 99, "close": 100,
            "volume": 1000, "trading_money": None,
            "trading_turnover": None, "spread": None,
        }
        for i in range(10)  # 只 10 日,不足 ATR(14) 的 15 筆
    ])
    tp = strat.compute_target_prices("X", target_date=_DATES[9])
    assert tp is None


def test_compute_target_prices_default_date_is_today(tmp_db):
    """target_date=None → 預設用今日(若 SQLite 沒今日資料則回 None)。"""
    # 不灌資料,該回 None(今日 cache 沒這檔)
    tp = strat.compute_target_prices("NEVER_TRADED")
    assert tp is None


# === run_all_strategies 聚合 ===

def test_run_all_strategies_aggregates_signals(tmp_db, monkeypatch):
    """同一檔被兩套策略選中 → signals 該包含兩個。"""
    fake_vol = pd.DataFrame([{
        "stock_id": "A", "name": "A", "close": 100, "volume": 1000,
        "ma_volume_5": 800, "k": 30, "d": 25, "inst_total_3d": 100000,
        "matched_at": "2024-01-29",
    }])
    fake_ma = pd.DataFrame([{
        "stock_id": "A", "name": "A", "close": 100,
        "ma5": 99, "ma10": 98, "ma20": 95, "ma60": 90,
        "matched_at": "2024-01-29",
    }])
    fake_bias = pd.DataFrame()  # 空

    monkeypatch.setattr(strat, "screen_volume_kd", lambda *a, **kw: fake_vol)
    monkeypatch.setattr(strat, "screen_ma_alignment", lambda *a, **kw: fake_ma)
    monkeypatch.setattr(strat, "screen_bias_convergence", lambda *a, **kw: fake_bias)
    monkeypatch.setattr(strat, "ALL_STRATEGIES", {
        "volume_kd": strat.screen_volume_kd,
        "ma_alignment": strat.screen_ma_alignment,
        "bias_convergence": strat.screen_bias_convergence,
    })

    agg = strat.run_all_strategies("2024-01-29")
    assert "A" in agg
    assert len(agg["A"]["signals"]) == 2
    assert "量價KD" in agg["A"]["signals"]
    assert "多頭排列" in agg["A"]["signals"]


def test_aggregated_to_dataframe_sorted_by_signal_count(tmp_db, monkeypatch):
    """有 2 信號的個股排在 1 信號之前。"""
    agg = {
        "A": {"name": "甲", "signals": ["量價KD"], "details": {"volume_kd": {"close": 100}}},
        "B": {"name": "乙", "signals": ["量價KD", "多頭排列"],
              "details": {"volume_kd": {"close": 200}, "ma_alignment": {"close": 200}}},
    }
    df = strat.aggregated_to_dataframe(agg)
    assert df.iloc[0]["stock_id"] == "B"  # 2 信號排前
    assert df.iloc[0]["信號數"] == 2
    assert df.iloc[1]["stock_id"] == "A"


def test_aggregated_to_dataframe_includes_target_columns():
    """DataFrame 要含 target_low / target_high / stop_loss / risk_reward / atr14。"""
    agg = {
        "2880": {
            "name": "華南金", "signals": ["乖離收斂"],
            "details": {
                "bias_convergence": {
                    "close": 33.05,
                    "atr14": 1.0,
                    "target_low": 34.55,
                    "target_high": 36.05,
                    "stop_loss": 31.55,
                    "risk_reward": 2.0,
                },
            },
        },
    }
    df = strat.aggregated_to_dataframe(agg)
    for col in ["target_low", "target_high", "stop_loss", "risk_reward", "atr14"]:
        assert col in df.columns, f"missing {col}"
    row = df.iloc[0]
    assert row["target_low"] == pytest.approx(34.55)
    assert row["target_high"] == pytest.approx(36.05)
    assert row["stop_loss"] == pytest.approx(31.55)
    assert row["risk_reward"] == pytest.approx(2.0)
    assert row["atr14"] == pytest.approx(1.0)


def test_run_all_strategies_respects_enabled_filter(tmp_db, monkeypatch):
    """enabled=['volume_kd'] 該只跑那一套,其他兩套不該被叫。"""
    called = []
    monkeypatch.setattr(
        strat, "screen_volume_kd",
        lambda *a, **kw: (called.append("vol_kd"), pd.DataFrame())[1],
    )
    monkeypatch.setattr(
        strat, "screen_ma_alignment",
        lambda *a, **kw: (called.append("ma"), pd.DataFrame())[1],
    )
    monkeypatch.setattr(
        strat, "screen_bias_convergence",
        lambda *a, **kw: (called.append("bias"), pd.DataFrame())[1],
    )
    monkeypatch.setattr(strat, "ALL_STRATEGIES", {
        "volume_kd": strat.screen_volume_kd,
        "ma_alignment": strat.screen_ma_alignment,
        "bias_convergence": strat.screen_bias_convergence,
    })
    strat.run_all_strategies("2024-01-29", enabled=["volume_kd"])
    assert called == ["vol_kd"]
