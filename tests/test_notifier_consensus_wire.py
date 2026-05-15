"""共識 wire 進 notifier 的結構性守住測試。

純結構性 — inspect.getsource / inspect.signature 確認 wire 點存在,不跑
end-to-end pipeline(SQL / strategies / ml model 都不會 invoke)。

守住:
1. _compute_pick_score 接 consensus_meta kwarg
2. _select_top_picks source 含 compute_strategy_consensus call
3. _select_top_picks 傳 consensus_meta 到 sort key
4. format_yesterday_recap 也算 consensus(M4/U1 順序對齊)
5. 模組級 _LAST_CONSENSUS cache 存在
6. _format_short_picks_section 用 _consensus_summary_line
7. format_pick_block source 含 consensus_badge call(輸出帶 ⭐)
"""
from __future__ import annotations

import inspect

from src import consensus, notifier


def test_compute_pick_score_accepts_consensus_meta():
    """_compute_pick_score 必須接 consensus_meta kwarg(共識 wire 點)。"""
    sig = inspect.signature(notifier._compute_pick_score)
    assert "consensus_meta" in sig.parameters, (
        f"_compute_pick_score 缺 consensus_meta 參數: {list(sig.parameters)}"
    )


def test_select_top_picks_computes_consensus():
    """_select_top_picks 必須呼叫 compute_strategy_consensus 並注入 picks。"""
    src = inspect.getsource(notifier._select_top_picks)
    assert "compute_strategy_consensus" in src, (
        "_select_top_picks 沒呼叫 compute_strategy_consensus"
    )
    assert "consensus_meta" in src, (
        "_select_top_picks 沒把 consensus_meta 傳進 sort key"
    )
    assert '"consensus"' in src or "'consensus'" in src, (
        "_select_top_picks 沒把 consensus 寫進 pick dict"
    )


def test_format_yesterday_recap_uses_consensus():
    """format_yesterday_recap 必須讀同樣的共識,否則 recap 順序會跟實際分歧。"""
    src = inspect.getsource(notifier.format_yesterday_recap)
    assert "compute_strategy_consensus" in src, (
        "format_yesterday_recap 沒算 consensus — 順序可能跟昨日推播分歧"
    )
    assert "consensus_meta" in src, (
        "format_yesterday_recap 沒把 consensus_meta 傳進 sort key"
    )


def test_notifier_has_last_consensus_cache():
    """模組級 _LAST_CONSENSUS cache 給 UI summary 用。"""
    assert hasattr(notifier, "_LAST_CONSENSUS"), (
        "notifier 缺 _LAST_CONSENSUS 模組級 cache"
    )
    # 初始 / 沒跑過 picks → 空 dict
    assert isinstance(notifier._LAST_CONSENSUS, dict)


def test_short_picks_section_uses_summary():
    """_format_short_picks_section 必須呼叫 _consensus_summary_line。"""
    assert hasattr(notifier, "_consensus_summary_line"), (
        "notifier 缺 _consensus_summary_line helper"
    )
    src = inspect.getsource(notifier._format_short_picks_section)
    assert "_consensus_summary_line" in src, (
        "_format_short_picks_section 沒呼叫 _consensus_summary_line"
    )


def test_consensus_summary_line_text_format():
    """summary line 包含「共識統計」+ tier 字串。"""
    picks = [
        {"consensus": {"strategy_count": 3, "category_count": 3}},
        {"consensus": {"strategy_count": 2, "category_count": 2}},
        {"consensus": {"strategy_count": 2, "category_count": 1}},
    ]
    line = notifier._consensus_summary_line(picks, channel="telegram")
    assert "共識統計" in line
    assert "強共識" in line
    assert "1 檔" in line  # 強共識 1 檔


def test_consensus_summary_line_empty_when_all_single():
    """全是單策略 picks → summary 回空字串(graceful skip)。"""
    picks = [
        {"consensus": {"strategy_count": 1, "category_count": 1}},
        {"consensus": None},
    ]
    line = notifier._consensus_summary_line(picks, channel="telegram")
    assert line == ""


def test_format_pick_block_renders_badge():
    """format_pick_block 必須呼叫 consensus_badge,且跨類 picks 輸出帶 ⭐⭐。"""
    src = inspect.getsource(notifier.format_pick_block)
    assert "consensus_badge" in src, (
        "format_pick_block 沒呼叫 consensus_badge"
    )
    pick = {
        "rank": 1,
        "sid": "2330",
        "name": "台積電",
        "close": 779.0,
        "consensus": {"strategy_count": 2, "category_count": 2},
        "matched_labels": [],
    }
    out = notifier.format_pick_block(pick, channel="telegram")
    assert "⭐⭐" in out, (
        "format_pick_block 輸出沒含跨類 2 票的 ⭐⭐ badge"
    )


