"""src/warnings_filter.py 單元測試。

涵蓋:
  - exclude_warned_stocks 硬擋(default + full_cash)— kept/excluded 分離正確
  - 過期 warning(effective_to < as_of)不該擋
  - apply_soft_warning_penalty soft 降權 — ml_prob × 0.7 對
  - format_excluded_caption 字串組成
  - kill-switch WARNING_FILTER_ENABLED=false 跳過所有過濾
"""
from __future__ import annotations

import pytest

from src import config, database as db
from src import warnings_filter as wf


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """每個測試一份乾淨 DB(用 production schema,不自編 CREATE TABLE)。"""
    db_file = tmp_path / "wf_test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    yield db_file
    db._reset_path_cache()  # type: ignore[attr-defined]


@pytest.fixture
def enable_filter(monkeypatch):
    """確保測試時 WARNING_FILTER_ENABLED 為 true(避免 CI env 影響)。"""
    monkeypatch.setenv("WARNING_FILTER_ENABLED", "true")


def _seed_warnings(rows: list[dict]) -> None:
    """測試 helper:把警示 rows 灌進 stock_warnings 表。"""
    db.upsert_stock_warnings(rows)


# ============================================================================
# exclude_warned_stocks — 硬擋
# ============================================================================

def test_exclude_default_settlement_kept_excluded_split(tmp_db, enable_filter):
    """違約交割股應被剔除,正常股留下。"""
    _seed_warnings([
        {
            "stock_id": "9999",
            "warning_type": "default_settlement",
            "announced_date": "2026-05-12",
            "effective_to": None,
            "reason": "違約交割 NT$1,000,000",
        },
    ])
    picks = [
        {"sid": "2330", "ml_prob": 0.7},
        {"sid": "9999", "ml_prob": 0.6},  # 違約交割,應被剔
        {"sid": "0050", "ml_prob": 0.55},
    ]
    with db.get_conn() as conn:
        kept, excluded = wf.exclude_warned_stocks(
            conn, picks, as_of="2026-05-15",
        )
    assert [p["sid"] for p in kept] == ["2330", "0050"]
    assert len(excluded) == 1
    assert excluded[0]["sid"] == "9999"
    assert excluded[0]["warning_type"] == "default_settlement"
    assert "違約" in excluded[0]["warning_reason"]


def test_exclude_full_cash_default(tmp_db, enable_filter):
    """全額交割股應被預設硬擋 (full_cash 在 HARD_EXCLUDE_TYPES 內)。"""
    _seed_warnings([
        {
            "stock_id": "8888",
            "warning_type": "full_cash",
            "announced_date": "2026-05-10",
            "effective_to": None,
            "reason": "變更交易方法為全額交割",
        },
    ])
    picks = [{"sid": "8888", "ml_prob": 0.8}]
    with db.get_conn() as conn:
        kept, excluded = wf.exclude_warned_stocks(
            conn, picks, as_of="2026-05-15",
        )
    assert kept == []
    assert len(excluded) == 1
    assert excluded[0]["warning_type"] == "full_cash"


def test_expired_warning_does_not_exclude(tmp_db, enable_filter):
    """effective_to 在 as_of 之前的警示視為已解除,不該擋。"""
    _seed_warnings([
        {
            "stock_id": "7777",
            "warning_type": "default_settlement",
            "announced_date": "2026-04-01",
            "effective_to": "2026-04-30",  # 已過期
            "reason": "違約紀錄",
        },
    ])
    picks = [{"sid": "7777", "ml_prob": 0.6}]
    with db.get_conn() as conn:
        kept, excluded = wf.exclude_warned_stocks(
            conn, picks, as_of="2026-05-15",
        )
    assert [p["sid"] for p in kept] == ["7777"]
    assert excluded == []


