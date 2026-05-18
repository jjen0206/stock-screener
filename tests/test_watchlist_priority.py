"""watchlist_priority 測試 — watchlist 命中置頂排序 + ⭐ 標。

Roadmap #4(2026-05-18)個人化推播。
"""
from __future__ import annotations

import pytest

from src import config, database as db, watchlist_snapshot
from src.watchlist_priority import (
    annotate_watchlist_picks,
    any_watchlist_hit,
    load_watchlist,
    prioritize_watchlist,
    watchlist_sort_key,
)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """隔離 SQLite + watchlist CSV dump path,避免污染 data/twse_snapshot/watchlist.csv。

    對應 memory feedback_e2e_test_isolation_for_persistence.md 教訓。
    """
    db_file = tmp_path / "wl.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))

    # watchlist_snapshot 在 add_to_watchlist 後會 dump→push 真實檔案,必須三件套 patch
    snap_dir = tmp_path / "twse_snapshot"
    snap_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(watchlist_snapshot, "SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(
        watchlist_snapshot, "WATCHLIST_CSV", snap_dir / "watchlist.csv",
    )
    monkeypatch.setattr(
        watchlist_snapshot, "_db_inside_project", lambda _p: True,
    )
    db.init_db()
    return db_file


def _make_picks(*sids_with_rank: tuple[str, int]) -> list[dict]:
    """方便的 picks fixture builder。回傳已照 rank 排好的 list。"""
    picks = [
        {"sid": sid, "name": f"N{sid}", "rank": rank, "ml_prob": 0.5}
        for sid, rank in sids_with_rank
    ]
    return picks


# === load_watchlist =========================================================


def test_load_watchlist_empty(tmp_db):
    """空 DB → 回空 set,不擋。"""
    assert load_watchlist() == set()


def test_load_watchlist_returns_sids(tmp_db):
    db.add_to_watchlist("2330")
    db.add_to_watchlist("2454")
    wl = load_watchlist()
    assert wl == {"2330", "2454"}


def test_load_watchlist_failure_fallback_empty(monkeypatch):
    """get_watchlist 拋例外 → 回空 set 不擋推播。"""
    def _boom(**_kw):
        raise RuntimeError("DB down")
    monkeypatch.setattr(db, "get_watchlist", _boom)
    assert load_watchlist() == set()


# === annotate_watchlist_picks ===============================================


def test_annotate_supports_sid_key():
    picks = _make_picks(("2330", 1), ("2454", 2))
    annotate_watchlist_picks(picks, {"2330"})
    assert picks[0]["is_watchlist"] is True
    assert picks[1]["is_watchlist"] is False


def test_annotate_supports_stock_id_key():
    """DataFrame-flow 用 stock_id key 也要 work。"""
    picks = [
        {"stock_id": "2330", "rank": 1},
        {"stock_id": "2454", "rank": 2},
    ]
    annotate_watchlist_picks(picks, {"2454"})
    assert picks[0]["is_watchlist"] is False
    assert picks[1]["is_watchlist"] is True


def test_annotate_empty_list_safe():
    assert annotate_watchlist_picks([], {"2330"}) == []


def test_annotate_empty_watchlist_all_false():
    picks = _make_picks(("2330", 1), ("2454", 2))
    annotate_watchlist_picks(picks, set())
    assert all(p["is_watchlist"] is False for p in picks)


# === watchlist_sort_key =====================================================


def test_sort_key_in_watchlist_first():
    p_hit = {"sid": "2330", "rank": 3}
    p_miss = {"sid": "2454", "rank": 1}
    wl = {"2330"}
    assert watchlist_sort_key(p_hit, wl) < watchlist_sort_key(p_miss, wl)


def test_sort_key_preserves_rank_within_group():
    p1 = {"sid": "A", "rank": 1}
    p2 = {"sid": "B", "rank": 2}
    wl = {"A", "B"}
    assert watchlist_sort_key(p1, wl) < watchlist_sort_key(p2, wl)


def test_sort_key_missing_rank_pushed_to_back():
    p_no_rank = {"sid": "X"}
    p_rank = {"sid": "Y", "rank": 1}
    wl = set()
    assert watchlist_sort_key(p_rank, wl) < watchlist_sort_key(p_no_rank, wl)


