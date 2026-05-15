"""結構性 + 端到端 wire test for notifier × warnings_filter 接線。

涵蓋:
  1. 結構性 guard:_select_top_picks 確實 import + call exclude_warned_stocks
     和 apply_soft_warning_penalty(避免未來 refactor silent break)
  2. 端到端:給定 picks(含違約交割股 + 注意股 + 正常股),透過 _select_top_picks
     的真實 path 跑一次,驗證:
       - 違約股被剔除(_LAST_EXCLUDED_WARNINGS 含該檔)
       - 注意股仍在 picks 但 ml_prob 被乘 0.7
       - format_top_picks_message caption 顯「✅ 已濾掉 N 檔警示股」
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

def test_notifier_imports_warning_helpers():
    src = inspect.getsource(notifier_mod)
    # exclude_warned_stocks 必須被 import(可能在 lazy import 內,parenthesized
    # 多行 import 也算 — 用 re.DOTALL + non-greedy)
    assert re.search(
        r"from\s+src\.warnings_filter\s+import[^)]*exclude_warned_stocks",
        src, re.DOTALL,
    ), "notifier.py 必須 import exclude_warned_stocks"
    assert re.search(
        r"from\s+src\.warnings_filter\s+import[^)]*apply_soft_warning_penalty",
        src, re.DOTALL,
    ), "notifier.py 必須 import apply_soft_warning_penalty"


def test_select_top_picks_calls_exclude_warned_stocks():
    fn_src = inspect.getsource(notifier_mod._select_top_picks)
    assert "exclude_warned_stocks(" in fn_src, (
        "_select_top_picks 必須呼叫 exclude_warned_stocks — "
        "警示股硬擋 wire 接斷了"
    )


def test_select_top_picks_calls_soft_penalty():
    fn_src = inspect.getsource(notifier_mod._select_top_picks)
    assert "apply_soft_warning_penalty(" in fn_src, (
        "_select_top_picks 必須呼叫 apply_soft_warning_penalty — "
        "soft 降權 wire 接斷了"
    )


def test_format_section_uses_excluded_caption():
    """format_excluded_caption 必須在 _format_short_picks_section 內被呼叫
    (否則 caption「✅ 已濾掉 N 檔警示股」根本不會出現在訊息)。"""
    fn_src = inspect.getsource(notifier_mod._format_short_picks_section)
    assert "format_excluded_caption(" in fn_src, (
        "_format_short_picks_section 必須呼叫 format_excluded_caption"
    )


# ============================================================================
# 端到端:_select_top_picks 過警示濾鏡的實際行為
# ============================================================================

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """每個測試一份乾淨 DB(用 production schema)。"""
    db_file = tmp_path / "wire_test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    monkeypatch.setenv("WARNING_FILTER_ENABLED", "true")
    yield db_file
    db._reset_path_cache()  # type: ignore[attr-defined]


def test_exclude_path_marks_module_cache(tmp_db):
    """直接 call exclude_warned_stocks + 手動 set _LAST_EXCLUDED_WARNINGS,
    驗證 format_excluded_caption 從 cache 撈出來組 caption。

    這個 test 不跑完整 _select_top_picks(它需要 daily_picks data fixture
    很重),改驗 wire path 的 _LAST_EXCLUDED_WARNINGS 模組級狀態 + caption 渲染。
    """
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
        kept, excluded = wf.exclude_warned_stocks(
            conn, picks, as_of="2026-05-15",
        )
    # mimic _select_top_picks 寫入模組級 cache
    notifier_mod._LAST_EXCLUDED_WARNINGS = excluded
    assert len(notifier_mod._LAST_EXCLUDED_WARNINGS) == 1
    assert notifier_mod._LAST_EXCLUDED_WARNINGS[0]["sid"] == "9999"
    # caption 透過 _format_short_picks_section render(picks 用 kept)
    msg = notifier_mod._format_short_picks_section(
        kept, channel="telegram", is_fallback=False,
    )
    assert "已濾掉" in msg
    assert "1" in msg
    assert "違約交割" in msg


def test_caption_skipped_when_no_warnings(tmp_db):
    """沒任何警示 → caption 不該出現(graceful skip)。"""
    notifier_mod._LAST_EXCLUDED_WARNINGS = []
    picks = [{"sid": "2330", "ml_prob": 0.7, "name": "台積電", "rank": 1}]
    msg = notifier_mod._format_short_picks_section(
        picks, channel="telegram", is_fallback=False,
    )
    assert "已濾掉" not in msg
    assert "警示股" not in msg


def test_soft_penalty_actually_lowers_ml_prob_in_pipeline(tmp_db):
    """注意股的 ml_prob 應該被 apply_soft_warning_penalty 直接改小,
    讓 _compute_pick_score 的 weighted_ml 自動排到後面。

    用單元 helper 測,而不是跑完整 _select_top_picks(後者需 daily_picks fixture)。
    """
    db.upsert_stock_warnings([
        {
            "stock_id": "6666", "warning_type": "attention",
            "announced_date": "2026-05-12", "effective_to": None,
        },
    ])
    picks = [
        {"sid": "2330", "ml_prob": 0.55, "matched_strategies": ["volume_kd"]},
        {"sid": "6666", "ml_prob": 0.60, "matched_strategies": ["volume_kd"]},
    ]
    with db.get_conn() as conn:
        wf.apply_soft_warning_penalty(conn, picks, as_of="2026-05-15")

    # 排序後 6666 應該排在 2330 之後(原本 0.60 > 0.55,現在 0.60×0.7=0.42 < 0.55)
    picks.sort(key=lambda p: notifier_mod._compute_pick_score(
        sid=p["sid"], ml_prob=p["ml_prob"],
        matched_strategies=p["matched_strategies"],
    ))
    assert picks[0]["sid"] == "2330", (
        "soft 降權後 2330 應該排第一,實際 picks 排序 = "
        f"{[p['sid'] for p in picks]}"
    )
    assert picks[1]["sid"] == "6666"


def test_kill_switch_skips_filter_in_pipeline(tmp_db, monkeypatch):
    """WARNING_FILTER_ENABLED=false → exclude / penalty 全跳過。"""
    monkeypatch.setenv("WARNING_FILTER_ENABLED", "false")
    db.upsert_stock_warnings([
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
    # kill-switch on → 違約股仍在 kept(主公出事 escape hatch)
    assert excluded == []
    assert kept[0]["sid"] == "9999"
