"""scripts/backfill_company_profiles.py LLM 模式測試。

scripts/ 不是 package,用 importlib 載入。Mock 掉:
- data_fetcher.ensure_stock_info / _fetch_all_stock_info(不打 FinMind)
- company_profile.generate_with_gemini(不打 Gemini API)
- universe._resolve_universe(直接控制 sid 順序)

驗:
- LLM mode 該打 generate_with_gemini
- warm-up mode(預設)不打 Gemini
- Gemini 429 → fail-fast 整批中斷
- batch-start / batch-end / max-stocks 切視窗
- 沒設 GEMINI_API_KEY 在 LLM 模式 → exit 1
- schema 對齊 production(snapshot dump 欄位完整)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src import company_profile as cp
from src import config, data_fetcher, database as db

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "backfill_company_profiles.py"
)
_spec = importlib.util.spec_from_file_location("backfill_company_profiles", _SCRIPT)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """tmp DB,init schema,清掉 data_fetcher cache + streamlit session quota flag。"""
    db_file = tmp_path / "cp.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    data_fetcher._reset_stock_info_cache()
    # 清 quota flag(避免上個 test 的 quota_exceeded 殘留導致 LLM 被跳過)
    try:
        import streamlit as st
        st.session_state.pop(cp._QUOTA_FLAG_KEY, None)
    except Exception:
        pass
    yield db_file
    db._reset_path_cache()


def _seed_finmind_mock(monkeypatch, sids: list[str]):
    """讓 fetch_taiwan_stock_info 對指定 sid 不打 FinMind,回 fake facts。"""
    fake_map = {
        s: {"stock_id": s, "name": f"name_{s}", "industry": "半導體業"}
        for s in sids
    }
    monkeypatch.setattr(
        data_fetcher, "ensure_stock_info",
        lambda sid: fake_map.get(sid),
    )
    monkeypatch.setattr(
        data_fetcher, "_fetch_all_stock_info",
        lambda: {s: {"type": "twse"} for s in sids},
    )


def _patch_universe(monkeypatch, sids: list[str]):
    """讓 _resolve_universe 不打 DB,直接回指定 sids。"""
    def fake_resolve(name):
        if name in {"tw_top_50", "pure_stock", "watchlist"}:
            return list(sids)
        raise ValueError(f"unknown universe: {name}")
    monkeypatch.setattr(bf, "_resolve_universe", fake_resolve)


# === Warm-up mode(預設,不打 LLM)===

def test_warmup_mode_does_not_call_gemini(tmp_db, monkeypatch):
    """--llm-call false(預設)→ 不該打 generate_with_gemini,只填 FinMind facts。"""
    sids = ["2330", "2317", "1101"]
    _patch_universe(monkeypatch, sids)
    _seed_finmind_mock(monkeypatch, sids)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")  # 即使有 key 也別打

    llm_calls: list[str] = []
    def _spy_gen(*a, **kw):
        llm_calls.append(a[0])
        raise AssertionError("warm-up mode 不該打 LLM")
    monkeypatch.setattr(cp, "generate_with_gemini", _spy_gen)

    rc = bf.main(["--sleep", "0", "--progress-every", "100"])
    assert rc == 0
    assert llm_calls == []

    # facts 該被寫進 db
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id, industry, description FROM company_profiles"
        ).fetchall()
    assert len(rows) == 3
    for r in rows:
        assert r["industry"] == "半導體業"
        assert r["description"] is None  # 沒打 LLM,description 該是 NULL


# === LLM mode ===

def test_llm_mode_calls_gemini(tmp_db, monkeypatch):
    """--llm-call true → 該打 generate_with_gemini,寫進 description/uniqueness/moat。"""
    sids = ["2330", "2317"]
    _patch_universe(monkeypatch, sids)
    _seed_finmind_mock(monkeypatch, sids)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    llm_calls: list[str] = []
    def fake_gen(sid, name, ind):
        llm_calls.append(sid)
        return {
            "description": f"desc-{sid}",
            "uniqueness": f"uniq-{sid}",
            "moat": f"moat-{sid}",
        }
    monkeypatch.setattr(cp, "generate_with_gemini", fake_gen)

    rc = bf.main([
        "--llm-call", "true",
        "--sleep", "0", "--progress-every", "100",
    ])
    assert rc == 0
    assert set(llm_calls) == {"2330", "2317"}

    with db.get_conn() as conn:
        rows = {
            r["stock_id"]: r
            for r in conn.execute(
                "SELECT stock_id, description, uniqueness, moat FROM company_profiles"
            ).fetchall()
        }
    assert rows["2330"]["description"] == "desc-2330"
    assert rows["2330"]["uniqueness"] == "uniq-2330"
    assert rows["2330"]["moat"] == "moat-2330"


def test_llm_mode_missing_api_key_returns_1(tmp_db, monkeypatch, capsys):
    """--llm-call true + 缺 GEMINI_API_KEY → 前置檢查 exit 1,不該開始打。"""
    sids = ["2330"]
    _patch_universe(monkeypatch, sids)
    _seed_finmind_mock(monkeypatch, sids)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")

    rc = bf.main(["--llm-call", "true", "--sleep", "0"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "GEMINI_API_KEY" in err


def test_llm_mode_quota_exceeded_aborts_immediately(tmp_db, monkeypatch, capsys):
    """第 1 檔就撞 Gemini 429 → 整批中斷,不繼續打剩下的檔,exit 1。"""
    sids = [f"{1000 + i:04d}" for i in range(10)]
    _patch_universe(monkeypatch, sids)
    _seed_finmind_mock(monkeypatch, sids)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    calls: list[str] = []

    def quota_at_third(sid, name, ind):
        calls.append(sid)
        if len(calls) >= 3:
            # google.api_core.exceptions.ResourceExhausted 的訊息格式
            raise RuntimeError(
                "429 You exceeded your current quota, please check your "
                "plan and billing details. quota_metric: ..."
            )
        return {
            "description": f"desc-{sid}",
            "uniqueness": f"uniq-{sid}",
            "moat": f"moat-{sid}",
        }

    monkeypatch.setattr(cp, "generate_with_gemini", quota_at_third)

    rc = bf.main([
        "--llm-call", "true",
        "--sleep", "0", "--progress-every", "100",
    ])
    # 第 3 檔 quota → 整批中斷,第 4-10 檔不該被打
    assert len(calls) == 3
    assert rc == 1
    err = capsys.readouterr().err
    assert "quota" in err.lower()


def test_llm_mode_individual_failure_continues(tmp_db, monkeypatch):
    """個別 LLM 失敗(非 quota)→ 計入 failed 但繼續跑,不中斷。"""
    sids = ["2330", "2317", "1101"]
    _patch_universe(monkeypatch, sids)
    _seed_finmind_mock(monkeypatch, sids)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    calls: list[str] = []
    def fake_gen(sid, name, ind):
        calls.append(sid)
        if sid == "2317":
            raise RuntimeError("Network unreachable: connection refused")
        return {
            "description": f"desc-{sid}", "uniqueness": "u", "moat": "m",
        }

    monkeypatch.setattr(cp, "generate_with_gemini", fake_gen)

    rc = bf.main([
        "--llm-call", "true",
        "--sleep", "0", "--progress-every", "100",
    ])
    # 3 檔全打過(個別失敗不中斷)
    assert set(calls) == {"2330", "2317", "1101"}
    # 2/3 成功 > 0,不超過 50% 失敗 → exit 0
    assert rc == 0


# === Batch range / max-stocks ===

def test_batch_range_subsets_universe(tmp_db, monkeypatch):
    """--batch-start / --batch-end 該切視窗,只打範圍內的 sid。"""
    sids = [f"{1000 + i:04d}" for i in range(10)]
    _patch_universe(monkeypatch, sids)
    _seed_finmind_mock(monkeypatch, sids)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    calls: list[str] = []
    monkeypatch.setattr(
        cp, "generate_with_gemini",
        lambda sid, name, ind: (
            calls.append(sid) or {"description": "x", "uniqueness": "y", "moat": "z"}
        ),
    )

    rc = bf.main([
        "--llm-call", "true",
        "--batch-start", "2", "--batch-end", "5",
        "--sleep", "0", "--progress-every", "100",
    ])
    assert rc == 0
    # universe[2:5] = 1002, 1003, 1004
    assert set(calls) == {"1002", "1003", "1004"}


def test_max_stocks_caps_run(tmp_db, monkeypatch):
    """--max-stocks 限單次 run 上限。"""
    sids = [f"{1000 + i:04d}" for i in range(20)]
    _patch_universe(monkeypatch, sids)
    _seed_finmind_mock(monkeypatch, sids)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    calls: list[str] = []
    monkeypatch.setattr(
        cp, "generate_with_gemini",
        lambda sid, name, ind: (
            calls.append(sid) or {"description": "x", "uniqueness": "y", "moat": "z"}
        ),
    )

    rc = bf.main([
        "--llm-call", "true",
        "--max-stocks", "5",
        "--sleep", "0", "--progress-every", "100",
    ])
    assert rc == 0
    assert len(calls) == 5


def test_batch_start_beyond_universe_exits_0(tmp_db, monkeypatch, capsys):
    """--batch-start 超過 universe size → 沒事可做,exit 0。"""
    sids = ["2330", "2317", "1101"]
    _patch_universe(monkeypatch, sids)
    _seed_finmind_mock(monkeypatch, sids)

    rc = bf.main(["--batch-start", "10", "--sleep", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to do" in out


def test_batch_start_ge_end_exits_2(tmp_db, monkeypatch, capsys):
    """--batch-start >= --batch-end (但 < universe size) → 區間空,參數錯 exit 2。"""
    sids = [f"{1000 + i:04d}" for i in range(10)]
    _patch_universe(monkeypatch, sids)
    _seed_finmind_mock(monkeypatch, sids)

    rc = bf.main(["--batch-start", "5", "--batch-end", "3", "--sleep", "0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "區間空" in err or "batch-end" in err


# === Universe resolve ===

def test_resolve_universe_unknown_exits_2(tmp_db, monkeypatch, capsys):
    """argparse choices 應擋住 unknown universe(exit 2,argparse 標準行為)。"""
    with pytest.raises(SystemExit) as ei:
        bf.main(["--universe", "unknown_thing", "--sleep", "0"])
    assert ei.value.code == 2


def test_resolve_universe_supports_watchlist(monkeypatch, tmp_db):
    """新增的 watchlist universe option 該走 load_watchlist。"""
    # bf 內走 `from src.universe import load_watchlist` 直接綁定,monkeypatch
    # 該打在 bf module 上(patch src.universe 對已綁定的 reference 無效)
    monkeypatch.setattr(bf, "load_watchlist", lambda: [("2330", ""), ("2317", "")])
    sids = bf._resolve_universe("watchlist")
    assert sids == ["2330", "2317"]


# === Snapshot dump ===

def test_dump_snapshot_csv_schema(tmp_db, monkeypatch, tmp_path):
    """--dump-format csv → 寫 company_profiles.csv,欄位對齊 production schema。"""
    monkeypatch.setattr(bf, "SNAPSHOT_DIR", tmp_path / "snap")

    # seed 1 筆 profile
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO company_profiles (
                stock_id, industry, market, listing_date, foreign_limit,
                description, uniqueness, moat,
                finmind_updated_at, llm_updated_at
            ) VALUES (
                '2330', '半導體業', '上市', NULL, NULL,
                'd', 'u', 'm', '2026-05-17T00:00:00+00:00',
                '2026-05-17T00:00:00+00:00'
            )
            """,
        )

    out, n = bf._dump_snapshot(fmt="csv")
    assert n == 1
    assert out is not None and out.exists()

    df = pd.read_csv(out, dtype={"stock_id": str})
    # schema 對齊 production:CREATE TABLE company_profiles 內 10 欄
    expected_cols = {
        "stock_id", "industry", "market", "listing_date", "foreign_limit",
        "description", "uniqueness", "moat",
        "finmind_updated_at", "llm_updated_at",
    }
    assert set(df.columns) == expected_cols
    assert df.iloc[0]["stock_id"] == "2330"
    assert df.iloc[0]["description"] == "d"


