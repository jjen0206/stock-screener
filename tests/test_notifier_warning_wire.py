"""結構性 + 端到端 wire test for notifier × warnings_filter 接線
(2026-05-15 amendment:annotate-only,警示股仍在 picks,只是排序往後)。

涵蓋:
  1. 結構性 guard:_select_top_picks 確實 import + call annotate_warned_stocks
     和 apply_soft_warning_penalty(舊 exclude_warned_stocks 已移除,防 regression)
  2. 端到端:警示股仍出現在 picks 中(只是 ml_prob 被 × 0.3 / × 0.7 沉到後面),
     caption「⚠️ 推薦中含 N 檔警示股」出現,文案不能含「已濾掉」
"""
from __future__ import annotations

import inspect
import re

import pytest

from src import config, database as db
import src.notifier as notifier_mod
from src import warnings_filter as wf


# ============================================================================
# 結構性:notifier 確實 wire 警示股 helper
# ============================================================================

def test_notifier_imports_annotate_helpers():
    src = inspect.getsource(notifier_mod)
    assert re.search(
        r"from\s+src\.warnings_filter\s+import[^)]*annotate_warned_stocks",
        src, re.DOTALL,
    ), "notifier.py 必須 import annotate_warned_stocks"
    assert re.search(
        r"from\s+src\.warnings_filter\s+import[^)]*apply_soft_warning_penalty",
        src, re.DOTALL,
    ), "notifier.py 必須 import apply_soft_warning_penalty"


def test_notifier_does_not_use_old_exclude_api():
    """舊的 exclude_warned_stocks 已被 amendment 拿掉,notifier 不該再 call。
    若 regression 把它叫回來,違反「不替主公做隱藏決定」原則。"""
    src = inspect.getsource(notifier_mod)
    assert "exclude_warned_stocks" not in src, (
        "notifier.py 不該 call exclude_warned_stocks(已被 amendment 拿掉),"
        "改用 annotate_warned_stocks + soft penalty 讓主公自己看自己決定"
    )


def test_select_top_picks_calls_annotate():
    fn_src = inspect.getsource(notifier_mod._select_top_picks)
    assert "annotate_warned_stocks(" in fn_src, (
        "_select_top_picks 必須 call annotate_warned_stocks 注入 'warnings' 欄位"
    )


def test_select_top_picks_calls_soft_penalty():
    fn_src = inspect.getsource(notifier_mod._select_top_picks)
    assert "apply_soft_warning_penalty(" in fn_src, (
        "_select_top_picks 必須 call apply_soft_warning_penalty 做軟降權"
    )


def test_format_section_uses_warning_caption():
    """format_warning_caption 必須在 _format_short_picks_section 內被呼叫
    (確保 caption「⚠️ 推薦中含 N 檔警示股」會出現在訊息)。"""
    fn_src = inspect.getsource(notifier_mod._format_short_picks_section)
    assert "format_warning_caption(" in fn_src, (
        "_format_short_picks_section 必須 call format_warning_caption"
    )
    # 舊 caption 函式不該再被呼叫
    assert "format_excluded_caption" not in fn_src, (
        "format_excluded_caption 已 rename,別再用舊文案"
    )


def test_module_has_renamed_cache():
    """模組級 cache 已從 _LAST_EXCLUDED_WARNINGS rename 為 _LAST_ANNOTATED_WARNINGS。"""
    assert hasattr(notifier_mod, "_LAST_ANNOTATED_WARNINGS"), (
        "notifier 模組應有 _LAST_ANNOTATED_WARNINGS cache(annotate 結果)"
    )
    assert not hasattr(notifier_mod, "_LAST_EXCLUDED_WARNINGS"), (
        "_LAST_EXCLUDED_WARNINGS 已 rename 為 _LAST_ANNOTATED_WARNINGS"
    )


# ============================================================================
# 端到端:警示股仍在 picks + caption 出現
# ============================================================================

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "wire_test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    monkeypatch.setenv("WARNING_ANNOTATE_ENABLED", "true")
    yield db_file
    db._reset_path_cache()  # type: ignore[attr-defined]


def test_warned_pick_remains_in_picks_after_annotate(tmp_db):
    """違約股 annotate 後**仍在 picks**(主公規矩:不替他隱藏)。"""
    db.upsert_stock_warnings([
        {
            "stock_id": "9999", "warning_type": "default_settlement",
            "announced_date": "2026-05-12", "effective_to": None,
            "reason": "違約交割",
        },
    ])
    picks = [
        {"sid": "2330", "ml_prob": 0.7, "name": "台積電"},
        {"sid": "9999", "ml_prob": 0.6, "name": "違約檔"},
    ]
    with db.get_conn() as conn:
        annotated = wf.annotate_warned_stocks(conn, picks, as_of="2026-05-15")
    # 違約股仍在 picks(沒被剔)
    assert len(picks) == 2
    assert any(p["sid"] == "9999" for p in picks)
    # 但有 warnings 欄位
    by_sid = {p["sid"]: p for p in picks}
    assert "warnings" in by_sid["9999"]
    assert annotated == ["9999"]


