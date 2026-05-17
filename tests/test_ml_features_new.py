"""v4 進階特徵 unit tests — 純函式邏輯（不打 SQLite）。

對應 src/ml_features.py 加入的 10 個 features:
  籌碼:
    - concentration_change_rate
    - institutional_continuity
    - inst_divergence
  多時間軸:
    - ma5_above_ma20_pct / ma20_above_ma60_pct
    - momentum_5d / momentum_20d / momentum_60d
  產業:
    - industry_relative_strength / industry_rank_pct

關鍵 edge case:
  - 缺資料 / 全 NaN → fallback 0.0
  - 邊界 case（產業 < 3 檔、ma 不足、空 inst_df）→ 安全 fallback
"""
from __future__ import annotations

import pandas as pd
import pytest

from src import ml_features as mlf


# === concentration_change_rate ===

def test_concentration_change_rate_normal():
    """4 週前 0.10 → 現在 0.13，rate = 0.30。"""
    rows = [
        {"week_end": "2026-04-10", "holders_pct": 0.10, "holders_delta_w": 0},
        {"week_end": "2026-04-17", "holders_pct": 0.10, "holders_delta_w": 0},
        {"week_end": "2026-04-24", "holders_pct": 0.11, "holders_delta_w": 0},
        {"week_end": "2026-05-01", "holders_pct": 0.12, "holders_delta_w": 0},
        {"week_end": "2026-05-08", "holders_pct": 0.13, "holders_delta_w": 0},
    ]
    v = mlf.compute_concentration_change_rate(rows)
    assert v == pytest.approx(0.30, abs=1e-6)


def test_concentration_change_rate_small_denominator_smoothed():
    """old=0.001（小到容易爆值）→ 用 max(0.005, old)=0.005 平滑。"""
    rows = [
        {"week_end": "2026-04-10", "holders_pct": 0.001, "holders_delta_w": 0},
        {"week_end": "2026-04-17", "holders_pct": 0.001, "holders_delta_w": 0},
        {"week_end": "2026-04-24", "holders_pct": 0.001, "holders_delta_w": 0},
        {"week_end": "2026-05-01", "holders_pct": 0.001, "holders_delta_w": 0},
        {"week_end": "2026-05-08", "holders_pct": 0.010, "holders_delta_w": 0},
    ]
    v = mlf.compute_concentration_change_rate(rows)
    # (0.010 - 0.001) / max(0.005, 0.001) = 0.009 / 0.005 = 1.8
    assert v == pytest.approx(1.8, abs=1e-6)


def test_concentration_change_rate_insufficient_history():
    """< 5 週 → 0.0。"""
    rows = [
        {"week_end": "2026-05-01", "holders_pct": 0.10, "holders_delta_w": 0},
        {"week_end": "2026-05-08", "holders_pct": 0.12, "holders_delta_w": 0},
    ]
    assert mlf.compute_concentration_change_rate(rows) == 0.0


def test_concentration_change_rate_null_latest():
    rows = [
        {"week_end": "2026-04-10", "holders_pct": 0.10, "holders_delta_w": 0},
        {"week_end": "2026-04-17", "holders_pct": 0.10, "holders_delta_w": 0},
        {"week_end": "2026-04-24", "holders_pct": 0.10, "holders_delta_w": 0},
        {"week_end": "2026-05-01", "holders_pct": 0.10, "holders_delta_w": 0},
        {"week_end": "2026-05-08", "holders_pct": None, "holders_delta_w": 0},
    ]
    assert mlf.compute_concentration_change_rate(rows) == 0.0


# === institutional_continuity ===

def test_institutional_continuity_consecutive_buy():
    """最近 3 天外資+投信都淨買 → +3.0。第 4 天往前外資轉賣 → 截斷。"""
    df = pd.DataFrame({
        "date": ["2026-05-01", "2026-05-02", "2026-05-03",
                 "2026-05-04", "2026-05-05"],
        "foreign_buy_sell": [-100, 50, 200, 300, 150],
        "trust_buy_sell": [-10, -5, 20, 30, 40],  # day2 投信還是賣
    })
    v = mlf.compute_institutional_continuity(df)
    assert v == 3.0  # day3-5 共同買，day2 投信賣截斷


