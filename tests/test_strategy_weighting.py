"""src/strategy_weighting.py + _compute_pick_score 動態權重單元測試。

production schema fixture(tmp DB + db.init_db),不 mock streamlit。
測試重點(8 case):
  - 高 WR 策略 weight > 1
  - 低 WR 策略 weight < 1
  - N < MIN_N → weight = 1.0(資料不足保守)
  - clamp 上下限(極端 WR 不超出 [0.5, 1.5])
  - 同 ml_prob 不同 weight → 高 weight 排前
  - 沒 matched_strategies → graceful 走 weight=1.0
  - strategy_weights 為空 → 退回 phase 1 純 ml_prob 排序
  - analyst 加分仍排前(weight 不蓋過 analyst priority)
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src import config, database as db, notifier
from src.strategy_weighting import (
    DEFAULT_WEIGHT,
    MAX_WEIGHT,
    MIN_N,
    MIN_WEIGHT,
    get_strategy_weights_30d,
)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """乾淨 tmp SQLite + production schema(init_db),不 mock streamlit。"""
    db_file = tmp_path / "weighting.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    yield db_file
    db._reset_path_cache()  # type: ignore[attr-defined]


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _seed(strategy: str, pick_date: str, hits: list[int]) -> None:
    """灌 pick_outcomes,每筆 sid 用 idx 區分,hit_target 走 0/1。"""
    rows = [
        (pick_date, f"99{i:02d}", strategy, 100.0, 0.0, float(h), _today())
        for i, h in enumerate(hits)
    ]
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO pick_outcomes "
            "(pick_date, sid, strategy, entry_close, return_d1, "
            "hit_target, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


# === get_strategy_weights_30d ===

def test_high_wr_gives_weight_above_one(tmp_db):
    """灌 35 筆 hit_target=1 共 28 筆(WR=80%)→ weight = 80/50 = 1.6 → clamp 到 1.5。"""
    _seed("strategy_hot", _days_ago(5), [1] * 28 + [0] * 7)
    with db.get_conn() as conn:
        w = get_strategy_weights_30d(conn)
    assert "strategy_hot" in w
    assert w["strategy_hot"] > 1.0
    assert w["strategy_hot"] == MAX_WEIGHT  # 80% → 1.6 clamp 到 1.5


def test_low_wr_gives_weight_below_one(tmp_db):
    """灌 40 筆 hit_target=1 共 12 筆(WR=30%)→ weight = 30/50 = 0.6。"""
    _seed("strategy_cold", _days_ago(5), [1] * 12 + [0] * 28)
    with db.get_conn() as conn:
        w = get_strategy_weights_30d(conn)
    assert "strategy_cold" in w
    assert w["strategy_cold"] < 1.0
    assert abs(w["strategy_cold"] - 0.6) < 1e-6


def test_insufficient_samples_returns_default(tmp_db):
    """N < MIN_N(29 筆,即便 WR=90%)→ weight = 1.0(資料不足保守)。"""
    n = MIN_N - 1
    _seed("strategy_new", _days_ago(5), [1] * int(n * 0.9) + [0] * (n - int(n * 0.9)))
    with db.get_conn() as conn:
        w = get_strategy_weights_30d(conn)
    assert w["strategy_new"] == DEFAULT_WEIGHT


def test_clamp_lower_bound(tmp_db):
    """極端低 WR(0%)→ weight = 0/0.5 = 0 → clamp 到 MIN_WEIGHT=0.5。"""
    _seed("strategy_zero", _days_ago(5), [0] * 40)
    with db.get_conn() as conn:
        w = get_strategy_weights_30d(conn)
    assert w["strategy_zero"] == MIN_WEIGHT


def test_empty_outcomes_returns_empty_dict(tmp_db):
    """pick_outcomes 完全空 → 空 dict(caller 對 missing key 預設 1.0)。"""
    with db.get_conn() as conn:
        w = get_strategy_weights_30d(conn)
    assert w == {}


# === _compute_pick_score(notifier wire) ===

def test_score_higher_weight_ranks_first():
    """同 ml_prob,不同 weight → 高 weight 排前面(score tuple 小者前)。"""
    weights = {"strategy_hot": 1.5, "strategy_cold": 0.5}
    score_hot = notifier._compute_pick_score(
        sid="2330", ml_prob=0.6,
        matched_strategies=["strategy_hot"],
        strategy_weights=weights,
    )
    score_cold = notifier._compute_pick_score(
        sid="2454", ml_prob=0.6,
        matched_strategies=["strategy_cold"],
        strategy_weights=weights,
    )
    assert score_hot < score_cold  # tuple ascending → hot 排前


def test_score_empty_matched_strategies_graceful():
    """空 matched_strategies → avg_w 退 1.0,不該炸 ZeroDivisionError。"""
    score = notifier._compute_pick_score(
        sid="2330", ml_prob=0.6,
        matched_strategies=[],
        strategy_weights={"x": 1.5},
    )
    # 同空 weights 對照
    score_no_w = notifier._compute_pick_score(
        sid="2330", ml_prob=0.6,
        matched_strategies=[],
        strategy_weights=None,
    )
    assert score == score_no_w  # 兩者皆退化到 ml_prob × 1.0


def test_score_no_weights_falls_back_to_legacy():
    """strategy_weights=None → 等效於 phase 1 純 ml_prob 排序。"""
    score_new = notifier._compute_pick_score(
        sid="2330", ml_prob=0.6,
        matched_strategies=["s1", "s2"],
        strategy_weights=None,
    )
    # 手算 legacy:(0, -0.6, -2, "2330")
    assert score_new == (0, -0.6, -2, "2330")


def test_analyst_priority_overrides_weight():
    """analyst_target_mean 有值 → -100 永遠排在無 analyst 前,即便對方 weight 高。"""
    weights = {"strategy_hot": 1.5}
    score_analyst = notifier._compute_pick_score(
        sid="2330", ml_prob=0.4,
        matched_strategies=["whatever"],  # weight=1.0(missing key)
        analyst_target_mean=100.0,
        strategy_weights=weights,
    )
    score_high_weight = notifier._compute_pick_score(
        sid="2454", ml_prob=0.9,
        matched_strategies=["strategy_hot"],
        analyst_target_mean=None,
        strategy_weights=weights,
    )
    assert score_analyst < score_high_weight  # analyst 永遠排前