def test_dump_snapshot_empty_db_returns_none(tmp_db, monkeypatch, tmp_path):
    """空表不該寫檔(避免 0-row CSV 上傳 release)。"""
    monkeypatch.setattr(bf, "SNAPSHOT_DIR", tmp_path / "snap")
    out, n = bf._dump_snapshot(fmt="csv")
    assert out is None and n == 0
    assert not (tmp_path / "snap" / "company_profiles.csv").exists()


# === Regenerate flag ===

def test_regenerate_forces_recall_in_llm_mode(tmp_db, monkeypatch):
    """--regenerate + --llm-call true → 即使 cache 有 description 也重打 Gemini。"""
    sids = ["2330"]
    _patch_universe(monkeypatch, sids)
    _seed_finmind_mock(monkeypatch, sids)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    call_count = [0]
    def fake_gen(sid, name, ind):
        call_count[0] += 1
        return {
            "description": f"v{call_count[0]}",
            "uniqueness": "u", "moat": "m",
        }
    monkeypatch.setattr(cp, "generate_with_gemini", fake_gen)

    # 第 1 跑:miss → 打 1 次
    bf.main(["--llm-call", "true", "--sleep", "0", "--progress-every", "100"])
    # 第 2 跑:cache 已有 → 不該打
    bf.main(["--llm-call", "true", "--sleep", "0", "--progress-every", "100"])
    assert call_count[0] == 1
    # 第 3 跑:--regenerate → 該再打
    bf.main([
        "--llm-call", "true", "--regenerate",
        "--sleep", "0", "--progress-every", "100",
    ])
    assert call_count[0] == 2
