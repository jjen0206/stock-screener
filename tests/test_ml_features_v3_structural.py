"""Structural guard:守住 v3 feature 集存在 + back-compat shim 不被刪。

不跑功能測試(那在 test_ml_features_v3.py),只用 inspect.getsource regex
確保:
  1. 5 個新 feature 名都還在 extract_features 來源裡(不被誰 refactor 砍)
  2. _aligned_feature_names slicing shim 還在(舊 v2 pkl 還能跑)
  3. MODEL_VERSION = "v3"(meta 升版證據)
  4. FEATURE_NAMES 長度 ≥ 16(可加更多,但不能砍)
"""
from __future__ import annotations

import inspect
import re

from src import ml_predictor as m


V3_FEATURE_NAMES = [
    "holders_delta_w_zscore",
    "inst_5d_zscore",
    "regime_dummy",
    "holders_pct_change_4w",
    "is_theme_member",
]


def test_extract_features_source_contains_all_v3_features():
    """5 個新 feature 名都要在 extract_features 函式 source 內。"""
    src = inspect.getsource(m.extract_features)
    missing = [name for name in V3_FEATURE_NAMES if name not in src]
    assert not missing, (
        f"v3 features 從 extract_features 消失了:{missing}。"
        f"若有人砍了某 feature,請先補回對應的 helper + meta version bump。"
    )


def test_feature_names_constant_lists_all_v3():
    """FEATURE_NAMES 列表本身要包含 v3 features(不只 source 內提到)。"""
    missing = [name for name in V3_FEATURE_NAMES if name not in m.FEATURE_NAMES]
    assert not missing, f"FEATURE_NAMES 漏掉:{missing}"
    assert len(m.FEATURE_NAMES) >= 16, (
        f"FEATURE_NAMES 應 ≥ 16 個(v2 base 11 + v3 5);"
        f"目前 {len(m.FEATURE_NAMES)} 個"
    )


def test_feature_names_v2_base_unchanged_for_backcompat():
    """前 11 個 feature 順序與 v2 base 完全一致 — backward-compat 仰賴此契約。"""
    v2_base = [
        "kd_k", "kd_d", "macd_dif", "macd_osc", "ma_alignment",
        "bb_position", "vol_ratio", "bias_pct", "atr_normalized",
        "inst_5d", "inst_10d",
    ]
    assert m.FEATURE_NAMES[:11] == v2_base, (
        "前 11 個 feature 順序變了 — 會破壞 _aligned_feature_names "
        "slicing shim,舊 v2 pkl 推論結果就錯了。"
    )


def test_aligned_feature_names_helper_exists():
    """_aligned_feature_names slicing shim 必須存在(舊 model 相容性核心)。"""
    assert hasattr(m, "_aligned_feature_names"), (
        "_aligned_feature_names slicing shim 不見了 — 舊 v2 pkl 會推論炸掉。"
    )

    # Source 也要看到 n_features_in_ 的引用
    src = inspect.getsource(m._aligned_feature_names)
    assert "n_features_in_" in src, (
        "_aligned_feature_names 不再參考 model.n_features_in_,"
        "shim 邏輯壞了。"
    )


def test_model_version_is_v3_or_later():
    """MODEL_VERSION constant 應為 v3 / v4(任何 v3+,升版只升不降)。"""
    assert m.MODEL_VERSION in ("v3", "v4"), (
        f"MODEL_VERSION 不應降版,目前 {m.MODEL_VERSION}"
    )


def test_predict_paths_use_aligned_feature_names():
    """predict_short_pick_winrate / predict_batch 都要呼叫 _aligned_feature_names
    才能保証 backward-compat — 用 regex 守住引用。"""
    for fn_name in ("predict_short_pick_winrate", "predict_batch"):
        src = inspect.getsource(getattr(m, fn_name))
        assert "_aligned_feature_names" in src, (
            f"{fn_name} 不再呼叫 _aligned_feature_names — "
            f"舊 v2 pkl 會被丟 16 列 features 後爆炸。"
        )


def test_extract_features_has_per_feature_try_except_fallback():
    """每個 v3 feature 都應該被 try/except 包住,缺資料只 fallback 該 feature 0.0,
    不會把整列 row drop 掉。"""
    src = inspect.getsource(m.extract_features)
    # 計算 try/except 對數(extract_features 內)
    try_count = len(re.findall(r"\btry:", src))
    except_count = len(re.findall(r"\bexcept Exception", src))
    # 5 個 new features,每個一段 try/except;放寬到 ≥ 4 容忍 future refactor
    assert try_count >= 4, (
        f"extract_features 內 try 區塊太少({try_count}),"
        f"v3 features 可能沒有 fallback,缺資料會 drop 整列。"
    )
    assert except_count >= 4, (
        f"extract_features 內 except 區塊太少({except_count})。"
    )
