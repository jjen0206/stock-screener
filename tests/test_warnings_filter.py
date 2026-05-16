"""src/warnings_filter.py 單元測試(2026-05-15 amendment:annotate-only,
不 hard exclude,軍師不替主公做隱藏決定)。

涵蓋:
  - annotate_warned_stocks 注 'warnings' 欄位但**不過濾**
  - apply_soft_warning_penalty:SEVERE × 0.3 / SOFT × 0.7;同 pick 兩類取 SEVERE
  - 過期 warning(effective_to < as_of)不該被 annotate
  - format_warning_caption 文案「⚠️ 推薦中含 N 檔警示股」(不能出現「已濾掉」)
  - kill-switch WARNING_ANNOTATE_ENABLED=false
  - 舊 hard-exclude API 已移除(防 regression)
"""
from __future__ import annotations

import pytest

from src import database as db
from src import warnings_filter as wf

# tmp_db fixture 共用 tests/conftest.py


@pytest.fixture
def enable_filter(monkeypatch):
    """確保測試時 WARNING_ANNOTATE_ENABLED 為 true(避免 CI env 影響)。"""
    monkeypatch.setenv("WARNING_ANNOTATE_ENABLED", "true")


def _seed_warnings(rows: list[dict]) -> None:
    db.upsert_stock_warnings(rows)


# ============================================================================
# annotate_warned_stocks — 標註但不過濾
# ============================================================================

def test_annotate_does_not_filter_picks(tmp_db, enable_filter):
    """違約交割股應該**仍在 picks 中**(只是 'warnings' 欄位被注入),
    主公自己看到自己決定。
    """
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
        {"sid": "9999", "ml_prob": 0.6},  # 違約 — 仍應在 picks
        {"sid": "0050", "ml_prob": 0.55},
    ]
    with db.get_conn() as conn:
        annotated = wf.annotate_warned_stocks(conn, picks, as_of="2026-05-15")
    # picks 數量不變,違約股仍在
    assert [p["sid"] for p in picks] == ["2330", "9999", "0050"]
    # annotate 回傳被標註的 sid
    assert annotated == ["9999"]
    # 9999 多了 'warnings' 欄位
    by_sid = {p["sid"]: p for p in picks}
    assert "warnings" in by_sid["9999"]
    assert by_sid["9999"]["warnings"][0]["warning_type"] == "default_settlement"
    # 沒命中的乾淨股不該有 warnings 欄位
    assert "warnings" not in by_sid["2330"]
    assert "warnings" not in by_sid["0050"]