def test_institutional_continuity_consecutive_sell():
    """最近 4 天兩家共同淨賣 → -4.0。"""
    df = pd.DataFrame({
        "date": ["2026-05-01", "2026-05-02", "2026-05-03",
                 "2026-05-04", "2026-05-05"],
        "foreign_buy_sell": [100, -50, -100, -200, -150],
        "trust_buy_sell": [10, -5, -20, -30, -40],
    })
    v = mlf.compute_institutional_continuity(df)
    assert v == -4.0


def test_institutional_continuity_no_consensus_returns_zero():
    """最近一天兩家方向不同 → 0.0。"""
    df = pd.DataFrame({
        "date": ["2026-05-01", "2026-05-02"],
        "foreign_buy_sell": [100, -50],
        "trust_buy_sell": [-10, 30],
    })
    assert mlf.compute_institutional_continuity(df) == 0.0


def test_institutional_continuity_empty_df():
    assert mlf.compute_institutional_continuity(pd.DataFrame()) == 0.0


def test_institutional_continuity_missing_columns():
    df = pd.DataFrame({"date": ["2026-05-01"]})
    assert mlf.compute_institutional_continuity(df) == 0.0


# === inst_divergence ===

def test_inst_divergence_full_divergence():
    """外資 +1000, 投信 -1000 → 完全背離,divergence = 1.0。"""
    df = pd.DataFrame({
        "foreign_buy_sell": [200, 200, 200, 200, 200],   # f5 = 1000
        "trust_buy_sell":   [-200, -200, -200, -200, -200],  # t5 = -1000
    })
    v = mlf.compute_inst_divergence(df)
    assert v == pytest.approx(1.0, abs=1e-6)


def test_inst_divergence_full_cohesion():
    """兩家共同強買 → 完全同向,divergence = 0.0。"""
    df = pd.DataFrame({
        "foreign_buy_sell": [200, 200, 200, 200, 200],
        "trust_buy_sell":   [50, 50, 50, 50, 50],
    })
    v = mlf.compute_inst_divergence(df)
    assert v == pytest.approx(0.0, abs=1e-6)


def test_inst_divergence_partial():
    """f5 = +500, t5 = -300 → cohesion = |500-300|/(500+300) = 0.25
    → divergence = 0.75。"""
    df = pd.DataFrame({
        "foreign_buy_sell": [100, 100, 100, 100, 100],
        "trust_buy_sell":   [-60, -60, -60, -60, -60],
    })
    v = mlf.compute_inst_divergence(df)
    assert v == pytest.approx(0.75, abs=1e-6)


def test_inst_divergence_zero_volume():
    df = pd.DataFrame({
        "foreign_buy_sell": [0, 0, 0, 0, 0],
        "trust_buy_sell":   [0, 0, 0, 0, 0],
    })
    assert mlf.compute_inst_divergence(df) == 0.0


def test_inst_divergence_empty_df():
    assert mlf.compute_inst_divergence(pd.DataFrame()) == 0.0


# === ma above pct ===

def test_ma5_above_ma20_pct_all_above():
    """單調上漲的 close → 近 60 日 ma5 永遠 > ma20 → 1.0。"""
    close = pd.Series([float(i) for i in range(1, 100)])  # 1-99
    v = mlf.compute_ma_above_pct(close, fast=5, slow=20, lookback=60)
    assert v == pytest.approx(1.0, abs=1e-6)


def test_ma5_above_ma20_pct_all_below():
    """單調下跌 → ma5 < ma20 → 0.0。"""
    close = pd.Series([float(i) for i in range(100, 1, -1)])  # 100..2
    v = mlf.compute_ma_above_pct(close, fast=5, slow=20, lookback=60)
    assert v == pytest.approx(0.0, abs=1e-6)


def test_ma_above_pct_insufficient_data():
    """少於 slow+5 → 0.0。"""
    close = pd.Series([10.0] * 10)
    assert mlf.compute_ma_above_pct(close, fast=5, slow=20, lookback=60) == 0.0


