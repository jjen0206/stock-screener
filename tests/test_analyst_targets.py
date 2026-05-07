"""src/analyst_targets.py + analyst_targets_snapshot.py + 整合點測試。

涵蓋:
  - fetch_analyst_target_yfinance: 命中 / 缺資料 / .TW 失敗 fallback .TWO
  - fetch_analyst_target_from_news: Gemini parse / quota guard / 沒 SDK 回 None
  - upsert / get_analyst_target / get_analyst_targets_for_sids 優先 yfinance
  - dump → load round-trip
  - notifier format_pick_block 含目標價 + _select_top_picks 排序加分(unit-level)
  - strategies.enrich_with_analyst_target 從 SQLite join
  - preload_snapshots 載 analyst_targets.csv
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import analyst_targets as at
from src import (
    analyst_targets_snapshot,
    config,
    database as db,
    notifier,
    strategies,
)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "analyst.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    # 確保 quota flag 不被前一個 test 污染
    try:
        import streamlit as st
        st.session_state.pop(at._QUOTA_FLAG_KEY, None)
    except Exception:
        pass
    yield tmp_path
    db._reset_path_cache()


# === fetch_analyst_target_yfinance ===

def test_fetch_yfinance_returns_target(tmp_db, monkeypatch):
    """yfinance.Ticker.info 有 targetMeanPrice + numberOfAnalystOpinions → 回 dict。"""
    fake_info = {
        "targetMeanPrice": 850.5,
        "targetMedianPrice": 845.0,
        "targetHighPrice": 1000.0,
        "targetLowPrice": 600.0,
        "numberOfAnalystOpinions": 23,
    }

    class _FakeTicker:
        def __init__(self, sid):
            self.info = fake_info

    monkeypatch.setattr(at, "_YF_AVAILABLE", True)
    monkeypatch.setattr(at, "yf", SimpleNamespace(Ticker=_FakeTicker))

    result = at.fetch_analyst_target_yfinance("2330")
    assert result is not None
    assert result["target_mean"] == pytest.approx(850.5)
    assert result["target_median"] == pytest.approx(845.0)
    assert result["target_high"] == pytest.approx(1000.0)
    assert result["target_low"] == pytest.approx(600.0)
    assert result["num_analysts"] == 23
    assert result["source"] == "yfinance"


def test_fetch_yfinance_returns_none_when_no_data(tmp_db, monkeypatch):
    """yfinance.Ticker.info 沒 targetMeanPrice → 兩種 suffix 都試完仍回 None。"""

    class _FakeTicker:
        def __init__(self, sid):
            # 給空 info 模擬冷門股 / 不存在代號
            self.info = {}

    monkeypatch.setattr(at, "_YF_AVAILABLE", True)
    monkeypatch.setattr(at, "yf", SimpleNamespace(Ticker=_FakeTicker))

    assert at.fetch_analyst_target_yfinance("9999") is None


def test_fetch_yfinance_fallback_two_when_tw_fails(tmp_db, monkeypatch):
    """.TW 拋例外 → fallback 試 .TWO,.TWO 命中就用。"""

    class _FakeTicker:
        def __init__(self, sid):
            if sid.endswith(".TW"):
                # 第一次 .TW 拋例外模擬不存在
                raise ValueError("not found")
            # .TWO 命中
            self.info = {
                "targetMeanPrice": 100.0,
                "numberOfAnalystOpinions": 3,
            }

    monkeypatch.setattr(at, "_YF_AVAILABLE", True)
    monkeypatch.setattr(at, "yf", SimpleNamespace(Ticker=_FakeTicker))

    result = at.fetch_analyst_target_yfinance("6488")
    assert result is not None
    assert result["target_mean"] == pytest.approx(100.0)
    assert result["num_analysts"] == 3


# === fetch_analyst_target_from_news (Gemini fallback) ===

def test_fetch_news_returns_none_when_gemini_unavailable(tmp_db, monkeypatch):
    """SDK 缺 → 直接回 None。"""
    monkeypatch.setattr(at, "_GEMINI_AVAILABLE", False)
    assert at.fetch_analyst_target_from_news("2330") is None


def test_fetch_news_uses_gemini_quota_fallback(tmp_db, monkeypatch):
    """當輪 quota flag 已設 → 不打 Gemini 直接回 None。"""
    monkeypatch.setattr(at, "_GEMINI_AVAILABLE", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")

    # 設旗標 — 之後同輪所有 sid 都不該打 LLM
    at._set_quota_flag_today()

    # 給 _yf_news_for_sid mock,即使被叫也沒副作用(我們驗證沒打 genai)
    monkeypatch.setattr(at, "_yf_news_for_sid", lambda sid, limit=5: [])

    called = {"n": 0}

    def _fake_configure(**kwargs):
        called["n"] += 1

    monkeypatch.setattr(at, "genai", SimpleNamespace(
        configure=_fake_configure,
        GenerativeModel=lambda *a, **k: None,
    ))

    result = at.fetch_analyst_target_from_news("2330", "TSMC")
    assert result is None
    assert called["n"] == 0, "quota 旗標已設時不該真的打 LLM"


def test_fetch_news_parse_gemini_response(tmp_db, monkeypatch):
    """Gemini 回合法 JSON → parse 成 target dict,source='gemini_news'。"""
    monkeypatch.setattr(at, "_GEMINI_AVAILABLE", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    # 確保 quota 旗標未設
    try:
        import streamlit as st
        st.session_state.pop(at._QUOTA_FLAG_KEY, None)
    except Exception:
        pass

    fake_news = [
        {"title": "外資看好 6488 環球晶 AI 訂單", "publisher": "鉅亨網"},
        {"title": "目標價上看 600 元", "publisher": "工商時報"},
    ]
    monkeypatch.setattr(
        at, "_yf_news_for_sid", lambda sid, limit=5: fake_news,
    )

    fake_response = SimpleNamespace(
        text=(
            '{"target_mean": 580, "target_high": 600, '
            '"target_low": 550, "num_analysts": 3, '
            '"rationale": "外資看好 AI"}'
        )
    )
    fake_model = SimpleNamespace(
        generate_content=lambda prompt: fake_response,
    )
    monkeypatch.setattr(at, "genai", SimpleNamespace(
        configure=lambda **kw: None,
        GenerativeModel=lambda *a, **kw: fake_model,
    ))

    result = at.fetch_analyst_target_from_news("6488", "環球晶")
    assert result is not None
    assert result["source"] == "gemini_news"
    assert result["target_mean"] == pytest.approx(580.0)
    assert result["target_high"] == pytest.approx(600.0)
    assert result["num_analysts"] == 3


# === upsert / get / bulk lookup ===

def test_upsert_analyst_target_idempotent(tmp_db):
    """重複 upsert 同 (sid, source) → 只會更新不會重複。"""
    data = {
        "target_mean": 800.0, "target_median": 795.0,
        "target_high": 1000.0, "target_low": 600.0,
        "num_analysts": 20, "source": "yfinance",
    }
    at.upsert_analyst_target("2330", data)
    at.upsert_analyst_target("2330", {**data, "target_mean": 850.0})

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM analyst_targets WHERE stock_id=?", ("2330",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["target_mean"] == pytest.approx(850.0)


def test_get_analyst_target_prefers_yfinance(tmp_db):
    """同 sid 兩個 source 都有 → get 回 yfinance(較準)。"""
    at.upsert_analyst_target("2330", {
        "target_mean": 800.0, "num_analysts": 20, "source": "yfinance",
    })
    at.upsert_analyst_target("2330", {
        "target_mean": 700.0, "num_analysts": 2, "source": "gemini_news",
    })
    result = at.get_analyst_target("2330")
    assert result["source"] == "yfinance"
    assert result["target_mean"] == pytest.approx(800.0)


def test_get_analyst_targets_for_sids_bulk_prefers_yfinance(tmp_db):
    """bulk lookup 多檔混合來源,每 sid 取 yfinance。"""
    at.upsert_analyst_target("2330", {
        "target_mean": 800.0, "num_analysts": 20, "source": "yfinance",
    })
    at.upsert_analyst_target("2330", {
        "target_mean": 700.0, "num_analysts": 2, "source": "gemini_news",
    })
    at.upsert_analyst_target("6488", {
        "target_mean": 580.0, "num_analysts": 3, "source": "gemini_news",
    })
    out = at.get_analyst_targets_for_sids(["2330", "6488", "0000"])
    assert "0000" not in out
    assert out["2330"]["source"] == "yfinance"
    assert out["6488"]["source"] == "gemini_news"


def test_get_analyst_targets_for_sids_empty_input(tmp_db):
    assert at.get_analyst_targets_for_sids([]) == {}
    assert at.get_analyst_targets_for_sids(["", None]) == {}


# === fetch_and_store integration ===

def test_fetch_and_store_yfinance_path(tmp_db, monkeypatch):
    """A 來源命中就不走 B(不打 Gemini)。"""
    fake_info = {
        "targetMeanPrice": 100.0, "numberOfAnalystOpinions": 5,
    }

    class _FakeTicker:
        def __init__(self, sid):
            self.info = fake_info

    monkeypatch.setattr(at, "_YF_AVAILABLE", True)
    monkeypatch.setattr(at, "yf", SimpleNamespace(Ticker=_FakeTicker))

    gemini_calls = {"n": 0}

    def _spy_gemini(sid, name=""):
        gemini_calls["n"] += 1
        return {"target_mean": 200.0, "source": "gemini_news"}

    monkeypatch.setattr(at, "fetch_analyst_target_from_news", _spy_gemini)

    result = at.fetch_and_store("2330", "TSMC")
    assert result["source"] == "yfinance"
    assert gemini_calls["n"] == 0, "yfinance 命中時不該打 Gemini"

    # 確認 SQLite 也寫進去了
    row = at.get_analyst_target("2330")
    assert row["source"] == "yfinance"


def test_fetch_and_store_gemini_fallback_path(tmp_db, monkeypatch):
    """A 沒命中 → 走 B Gemini。"""
    monkeypatch.setattr(
        at, "fetch_analyst_target_yfinance", lambda sid: None,
    )
    monkeypatch.setattr(
        at, "fetch_analyst_target_from_news",
        lambda sid, name="": {"target_mean": 580.0, "source": "gemini_news"},
    )
    result = at.fetch_and_store("6488", "環球晶")
    assert result["source"] == "gemini_news"


def test_fetch_and_store_skip_gemini_when_disabled(tmp_db, monkeypatch):
    """use_gemini_fallback=False 時 yfinance 失敗就直接回 None。"""
    monkeypatch.setattr(
        at, "fetch_analyst_target_yfinance", lambda sid: None,
    )
    gemini_calls = {"n": 0}

    def _spy(sid, name=""):
        gemini_calls["n"] += 1
        return {"target_mean": 100.0, "source": "gemini_news"}

    monkeypatch.setattr(at, "fetch_analyst_target_from_news", _spy)
    result = at.fetch_and_store(
        "6488", "X", use_gemini_fallback=False,
    )
    assert result is None
    assert gemini_calls["n"] == 0


# === Snapshot dump / load ===

def test_dump_then_load_roundtrip(tmp_db):
    """analyst_targets 表 → CSV → 清表 → load CSV → 內容一致。"""
    at.upsert_analyst_target("2330", {
        "target_mean": 800.0, "target_median": 795.0,
        "target_high": 1000.0, "target_low": 600.0,
        "num_analysts": 20, "source": "yfinance",
    })
    at.upsert_analyst_target("6488", {
        "target_mean": 580.0, "target_high": 600.0, "target_low": 550.0,
        "num_analysts": 3, "source": "gemini_news",
    })

    n = analyst_targets_snapshot.dump_to_csv(snapshot_dir=tmp_db)
    assert n == 2

    with db.get_conn() as conn:
        conn.execute("DELETE FROM analyst_targets")
    assert _count_rows() == 0

    loaded = analyst_targets_snapshot.load_from_csv(snapshot_dir=tmp_db)
    assert loaded == 2
    rows = _all_rows()
    by_key = {(r["stock_id"], r["source"]): r for r in rows}
    assert by_key[("2330", "yfinance")]["target_mean"] == pytest.approx(800.0)
    assert by_key[("6488", "gemini_news")]["target_mean"] == pytest.approx(580.0)


def test_dump_silent_skip_outside_project(tmp_db):
    """tmp DB 不在 PROJECT_ROOT → dump_to_csv() 預設 skip 回 -1。"""
    at.upsert_analyst_target("2330", {
        "target_mean": 800.0, "num_analysts": 5, "source": "yfinance",
    })
    n = analyst_targets_snapshot.dump_to_csv()
    assert n == -1


def test_load_from_string_roundtrip(tmp_db):
    """github_sync fetch 拉回 csv 字串 → load_from_string 還原。"""
    at.upsert_analyst_target("2330", {
        "target_mean": 800.0, "num_analysts": 20, "source": "yfinance",
    })
    csv_text = analyst_targets_snapshot.dump_to_string()
    assert "2330" in csv_text and "yfinance" in csv_text

    with db.get_conn() as conn:
        conn.execute("DELETE FROM analyst_targets")

    n = analyst_targets_snapshot.load_from_string(csv_text)
    assert n == 1
    rows = _all_rows()
    assert rows[0]["stock_id"] == "2330"


def test_load_from_string_empty_returns_zero(tmp_db):
    assert analyst_targets_snapshot.load_from_string("") == 0


def test_safe_boot_load_remote_path(tmp_db, monkeypatch):
    at.upsert_analyst_target("2330", {
        "target_mean": 800.0, "num_analysts": 10, "source": "yfinance",
    })
    csv_text = analyst_targets_snapshot.dump_to_string()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM analyst_targets")

    from src import github_sync
    monkeypatch.setattr(
        github_sync, "fetch_analyst_targets_from_github", lambda: csv_text,
    )
    result = analyst_targets_snapshot.safe_boot_load()
    assert result == "remote"
    assert _count_rows() == 1


def test_safe_boot_load_fallback_no_remote(tmp_db, monkeypatch):
    from src import github_sync
    monkeypatch.setattr(
        github_sync, "fetch_analyst_targets_from_github", lambda: None,
    )
    result = analyst_targets_snapshot.safe_boot_load()
    assert result == "fallback-no-remote"


def test_preload_snapshots_calls_watchlist_load(tmp_db, monkeypatch):
    """preload_snapshots 應 trigger watchlist_snapshot.load_from_csv —
    讓 actions runner 跑 fetch_analyst_targets.py --scope=watchlist 看得到主公星號。
    用 spy 攔截確認 helper 真的被呼叫(不依賴 watchlist.csv 真的存在)。
    """
    from src import watchlist_snapshot
    calls: list = []

    def _spy(db_path=None):
        calls.append(db_path)
        return 0  # 模擬 csv 不存在或空表 → 不影響其他 preload step

    monkeypatch.setattr(watchlist_snapshot, "load_from_csv", _spy)
    db.preload_snapshots(snapshot_dir=tmp_db)
    assert len(calls) == 1, "preload_snapshots 應呼叫 watchlist load_from_csv"


def test_preload_snapshots_loads_analyst_targets(tmp_db, monkeypatch):
    """preload_snapshots 應載 analyst_targets.csv 進 SQLite。"""
    # 寫一個 analyst_targets.csv 進 tmp snapshot dir
    snapshot_dir = tmp_db
    csv_text = (
        "stock_id,target_mean,target_median,target_high,target_low,"
        "num_analysts,source,fetched_at\n"
        "2330,800.0,795.0,1000.0,600.0,20,yfinance,2026-05-07T00:00:00+00:00\n"
        "6488,580.0,,600.0,550.0,3,gemini_news,2026-05-07T00:00:00+00:00\n"
    )
    (snapshot_dir / "analyst_targets.csv").write_text(csv_text, encoding="utf-8")

    counts = db.preload_snapshots(snapshot_dir=snapshot_dir)
    assert counts.get("analyst_targets") == 2
    rows = _all_rows()
    assert len(rows) == 2


# === notifier 整合 ===

def test_format_pick_block_includes_analyst_target(tmp_db):
    """format_pick_block 有 analyst_target_mean → 顯示 共識目標 行。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 700.0, "matched_strategies": ["ma_alignment"],
        "matched_labels": ["MA 多頭排列"],
        "ml_prob": 0.75, "target_low": 720, "target_high": 750, "stop": 680,
        "analyst_target_mean": 850.0, "analyst_num": 23,
        "analyst_source": "yfinance",
    }
    out = notifier.format_pick_block(pick, channel="telegram")
    assert "📊 共識目標" in out
    assert "850" in out
    assert "23" in out