def test_attention_not_in_default_hard_exclude(tmp_db, enable_filter):
    """attention 預設 NOT 在 HARD_EXCLUDE_TYPES,只會 soft 降權不該擋。"""
    _seed_warnings([
        {
            "stock_id": "6666",
            "warning_type": "attention",
            "announced_date": "2026-05-12",
            "effective_to": None,
            "reason": "週轉率異常",
        },
    ])
    picks = [{"sid": "6666", "ml_prob": 0.6}]
    with db.get_conn() as conn:
        kept, excluded = wf.exclude_warned_stocks(
            conn, picks, as_of="2026-05-15",
        )
    assert [p["sid"] for p in kept] == ["6666"]
    assert excluded == []


def test_custom_warning_types_override_default(tmp_db, enable_filter):
    """caller 傳 warning_types 覆蓋 default — 可額外把 attention 也硬擋。"""
    _seed_warnings([
        {
            "stock_id": "6666",
            "warning_type": "attention",
            "announced_date": "2026-05-12",
            "effective_to": None,
        },
    ])
    picks = [{"sid": "6666", "ml_prob": 0.6}]
    with db.get_conn() as conn:
        kept, excluded = wf.exclude_warned_stocks(
            conn, picks, warning_types=["attention"], as_of="2026-05-15",
        )
    assert kept == []
    assert excluded[0]["warning_type"] == "attention"


def test_empty_picks_returns_empty(tmp_db, enable_filter):
    with db.get_conn() as conn:
        kept, excluded = wf.exclude_warned_stocks(conn, [])
    assert kept == [] and excluded == []


# ============================================================================
# apply_soft_warning_penalty — ml_prob × 0.7
# ============================================================================

def test_soft_penalty_multiplies_ml_prob(tmp_db, enable_filter):
    """attention 命中 → ml_prob × 0.7 (預設 multiplier)。"""
    _seed_warnings([
        {
            "stock_id": "6666",
            "warning_type": "attention",
            "announced_date": "2026-05-12",
            "effective_to": None,
        },
    ])
    picks = [
        {"sid": "2330", "ml_prob": 0.7},  # 沒警示
        {"sid": "6666", "ml_prob": 0.5},  # 注意股 → 0.5 × 0.7 = 0.35
    ]
    with db.get_conn() as conn:
        penalized = wf.apply_soft_warning_penalty(
            conn, picks, as_of="2026-05-15",
        )
    assert "6666" in penalized
    # 6666 的 ml_prob 應 = 0.5 × 0.7 = 0.35
    by_sid = {p["sid"]: p for p in picks}
    assert by_sid["2330"]["ml_prob"] == 0.7  # 沒被改
    assert abs(by_sid["6666"]["ml_prob"] - 0.35) < 1e-6
    assert by_sid["6666"].get("soft_warning_penalty_applied") is True
    assert "attention" in by_sid["6666"]["soft_warning_types"]


def test_soft_penalty_custom_multiplier(tmp_db, enable_filter):
    _seed_warnings([
        {
            "stock_id": "6666", "warning_type": "disposition",
            "announced_date": "2026-05-12", "effective_to": None,
        },
    ])
    picks = [{"sid": "6666", "ml_prob": 0.8}]
    with db.get_conn() as conn:
        wf.apply_soft_warning_penalty(
            conn, picks, multiplier=0.5, as_of="2026-05-15",
        )
    assert abs(picks[0]["ml_prob"] - 0.4) < 1e-6


def test_soft_penalty_skips_default_settlement(tmp_db, enable_filter):
    """default_settlement 在 HARD_EXCLUDE_TYPES,理論上應已被 exclude 剔掉,
    但 soft penalty 自己也不該誤把它列入(SOFT_PENALTY_TYPES 不含)。
    """
    _seed_warnings([
        {
            "stock_id": "9999", "warning_type": "default_settlement",
            "announced_date": "2026-05-12", "effective_to": None,
        },
    ])
    picks = [{"sid": "9999", "ml_prob": 0.6}]
    with db.get_conn() as conn:
        penalized = wf.apply_soft_warning_penalty(
            conn, picks, as_of="2026-05-15",
        )
    assert penalized == []
    assert picks[0]["ml_prob"] == 0.6  # 沒被改