def test_ma_above_pct_none_input():
    assert mlf.compute_ma_above_pct(None, fast=5, slow=20, lookback=60) == 0.0


# === momentum ===

def test_momentum_5d_positive():
    """7 elements: close[-1]=110, close[-6]=95 → (110/95-1)*100 ≈ 15.79%。"""
    close = pd.Series([90.0, 95.0, 100.0, 102.0, 105.0, 108.0, 110.0])
    v = mlf.compute_momentum_n(close, 5)
    assert v == pytest.approx((110.0 / 95.0 - 1.0) * 100.0, abs=1e-6)


def test_momentum_5d_negative():
    """7 elements: close[-1]=88, close[-6]=105 → ≈ -16.19%。"""
    close = pd.Series([110.0, 105.0, 100.0, 95.0, 92.0, 90.0, 88.0])
    v = mlf.compute_momentum_n(close, 5)
    assert v == pytest.approx((88.0 / 105.0 - 1.0) * 100.0, abs=1e-6)


def test_momentum_insufficient_data():
    close = pd.Series([100.0, 102.0])
    assert mlf.compute_momentum_n(close, 5) == 0.0


def test_momentum_zero_base():
    close = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0, 100.0])
    assert mlf.compute_momentum_n(close, 5) == 0.0


# === industry_relative_strength ===

def test_industry_relative_strength_basic():
    """sid 漲 5%、產業平均 2% → +3 點。"""
    v = mlf.compute_industry_relative_strength(
        sid_return_5d=5.0,
        industry_returns_5d=[1.0, 2.0, 3.0, 5.0],  # avg = 2.75
    )
    assert v == pytest.approx(5.0 - 2.75, abs=1e-6)


def test_industry_relative_strength_too_few_peers():
    """產業內樣本 < 3 → 0.0（信號不足）。"""
    v = mlf.compute_industry_relative_strength(
        sid_return_5d=10.0, industry_returns_5d=[1.0, 2.0],
    )
    assert v == 0.0


def test_industry_relative_strength_empty():
    assert mlf.compute_industry_relative_strength(5.0, []) == 0.0
    assert mlf.compute_industry_relative_strength(5.0, None) == 0.0


def test_industry_relative_strength_with_none_entries():
    """list 含 None → 忽略,只用 valid entries 算 avg。"""
    v = mlf.compute_industry_relative_strength(
        sid_return_5d=5.0,
        industry_returns_5d=[1.0, None, 3.0, None, 5.0],
    )
    # valid = [1, 3, 5] avg = 3 → 5 - 3 = 2
    assert v == pytest.approx(2.0, abs=1e-6)


# === industry_rank_pct ===

def test_industry_rank_pct_top():
    """sid 強過所有同產業 → 1.0。"""
    v = mlf.compute_industry_rank_pct(
        sid_return_5d=10.0,
        industry_returns_5d=[1.0, 2.0, 3.0, 4.0, 5.0],
    )
    assert v == pytest.approx(1.0)


def test_industry_rank_pct_bottom():
    """sid 弱過所有 → 0.0。"""
    v = mlf.compute_industry_rank_pct(
        sid_return_5d=-5.0,
        industry_returns_5d=[1.0, 2.0, 3.0, 4.0, 5.0],
    )
    assert v == 0.0


def test_industry_rank_pct_median():
    v = mlf.compute_industry_rank_pct(
        sid_return_5d=3.5,
        industry_returns_5d=[1.0, 2.0, 3.0, 4.0, 5.0],
    )
    # 3.5 strict-greater 3 個（1, 2, 3）, 5 個 total → 0.6
    assert v == pytest.approx(0.6, abs=1e-6)


def test_industry_rank_pct_too_few_peers_returns_neutral():
    """< 3 檔 → 0.5(中性)。"""
    v = mlf.compute_industry_rank_pct(10.0, [1.0])
    assert v == 0.5


# === FEATURE_NAMES contract ===

def test_new_feature_names_count():
    """v4 加 10 個新 feature。"""
    assert len(mlf.NEW_FEATURE_NAMES) == 10


