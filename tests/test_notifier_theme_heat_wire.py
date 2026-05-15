"""題材熱度動態權重 wire 結構性守住測試(2026-05-15 v2:冷改 hard exclude)。

純結構性 — 不 mock streamlit、不跑 _select_top_picks 全 pipeline,用
inspect.getsource + inspect.signature 看 function 接口跟 source 內 token
有沒有 wire 對。對齊 test_dynamic_weighting_wire.py pattern。

守住:
1. src.theme_heat.compute_theme_heat / get_pick_theme_multiplier 都 existing
2. theme_heat 常數:HOT_MULTIPLIER=1.3 / NEUTRAL_MULTIPLIER=1.0 /
   COLD_EXCLUDE is None
3. notifier._compute_pick_score 接受 theme_multiplier 參數(預設 1.0)
4. notifier._select_top_picks source 含 theme_heat 撈取 + hard exclude wire
5. notifier.format_yesterday_recap source 也撈 theme_heat + hard exclude
   (recap 順序一致,且不顯示已被擋的 sids)
6. notifier 有 _LAST_THEME_HEAT + _LAST_THEME_EXCLUDED 兩個模組級 cache
7. notifier.format_top_picks_message 串 theme_block + 把 excluded 餵進 caption
8. format_pick_block source 內含 🔥題材 badge(冷 sids 已被擋,不顯)
9. _compute_pick_score 排序行為:同 ml_prob,熱題材(×1.3)排前
10. Kill switch:env THEME_HEAT_ENABLED=false → multiplier 不影響
"""
from __future__ import annotations

import inspect

from src import notifier, theme_heat


# === src/theme_heat.py exists ===

def test_compute_theme_heat_exists():
    """src.theme_heat.compute_theme_heat module-level callable。"""
    assert hasattr(theme_heat, "compute_theme_heat")
    assert callable(theme_heat.compute_theme_heat)


def test_get_pick_theme_multiplier_exists():
    """src.theme_heat.get_pick_theme_multiplier module-level callable。"""
    assert hasattr(theme_heat, "get_pick_theme_multiplier")
    assert callable(theme_heat.get_pick_theme_multiplier)


def test_format_theme_heat_caption_exists():
    """推播 caption helper 必須存在。"""
    assert hasattr(theme_heat, "format_theme_heat_caption")
    assert callable(theme_heat.format_theme_heat_caption)


def test_theme_heat_constants_exist():
    """multiplier 常數命名一致(2026-05-15 v2:冷改 hard exclude → COLD_EXCLUDE)。"""
    assert hasattr(theme_heat, "HOT_MULTIPLIER")
    assert hasattr(theme_heat, "NEUTRAL_MULTIPLIER")
    assert hasattr(theme_heat, "COLD_EXCLUDE")
    assert theme_heat.HOT_MULTIPLIER == 1.3
    assert theme_heat.NEUTRAL_MULTIPLIER == 1.0
    assert theme_heat.COLD_EXCLUDE is None  # sentinel = None,代表「擋掉」
    # 舊 COLD_MULTIPLIER 已移除(避免有人誤用 ×0.7 當降權)
    assert not hasattr(theme_heat, "COLD_MULTIPLIER"), (
        "舊 COLD_MULTIPLIER 應移除,改用 COLD_EXCLUDE=None"
    )


# === _compute_pick_score 接 theme_multiplier ===

def test_compute_pick_score_accepts_theme_multiplier():
    """_compute_pick_score 必須接 theme_multiplier kwarg。"""
    sig = inspect.signature(notifier._compute_pick_score)
    assert "theme_multiplier" in sig.parameters, (
        f"_compute_pick_score 缺 theme_multiplier 參數:{list(sig.parameters)}"
    )
    # 預設 1.0(legacy 行為不受影響)
    assert sig.parameters["theme_multiplier"].default == 1.0


def test_score_hot_theme_ranks_above_neutral():
    """同 ml_prob,熱題材(×1.3)排在中性(×1.0)前面(score tuple 小者前)。

    註:冷題材 sids 在 _select_top_picks 內 hard-excluded,不會走到 score
    function;所以這裡測「熱 vs 中性」而非「熱 vs 冷」。
    """
    score_hot = notifier._compute_pick_score(
        sid="2330", ml_prob=0.6,
        matched_strategies=["s1"],
        theme_multiplier=1.3,
    )
    score_neutral = notifier._compute_pick_score(
        sid="2454", ml_prob=0.6,
        matched_strategies=["s1"],
        theme_multiplier=1.0,
    )
    assert score_hot < score_neutral  # tuple ascending → hot 排前


def test_score_neutral_theme_unchanged_from_legacy():
    """theme_multiplier=1.0(預設)→ score 跟沒傳一樣(legacy 行為)。"""
    s_default = notifier._compute_pick_score(
        sid="2330", ml_prob=0.6, matched_strategies=["s1"],
    )
    s_explicit_neutral = notifier._compute_pick_score(
        sid="2330", ml_prob=0.6, matched_strategies=["s1"],
        theme_multiplier=1.0,
    )
    assert s_default == s_explicit_neutral


