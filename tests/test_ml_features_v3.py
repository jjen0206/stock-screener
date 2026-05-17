"""v3 高階特徵 unit tests:守住 5 個新 feature 的正常計算與 fallback 行為。

對應 src/ml_predictor.py 加入的:
  - holders_delta_w_zscore  千張戶週變化 z-score(滾動 4 週)
  - holders_pct_change_4w   千張戶占比 4 週前對比
  - inst_5d_zscore          法人 5d 累計 vs 20d-rolling-sum z-score
  - regime_dummy            大盤 regime ordinal
  - is_theme_member         theme YAML union 命中與否

純函式 test,不需 SQLite — 直接餵 list[dict] / pd.Series 給內部 helper。
也包含 _aligned_feature_names backward-compat slicing(舊 v2 model 仍能用)。
"""
from __future__ import annotations

import pandas as pd
import pytest

from src import ml_predictor as m


# === holders_delta_w_zscore ===

def test_holders_delta_w_zscore_normal_breakout():
    """前 4 週 [10, 12, 11, 13] 算 μ ≈ 11.5,σ ≈ 1.29,本週 = 30 → z ≈ 14.3。"""
    rows = [
        {"week_end": "2026-04-10", "holders_delta_w": 10, "holders_pct": 0.10},
        {"week_end": "2026-04-17", "holders_delta_w": 12, "holders_pct": 0.10},
        {"week_end": "2026-04-24", "holders_delta_w": 11, "holders_pct": 0.10},
        {"week_end": "2026-05-01", "holders_delta_w": 13, "holders_pct": 0.10},
        {"week_end": "2026-05-08", "holders_delta_w": 30, "holders_pct": 0.10},
    ]
    z = m._compute_holders_delta_w_zscore(rows)
    assert z > 10.0  # 巨幅突破


def test_holders_delta_w_zscore_insufficient_history_returns_zero():
    """只有 3 週(< 5)→ fallback 0.0,不爆。"""
    rows = [
        {"week_end": "2026-04-24", "holders_delta_w": 10, "holders_pct": 0.10},
        {"week_end": "2026-05-01", "holders_delta_w": 11, "holders_pct": 0.10},
        {"week_end": "2026-05-08", "holders_delta_w": 30, "holders_pct": 0.10},
    ]
    assert m._compute_holders_delta_w_zscore(rows) == 0.0


def test_holders_delta_w_zscore_zero_sigma_returns_zero():
    """前 4 週全相同 → σ=0 → fallback 0.0(不除以 0)。"""
    rows = [
        {"week_end": "2026-04-10", "holders_delta_w": 5, "holders_pct": 0.10},
        {"week_end": "2026-04-17", "holders_delta_w": 5, "holders_pct": 0.10},
        {"week_end": "2026-04-24", "holders_delta_w": 5, "holders_pct": 0.10},
        {"week_end": "2026-05-01", "holders_delta_w": 5, "holders_pct": 0.10},
        {"week_end": "2026-05-08", "holders_delta_w": 20, "holders_pct": 0.10},
    ]
    assert m._compute_holders_delta_w_zscore(rows) == 0.0


def test_holders_delta_w_zscore_null_latest_returns_zero():
    """本週 delta NULL → 0.0(不命中)。"""
    rows = [
        {"week_end": "2026-04-10", "holders_delta_w": 10, "holders_pct": 0.10},
        {"week_end": "2026-04-17", "holders_delta_w": 11, "holders_pct": 0.10},
        {"week_end": "2026-04-24", "holders_delta_w": 9, "holders_pct": 0.10},
        {"week_end": "2026-05-01", "holders_delta_w": 12, "holders_pct": 0.10},
        {"week_end": "2026-05-08", "holders_delta_w": None, "holders_pct": 0.10},
    ]
    assert m._compute_holders_delta_w_zscore(rows) == 0.0


# === holders_pct_change_4w ===

def test_holders_pct_change_4w_normal():
    """4 週前 0.10 → 現在 0.12,相對變化 +20%。"""
    rows = [
        {"week_end": "2026-04-10", "holders_delta_w": 0, "holders_pct": 0.10},
        {"week_end": "2026-04-17", "holders_delta_w": 0, "holders_pct": 0.11},
        {"week_end": "2026-04-24", "holders_delta_w": 0, "holders_pct": 0.11},
        {"week_end": "2026-05-01", "holders_delta_w": 0, "holders_pct": 0.12},
        {"week_end": "2026-05-08", "holders_delta_w": 0, "holders_pct": 0.12},
    ]
    v = m._compute_holders_pct_change_4w(rows)
    assert v == pytest.approx(0.20, abs=1e-6)


def test_holders_pct_change_4w_insufficient_history_returns_zero():
    rows = [
        {"week_end": "2026-05-01", "holders_delta_w": 0, "holders_pct": 0.10},
        {"week_end": "2026-05-08", "holders_delta_w": 0, "holders_pct": 0.12},
    ]
    assert m._compute_holders_pct_change_4w(rows) == 0.0