def test_format_pick_block_no_badge_for_single_strategy():
    """單策略 pick → 標題不該帶 ⭐。"""
    pick = {
        "rank": 1,
        "sid": "2330",
        "name": "台積電",
        "close": 779.0,
        "consensus": {"strategy_count": 1, "category_count": 1},
        "matched_labels": [],
    }
    out = notifier.format_pick_block(pick, channel="telegram")
    # 標題那一行不該有 ⭐(底下可能有其他 emoji,只看第一行)
    first_line = out.split("\n", 1)[0]
    assert "⭐" not in first_line


# === Score multiplier 影響排序 ===

def test_score_multiplier_pushes_consensus_picks_up():
    """ml_prob 相同的兩張 picks,有共識的應排前(score tuple smaller)。"""
    no_cons = notifier._compute_pick_score(
        sid="A", ml_prob=0.6, matched_strategies=["macd_golden"],
        consensus_meta={"strategy_count": 1, "category_count": 1},
    )
    cross_cons = notifier._compute_pick_score(
        sid="B", ml_prob=0.6,
        matched_strategies=["macd_golden", "inst_consensus"],
        consensus_meta={"strategy_count": 2, "category_count": 2},
    )
    # 字典序 ascending → 跨類共識的 score 應該 < 單策略的 score
    assert cross_cons < no_cons, (
        f"跨類共識 score 沒排前: cross={cross_cons} vs none={no_cons}"
    )


def test_app_has_consensus_filter_toggle():
    """app.py 必須有 consensus_only_on toggle + _row_has_consensus 過濾 helper。"""
    import app
    src_sidebar = inspect.getsource(app._render_high_confidence_sidebar)
    assert "consensus_only_on" in src_sidebar, (
        "app._render_high_confidence_sidebar 缺 ⭐ 共識 quick filter toggle"
    )
    assert hasattr(app, "_row_has_consensus"), (
        "app 缺 _row_has_consensus 過濾 helper"
    )
    src_apply = inspect.getsource(app._apply_confidence_filter)
    assert "consensus_only_on" in src_apply, (
        "_apply_confidence_filter 沒套用 consensus_only_on filter"
    )


def test_app_has_enrich_df_with_consensus():
    """app.py 必須有 _enrich_df_with_consensus 注入 consensus 欄到 df。"""
    import app
    assert hasattr(app, "_enrich_df_with_consensus"), (
        "app 缺 _enrich_df_with_consensus helper"
    )


def test_app_detail_page_shows_strategy_hits():
    """detail page 必須有 _render_detail_strategy_hits + 列出分類。"""
    import app
    assert hasattr(app, "_render_detail_strategy_hits"), (
        "app 缺 _render_detail_strategy_hits helper"
    )
    src = inspect.getsource(app._render_detail_strategy_hits)
    assert "STRATEGY_CATEGORIES" in src, (
        "detail page 沒顯示策略分類"
    )
    assert "consensus_badge" in src, (
        "detail page 沒顯示共識 badge"
    )


def test_score_multiplier_off_when_kill_switch(monkeypatch):
    """kill switch off → consensus_meta 不影響 score(退回 legacy)。"""
    monkeypatch.setenv("STRATEGY_CONSENSUS_ENABLED", "false")
    no_cons = notifier._compute_pick_score(
        sid="A", ml_prob=0.6, matched_strategies=["macd_golden"],
        consensus_meta={"strategy_count": 1, "category_count": 1},
    )
    cross_cons = notifier._compute_pick_score(
        sid="B", ml_prob=0.6,
        matched_strategies=["macd_golden", "inst_consensus"],
        consensus_meta={"strategy_count": 2, "category_count": 2},
    )
    # ml_prob 一樣、matched count 不同 → 退回 legacy(命中越多排越前)
    # 但兩張 score 的 ml_prob 部分一致(沒共識加成)— 看 matched_count 排序
    # _compute_pick_score 的 tuple: (-AT, -weighted_ml, -len(matched), sid)
    # B 有 2 matched > A 1 matched → B 仍排前(legacy 行為,跟共識無關)
    assert cross_cons < no_cons
    # 但 weighted_ml 部分應該相同(都是 0.6 × 1.0 × 1.0 = 0.6)
    assert cross_cons[1] == no_cons[1], (
        f"kill switch off 時 weighted_ml 不該被 consensus 影響: "
        f"{cross_cons[1]} vs {no_cons[1]}"
    )