def test_annotate_includes_all_warning_types(tmp_db, enable_filter):
    """annotate 應涵蓋全部 5 類(SEVERE + SOFT),不只硬擋的兩類。"""
    _seed_warnings([
        {"stock_id": "1111", "warning_type": "default_settlement",
         "announced_date": "2026-05-12", "effective_to": None},
        {"stock_id": "2222", "warning_type": "full_cash",
         "announced_date": "2026-05-12", "effective_to": None},
        {"stock_id": "3333", "warning_type": "attention",
         "announced_date": "2026-05-12", "effective_to": None},
        {"stock_id": "4444", "warning_type": "disposition",
         "announced_date": "2026-05-12", "effective_to": None},
        {"stock_id": "5555", "warning_type": "method_changed",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [{"sid": s} for s in ("1111", "2222", "3333", "4444", "5555")]
    with db.get_conn() as conn:
        annotated = wf.annotate_warned_stocks(conn, picks, as_of="2026-05-15")
    assert sorted(annotated) == ["1111", "2222", "3333", "4444", "5555"]
    for p in picks:
        assert "warnings" in p


def test_expired_warning_not_annotated(tmp_db, enable_filter):
    """effective_to < as_of 視為已解除,不該被 annotate。"""
    _seed_warnings([
        {
            "stock_id": "7777", "warning_type": "default_settlement",
            "announced_date": "2026-04-01", "effective_to": "2026-04-30",
        },
    ])
    picks = [{"sid": "7777", "ml_prob": 0.6}]
    with db.get_conn() as conn:
        annotated = wf.annotate_warned_stocks(conn, picks, as_of="2026-05-15")
    assert annotated == []
    assert "warnings" not in picks[0]


def test_annotate_empty_picks_returns_empty(tmp_db, enable_filter):
    with db.get_conn() as conn:
        assert wf.annotate_warned_stocks(conn, []) == []


# ============================================================================
# apply_soft_warning_penalty — SEVERE × 0.3 / SOFT × 0.7
# ============================================================================

def test_severe_penalty_uses_03_multiplier(tmp_db, enable_filter):
    """default_settlement → ml_prob × 0.3(嚴重等級)。"""
    _seed_warnings([
        {"stock_id": "9999", "warning_type": "default_settlement",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [{"sid": "9999", "ml_prob": 0.8}]
    with db.get_conn() as conn:
        penalized = wf.apply_soft_warning_penalty(
            conn, picks, as_of="2026-05-15",
        )
    assert penalized == ["9999"]
    # 0.8 × 0.3 = 0.24
    assert abs(picks[0]["ml_prob"] - 0.24) < 1e-6
    assert picks[0]["warning_penalty_tier"] == "severe"
    assert picks[0]["warning_penalty_multiplier"] == pytest.approx(0.3)
    assert "default_settlement" in picks[0]["warning_types"]


def test_full_cash_uses_severe_multiplier(tmp_db, enable_filter):
    """full_cash 屬 SEVERE,× 0.3 不是 0.7。"""
    _seed_warnings([
        {"stock_id": "8888", "warning_type": "full_cash",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [{"sid": "8888", "ml_prob": 0.9}]
    with db.get_conn() as conn:
        wf.apply_soft_warning_penalty(conn, picks, as_of="2026-05-15")
    assert abs(picks[0]["ml_prob"] - 0.27) < 1e-6  # 0.9 × 0.3
    assert picks[0]["warning_penalty_tier"] == "severe"


def test_soft_penalty_uses_07_multiplier(tmp_db, enable_filter):
    """attention / disposition / method_changed → ml_prob × 0.7(一般等級)。"""
    _seed_warnings([
        {"stock_id": "6666", "warning_type": "attention",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [{"sid": "6666", "ml_prob": 0.5}]
    with db.get_conn() as conn:
        wf.apply_soft_warning_penalty(conn, picks, as_of="2026-05-15")
    assert abs(picks[0]["ml_prob"] - 0.35) < 1e-6  # 0.5 × 0.7
    assert picks[0]["warning_penalty_tier"] == "soft"


def test_mixed_warnings_use_severe_multiplier(tmp_db, enable_filter):
    """同 pick 同時命中 SEVERE + SOFT → 取 SEVERE multiplier(避免被稀釋)。"""
    _seed_warnings([
        {"stock_id": "5555", "warning_type": "default_settlement",
         "announced_date": "2026-05-10", "effective_to": None},
        {"stock_id": "5555", "warning_type": "attention",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [{"sid": "5555", "ml_prob": 0.8}]
    with db.get_conn() as conn:
        wf.apply_soft_warning_penalty(conn, picks, as_of="2026-05-15")
    # SEVERE × 0.3 應該套用,不是 SOFT × 0.7
    assert abs(picks[0]["ml_prob"] - 0.24) < 1e-6
    assert picks[0]["warning_penalty_tier"] == "severe"
    # warning_types 應該含兩類
    assert "default_settlement" in picks[0]["warning_types"]
    assert "attention" in picks[0]["warning_types"]


def test_custom_multipliers_override_default(tmp_db, enable_filter):
    _seed_warnings([
        {"stock_id": "1234", "warning_type": "default_settlement",
         "announced_date": "2026-05-12", "effective_to": None},
        {"stock_id": "5678", "warning_type": "attention",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [
        {"sid": "1234", "ml_prob": 1.0},
        {"sid": "5678", "ml_prob": 1.0},
    ]
    with db.get_conn() as conn:
        wf.apply_soft_warning_penalty(
            conn, picks, as_of="2026-05-15",
            severe_multiplier=0.1, soft_multiplier=0.5,
        )
    assert abs(picks[0]["ml_prob"] - 0.1) < 1e-6
    assert abs(picks[1]["ml_prob"] - 0.5) < 1e-6


def test_penalty_handles_none_ml_prob(tmp_db, enable_filter):
    """ml_prob=None 時不該爆,但仍記錄 tier / types。"""
    _seed_warnings([
        {"stock_id": "6666", "warning_type": "attention",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [{"sid": "6666", "ml_prob": None}]
    with db.get_conn() as conn:
        penalized = wf.apply_soft_warning_penalty(
            conn, picks, as_of="2026-05-15",
        )
    assert "6666" in penalized
    assert picks[0]["ml_prob"] is None  # 不該被改成 0
    assert picks[0]["warning_penalty_tier"] == "soft"


# ============================================================================
# format_warning_caption — 新文案「⚠️ 推薦中含 N 檔警示股」
# ============================================================================

def test_caption_empty_returns_empty():
    assert wf.format_warning_caption([]) == ""


def test_caption_no_warnings_field_returns_empty():
    """picks 都沒 warnings 欄位 → 回空(graceful skip)。"""
    picks = [{"sid": "2330", "ml_prob": 0.7}]
    assert wf.format_warning_caption(picks) == ""


def test_caption_does_not_say_filtered_out():
    """文案絕對不能含「已濾掉」之類代主公決定的措辭(主公規矩)。"""
    annotated = [
        {"sid": "9999", "warnings": [{"warning_type": "default_settlement"}]},
    ]
    cap = wf.format_warning_caption(annotated)
    assert "已濾掉" not in cap
    assert "已過濾" not in cap
    assert "filter" not in cap.lower()


def test_caption_format_with_severe_and_soft():
    """文案應 = 「⚠️ 推薦中含 N 檔警示股 (違約X 全額Y 注意Z ...)」順序 SEVERE 先。"""
    annotated = [
        {"sid": "9999", "warnings": [{"warning_type": "default_settlement"}]},
        {"sid": "8888", "warnings": [{"warning_type": "default_settlement"}]},
        {"sid": "7777", "warnings": [{"warning_type": "full_cash"}]},
        {"sid": "6666", "warnings": [{"warning_type": "attention"}]},
    ]
    cap = wf.format_warning_caption(annotated)
    assert "⚠️" in cap
    assert "推薦中含 4 檔警示股" in cap
    assert "違約交割2" in cap
    assert "全額交割1" in cap
    assert "注意股1" in cap
    # SEVERE 出現在 SOFT 之前
    assert cap.index("違約交割") < cap.index("注意股")
    # 結尾應提示主公自己判斷
    assert "主公自行判斷" in cap


def test_caption_unique_sid_count(tmp_db):
    """同 sid 兩個 warning_type 也只算 1 檔(unique sid 數),不重複計數。"""
    annotated = [
        {
            "sid": "5555",
            "warnings": [
                {"warning_type": "default_settlement"},
                {"warning_type": "attention"},
            ],
        },
    ]
    cap = wf.format_warning_caption(annotated)
    # 只有 1 檔 unique sid
    assert "推薦中含 1 檔警示股" in cap
    # 但兩類別仍各算 1 次顯在 parens
    assert "違約交割1" in cap
    assert "注意股1" in cap


# ============================================================================
# Kill-switch
# ============================================================================

def test_kill_switch_disables_annotate(tmp_db, monkeypatch):
    """WARNING_ANNOTATE_ENABLED=false → annotate no-op。"""
    monkeypatch.setenv("WARNING_ANNOTATE_ENABLED", "false")
    _seed_warnings([
        {"stock_id": "9999", "warning_type": "default_settlement",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [{"sid": "9999", "ml_prob": 0.5}]
    with db.get_conn() as conn:
        annotated = wf.annotate_warned_stocks(conn, picks, as_of="2026-05-15")
    assert annotated == []
    assert "warnings" not in picks[0]


def test_kill_switch_disables_penalty(tmp_db, monkeypatch):
    """WARNING_ANNOTATE_ENABLED=false → penalty no-op,ml_prob 不變。"""
    monkeypatch.setenv("WARNING_ANNOTATE_ENABLED", "false")
    _seed_warnings([
        {"stock_id": "9999", "warning_type": "default_settlement",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [{"sid": "9999", "ml_prob": 0.5}]
    with db.get_conn() as conn:
        penalized = wf.apply_soft_warning_penalty(
            conn, picks, as_of="2026-05-15",
        )
    assert penalized == []
    assert picks[0]["ml_prob"] == 0.5


# ============================================================================
# Regression guard:舊 hard-exclude API 已移除
# ============================================================================

def test_old_exclude_api_removed():
    """舊的 exclude_warned_stocks 已被 amendment 拿掉,不該再存在
    (避免有人不小心 import 舊 API 寫出 hard exclude 行為)。"""
    assert not hasattr(wf, "exclude_warned_stocks"), (
        "exclude_warned_stocks 已被 amendment 拿掉 — 軍師不替主公做隱藏決定。"
        "如要過濾請改用 annotate_warned_stocks + UI badge。"
    )
    assert not hasattr(wf, "HARD_EXCLUDE_TYPES"), (
        "HARD_EXCLUDE_TYPES 已被 amendment 拿掉,改用 SEVERE_PENALTY_TYPES + "
        "SOFT_PENALTY_TYPES 表示嚴重等級(全 soft,不 hard exclude)"
    )
    assert not hasattr(wf, "format_excluded_caption"), (
        "format_excluded_caption 已 rename 為 format_warning_caption,"
        "舊文案「已濾掉」會誤導主公以為 picks 被過濾"
    )


# ============================================================================
# Schema 對齊 production
# ============================================================================

def test_db_get_active_warnings_helper_works(tmp_db):
    """db.get_active_warnings_for_sids 回 dict 格式對。"""
    _seed_warnings([
        {"stock_id": "1234", "warning_type": "default_settlement",
         "announced_date": "2026-05-12", "effective_to": None},
        {"stock_id": "5678", "warning_type": "attention",
         "announced_date": "2026-05-13", "effective_to": "2026-05-20"},
    ])
    result = db.get_active_warnings_for_sids(
        ["1234", "5678", "9999"], as_of="2026-05-15",
    )
    assert "1234" in result
    assert "5678" in result
    assert "9999" not in result
    only_default = db.get_active_warnings_for_sids(
        ["1234", "5678"], warning_types=["default_settlement"],
        as_of="2026-05-15",
    )
    assert "1234" in only_default
    assert "5678" not in only_default