def test_score_combines_strategy_and_theme_weights():
    """strategy_weight × theme_multiplier 都套到 ml_prob。"""
    weights = {"s_hot": 1.5}
    s = notifier._compute_pick_score(
        sid="2330", ml_prob=0.5,
        matched_strategies=["s_hot"],
        strategy_weights=weights,
        theme_multiplier=1.3,
    )
    # weighted_ml = 0.5 × 1.5 × 1.3 = 0.975
    assert abs(s[1] - (-0.975)) < 1e-6


# === Hard exclude wire ===

def test_select_top_picks_applies_hard_exclude():
    """_select_top_picks source 必須含 hard-exclude logic — 撈到 None 就 skip。"""
    src = inspect.getsource(notifier._select_top_picks)
    # multiplier=None / pick_theme_mult ... is None pattern
    assert "is None" in src and "pick_theme_mult" in src, (
        "_select_top_picks 沒實作 hard exclude(沒 check pick_theme_mult is None)"
    )
    # excluded_per_theme 收集已被擋的 sids 給 caption
    assert "excluded_per_theme" in src or "_LAST_THEME_EXCLUDED" in src, (
        "_select_top_picks 沒收集 excluded sids 給 caption"
    )


def test_format_yesterday_recap_applies_hard_exclude():
    """recap 也要套 hard exclude,順序 + 內容才會跟實際推播一致。"""
    src = inspect.getsource(notifier.format_yesterday_recap)
    assert "is None" in src or "is not None" in src, (
        "format_yesterday_recap 沒套 hard exclude(冷題材 sids 不該出現在複盤)"
    )


# === notifier._select_top_picks 撈 theme heat + 套排序 ===

def test_select_top_picks_calls_theme_heat():
    """_select_top_picks source 必須撈 theme_heat 並套 multiplier。"""
    src = inspect.getsource(notifier._select_top_picks)
    assert "theme_heat" in src, (
        "_select_top_picks 沒 import / call src.theme_heat"
    )
    assert "compute_theme_heat" in src, (
        "_select_top_picks 沒呼叫 compute_theme_heat"
    )
    assert "theme_multiplier" in src, (
        "_select_top_picks 沒把 theme_multiplier 傳到 _compute_pick_score"
    )


def test_format_yesterday_recap_calls_theme_heat():
    """recap 也要套 theme heat,順序才會跟實際推播一致(M4/U1)。"""
    src = inspect.getsource(notifier.format_yesterday_recap)
    assert "theme_heat" in src or "compute_theme_heat" in src, (
        "format_yesterday_recap 沒撈 theme_heat — recap 順序會跟實際分歧"
    )


# === Module-level cache + caption ===

def test_notifier_has_last_theme_heat_cache():
    """_LAST_THEME_HEAT 模組級 cache 必須存在(給 caption / UI 用)。"""
    assert hasattr(notifier, "_LAST_THEME_HEAT"), (
        "notifier 缺 _LAST_THEME_HEAT 模組級 cache"
    )
    assert isinstance(notifier._LAST_THEME_HEAT, dict)


def test_notifier_has_last_theme_excluded_cache():
    """_LAST_THEME_EXCLUDED 模組級 cache 必須存在(給 caption 算「N 檔已擋」)。"""
    assert hasattr(notifier, "_LAST_THEME_EXCLUDED"), (
        "notifier 缺 _LAST_THEME_EXCLUDED 模組級 cache"
    )
    assert isinstance(notifier._LAST_THEME_EXCLUDED, dict)


def test_format_top_picks_message_includes_theme_block():
    """format_top_picks_message source 必須串 theme caption block + 餵 excluded。"""
    src = inspect.getsource(notifier.format_top_picks_message)
    assert "format_theme_heat_caption" in src or "theme_block" in src, (
        "format_top_picks_message 沒組 theme heat caption section"
    )
    assert "excluded" in src or "_LAST_THEME_EXCLUDED" in src, (
        "format_top_picks_message 沒把 excluded 餵給 caption"
    )


def test_format_pick_block_includes_hot_theme_badge():
    """format_pick_block source 必須讀 theme_multiplier 加 🔥題材 badge。

    冷題材 sids 已被 _select_top_picks hard-excluded,不會走到這裡,
    所以只需要 🔥 熱題材 badge,不需要 🧊/🚫。
    """
    src = inspect.getsource(notifier.format_pick_block)
    assert "theme_multiplier" in src, (
        "format_pick_block 沒讀 pick['theme_multiplier']"
    )
    assert "🔥" in src and "題材" in src, (
        "format_pick_block 沒加 🔥題材 badge"
    )


# === Kill switch ===

def test_theme_heat_kill_switch_via_env(monkeypatch):
    """env THEME_HEAT_ENABLED=false → _is_enabled() False → multiplier 不生效。"""
    monkeypatch.setenv("THEME_HEAT_ENABLED", "false")
    assert theme_heat._is_enabled() is False
    monkeypatch.setenv("THEME_HEAT_ENABLED", "true")
    assert theme_heat._is_enabled() is True


def test_theme_heat_default_enabled(monkeypatch):
    """預設 ON(沒設 env 時)。"""
    monkeypatch.delenv("THEME_HEAT_ENABLED", raising=False)
    assert theme_heat._is_enabled() is True