# === inst_5d_zscore ===

def test_inst_5d_zscore_normal():
    """30 天 inst_total,最後 5 天遠高於前 25 天 baseline → z > 0。"""
    # 前 25 天小波動,最後 5 天每天 +1000
    s = pd.Series([10.0, -5.0, 8.0, -3.0, 6.0] * 5 + [1000.0] * 5)
    z = m._compute_inst_5d_zscore(s)
    # 5d cumulative 5000 vs rolling-20 分布(包含部分 spike windows)→ z ≈ 2.8
    assert z > 2.0  # 顯著正向訊號


def test_inst_5d_zscore_short_series_returns_zero():
    """< 20 天 → fallback 0.0(rolling-5 樣本太少)。"""
    s = pd.Series([100.0] * 15)
    assert m._compute_inst_5d_zscore(s) == 0.0


def test_inst_5d_zscore_flat_distribution_returns_zero():
    """全相同 → σ=0 → 0.0。"""
    s = pd.Series([50.0] * 30)
    assert m._compute_inst_5d_zscore(s) == 0.0


# === regime_dummy ===

def test_regime_dummy_uses_regime_ordinal_and_caches(monkeypatch):
    """compute_regime 回 'bull' → ordinal 2.0;同 target_date 第二次 hit cache。"""
    calls: list[str] = []

    def _fake_compute(target_date, db_path=None):
        calls.append(target_date)
        return {
            "regime": "bull", "label": "多頭", "badge_emoji": "📈",
            "close": 21000.0, "ma20": 20000.0, "ma60": 19000.0,
            "target_date": target_date,
        }

    monkeypatch.setattr(
        "src.market_regime.compute_regime", _fake_compute,
    )
    # 清 cache 確保純測
    m._REGIME_CACHE.clear()

    v1 = m._compute_regime_dummy("2026-05-08")
    v2 = m._compute_regime_dummy("2026-05-08")
    assert v1 == pytest.approx(2.0)
    assert v2 == pytest.approx(2.0)
    assert len(calls) == 1  # cache hit on 2nd call


def test_regime_dummy_fallback_zero_on_exception(monkeypatch):
    """compute_regime 拋 → 0.0,不爆。"""
    def _boom(target_date, db_path=None):
        raise RuntimeError("synthetic TAIEX fail")

    monkeypatch.setattr(
        "src.market_regime.compute_regime", _boom,
    )
    m._REGIME_CACHE.clear()
    assert m._compute_regime_dummy("2026-05-08") == 0.0


# === is_theme_member ===

def test_is_theme_member_loaded_from_yaml(monkeypatch, tmp_path):
    """讀 themes/*.yaml union,2330 / 2317 在 set 內回 True,fake_sid 不在。"""
    themes_dir = tmp_path / "data" / "themes"
    themes_dir.mkdir(parents=True)
    (themes_dir / "a.yaml").write_text(
        'sids:\n  - "2330"\n  - "2317"\n', encoding="utf-8",
    )
    (themes_dir / "b.yaml").write_text(
        'sids:\n  - "2454"\n', encoding="utf-8",
    )

    class _FakeConfig:
        PROJECT_ROOT = tmp_path

    monkeypatch.setitem(__import__("sys").modules, "src.config", _FakeConfig)
    m._THEME_MEMBER_SIDS = None  # 清 cache

    sids = m._load_theme_member_sids()
    assert "2330" in sids and "2317" in sids and "2454" in sids
    assert "9999" not in sids


# === _aligned_feature_names backward-compat shim ===

def test_aligned_feature_names_old_v2_model_uses_first_11():
    """舊 v2 model n_features_in_=11 → 回 FEATURE_NAMES[:11](不含 v3 新 5 個)。"""
    class _V2Model:
        n_features_in_ = 11

    aligned = m._aligned_feature_names(_V2Model())
    assert aligned == m.FEATURE_NAMES[:11]
    assert "holders_delta_w_zscore" not in aligned  # v3 新 feature 不出現


def test_aligned_feature_names_new_v3_model_uses_all_16():
    """v3 model n_features_in_=16 → 回 FEATURE_NAMES 前 16 個(v4 升版後 FEATURE_NAMES
    擴成 26,前 16 仍是 v3 那批)。"""
    class _V3Model:
        n_features_in_ = 16

    aligned = m._aligned_feature_names(_V3Model())
    assert aligned == m.FEATURE_NAMES[:16]
    assert len(aligned) == 16


def test_aligned_feature_names_missing_attr_uses_all():
    """model 沒 n_features_in_ 屬性 → 假設 v3 用全部 feature。"""
    class _UnknownModel:
        pass

    aligned = m._aligned_feature_names(_UnknownModel())
    assert aligned == m.FEATURE_NAMES