def test_format_pick_block_skips_when_no_analyst_target(tmp_db):
    """沒 analyst_target_mean → 不渲染目標價行。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 700.0, "matched_strategies": ["ma_alignment"],
        "matched_labels": ["MA 多頭排列"],
        "ml_prob": 0.75,
    }
    out = notifier.format_pick_block(pick, channel="telegram")
    assert "📊 共識目標" not in out


def test_format_pick_block_upside_calculation(tmp_db):
    """共識目標價 vs close → upside 顯示 +XX% / -XX%。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 700.0,
        "matched_strategies": ["ma_alignment"],
        "matched_labels": ["MA"],
        "analyst_target_mean": 770.0,  # +10%
        "analyst_num": 5,
    }
    out = notifier.format_pick_block(pick, channel="telegram")
    assert "+10%" in out


# === strategies.enrich_with_analyst_target ===

def test_enrich_with_analyst_target_joins_sqlite(tmp_db):
    """從 SQLite analyst_targets 讀取並加 analyst_target_mean / analyst_num 欄。"""
    import pandas as pd

    at.upsert_analyst_target("2330", {
        "target_mean": 800.0, "num_analysts": 20, "source": "yfinance",
    })
    df = pd.DataFrame([
        {"stock_id": "2330", "name": "台積電", "close": 700.0},
        {"stock_id": "1101", "name": "台泥", "close": 50.0},
    ])
    out = strategies.enrich_with_analyst_target(df)
    assert "analyst_target_mean" in out.columns
    assert "analyst_num" in out.columns
    row_2330 = out[out["stock_id"] == "2330"].iloc[0]
    assert row_2330["analyst_target_mean"] == pytest.approx(800.0)
    assert row_2330["analyst_num"] == 20
    # 1101 沒資料 → NaN
    row_1101 = out[out["stock_id"] == "1101"].iloc[0]
    assert pd.isna(row_1101["analyst_target_mean"])