def test_new_feature_names_no_overlap_with_v3():
    """新 features 不能跟 v3 既有 FEATURE_NAMES 重名。"""
    from src import ml_predictor
    v3_features = set(ml_predictor.FEATURE_NAMES[:16])
    for name in mlf.NEW_FEATURE_NAMES:
        assert name not in v3_features, (
            f"新 feature {name} 跟 v3 重名,會 dict 覆蓋造成混淆"
        )


# === SQL-backed helpers(用 in-memory sqlite 測） ===

def test_load_industry_for_sid_in_memory(monkeypatch, tmp_path):
    """建臨時 stocks 表測 _load_industry_for_sid。"""
    from src import config, database as db
    test_db = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(test_db))
    db._reset_path_cache()
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO stocks (stock_id, name, market, industry, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2330", "台積電", "TW", "半導體業", "2026-05-17"),
        )
        conn.commit()

    assert mlf._load_industry_for_sid("2330") == "半導體業"
    assert mlf._load_industry_for_sid("9999") is None


def test_load_industry_returns_5d_with_real_db(monkeypatch, tmp_path):
    """3 sid 在同產業,各 6 天 close — 確認回 3 個 5d returns。"""
    from src import config, database as db
    test_db = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(test_db))
    db._reset_path_cache()
    db.init_db()
    with db.get_conn() as conn:
        for sid in ("2330", "2454", "3008"):
            conn.execute(
                "INSERT INTO stocks (stock_id, name, market, industry, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, sid, "TW", "半導體業", "2026-05-17"),
            )
            # 6 天 close,從 100 漲到 110(+10%)
            for i, close in enumerate([100, 102, 104, 106, 108, 110]):
                conn.execute(
                    "INSERT INTO daily_prices (stock_id, date, close) "
                    "VALUES (?, ?, ?)",
                    (sid, f"2026-05-{10 + i:02d}", float(close)),
                )
        conn.commit()

    arr = mlf._load_industry_returns_5d("半導體業", "2026-05-15")
    # 每檔 5d return: (110/100 - 1)*100 = 10%
    assert len(arr) == 3
    for r in arr:
        assert r == pytest.approx(10.0, abs=1e-6)


def test_industry_returns_cache_reuses(monkeypatch, tmp_path):
    """cache 命中:同 (target_date, industry) 第二次不會再打 SQL。"""
    from src import config, database as db
    test_db = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(test_db))
    db._reset_path_cache()
    db.init_db()
    with db.get_conn() as conn:
        for sid in ("2330", "2454", "3008"):
            conn.execute(
                "INSERT INTO stocks (stock_id, name, market, industry, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, sid, "TW", "半導體業", "2026-05-17"),
            )
            for i, close in enumerate([100, 102, 104, 106, 108, 110]):
                conn.execute(
                    "INSERT INTO daily_prices (stock_id, date, close) "
                    "VALUES (?, ?, ?)",
                    (sid, f"2026-05-{10 + i:02d}", float(close)),
                )
        conn.commit()

    mlf._reset_industry_cache()
    a1 = mlf.get_industry_returns_5d_cached("半導體業", "2026-05-15")
    # 改一筆 close,確認 cache hit 後不會 re-query
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE daily_prices SET close=999 "
            "WHERE stock_id='2330' AND date='2026-05-15'"
        )
        conn.commit()
    a2 = mlf.get_industry_returns_5d_cached("半導體業", "2026-05-15")
    assert a1 == a2  # cache 命中,不會看到 999 改動

    # 不同 target_date 應該重新 query(只是不會剛好有資料)
    mlf._reset_industry_cache()
    a3 = mlf.get_industry_returns_5d_cached("半導體業", "2026-05-15")
    # cache 清掉後讀到改過的 close,2330 變 (999/100 - 1)*100 = 899%
    assert any(r > 100.0 for r in a3)


def test_load_industry_returns_5d_empty_industry():
    """empty industry name → []。"""
    assert mlf._load_industry_returns_5d("", "2026-05-15") == []