# === prioritize_watchlist ===================================================


def test_prioritize_empty_watchlist_no_reorder(tmp_db):
    """watchlist 為空 → 排序不變,僅注 is_watchlist=False(無 regression)。"""
    picks = _make_picks(("2330", 1), ("2454", 2), ("2317", 3))
    out = prioritize_watchlist(picks)
    assert [p["sid"] for p in out] == ["2330", "2454", "2317"]
    assert all(p["is_watchlist"] is False for p in out)


def test_prioritize_partial_hit_moves_to_top(tmp_db):
    """部分命中 → 命中那筆排最前 + ⭐ flag。"""
    db.add_to_watchlist("2317")  # 鴻海 rank 3 → 應跳到第 1
    picks = _make_picks(("2330", 1), ("2454", 2), ("2317", 3))
    out = prioritize_watchlist(picks)
    assert [p["sid"] for p in out] == ["2317", "2330", "2454"]
    is_wl = {p["sid"]: p["is_watchlist"] for p in out}
    assert is_wl == {"2317": True, "2330": False, "2454": False}


def test_prioritize_multiple_hits_keep_internal_order(tmp_db):
    """多筆命中 → 內部維持原 rank 排序。"""
    db.add_to_watchlist("2330")
    db.add_to_watchlist("2317")
    picks = _make_picks(
        ("2330", 1), ("2454", 2), ("2317", 3), ("2308", 4),
    )
    out = prioritize_watchlist(picks)
    # 命中 (rank 1, 3) 排前;未命中 (rank 2, 4) 排後;組內按 rank 升冪
    assert [p["sid"] for p in out] == ["2330", "2317", "2454", "2308"]


def test_prioritize_all_hit_order_preserved(tmp_db):
    """watchlist 全命中 → 全 ⭐,排序維持。"""
    for sid in ("2330", "2454", "2317"):
        db.add_to_watchlist(sid)
    picks = _make_picks(("2330", 1), ("2454", 2), ("2317", 3))
    out = prioritize_watchlist(picks)
    assert [p["sid"] for p in out] == ["2330", "2454", "2317"]
    assert all(p["is_watchlist"] for p in out)


def test_prioritize_no_hit_order_preserved(tmp_db):
    """watchlist 完全沒命中 → 全非 ⭐,排序按原 rank。"""
    db.add_to_watchlist("9999")  # 不在 picks
    picks = _make_picks(("2330", 1), ("2454", 2))
    out = prioritize_watchlist(picks)
    assert [p["sid"] for p in out] == ["2330", "2454"]
    assert all(p["is_watchlist"] is False for p in out)


def test_prioritize_empty_picks_safe(tmp_db):
    assert prioritize_watchlist([]) == []


# === any_watchlist_hit ======================================================


def test_any_watchlist_hit_true_if_any_group_hits():
    g1 = [{"sid": "A", "is_watchlist": False}]
    g2 = [{"sid": "B", "is_watchlist": True}]
    assert any_watchlist_hit(g1, g2) is True


def test_any_watchlist_hit_false_when_none():
    g1 = [{"sid": "A", "is_watchlist": False}]
    g2 = [{"sid": "B", "is_watchlist": False}]
    assert any_watchlist_hit(g1, g2) is False


def test_any_watchlist_hit_handles_empty_and_none_groups():
    assert any_watchlist_hit(None, [], None) is False


# === regression: notifier 內 render 真的有加 ⭐ ==============================


def test_format_pick_block_renders_star_when_is_watchlist():
    """integration:format_pick_block 看到 is_watchlist=True 要產生 ⭐ 前綴。"""
    from src.notifier import format_pick_block
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "is_watchlist": True, "ml_prob": 0.7,
    }
    out = format_pick_block(pick, channel="telegram")
    assert "⭐" in out
    # 沒命中時不該有 ⭐(用 consensus badge 那種其他 ⭐ 排除掉)
    pick2 = {**pick, "sid": "9999", "is_watchlist": False}
    out2 = format_pick_block(pick2, channel="telegram")
    # 這筆沒 consensus 沒 watchlist → 全篇不該有 ⭐
    assert "⭐" not in out2