def test_caption_format_uses_new_wording(tmp_db):
    """caption 用「⚠️ 推薦中含 N 檔警示股」文案,不能含「已濾掉」。"""
    db.upsert_stock_warnings([
        {"stock_id": "9999", "warning_type": "default_settlement",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [
        {
            "sid": "9999", "ml_prob": 0.6, "name": "違約檔",
            "rank": 1, "matched_strategies": ["volume_kd"],
        },
    ]
    with db.get_conn() as conn:
        wf.annotate_warned_stocks(conn, picks, as_of="2026-05-15")

    msg = notifier_mod._format_short_picks_section(
        picks, channel="telegram", is_fallback=False,
    )
    assert "⚠️" in msg
    assert "推薦中含" in msg
    assert "1" in msg
    assert "違約交割" in msg
    assert "已濾掉" not in msg, (
        "caption 文案不該含「已濾掉」— 主公規矩:不替他隱藏決定"
    )
    assert "主公自行判斷" in msg


def test_caption_skipped_when_no_warned_picks(tmp_db):
    """所有 picks 都乾淨 → caption 不該出現(graceful skip)。"""
    picks = [
        {
            "sid": "2330", "ml_prob": 0.7, "name": "台積電",
            "rank": 1, "matched_strategies": ["volume_kd"],
        },
    ]
    msg = notifier_mod._format_short_picks_section(
        picks, channel="telegram", is_fallback=False,
    )
    assert "推薦中含" not in msg
    assert "警示股" not in msg


def test_severe_penalty_drops_pick_to_bottom(tmp_db):
    """違約股 ml_prob 應被 × 0.3 沉到後面(原本 0.8 > 0.5,降權後 0.24 < 0.5)。"""
    db.upsert_stock_warnings([
        {"stock_id": "9999", "warning_type": "default_settlement",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [
        {"sid": "2330", "ml_prob": 0.5, "matched_strategies": ["volume_kd"]},
        {"sid": "9999", "ml_prob": 0.8, "matched_strategies": ["volume_kd"]},
    ]
    with db.get_conn() as conn:
        wf.annotate_warned_stocks(conn, picks, as_of="2026-05-15")
        wf.apply_soft_warning_penalty(conn, picks, as_of="2026-05-15")

    # ml_prob 應被改 × 0.3
    by_sid = {p["sid"]: p for p in picks}
    assert abs(by_sid["9999"]["ml_prob"] - 0.24) < 1e-6
    assert by_sid["2330"]["ml_prob"] == 0.5

    # 排序後 2330 應排第一(0.5 > 0.24)
    picks.sort(key=lambda p: notifier_mod._compute_pick_score(
        sid=p["sid"], ml_prob=p["ml_prob"],
        matched_strategies=p["matched_strategies"],
    ))
    assert picks[0]["sid"] == "2330"
    assert picks[1]["sid"] == "9999"
    # 違約股仍在 picks(只是排到最後)— 主公自己看
    assert len(picks) == 2


def test_soft_penalty_uses_07_in_pipeline(tmp_db):
    """注意股 → ml_prob × 0.7 一般等級降權。"""
    db.upsert_stock_warnings([
        {"stock_id": "6666", "warning_type": "attention",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [
        {"sid": "2330", "ml_prob": 0.55, "matched_strategies": ["volume_kd"]},
        {"sid": "6666", "ml_prob": 0.60, "matched_strategies": ["volume_kd"]},
    ]
    with db.get_conn() as conn:
        wf.apply_soft_warning_penalty(conn, picks, as_of="2026-05-15")
    by_sid = {p["sid"]: p for p in picks}
    # 0.6 × 0.7 = 0.42(SOFT,不是 SEVERE 的 × 0.3 = 0.18)
    assert abs(by_sid["6666"]["ml_prob"] - 0.42) < 1e-6
    assert by_sid["6666"]["warning_penalty_tier"] == "soft"


def test_kill_switch_skips_filter_in_pipeline(tmp_db, monkeypatch):
    """WARNING_ANNOTATE_ENABLED=false → annotate / penalty 全跳過。"""
    monkeypatch.setenv("WARNING_ANNOTATE_ENABLED", "false")
    db.upsert_stock_warnings([
        {"stock_id": "9999", "warning_type": "default_settlement",
         "announced_date": "2026-05-12", "effective_to": None},
    ])
    picks = [{"sid": "9999", "ml_prob": 0.5}]
    with db.get_conn() as conn:
        annotated = wf.annotate_warned_stocks(conn, picks, as_of="2026-05-15")
        penalized = wf.apply_soft_warning_penalty(
            conn, picks, as_of="2026-05-15",
        )
    assert annotated == []
    assert penalized == []
    # picks 完全不變
    assert "warnings" not in picks[0]
    assert picks[0]["ml_prob"] == 0.5
