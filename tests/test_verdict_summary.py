"""src/verdict_summary.py 單元測試。

涵蓋:
  - build_summary 空 input / None / empty df → counts 全 0
  - counts 正確 + Top N 排序按 score
  - 紅燈 sid 用 score 升序(最深負最先)
  - 去重(同 sid 多筆只算一次)
  - kill switch(enabled=False)→ 整批 skip
  - dump_to_csv / load_from_csv round-trip
  - DataFrame / list[dict] 兩種 input

Mock 策略:傳 `compute_verdict_fn` 注入固定回傳,完全不碰 SQLite / ML。
"""
from __future__ import annotations

import pandas as pd

from src import verdict_summary as vs


def _mk_verdict(sid: str, color: str, score: int, **extra) -> dict:
    """造一個 compute_verdict 回傳結構,test 用。"""
    verdict_map = {"🟢": "可進場", "🟡": "觀望", "🔴": "不進場"}
    return {
        "enabled": True,
        "sid": sid,
        "verdict": verdict_map[color],
        "verdict_color": color,
        "score": score,
        "reasons_pro": extra.get("reasons_pro", [f"{sid} pro 理由"]),
        "reasons_con": extra.get("reasons_con", []),
        "action_suggestion": extra.get("action_suggestion", f"{sid} 建議"),
        "entry_zone": None,
        "stop_loss": None,
        "take_profit": None,
        "signals": {},
    }


# ============================================================================
# build_summary — empty / None / kill-switch
# ============================================================================

def test_build_summary_none_input():
    out = vs.build_summary(None, "2026-05-18", compute_verdict_fn=lambda s: {})
    assert out["counts"] == {"green": 0, "yellow": 0, "red": 0}
    assert out["top_green"] == []
    assert out["top_yellow"] == []
    assert out["top_red"] == []
    assert out["trade_date"] == "2026-05-18"


def test_build_summary_empty_df():
    out = vs.build_summary(
        pd.DataFrame(), "2026-05-18", compute_verdict_fn=lambda s: {},
    )
    assert out["counts"] == {"green": 0, "yellow": 0, "red": 0}


def test_build_summary_empty_list():
    out = vs.build_summary([], "2026-05-18", compute_verdict_fn=lambda s: {})
    assert out["counts"] == {"green": 0, "yellow": 0, "red": 0}


def test_build_summary_kill_switch_skips_all():
    """compute_verdict 回 enabled=False → 全 skip,counts 全 0。"""
    fn = lambda s: {"enabled": False, "verdict_color": "🟡", "score": 0}
    out = vs.build_summary(
        [{"sid": "2330"}, {"sid": "2317"}],
        "2026-05-18",
        compute_verdict_fn=fn,
    )
    assert out["counts"] == {"green": 0, "yellow": 0, "red": 0}


# ============================================================================
# build_summary — counts + ordering
# ============================================================================

def test_build_summary_counts_correct():
    """3 綠 + 2 黃 + 1 紅 → counts 對應正確。"""
    mapping = {
        "1001": _mk_verdict("1001", "🟢", 5),
        "1002": _mk_verdict("1002", "🟢", 4),
        "1003": _mk_verdict("1003", "🟢", 3),
        "2001": _mk_verdict("2001", "🟡", 2),
        "2002": _mk_verdict("2002", "🟡", 1),
        "3001": _mk_verdict("3001", "🔴", -5),
    }
    fn = lambda s: mapping[s]
    out = vs.build_summary(
        [{"sid": s, "name": f"N{s}"} for s in mapping],
        "2026-05-18",
        compute_verdict_fn=fn,
    )
    assert out["counts"] == {"green": 3, "yellow": 2, "red": 1}


def test_build_summary_top_green_sorted_by_score_desc():
    """🟢 列按 score 由高到低。"""
    mapping = {
        "1001": _mk_verdict("1001", "🟢", 3),
        "1002": _mk_verdict("1002", "🟢", 7),
        "1003": _mk_verdict("1003", "🟢", 5),
        "1004": _mk_verdict("1004", "🟢", 6),
    }
    fn = lambda s: mapping[s]
    out = vs.build_summary(
        [{"sid": s, "name": f"N{s}"} for s in mapping],
        "2026-05-18",
        compute_verdict_fn=fn,
    )
    sids = [it["sid"] for it in out["top_green"]]
    # Top 3 = score 7, 6, 5 → sid 1002, 1004, 1003
    assert sids == ["1002", "1004", "1003"]


def test_build_summary_top_red_sorted_by_score_asc():
    """🔴 列按 score 由負最深到較淺(風險最大優先)。"""
    mapping = {
        "9001": _mk_verdict("9001", "🔴", -3),
        "9002": _mk_verdict("9002", "🔴", -7),
        "9003": _mk_verdict("9003", "🔴", -5),
        "9004": _mk_verdict("9004", "🔴", -2),
    }
    fn = lambda s: mapping[s]
    out = vs.build_summary(
        [{"sid": s, "name": f"N{s}"} for s in mapping],
        "2026-05-18",
        compute_verdict_fn=fn,
    )
    sids = [it["sid"] for it in out["top_red"]]
    # Top 3 紅 = -7, -5, -3 → 9002, 9003, 9001
    assert sids == ["9002", "9003", "9001"]


