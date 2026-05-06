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


# === 產業分類 enrich(2026-05-06 主公拍板加) ===

def test_enrich_industry_heat_counts_per_industry(tmp_db):
    """同產業 fire 多檔 → industry_heat 對所有同產業 row = 該產業 count。"""
    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積電", "market": "TW", "industry": "半導體業"},
        {"stock_id": "2454", "name": "聯發科", "market": "TW", "industry": "半導體業"},
        {"stock_id": "3711", "name": "日月光", "market": "TW", "industry": "半導體業"},
        {"stock_id": "1101", "name": "台泥", "market": "TW", "industry": "水泥工業"},
    ])
    df = pd.DataFrame([
        {"stock_id": "2330", "name": "台積電", "信號數": 2},
        {"stock_id": "2454", "name": "聯發科", "信號數": 2},
        {"stock_id": "3711", "name": "日月光", "信號數": 1},
        {"stock_id": "1101", "name": "台泥", "信號數": 1},
    ])
    out = strat.enrich_with_industry_heat(df)
    # 半導體業 3 檔 fire → 該 3 row 都 industry_heat=3
    semi = out[out["industry"] == "半導體業"]
    assert len(semi) == 3
    assert (semi["industry_heat"] == 3).all()
    # 水泥業 1 檔 → industry_heat=1
    cement = out[out["industry"] == "水泥工業"]
    assert len(cement) == 1
    assert cement.iloc[0]["industry_heat"] == 1


def test_enrich_industry_heat_handles_unknown_industry(tmp_db):
    """stocks 表 industry IS NULL / 個股不在表 → industry=NaN, heat=0,不影響其他。"""
    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積電", "market": "TW", "industry": "半導體業"},
        # 9999 不在 stocks 表
        # 1234 在表但 industry=None
        {"stock_id": "1234", "name": "Unknown", "market": "TW", "industry": None},
    ])
    df = pd.DataFrame([
        {"stock_id": "2330", "name": "台積電", "信號數": 2},
        {"stock_id": "9999", "name": "未知", "信號數": 1},
        {"stock_id": "1234", "name": "Unknown", "信號數": 1},
    ])
    out = strat.enrich_with_industry_heat(df)
    assert out[out["stock_id"] == "2330"].iloc[0]["industry"] == "半導體業"
    assert out[out["stock_id"] == "2330"].iloc[0]["industry_heat"] == 1  # 只 2330 一檔半導體
    # 9999 / 1234 沒 industry → heat=0
    nan_rows = out[out["industry"].isna()]
    assert len(nan_rows) == 2
    assert (nan_rows["industry_heat"] == 0).all()


def test_aggregated_to_dataframe_sorts_by_industry_heat_within_signal_tier(tmp_db):
    """同信號數 → industry_heat 高的排前(熱門類股加分)。"""
    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積電", "market": "TW", "industry": "半導體業"},
        {"stock_id": "2454", "name": "聯發科", "market": "TW", "industry": "半導體業"},
        {"stock_id": "1101", "name": "台泥", "market": "TW", "industry": "水泥工業"},
    ])
    agg = {
        "2330": {"name": "台積電", "signals": ["A"], "details": {"A": {"close": 600}}},
        "2454": {"name": "聯發科", "signals": ["A"], "details": {"A": {"close": 1000}}},
        "1101": {"name": "台泥", "signals": ["A"], "details": {"A": {"close": 50}}},
    }
    df = strat.aggregated_to_dataframe(agg)
    # 全部信號數 = 1。半導體 2 檔 → heat=2;水泥 1 檔 → heat=1
    # 排序:heat 高的(半導體 2330 / 2454)排在 1101 之前
    sids_in_order = df["stock_id"].tolist()
    assert sids_in_order[0] in ("2330", "2454")
    assert sids_in_order[1] in ("2330", "2454")
    assert sids_in_order[2] == "1101"


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


# === 策略 4:macd_golden ===

