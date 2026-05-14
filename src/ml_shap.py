"""SHAP ML 解釋性 helper(2026-05-14 加)。

對 daily-notify picks 算 SHAP values → Top 3 feature 貢獻,顯示「為什麼這檔分數
高」。Cache 寫進 `pick_shap_explanations` 表,Telegram / Streamlit 都從 cache 撈。

設計重點:
- 用 `shap.TreeExplainer(model)`:RandomForest tree-based,TreeExplainer 比
  KernelExplainer 快 100x。
- 取絕對值最大的 Top 3 features;contribution_pct = |shap_value| / sum(|all|)。
- direction: '+' = 推高 ML 分數,'-' = 拉低 ML 分數(based on raw SHAP sign)。
- shap import 走 lazy(在 compute_pick_shap 內 import),避免沒裝 shap 時 src/
  其他 module 連帶炸掉。

Caller 範例:
    from src.ml_predictor import load_model, extract_features
    from src.ml_shap import compute_pick_shap, format_shap_reason

    model = load_model("models/short_pick.pkl")
    feats = extract_features("2330", "2026-05-14")
    explanations = compute_pick_shap("2330", model, feats)
    print(format_shap_reason(explanations))
    # → "🧠 SHAP: holders_delta_w_zscore +12% / inst_5d_zscore +8% / is_theme_member +3%"
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _aligned_feature_names(model) -> list[str]:
    """跟 ml_predictor._aligned_feature_names 同邏輯:依 model.n_features_in_ slice。

    避免 import cycle(ml_shap → ml_predictor → 其他),這裡複寫一份。
    """
    from src.ml_predictor import FEATURE_NAMES

    n = getattr(model, "n_features_in_", None)
    if n is not None and n < len(FEATURE_NAMES):
        return FEATURE_NAMES[:n]
    return FEATURE_NAMES


def compute_pick_shap(
    sid: str,
    model: Any,
    features: dict[str, float] | None,
    top_k: int = 3,
) -> list[dict]:
    """對單檔算 SHAP top-K feature 貢獻。

    Args:
        sid: 股號(只給 log 用,計算不依賴)
        model: 已 load 的 sklearn classifier(支援 shap.TreeExplainer)
        features: extract_features 回的 dict(feature_name → value);None → 回空 list
        top_k: 取 top 幾個 features(default 3)

    Returns:
        list of dict,每筆:
          {
            "feature": str,
            "value": float,            # 該 feature 的原始值
            "contribution": float,     # raw SHAP value(class 1 logit shift,可正可負)
            "contribution_pct": float, # |shap| / sum(|all|),0-100 內整數
            "direction": "+" | "-",    # raw shap 正負號
          }

        SHAP 算失敗 / features=None / model=None → 回空 list(caller graceful skip)。
    """
    if model is None or not features:
        return []

    try:
        import shap  # lazy import
    except ImportError as e:
        logger.warning("[SHAP] shap 未安裝,跳過:%s", e)
        return []

    import numpy as np

    feat_names = _aligned_feature_names(model)
    try:
        X_row = np.array(
            [[float(features.get(name, 0.0)) for name in feat_names]],
            dtype=float,
        )
    except (TypeError, ValueError) as e:
        logger.warning("[SHAP] %s feature dict 轉 row 失敗:%s", sid, e)
        return []

    try:
        explainer = shap.TreeExplainer(model)
        # shap.TreeExplainer 對 sklearn RF classifier 回 (n_classes, n_samples,
        # n_features) array(舊版)或 Explanation object(新版)。我們要 class=1
        # (win)的 shap values。
        raw = explainer.shap_values(X_row)
    except Exception as e:  # noqa: BLE001
        logger.warning("[SHAP] %s TreeExplainer 失敗:%s", sid, e)
        return []

    # 處理不同 shap 版本回傳格式:
    # - shap < 0.46:list of [n_samples, n_features](per class)
    # - shap >= 0.46:np.array shape (n_samples, n_features, n_classes)
    try:
        arr = np.array(raw)
        if arr.ndim == 3 and arr.shape[0] == 2:
            # 舊版 list-of-2 → np.array((2, 1, N))
            class1 = arr[1][0]
        elif arr.ndim == 3 and arr.shape[-1] == 2:
            # 新版 (1, N, 2)
            class1 = arr[0, :, 1]
        elif arr.ndim == 2:
            # (1, N) — binary classifier 偶爾直接給 class 1 shap
            class1 = arr[0]
        else:
            logger.warning(
                "[SHAP] %s 不認得的 shap shape:%s", sid, arr.shape
            )
            return []
    except Exception as e:  # noqa: BLE001
        logger.warning("[SHAP] %s 解析 shap 輸出失敗:%s", sid, e)
        return []

    shap_vals = [float(v) for v in class1]
    abs_sum = sum(abs(v) for v in shap_vals)
    if abs_sum <= 0:
        # 全 0:稀有 case(features 全 0 or model 退化)— 回空 list 避免顯誤導
        return []

    # 取 |shap| 最大的 top_k
    indexed = list(enumerate(shap_vals))
    indexed.sort(key=lambda kv: abs(kv[1]), reverse=True)
    top: list[dict] = []
    for idx, val in indexed[:top_k]:
        name = feat_names[idx]
        pct = abs(val) / abs_sum * 100.0
        top.append({
            "feature": name,
            "value": float(features.get(name, 0.0)),
            "contribution": float(val),
            "contribution_pct": round(pct, 1),
            "direction": "+" if val >= 0 else "-",
        })
    return top


def format_shap_reason(
    explanations: list[dict],
    max_features: int = 3,
) -> str:
    """格式化 SHAP top features 成 Telegram 一行。

    格式:`🧠 SHAP: feat1 +12% / feat2 +8% / feat3 -3%`
    沒資料 → 回空 string(caller 看到 falsy 就 graceful skip 不顯該行)。
    """
    if not explanations:
        return ""
    parts: list[str] = []
    for e in explanations[:max_features]:
        feat = e.get("feature", "?")
        pct = e.get("contribution_pct", 0.0)
        direction = e.get("direction", "+")
        sign = "+" if direction == "+" else "-"
        try:
            pct_int = int(round(float(pct)))
        except (TypeError, ValueError):
            pct_int = 0
        parts.append(f"{feat} {sign}{pct_int}%")
    return "🧠 SHAP: " + " / ".join(parts)


__all__ = [
    "compute_pick_shap",
    "format_shap_reason",
]