def test_build_summary_top_yellow_capped_at_5():
    """🟡 列最多 5 個。"""
    mapping = {f"20{i:02d}": _mk_verdict(f"20{i:02d}", "🟡", i) for i in range(8)}
    fn = lambda s: mapping[s]
    out = vs.build_summary(
        [{"sid": s, "name": f"N{s}"} for s in mapping],
        "2026-05-18",
        compute_verdict_fn=fn,
    )
    assert len(out["top_yellow"]) == 5
    assert out["counts"]["yellow"] == 8


# ============================================================================
# build_summary — dedup + DataFrame input
# ============================================================================

def test_build_summary_dedup_same_sid():
    """同 sid 出現多次(多策略 fire)只算一次。"""
    mapping = {"2330": _mk_verdict("2330", "🟢", 5)}
    call_count = {"n": 0}

    def fn(s):
        call_count["n"] += 1
        return mapping[s]

    out = vs.build_summary(
        [
            {"sid": "2330", "name": "台積電"},
            {"sid": "2330", "name": "台積電"},
            {"sid": "2330", "name": "台積電"},
        ],
        "2026-05-18",
        compute_verdict_fn=fn,
    )
    assert out["counts"]["green"] == 1
    assert call_count["n"] == 1


def test_build_summary_dataframe_input():
    """DataFrame input 應該跟 list[dict] 行為一致。"""
    mapping = {
        "2330": _mk_verdict("2330", "🟢", 5),
        "2317": _mk_verdict("2317", "🟡", 2),
    }
    fn = lambda s: mapping[s]

    df = pd.DataFrame([
        {"sid": "2330", "name": "台積電"},
        {"sid": "2317", "name": "鴻海"},
    ])
    out = vs.build_summary(df, "2026-05-18", compute_verdict_fn=fn)
    assert out["counts"] == {"green": 1, "yellow": 1, "red": 0}
    assert out["top_green"][0]["name"] == "台積電"


def test_build_summary_main_reason_picks_pro_for_green():
    mapping = {
        "2330": _mk_verdict(
            "2330", "🟢", 5,
            reasons_pro=["🎯 AI 勝率 78%(偏高)", "📊 三紅兵"],
        ),
    }
    fn = lambda s: mapping[s]
    out = vs.build_summary(
        [{"sid": "2330", "name": "台積電"}],
        "2026-05-18",
        compute_verdict_fn=fn,
    )
    assert out["top_green"][0]["main_reason"] == "🎯 AI 勝率 78%(偏高)"


def test_build_summary_main_reason_picks_con_for_red():
    mapping = {
        "9999": _mk_verdict(
            "9999", "🔴", -7,
            reasons_pro=[],
            reasons_con=["⚠️ 全額交割警示生效中"],
        ),
    }
    fn = lambda s: mapping[s]
    out = vs.build_summary(
        [{"sid": "9999", "name": "壞股"}],
        "2026-05-18",
        compute_verdict_fn=fn,
    )
    assert out["top_red"][0]["main_reason"] == "⚠️ 全額交割警示生效中"


def test_build_summary_skips_compute_exception():
    """單支 compute 爆 exception 不擋整體。"""
    def fn(s):
        if s == "BAD":
            raise RuntimeError("boom")
        return _mk_verdict(s, "🟢", 5)

    out = vs.build_summary(
        [{"sid": "BAD"}, {"sid": "2330", "name": "台積電"}],
        "2026-05-18",
        compute_verdict_fn=fn,
    )
    assert out["counts"]["green"] == 1


# ============================================================================
# dump_to_csv / load_from_csv round-trip
# ============================================================================

def test_dump_and_load_roundtrip(tmp_path):
    mapping = {
        "1001": _mk_verdict("1001", "🟢", 5),
        "1002": _mk_verdict("1002", "🟢", 3),
        "2001": _mk_verdict("2001", "🟡", 1),
        "3001": _mk_verdict("3001", "🔴", -5),
    }
    fn = lambda s: mapping[s]
    summary = vs.build_summary(
        [{"sid": s, "name": f"N{s}"} for s in mapping],
        "2026-05-18",
        compute_verdict_fn=fn,
    )

    csv_path = tmp_path / "daily_verdict_summary.csv"
    rows_written = vs.dump_to_csv(summary, csv_path)
    assert rows_written >= 3  # 至少 3 個 count rows
    assert csv_path.exists()

    loaded = vs.load_from_csv(csv_path)
    assert loaded is not None
    assert loaded["trade_date"] == "2026-05-18"
    assert loaded["counts"] == {"green": 2, "yellow": 1, "red": 1}
    assert len(loaded["top_green"]) == 2
    # ordering 保留
    assert loaded["top_green"][0]["sid"] == "1001"
    assert loaded["top_green"][0]["score"] == 5
    assert loaded["top_red"][0]["sid"] == "3001"


def test_load_from_csv_missing_returns_none(tmp_path):
    assert vs.load_from_csv(tmp_path / "nope.csv") is None


def test_dump_empty_summary_still_writes_count_rows(tmp_path):
    """空 universe 也要寫 3 個 count=0 row,讓 cache load 能拿到 stale 標記。"""
    summary = vs.build_summary(
        None, "2026-05-18", compute_verdict_fn=lambda s: {},
    )
    csv_path = tmp_path / "x.csv"
    rows = vs.dump_to_csv(summary, csv_path)
    assert rows == 3  # 3 個 count row
    loaded = vs.load_from_csv(csv_path)
    assert loaded["counts"] == {"green": 0, "yellow": 0, "red": 0}