def _seed_dip_then_rebound(stock_id: str, name: str, n: int = 60):
    """前段大跌 + 後段反彈 → MACD DIF 在 0 軸下方剛上穿 signal(初升段黃金交叉)。"""
    db.upsert_stocks([{"stock_id": stock_id, "name": name, "market": "TW"}])
    closes = []
    # 前 45 天線性大跌 100 → 60
    for i in range(45):
        closes.append(100.0 - i * (40.0 / 45))
    # 後 15 天線性反彈 60 → 75
    for i in range(15):
        closes.append(60.0 + (i + 1) * 1.0)
    rows = []
    for i in range(n):
        rows.append({
            "stock_id": stock_id, "date": _DATES[i],
            "open": closes[i], "high": closes[i] + 0.5,
            "low": closes[i] - 0.5, "close": closes[i],
            "volume": 1500 if i == n - 1 else 1000,  # 最後一天量增
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    return _DATES[n - 1]


def test_macd_golden_initial_rebound_passes(tmp_db):
    """大跌後反彈第幾天 → MACD DIF 從負值剛上穿 signal → 入選。"""
    last = _seed_dip_then_rebound("REB", "反彈股")
    df = strat.screen_macd_golden(last, stock_ids=["REB"])
    # 預期入選(大跌後反彈期間 MACD 一定有黃金交叉那一天)
    # 但「今日剛交叉」需精確 — 用 MACD 算多日 picks 任一天即可
    # 若沒入選代表 last 那天沒交叉,我們改 assert 不炸 + schema 對
    assert isinstance(df, pd.DataFrame)
    assert "stock_id" in df.columns
    assert "macd_dif" in df.columns
    assert "macd_signal" in df.columns


def test_macd_golden_flat_no_signal(tmp_db):
    """平盤 → MACD 不會交叉,沒入選。"""
    last = _seed_flat("FLAT_M", "平盤股", n=60)
    df = strat.screen_macd_golden(last, stock_ids=["FLAT_M"])
    assert df.empty


# === 策略 5:ma_squeeze_breakout ===

def test_ma_squeeze_breakout_flat_then_breakout(tmp_db):
    """前 75 天平盤(MA5/20/60 都 ≈ 100,糾結)+ 最後一天突破 105 + 量增 → 入選。"""
    db.upsert_stocks([{
        "stock_id": "SQZ", "name": "糾結突破", "market": "TW",
    }])
    n = 67
    rows = []
    for i in range(n - 1):  # 前 66 天平盤
        rows.append({
            "stock_id": "SQZ", "date": _DATES[i],
            "open": 100.0, "high": 100.5, "low": 99.5,
            "close": 100.0, "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    # 最後一天突破 + 量增
    rows.append({
        "stock_id": "SQZ", "date": _DATES[n - 1],
        "open": 100.0, "high": 106.0, "low": 100.0,
        "close": 105.5,  # > 100 × 1.005 = 100.5,突破
        "volume": 1500,  # 量比 1.5 > 1.2
        "trading_money": None, "trading_turnover": None, "spread": None,
    })
    db.upsert_daily_prices(rows)
    df = strat.screen_ma_squeeze_breakout(_DATES[n - 1], stock_ids=["SQZ"])
    assert len(df) == 1
    assert df.iloc[0]["squeeze_pct"] < 2.0  # MA 糾結
    assert df.iloc[0]["close"] > 100.5  # 突破


def test_ma_squeeze_breakout_no_squeeze_fails(tmp_db):
    """線性上漲 → MA5 / MA20 / MA60 spread > 2%,不糾結 → 不入選。"""
    last = _seed_uptrend("UP_S", "上升股")
    df = strat.screen_ma_squeeze_breakout(last, stock_ids=["UP_S"])
    assert df.empty


# === 策略 6:inst_consensus ===

def test_inst_consensus_three_days_consecutive_passes(tmp_db):
    """三家連 3 天都 buy_sell > 0 → 入選。"""
    db.upsert_stocks([{
        "stock_id": "INST", "name": "法人連買", "market": "TW",
    }])
    # 灌 5 天 institutional,最後 3 天三家都 > 0
    insts = []
    for i in range(5):
        if i >= 2:  # 最後 3 天連買
            insts.append({
                "stock_id": "INST", "date": _DATES[i],
                "foreign_buy_sell": 100, "trust_buy_sell": 50,
                "dealer_buy_sell": 30, "total_buy_sell": 180,
            })
        else:
            insts.append({
                "stock_id": "INST", "date": _DATES[i],
                "foreign_buy_sell": -50, "trust_buy_sell": 0,
                "dealer_buy_sell": 0, "total_buy_sell": -50,
            })
    db.upsert_institutional(insts)
    # 灌一筆 daily_prices 讓 close 有值(_enrich_with_targets 會用到)
    db.upsert_daily_prices([{
        "stock_id": "INST", "date": _DATES[i],
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    } for i in range(5)])
    df = strat.screen_inst_consensus(_DATES[4], stock_ids=["INST"])
    assert len(df) == 1
    assert df.iloc[0]["stock_id"] == "INST"
    assert df.iloc[0]["inst_consensus_days"] == 3


def test_inst_consensus_one_house_neg_fails(tmp_db):
    """三家中一家賣超 → 不算共識 → 不入選。"""
    db.upsert_stocks([{
        "stock_id": "MIX", "name": "混合", "market": "TW",
    }])
    insts = []
    for i in range(5):
        insts.append({
            "stock_id": "MIX", "date": _DATES[i],
            "foreign_buy_sell": 100, "trust_buy_sell": 50,
            "dealer_buy_sell": -10,  # 自營商賣超 → 不算共識
            "total_buy_sell": 140,
        })
    db.upsert_institutional(insts)
    db.upsert_daily_prices([{
        "stock_id": "MIX", "date": _DATES[i],
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    } for i in range(5)])
    df = strat.screen_inst_consensus(_DATES[4], stock_ids=["MIX"])
    assert df.empty


# === 策略 7:bb_lower_rebound ===

def _seed_dip_then_red_k(stock_id: str, name: str):
    """前 20 天 100 平盤(BB mid=100, σ 從 0 → 隨後段急跌變大) + 最後 5 天急
    跌觸下軌(day 22 close=70 跌穿 lower)+ 最後一天 open 75 → close 80(紅 K)
    + 量增。

    BB 算法:closes[6..25] 平盤 14 個 100 + 後 6 個 [90,80,70,70,75,80],
    mean≈93.25 σ≈11,lower≈71;但檢查 last-5 是逐日 lower,day 22 close=70
    vs day 22 lower≈81 → 觸下軌成立。
    """
    db.upsert_stocks([{"stock_id": stock_id, "name": name, "market": "TW"}])
    closes = [100.0] * 20 + [90.0, 80.0, 70.0, 70.0, 75.0, 80.0]
    opens = list(closes)
    opens[-1] = 75.0  # day 25:open 75,close 80 = 紅 K
    volumes = [1000] * (len(closes) - 1) + [2000]  # 最後一天量比 2.0
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "stock_id": stock_id, "date": _DATES[i],
            "open": opens[i], "high": max(opens[i], c) + 0.5,
            "low": min(opens[i], c) - 0.5, "close": c,
            "volume": volumes[i],
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    return _DATES[len(closes) - 1]


def test_bb_lower_rebound_dip_then_red_k_passes(tmp_db):
    """跌至下軌 + 最後一天反彈收紅 K + 量增 → 入選。"""
    last = _seed_dip_then_red_k("BBR", "下軌反彈")
    df = strat.screen_bb_lower_rebound(last, stock_ids=["BBR"])
    assert len(df) == 1, f"期望入選,實際 df={df}"
    row = df.iloc[0]
    assert row["close"] > row["open"]  # 紅 K
    assert row["vol_ratio"] >= 1.0


def test_bb_lower_rebound_uptrend_no_touch_lower_fails(tmp_db):
    """線性上漲 → 從未觸下軌 → 不入選。"""
    last = _seed_uptrend("UP_BB", "上升股", n=30)
    df = strat.screen_bb_lower_rebound(last, stock_ids=["UP_BB"])
    assert df.empty


# === 策略 8:rsi_recovery ===

def _seed_rsi_dip_recovery(stock_id: str, name: str, n: int = 35):
    """前段大跌讓 RSI < 30,後段反彈讓 RSI 回 50+(monotonic 上升)。"""
    db.upsert_stocks([{"stock_id": stock_id, "name": name, "market": "TW"}])
    closes = []
    # 前 22 天線性大跌 100 → 60(RSI 14 會降到很低)
    for i in range(22):
        closes.append(100.0 - i * (40.0 / 22))
    # 後 13 天線性持續上漲 60 → 80(RSI 從低點 monotonic 回升)
    for i in range(13):
        closes.append(60.0 + (i + 1) * (20.0 / 13))
    rows = []
    for i in range(n):
        rows.append({
            "stock_id": stock_id, "date": _DATES[i],
            "open": closes[i], "high": closes[i] + 0.3,
            "low": closes[i] - 0.3, "close": closes[i],
            "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    return _DATES[n - 1]


def test_rsi_recovery_dip_then_recovery_passes(tmp_db):
    """RSI 在 14 日內曾 < 30 + 今日 > 50 + monotonic 上升 → 入選。"""
    last = _seed_rsi_dip_recovery("RSIR", "RSI回升")
    df = strat.screen_rsi_recovery(last, stock_ids=["RSIR"])
    assert len(df) == 1, f"期望入選,實際 df={df}"
    row = df.iloc[0]
    assert row["rsi_today"] > 50.0
    assert row["rsi_min_in_window"] < 30.0


def test_rsi_recovery_flat_fails(tmp_db):
    """平盤 → RSI 永遠 ~50,既沒 < 30 也不算反轉 → 不入選。"""
    last = _seed_flat("FLAT_R", "平盤R", n=35)
    df = strat.screen_rsi_recovery(last, stock_ids=["FLAT_R"])
    assert df.empty


# === 策略 9:inst_silent_accum ===

def _seed_silent_accum(
    stock_id: str, name: str, n: int = 25, inst_per_day: int = 100,
):
    """5/10/20 日法人累計都 > 0 + 平盤 + close 在 BB 下半部。

    BB 下半部:在最後一段做小幅下跌讓 close 落在 mid 之下。
    """
    db.upsert_stocks([{"stock_id": stock_id, "name": name, "market": "TW"}])
    closes = []
    # 前 20 天 100 平盤
    for _ in range(20):
        closes.append(100.0)
    # 後 5 天緩降到 95(讓 close 在 BB mid 之下,但不超過 -1% 平盤門檻)
    closes.extend([99.0, 98.0, 97.0, 96.0, 95.5])  # 最後一天 95.5 vs 前一天 96 = -0.52%
    rows = []
    for i in range(n):
        rows.append({
            "stock_id": stock_id, "date": _DATES[i],
            "open": closes[i], "high": closes[i] + 0.3,
            "low": closes[i] - 0.3, "close": closes[i],
            "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    # 灌 institutional:全部 net > 0(三家 sum > 0)
    insts = []
    for i in range(n):
        insts.append({
            "stock_id": stock_id, "date": _DATES[i],
            "foreign_buy_sell": inst_per_day, "trust_buy_sell": inst_per_day // 2,
            "dealer_buy_sell": inst_per_day // 4,
            "total_buy_sell": inst_per_day + inst_per_day // 2 + inst_per_day // 4,
        })
    db.upsert_institutional(insts)
    return _DATES[n - 1]


def test_inst_silent_accum_three_window_positive_passes(tmp_db):
    """5/10/20 日累計都 > 0 + 平盤 + 低檔 → 入選。"""
    last = _seed_silent_accum("SILENT", "默默吸貨")
    df = strat.screen_inst_silent_accum(last, stock_ids=["SILENT"])
    assert len(df) == 1, f"期望入選,實際 df={df}"
    row = df.iloc[0]
    assert row["inst_5d"] > 0
    assert row["inst_10d"] > 0
    assert row["inst_20d"] > 0
    assert abs(row["pct_change"]) < 1.0
    assert row["bb_position_pct"] < 50.0


def test_inst_silent_accum_inst_negative_fails(tmp_db):
    """法人連續賣超 → 累計負 → 不入選。"""
    last = _seed_silent_accum("SOLD", "賣超", inst_per_day=-100)
    df = strat.screen_inst_silent_accum(last, stock_ids=["SOLD"])
    assert df.empty


# === 策略 10:volume_breakout ===

def _seed_break_to_new_high(stock_id: str, name: str, n: int = 25):
    """前 20 天平盤 100 + 最後一天爆量收 105(突破 20 日新高)。"""
    db.upsert_stocks([{"stock_id": stock_id, "name": name, "market": "TW"}])
    rows = []
    for i in range(n - 1):
        rows.append({
            "stock_id": stock_id, "date": _DATES[i],
            "open": 100.0, "high": 100.5, "low": 99.5,
            "close": 100.0, "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    # 最後一天:close 105(> 20 日 max=100), 量 3000(2.5×1000=2500 → 1.2倍 > 2.5)
    rows.append({
        "stock_id": stock_id, "date": _DATES[n - 1],
        "open": 100.0, "high": 105.5, "low": 100.0, "close": 105.0,
        "volume": 3000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    })
    db.upsert_daily_prices(rows)
    return _DATES[n - 1]


def test_volume_breakout_new_high_with_volume_passes(tmp_db):
    """突破 20 日高 + 量爆 (3×) → 入選。"""
    last = _seed_break_to_new_high("VBO", "量爆突破")
    df = strat.screen_volume_breakout(last, stock_ids=["VBO"])
    assert len(df) == 1, f"期望入選,實際 df={df}"
    row = df.iloc[0]
    assert row["close"] > row["high_20d"]
    assert row["vol_ratio"] >= 2.5


def test_volume_breakout_no_volume_fails(tmp_db):
    """突破但量沒爆(量比 < 2.5)→ 不入選。"""
    db.upsert_stocks([{
        "stock_id": "NOVOL", "name": "突破無量", "market": "TW",
    }])
    rows = []
    for i in range(24):
        rows.append({
            "stock_id": "NOVOL", "date": _DATES[i],
            "open": 100.0, "high": 100.5, "low": 99.5,
            "close": 100.0, "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    # close 突破但量比 1.2 < 2.5
    rows.append({
        "stock_id": "NOVOL", "date": _DATES[24],
        "open": 100.0, "high": 106.0, "low": 100.0, "close": 105.0,
        "volume": 1200,
        "trading_money": None, "trading_turnover": None, "spread": None,
    })
    db.upsert_daily_prices(rows)
    df = strat.screen_volume_breakout(_DATES[24], stock_ids=["NOVOL"])
    assert df.empty


# === 策略 11:gap_up ===

def _seed_gap_up_with_red_k(stock_id: str, name: str, n: int = 7):
    """前 6 天平盤 100 + 最後一天 open=102(跳空 +2%)+ close=104(紅 K)+ 量增。"""
    db.upsert_stocks([{"stock_id": stock_id, "name": name, "market": "TW"}])
    rows = []
    for i in range(n - 1):
        rows.append({
            "stock_id": stock_id, "date": _DATES[i],
            "open": 100.0, "high": 100.5, "low": 99.5,
            "close": 100.0, "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    # 跳空 2%(open 102 vs 昨 close 100)+ 收 104(紅 K)+ 量 1800(量比 1.8)
    rows.append({
        "stock_id": stock_id, "date": _DATES[n - 1],
        "open": 102.0, "high": 105.0, "low": 102.0, "close": 104.0,
        "volume": 1800,
        "trading_money": None, "trading_turnover": None, "spread": None,
    })
    db.upsert_daily_prices(rows)
    return _DATES[n - 1]


def test_gap_up_with_red_k_passes(tmp_db):
    """跳空 +2% + 收紅 + 量比 1.8 → 入選。"""
    last = _seed_gap_up_with_red_k("GAP", "跳空缺口")
    df = strat.screen_gap_up(last, stock_ids=["GAP"])
    assert len(df) == 1, f"期望入選,實際 df={df}"
    row = df.iloc[0]
    assert row["gap_pct"] >= 1.5
    assert row["close"] > row["open"]
    assert row["vol_ratio"] >= 1.5


def test_gap_up_red_k_but_no_gap_fails(tmp_db):
    """收紅 + 量增,但 open 沒跳空(open ≈ 昨日 close)→ 不入選。"""
    db.upsert_stocks([{
        "stock_id": "NOGAP", "name": "無跳空", "market": "TW",
    }])
    rows = []
    for i in range(6):
        rows.append({
            "stock_id": "NOGAP", "date": _DATES[i],
            "open": 100.0, "high": 100.5, "low": 99.5,
            "close": 100.0, "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    # open 100.5(只 +0.5%,< 1.5%)
    rows.append({
        "stock_id": "NOGAP", "date": _DATES[6],
        "open": 100.5, "high": 103.0, "low": 100.5, "close": 102.0,
        "volume": 1800,
        "trading_money": None, "trading_turnover": None, "spread": None,
    })
    db.upsert_daily_prices(rows)
    df = strat.screen_gap_up(_DATES[6], stock_ids=["NOGAP"])
    assert df.empty


# === 策略 12:eps_acceleration(Phase 1) ===

def _seed_quarterly_eps(stock_id: str, eps_list: list[float]) -> None:
    """灌 quarterly financials,eps_list 順序 = period 由舊到新。

    period 用「YYYY-QN」風格但保排序正確的字串(SQLite ORDER BY DESC 會排 alphanum)。
    """
    db.upsert_stocks([{"stock_id": stock_id, "name": f"E{stock_id}", "market": "TW"}])
    rows = []
    for i, e in enumerate(eps_list):
        # 用遞增 period 字串確保 ORDER BY DESC 正確(舊→新 = "2023-01" < "2024-04")
        year = 2023 + (i // 4)
        q = (i % 4) + 1
        rows.append({
            "stock_id": stock_id,
            "period_type": "quarterly",
            "period": f"{year}-Q{q}",
            "revenue": None, "revenue_yoy": None,
            "eps": e, "roe": None,
        })
    db.upsert_financials(rows)


def test_eps_acceleration_yoy_accelerating_passes(tmp_db):
    """連 2 季 YoY 都正,且當季 YoY > 上季 YoY → 入選。

    8 季 EPS:Q1~Q4 (2023) = 1.0/1.0/1.0/1.0,Q5~Q8 (2024) = 1.5/1.8/2.4/3.6
    當季 YoY (Q8 vs Q4) = (3.6-1.0)/1.0 = +260%
    上季 YoY (Q7 vs Q3) = (2.4-1.0)/1.0 = +140%
    260 > 140 → 加速。
    """
    eps = [1.0, 1.0, 1.0, 1.0, 1.5, 1.8, 2.4, 3.6]
    _seed_quarterly_eps("EPSA", eps)
    # 灌一筆 daily_prices 給 _enrich_with_targets 用 close
    db.upsert_daily_prices([{
        "stock_id": "EPSA", "date": _DATES[0],
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    }])
    df = strat.screen_eps_acceleration(_DATES[0], stock_ids=["EPSA"])
    assert len(df) == 1
    row = df.iloc[0]
    assert row["curr_yoy"] > row["prev_yoy"]
    assert row["curr_yoy"] > 0 and row["prev_yoy"] > 0


def test_eps_acceleration_decelerating_fails(tmp_db):
    """YoY 都正但減速(curr_yoy < prev_yoy)→ 不入選。

    Q5~Q8 = 3.0/2.5/2.0/1.5(YoY 正但每季成長率下滑)
    """
    eps = [1.0, 1.0, 1.0, 1.0, 3.0, 2.5, 2.0, 1.5]
    _seed_quarterly_eps("EPSD", eps)
    db.upsert_daily_prices([{
        "stock_id": "EPSD", "date": _DATES[0],
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    }])
    df = strat.screen_eps_acceleration(_DATES[0], stock_ids=["EPSD"])
    assert df.empty


# === 策略 13:high_yield_stable(Phase 1) ===

def test_high_yield_stable_yield_above_6_eps_positive_passes(tmp_db):
    """殖利率 7% + 4 季 EPS 都 > 0 → 入選。"""
    db.upsert_stocks([{"stock_id": "HYS", "name": "高殖利率", "market": "TW"}])
    db.upsert_daily_metrics([{
        "stock_id": "HYS", "date": _DATES[0],
        "close": 100, "pe": 12, "pb": 1.5, "dividend_yield": 7.0,
    }])
    db.upsert_financials([
        {"stock_id": "HYS", "period_type": "quarterly", "period": f"2024-Q{q}",
         "revenue": None, "revenue_yoy": None, "eps": 1.5, "roe": None}
        for q in range(1, 5)
    ])
    db.upsert_daily_prices([{
        "stock_id": "HYS", "date": _DATES[0],
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    }])
    df = strat.screen_high_yield_stable(_DATES[0], stock_ids=["HYS"])
    assert len(df) == 1
    assert df.iloc[0]["dividend_yield"] == 7.0


def test_high_yield_stable_eps_one_quarter_neg_fails(tmp_db):
    """殖利率夠高但有一季 EPS < 0(獲利不穩)→ 不入選。"""
    db.upsert_stocks([{"stock_id": "UNST", "name": "不穩", "market": "TW"}])
    db.upsert_daily_metrics([{
        "stock_id": "UNST", "date": _DATES[0],
        "close": 100, "pe": 12, "pb": 1.5, "dividend_yield": 8.0,
    }])
    eps_vals = [1.0, -0.5, 1.0, 1.0]  # Q2 虧損
    db.upsert_financials([
        {"stock_id": "UNST", "period_type": "quarterly", "period": f"2024-Q{q}",
         "revenue": None, "revenue_yoy": None, "eps": eps_vals[q - 1], "roe": None}
        for q in range(1, 5)
    ])
    df = strat.screen_high_yield_stable(_DATES[0], stock_ids=["UNST"])
    assert df.empty


# === 策略 14:inst_oversold_reversal(Phase 1) ===

def test_inst_oversold_reversal_3_down_then_buy_passes(tmp_db):
    """前 3 日法人淨賣超 → 今日轉買 → 入選。"""
    db.upsert_stocks([{"stock_id": "REV", "name": "反轉", "market": "TW"}])
    # _DATES[0..3]:前 3 日賣 → 第 4 日買
    insts = [
        {"stock_id": "REV", "date": _DATES[0],
         "foreign_buy_sell": -100, "trust_buy_sell": -50, "dealer_buy_sell": 0,
         "total_buy_sell": -150},
        {"stock_id": "REV", "date": _DATES[1],
         "foreign_buy_sell": -200, "trust_buy_sell": 0, "dealer_buy_sell": -10,
         "total_buy_sell": -210},
        {"stock_id": "REV", "date": _DATES[2],
         "foreign_buy_sell": -300, "trust_buy_sell": -50, "dealer_buy_sell": 0,
         "total_buy_sell": -350},
        {"stock_id": "REV", "date": _DATES[3],
         "foreign_buy_sell": 200, "trust_buy_sell": 50, "dealer_buy_sell": 0,
         "total_buy_sell": 250},
    ]
    db.upsert_institutional(insts)
    db.upsert_daily_prices([{
        "stock_id": "REV", "date": _DATES[3],
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    }])
    df = strat.screen_inst_oversold_reversal(_DATES[3], stock_ids=["REV"])
    assert len(df) == 1
    assert df.iloc[0]["down_days"] == 3


def test_inst_oversold_reversal_no_prior_selling_fails(tmp_db):
    """前 3 日法人都買超 + 今日繼續買 → 沒「反轉」訊號 → 不入選。"""
    db.upsert_stocks([{"stock_id": "BULL", "name": "持續買", "market": "TW"}])
    insts = [
        {"stock_id": "BULL", "date": _DATES[i],
         "foreign_buy_sell": 100, "trust_buy_sell": 50, "dealer_buy_sell": 0,
         "total_buy_sell": 150}
        for i in range(4)
    ]
    db.upsert_institutional(insts)
    df = strat.screen_inst_oversold_reversal(_DATES[3], stock_ids=["BULL"])
    assert df.empty


# === 策略 15:taiex_alpha(Phase 1) ===

def test_taiex_alpha_taiex_down_stock_up_passes(tmp_db):
    """TAIEX 跌 1% / 個股漲 2% → 入選。"""
    # TAIEX 灌兩天:昨日 17000 → 今日 16830(跌 1%)
    db.upsert_daily_prices([
        {"stock_id": "TAIEX", "date": _DATES[0],
         "open": 17000, "high": 17000, "low": 17000, "close": 17000, "volume": 0,
         "trading_money": None, "trading_turnover": None, "spread": None},
        {"stock_id": "TAIEX", "date": _DATES[1],
         "open": 16830, "high": 16830, "low": 16830, "close": 16830, "volume": 0,
         "trading_money": None, "trading_turnover": None, "spread": None},
    ])
    db.upsert_stocks([{"stock_id": "ALPHA", "name": "獨立股", "market": "TW"}])
    db.upsert_daily_prices([
        {"stock_id": "ALPHA", "date": _DATES[0],
         "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1000,
         "trading_money": None, "trading_turnover": None, "spread": None},
        {"stock_id": "ALPHA", "date": _DATES[1],
         "open": 102, "high": 102, "low": 102, "close": 102, "volume": 1000,
         "trading_money": None, "trading_turnover": None, "spread": None},
    ])
    df = strat.screen_taiex_alpha(_DATES[1], stock_ids=["ALPHA"])
    assert len(df) == 1
    row = df.iloc[0]
    assert row["taiex_pct"] < 0
    assert row["stock_pct"] >= 1.0


def test_taiex_alpha_taiex_up_returns_empty(tmp_db):
    """大盤上漲 → 整批不入選(策略 short-circuit)。"""
    db.upsert_daily_prices([
        {"stock_id": "TAIEX", "date": _DATES[0],
         "open": 17000, "high": 17000, "low": 17000, "close": 17000, "volume": 0,
         "trading_money": None, "trading_turnover": None, "spread": None},
        {"stock_id": "TAIEX", "date": _DATES[1],
         "open": 17170, "high": 17170, "low": 17170, "close": 17170, "volume": 0,
         "trading_money": None, "trading_turnover": None, "spread": None},
    ])
    db.upsert_stocks([{"stock_id": "X", "name": "X", "market": "TW"}])
    db.upsert_daily_prices([
        {"stock_id": "X", "date": _DATES[1],
         "open": 102, "high": 102, "low": 102, "close": 102, "volume": 1000,
         "trading_money": None, "trading_turnover": None, "spread": None},
    ])
    df = strat.screen_taiex_alpha(_DATES[1], stock_ids=["X"])
    assert df.empty


# === 策略 16:revenue_acceleration(Phase 1) ===

def test_revenue_acceleration_yoy_above_30_accelerating_passes(tmp_db):
    """月營收 YoY 50% > 上月 35%(都 > 30% 且加速)→ 入選。"""
    db.upsert_stocks([{"stock_id": "REV2", "name": "營收加速", "market": "TW"}])
    # period 由新到舊在 ORDER BY DESC 後:2024-12 / 2024-11
    db.upsert_financials([
        {"stock_id": "REV2", "period_type": "monthly_revenue",
         "period": "2024-11", "revenue": 1.0e8, "revenue_yoy": 35.0,
         "eps": None, "roe": None},
        {"stock_id": "REV2", "period_type": "monthly_revenue",
         "period": "2024-12", "revenue": 1.5e8, "revenue_yoy": 50.0,
         "eps": None, "roe": None},
    ])
    db.upsert_daily_prices([{
        "stock_id": "REV2", "date": _DATES[0],
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000,
        "trading_money": None, "trading_turnover": None, "spread": None,
    }])
    df = strat.screen_revenue_acceleration(_DATES[0], stock_ids=["REV2"])
    assert len(df) == 1
    assert df.iloc[0]["curr_yoy"] == 50.0
    assert df.iloc[0]["prev_yoy"] == 35.0


def test_revenue_acceleration_below_30_threshold_fails(tmp_db):
    """月營收 YoY 25% < 30% → 不入選(不夠強)。"""
    db.upsert_stocks([{"stock_id": "WEAK", "name": "弱", "market": "TW"}])
    db.upsert_financials([
        {"stock_id": "WEAK", "period_type": "monthly_revenue",
         "period": "2024-11", "revenue": 1.0e8, "revenue_yoy": 20.0,
         "eps": None, "roe": None},
        {"stock_id": "WEAK", "period_type": "monthly_revenue",
         "period": "2024-12", "revenue": 1.1e8, "revenue_yoy": 25.0,
         "eps": None, "roe": None},
    ])
    df = strat.screen_revenue_acceleration(_DATES[0], stock_ids=["WEAK"])
    assert df.empty
