"""src/score_to_ev.py 單元測試。

覆蓋:
- build_score_to_ev_mapping 從 picks+outcomes 切 bucket 算 avg_ev
- score_to_ev 主 API 落點對應 bucket
- render_ev_str 顯示格式(spec 範例 EV +2.3% / EV -0.5%)
- Fallback 鏈:無 mapping → linear / 樣本不足 → 退 global / 全空 → linear
- Per-strategy vs global mapping 衝突時 per-strategy 優先
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import score_to_ev as s2e


@pytest.fixture(autouse=True)
def _reset_cache():
    """每個 test 前後清 lru_cache,避免 fixture 之間污染。"""
    s2e.invalidate_cache()
    yield
    s2e.invalidate_cache()


def _make_picks(n: int, strategy: str = "bias_convergence") -> pd.DataFrame:
    """合成 daily_picks:固定 trade_date / 單一 strategy / 線性 ml_prob。"""
    rng = np.random.default_rng(42)
    return pd.DataFrame({
        "trade_date": ["2026-05-01"] * n,
        "sid": [f"{i:04d}" for i in range(n)],
        "strategy": [strategy] * n,
        "ml_prob": rng.uniform(0.0, 1.0, size=n),
    })


def _make_outcomes(picks: pd.DataFrame, ev_per_prob: float = 5.0) -> pd.DataFrame:
    """合成 pick_outcomes:return_d5 跟 ml_prob 正相關(每 0.1 prob → ev_per_prob%)。

    return_d5 是 PERCENT(0.5 表 +0.5%),join 後內部會 /100 轉 fraction。
    """
    return pd.DataFrame({
        "pick_date": picks["trade_date"].tolist(),
        "sid": picks["sid"].tolist(),
        "strategy": picks["strategy"].tolist(),
        "entry_close": [100.0] * len(picks),
        "return_d5": (picks["ml_prob"] * ev_per_prob).tolist(),
    })


def test_render_ev_str_spec_examples():
    """spec roadmap 範例:EV +2.3% / EV -0.5%。"""
    assert s2e.render_ev_str(0.023) == "EV +2.3%"
    assert s2e.render_ev_str(-0.005) == "EV -0.5%"
    assert s2e.render_ev_str(0.0) == "EV +0.0%"


def test_render_ev_str_none_nan():
    """None / NaN → 'EV —' 佔位。"""
    assert s2e.render_ev_str(None) == "EV —"
    assert s2e.render_ev_str(float("nan")) == "EV —"


def test_build_mapping_monotonic():
    """ml_prob 跟 ev 正相關 → 高 bucket avg_ev > 低 bucket。"""
    picks = _make_picks(500)
    outcomes = _make_outcomes(picks)
    mapping = s2e.build_score_to_ev_mapping(picks, outcomes)

    g = mapping[mapping["strategy"] == "__global__"].sort_values("bucket_lo")
    # 高 bucket (top) avg_ev > 低 bucket (bottom)
    assert g.iloc[-1]["avg_ev"] > g.iloc[0]["avg_ev"]
    # 第一 bucket lo = -inf,最後 hi = +inf(覆蓋全範圍)
    assert g.iloc[0]["bucket_lo"] == float("-inf")
    assert g.iloc[-1]["bucket_hi"] == float("inf")


def test_score_to_ev_bucket_lookup(tmp_path):
    """寫 mapping → score_to_ev 落 bucket → 回該 bucket avg_ev。"""
    picks = _make_picks(500)
    outcomes = _make_outcomes(picks)
    mapping = s2e.build_score_to_ev_mapping(picks, outcomes)
    s2e.dump_mapping_to_csv(mapping, snapshot_dir=tmp_path)
    s2e.invalidate_cache()

    # score=0.5 → 中間 bucket(因 ev_per_prob=5.0、線性,EV ≈ 0.5 * 5 / 100 = 0.025)
    ev_mid = s2e.score_to_ev(0.5, snapshot_dir=tmp_path)
    assert ev_mid is not None
    assert 0.01 < ev_mid < 0.04

    # score=0.95 高 bucket EV > score=0.5 中間 bucket EV
    ev_high = s2e.score_to_ev(0.95, snapshot_dir=tmp_path)
    assert ev_high is not None
    assert ev_high > ev_mid


def test_score_to_ev_per_strategy_overrides_global(tmp_path):
    """某策略有 ≥ 100 samples 且 EV 顯著不同 → per-strategy 優先於 global。"""
    # gap_up 策略 EV 倍率 = 20(很賺)
    gap = _make_picks(200, strategy="gap_up")
    gap_out = _make_outcomes(gap, ev_per_prob=20.0)
    # bias 策略 EV 倍率 = 0.5(平庸)
    bias = _make_picks(200, strategy="bias_convergence")
    bias_out = _make_outcomes(bias, ev_per_prob=0.5)
    picks = pd.concat([gap, bias], ignore_index=True)
    outcomes = pd.concat([gap_out, bias_out], ignore_index=True)

    mapping = s2e.build_score_to_ev_mapping(picks, outcomes)
    s2e.dump_mapping_to_csv(mapping, snapshot_dir=tmp_path)
    s2e.invalidate_cache()

    ev_gap = s2e.score_to_ev(0.8, strategy_key="gap_up", snapshot_dir=tmp_path)
    ev_bias = s2e.score_to_ev(
        0.8, strategy_key="bias_convergence", snapshot_dir=tmp_path
    )
    assert ev_gap is not None and ev_bias is not None
    # gap_up 比 bias_convergence 賺很多 — 校準 mapping 必須反映這差距
    assert ev_gap > ev_bias * 5


def test_score_to_ev_fallback_to_linear_no_mapping(tmp_path):
    """Mapping CSV 不存在 → 退回 linear fallback(原 5%/3% 公式)。"""
    s2e.invalidate_cache()
    # tmp_path 是空目錄,score_to_ev.csv 不存在
    ev = s2e.score_to_ev(0.7, snapshot_dir=tmp_path)
    # linear: 0.7 * 0.05 - 0.3 * 0.03 = 0.035 - 0.009 = 0.026
    assert ev is not None
    assert abs(ev - 0.026) < 1e-6


def test_score_to_ev_fallback_to_global_for_unknown_strategy(tmp_path):
    """strategy_key 不在 per-strategy mapping → 退回 global bucket。"""
    picks = _make_picks(500, strategy="bias_convergence")
    outcomes = _make_outcomes(picks)
    mapping = s2e.build_score_to_ev_mapping(picks, outcomes)
    s2e.dump_mapping_to_csv(mapping, snapshot_dir=tmp_path)
    s2e.invalidate_cache()

    # 未知 strategy_key("foobar")— 落 global bucket
    ev_unknown = s2e.score_to_ev(0.7, strategy_key="foobar", snapshot_dir=tmp_path)
    ev_global = s2e.score_to_ev(0.7, snapshot_dir=tmp_path)
    assert ev_unknown is not None and ev_global is not None
    assert abs(ev_unknown - ev_global) < 1e-9  # 完全同 bucket


def test_score_to_ev_none_input():
    """score=None → 回 None(caller 拿來決定要不要顯示)。"""
    assert s2e.score_to_ev(None) is None
    assert s2e.score_to_ev(float("nan")) is None


def test_score_to_ev_for_pick_picks_largest_strategy(tmp_path):
    """matched_strategies 內第一個有 mapping 的策略被選中,不在 mapping 的略過。"""
    gap = _make_picks(200, strategy="gap_up")
    gap_out = _make_outcomes(gap, ev_per_prob=20.0)
    mapping = s2e.build_score_to_ev_mapping(gap, gap_out)
    s2e.dump_mapping_to_csv(mapping, snapshot_dir=tmp_path)
    s2e.invalidate_cache()

    # matched = [未知策略, gap_up] — 跳第一個拿 gap_up 的 mapping
    ev = s2e.score_to_ev_for_pick(
        0.8, matched_strategies=["unknown_strat", "gap_up"],
        snapshot_dir=tmp_path,
    )
    ev_direct = s2e.score_to_ev(0.8, strategy_key="gap_up", snapshot_dir=tmp_path)
    assert ev is not None and ev_direct is not None
    assert abs(ev - ev_direct) < 1e-9


def test_build_mapping_insufficient_samples():
    """< 30 樣本 → mapping 空(讓 caller 走 fallback)。"""
    picks = _make_picks(20)
    outcomes = _make_outcomes(picks)
    mapping = s2e.build_score_to_ev_mapping(picks, outcomes)
    assert mapping.empty


def test_build_mapping_strategy_below_100_skipped():
    """策略樣本 < 100 → 不算 per-strategy mapping,僅 global。"""
    picks = _make_picks(80, strategy="rare_strat")
    outcomes = _make_outcomes(picks)
    mapping = s2e.build_score_to_ev_mapping(picks, outcomes)
    assert not mapping.empty
    # 只有 global,沒有 rare_strat 的 per-strategy mapping
    assert mapping["strategy"].unique().tolist() == ["__global__"]


def test_score_to_ev_extreme_values(tmp_path):
    """score 超出訓練樣本範圍(< 0 或 > 1)→ 落入端點 bucket,不 crash。"""
    picks = _make_picks(500)
    outcomes = _make_outcomes(picks)
    mapping = s2e.build_score_to_ev_mapping(picks, outcomes)
    s2e.dump_mapping_to_csv(mapping, snapshot_dir=tmp_path)
    s2e.invalidate_cache()

    # score=-0.5(理論不可能,但 mapping 必須 graceful)
    ev_low = s2e.score_to_ev(-0.5, snapshot_dir=tmp_path)
    assert ev_low is not None  # 落第一 bucket(bucket_lo=-inf)

    # score=2.0(超出 [0,1])
    ev_high = s2e.score_to_ev(2.0, snapshot_dir=tmp_path)
    assert ev_high is not None  # 落最後 bucket(bucket_hi=+inf)


def test_dump_empty_mapping_writes_header_only(tmp_path):
    """空 mapping dump → CSV 只有 header(讓 reader graceful 走 fallback)。"""
    empty = pd.DataFrame(columns=["strategy", "bucket_lo", "bucket_hi", "avg_ev", "n_samples"])
    path = s2e.dump_mapping_to_csv(empty, snapshot_dir=tmp_path)
    assert path.exists()
    df = pd.read_csv(path)
    assert df.empty
    assert list(df.columns) == ["strategy", "bucket_lo", "bucket_hi", "avg_ev", "n_samples"]
