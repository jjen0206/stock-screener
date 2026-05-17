"""驗證 v4 features wire 進 ml_predictor 的整合行為。

不測 feature 數值計算（test_ml_features_new.py 已 cover），這裡只測:
  1. FEATURE_NAMES 順序契約（v3 前 16 / v4 後 10）
  2. extract_features 正常路徑回 26-key dict
  3. kill-switch ML_NEW_FEATURES_ENABLED=false → v4 features 全 0.0 但 key 還在
  4. _aligned_feature_names 對 v3 / v4 模型分別 slice 16 / 26
  5. predict_batch 餵全 26 features 不炸 + kill-switch 下行為相容
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import ml_predictor


# === FEATURE_NAMES contract ===

def test_feature_names_total_count():
    """v4 model 應 26 features(v2 11 + v3 5 + v4 10)。"""
    assert len(ml_predictor.FEATURE_NAMES) == 26


def test_feature_names_v3_prefix_unchanged():
    """前 16 個 feature 順序 = v3 base — 動到會破壞所有 v3 pkl。"""
    v3_expected = [
        "kd_k", "kd_d", "macd_dif", "macd_osc", "ma_alignment",
        "bb_position", "vol_ratio", "bias_pct", "atr_normalized",
        "inst_5d", "inst_10d",
        "holders_delta_w_zscore",
        "inst_5d_zscore",
        "regime_dummy",
        "holders_pct_change_4w",
        "is_theme_member",
    ]
    assert ml_predictor.FEATURE_NAMES[:16] == v3_expected


def test_feature_names_v4_appended_to_tail():
    v4_expected = [
        "concentration_change_rate",
        "institutional_continuity",
        "inst_divergence",
        "ma5_above_ma20_pct",
        "ma20_above_ma60_pct",
        "momentum_5d",
        "momentum_20d",
        "momentum_60d",
        "industry_relative_strength",
        "industry_rank_pct",
    ]
    assert ml_predictor.FEATURE_NAMES[16:] == v4_expected


def test_v3_feature_count_constant():
    assert ml_predictor.V3_FEATURE_COUNT == 16


def test_model_version_bumped_to_v4():
    assert ml_predictor.MODEL_VERSION == "v4"


# === kill-switch ===

def test_new_features_enabled_default_true(monkeypatch):
    monkeypatch.delenv("ML_NEW_FEATURES_ENABLED", raising=False)
    assert ml_predictor._new_features_enabled() is True


def test_new_features_enabled_explicit_true(monkeypatch):
    monkeypatch.setenv("ML_NEW_FEATURES_ENABLED", "true")
    assert ml_predictor._new_features_enabled() is True


@pytest.mark.parametrize("val", ["false", "FALSE", "0", "off", "no"])
def test_new_features_enabled_off(monkeypatch, val):
    monkeypatch.setenv("ML_NEW_FEATURES_ENABLED", val)
    assert ml_predictor._new_features_enabled() is False


def test_new_features_enabled_empty_string_is_true(monkeypatch):
    monkeypatch.setenv("ML_NEW_FEATURES_ENABLED", "")
    assert ml_predictor._new_features_enabled() is True


# === _aligned_feature_names slicing ===

def test_aligned_v3_model_slices_to_16():
    class _V3Model:
        n_features_in_ = 16
    aligned = ml_predictor._aligned_feature_names(_V3Model())
    assert len(aligned) == 16
    # v4 features 不出現
    assert "concentration_change_rate" not in aligned
    assert "momentum_60d" not in aligned


def test_aligned_v4_model_uses_all_26():
    class _V4Model:
        n_features_in_ = 26
    aligned = ml_predictor._aligned_feature_names(_V4Model())
    assert len(aligned) == 26
    assert "momentum_60d" in aligned
    assert "industry_rank_pct" in aligned


def test_aligned_v2_model_still_slices_to_11():
    """確認舊 v2 (11 features) 一樣能 slicing。"""
    class _V2Model:
        n_features_in_ = 11
    aligned = ml_predictor._aligned_feature_names(_V2Model())
    assert aligned == ml_predictor.FEATURE_NAMES[:11]


# === predict_batch with v4 model expecting all 26 features ===

class _FakeV4Model:
    """mock sklearn classifier 對應 26 features。"""
    n_features_in_ = 26

    def __init__(self, classes=(0, 1), proba_per_row=None):
        self.classes_ = list(classes)
        self._proba_per_row = proba_per_row or []

    def predict_proba(self, X: pd.DataFrame):
        # 確認 caller 傳了全 26 個 feature
        assert len(X.columns) == 26, (
            f"predict_batch 沒餵 26 features 給 v4 model: {len(X.columns)}"
        )
        n = len(X)
        if self._proba_per_row and len(self._proba_per_row) >= n:
            return np.array(self._proba_per_row[:n])
        return np.array([[0.5, 0.5]] * n)


def test_predict_batch_with_v4_model_full_26_features(monkeypatch):
    """v4 model 收到 26-key feature dict 不炸。"""
    full_feats = {f: 0.0 for f in ml_predictor.FEATURE_NAMES}
    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, td, db_path=None: dict(full_feats),
    )
    model = _FakeV4Model(proba_per_row=[[0.3, 0.7]])
    out = ml_predictor.predict_batch(model, ["2330"], "2026-05-04")
    assert out["2330"] == pytest.approx(0.7)


def test_predict_batch_kill_switch_off_still_works(monkeypatch):
    """kill-switch off → extract_features 仍回完整 26-key dict（值是 fallback 0.0）。
    v4 model 不會炸（依然 26 cols）。"""
    monkeypatch.setenv("ML_NEW_FEATURES_ENABLED", "false")
    # 模擬 extract_features 走 kill-switch 路徑回 26-key dict(v4 features = 0.0)
    fake_dict = {f: 0.5 for f in ml_predictor.FEATURE_NAMES[:16]}
    for f in ml_predictor.FEATURE_NAMES[16:]:
        fake_dict[f] = 0.0  # kill-switch off fallback

    monkeypatch.setattr(
        ml_predictor, "extract_features",
        lambda sid, td, db_path=None: dict(fake_dict),
    )
    model = _FakeV4Model(proba_per_row=[[0.4, 0.6]])
    out = ml_predictor.predict_batch(model, ["2330"], "2026-05-04")
    assert out["2330"] == pytest.approx(0.6)


# === Smoke test: real extract_features with mocked SQL ===
def test_extract_features_with_kill_switch_off_returns_v4_zeros(monkeypatch):
    """整合測:kill-switch off → 真實 extract_features 路徑跑下去,v4 全 0.0。

    用 monkeypatch 把 _load_history / _load_inst / _load_holders_weeks 都 mock 成
    minimum 可走完 extract_features 的 fake data。
    """
    monkeypatch.setenv("ML_NEW_FEATURES_ENABLED", "false")
    # 模擬一個正常的 90 天 daily_prices(close 100-189)
    fake_df = pd.DataFrame({
        "date": [f"2026-{(1 + i // 30):02d}-{((i % 30) + 1):02d}"
                 for i in range(90)],
        "open": [100.0 + i for i in range(90)],
        "high": [101.0 + i for i in range(90)],
        "low": [99.0 + i for i in range(90)],
        "close": [100.0 + i for i in range(90)],
        "volume": [1000 + i * 10 for i in range(90)],
    })
    monkeypatch.setattr(
        ml_predictor, "_load_history",
        lambda sid, end_date, days=90, db_path=None: fake_df.copy(),
    )
    monkeypatch.setattr(
        ml_predictor, "_load_inst",
        lambda sid, end_date, days=30, db_path=None: pd.DataFrame(),
    )
    monkeypatch.setattr(
        ml_predictor, "_load_holders_weeks",
        lambda sid, target_date, weeks=5, db_path=None: [],
    )
    # regime / theme 預設 fallback 0.0 OK,讓 _compute_regime_dummy 走 cache
    ml_predictor._REGIME_CACHE.clear()
    ml_predictor._THEME_MEMBER_SIDS = frozenset()

    feats = ml_predictor.extract_features("2330", "2026-03-31")
    assert feats is not None
    # v3 features 該有(雖然多 0.0)
    assert "kd_k" in feats
    # v4 features 必有 key,但 kill-switch off → 都 0.0
    for f in [
        "concentration_change_rate", "institutional_continuity",
        "inst_divergence", "ma5_above_ma20_pct", "ma20_above_ma60_pct",
        "momentum_5d", "momentum_20d", "momentum_60d",
        "industry_relative_strength", "industry_rank_pct",
    ]:
        assert f in feats, f"v4 feature {f} 必須存在(維持 shape)"
        assert feats[f] == 0.0, f"kill-switch off 時 {f} 應 = 0.0,實際 {feats[f]}"
    # 共 26 keys
    assert len(feats) == 26


def test_extract_features_with_kill_switch_on_computes_v4(monkeypatch):
    """kill-switch on(default)→ 真實 extract_features 有跑 v4 computation。

    momentum_5d 應該基於 fake close 算出非 0 值（單調上漲）。
    """
    monkeypatch.delenv("ML_NEW_FEATURES_ENABLED", raising=False)
    # 90 天 close 從 100 漲到 189(每天 +1)→ 5d momentum 應 > 0
    fake_df = pd.DataFrame({
        "date": [f"2026-{(1 + i // 30):02d}-{((i % 30) + 1):02d}"
                 for i in range(90)],
        "open": [100.0 + i for i in range(90)],
        "high": [101.0 + i for i in range(90)],
        "low": [99.0 + i for i in range(90)],
        "close": [100.0 + i for i in range(90)],
        "volume": [1000 + i * 10 for i in range(90)],
    })
    monkeypatch.setattr(
        ml_predictor, "_load_history",
        lambda sid, end_date, days=90, db_path=None: fake_df.copy(),
    )
    monkeypatch.setattr(
        ml_predictor, "_load_inst",
        lambda sid, end_date, days=30, db_path=None: pd.DataFrame(),
    )
    monkeypatch.setattr(
        ml_predictor, "_load_holders_weeks",
        lambda sid, target_date, weeks=5, db_path=None: [],
    )
    # 避免 industry SQL 嘗試打真實 DB
    from src import ml_features as mlf
    monkeypatch.setattr(
        mlf, "_load_industry_for_sid",
        lambda sid, db_path=None: None,
    )
    ml_predictor._REGIME_CACHE.clear()
    ml_predictor._THEME_MEMBER_SIDS = frozenset()

    feats = ml_predictor.extract_features("2330", "2026-03-31")
    assert feats is not None
    # 單調上漲,momentum 應 > 0
    assert feats["momentum_5d"] > 0
    assert feats["momentum_20d"] > 0
    # ma5 > ma20 應 = 1.0(整段 series 都上漲)
    assert feats["ma5_above_ma20_pct"] == pytest.approx(1.0, abs=1e-6)
    # 沒 industry → industry_relative_strength fallback 0.0
    assert feats["industry_relative_strength"] == 0.0