def test_soft_penalty_handles_none_ml_prob(tmp_db, enable_filter):
    """ml_prob=None 時不該爆,但仍標記 soft_warning_types。"""
    _seed_warnings([
        {
            "stock_id": "6666", "warning_type": "attention",
            "announced_date": "2026-05-12", "effective_to": None,
        },
    ])
    picks = [{"sid": "6666", "ml_prob": None}]
    with db.get_conn() as conn:
        penalized = wf.apply_soft_warning_penalty(
            conn, picks, as_of="2026-05-15",
        )
    assert "6666" in penalized
    assert picks[0]["ml_prob"] is None  # 不該被改成 0
    assert "attention" in picks[0]["soft_warning_types"]


# ============================================================================
# format_excluded_caption
# ============================================================================

def test_format_caption_empty():
    assert wf.format_excluded_caption([]) == ""


def test_format_caption_with_counts():
    excluded = [
        {"sid": "9999", "warning_type": "default_settlement"},
        {"sid": "8888", "warning_type": "default_settlement"},
        {"sid": "7777", "warning_type": "full_cash"},
    ]
    caption = wf.format_excluded_caption(excluded)
    assert "✅" in caption
    assert "3" in caption
    assert "違約交割2" in caption
    assert "全額交割1" in caption


# ============================================================================
# Kill-switch
# ============================================================================

def test_kill_switch_disables_exclude(tmp_db, monkeypatch):
    """WARNING_FILTER_ENABLED=false → exclude_warned_stocks no-op pass-through。"""
    monkeypatch.setenv("WARNING_FILTER_ENABLED", "false")
    _seed_warnings([
        {
            "stock_id": "9999", "warning_type": "default_settlement",
            "announced_date": "2026-05-12", "effective_to": None,
        },
    ])
    picks = [{"sid": "9999", "ml_prob": 0.5}]
    with db.get_conn() as conn:
        kept, excluded = wf.exclude_warned_stocks(
            conn, picks, as_of="2026-05-15",
        )
    # kill-switch on → 違約股被放行
    assert [p["sid"] for p in kept] == ["9999"]
    assert excluded == []


def test_kill_switch_disables_soft_penalty(tmp_db, monkeypatch):
    """WARNING_FILTER_ENABLED=false → apply_soft_warning_penalty no-op。"""
    monkeypatch.setenv("WARNING_FILTER_ENABLED", "false")
    _seed_warnings([
        {
            "stock_id": "6666", "warning_type": "attention",
            "announced_date": "2026-05-12", "effective_to": None,
        },
    ])
    picks = [{"sid": "6666", "ml_prob": 0.5}]
    with db.get_conn() as conn:
        penalized = wf.apply_soft_warning_penalty(
            conn, picks, as_of="2026-05-15",
        )
    assert penalized == []
    assert picks[0]["ml_prob"] == 0.5  # 沒被乘 0.7


# ============================================================================
# Schema 對齊 production
# ============================================================================

def test_db_get_active_warnings_helper_works(tmp_db):
    """db.get_active_warnings_for_sids 對齊 _query_active_warnings 結果。"""
    _seed_warnings([
        {
            "stock_id": "1234", "warning_type": "default_settlement",
            "announced_date": "2026-05-12", "effective_to": None,
        },
        {
            "stock_id": "5678", "warning_type": "attention",
            "announced_date": "2026-05-13", "effective_to": "2026-05-20",
        },
    ])
    result = db.get_active_warnings_for_sids(
        ["1234", "5678", "9999"], as_of="2026-05-15",
    )
    assert "1234" in result
    assert "5678" in result
    assert "9999" not in result
    # 過濾 type
    only_default = db.get_active_warnings_for_sids(
        ["1234", "5678"], warning_types=["default_settlement"],
        as_of="2026-05-15",
    )
    assert "1234" in only_default
    assert "5678" not in only_default