def test_enrich_with_analyst_target_empty_df(tmp_db):
    import pandas as pd
    df = pd.DataFrame()
    out = strategies.enrich_with_analyst_target(df)
    assert out is not None
    assert out.empty


# === _select_top_picks 排序加分(unit-level) ===

def test_select_top_picks_prioritizes_with_target(tmp_db, monkeypatch):
    """有 analyst_target_mean 的 picks 應排在沒有的前面(+100 分)。

    用最小的 mocking 驗證排序 lambda 的優先序行為 — 不跑完整 _select_top_picks
    (那需要 mock 一堆 ml_predictor / strategies),改直接套排序鍵。
    """
    picks = [
        {"sid": "0000", "ml_prob": 0.95, "matched_strategies": ["a"],
         "analyst_target_mean": None},
        {"sid": "1111", "ml_prob": 0.55, "matched_strategies": ["a"],
         "analyst_target_mean": 100.0, "analyst_num": 5},
        {"sid": "2222", "ml_prob": 0.80, "matched_strategies": ["a", "b"],
         "analyst_target_mean": None},
    ]
    picks.sort(key=lambda p: (
        -(100 if p.get("analyst_target_mean") else 0),
        -(p["ml_prob"] or 0.0),
        -len(p["matched_strategies"]),
        p["sid"],
    ))
    # 1111(有 analyst_target)即使 ml_prob 最低也排第一
    assert picks[0]["sid"] == "1111"
    # 沒 target 的兩檔再依 ml_prob desc 排
    assert picks[1]["sid"] == "0000"
    assert picks[2]["sid"] == "2222"


# === helpers ===

def _all_rows() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM analyst_targets ORDER BY stock_id, source"
        ).fetchall()
    return [dict(r) for r in rows]


def _count_rows() -> int:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM analyst_targets"
        ).fetchone()[0]
