"""src/company_profile.py 測試 — FinMind facts + Gemini cache 行為。

策略:
- 一律 mock data_fetcher.ensure_stock_info / _fetch_all_stock_info(不打 FinMind)
- mock company_profile.generate_with_gemini(不打 Gemini API、不需 google-generativeai)
- 用 tmp_db fixture 確保 SQLite 寫入隔離
"""
from __future__ import annotations

import pytest

from src import company_profile as cp
from src import config, database as db, data_fetcher


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "company.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    # 清掉 data_fetcher 的記憶體 cache 避免測試之間污染
    data_fetcher._reset_stock_info_cache()
    # 清掉 streamlit session_state 內 quota flag(streamlit 在 pytest 程序內
    # 有 SessionStateProxy 會跨 tests 殘留;測試 quota 路徑時若上個 test 設了
    # 旗標,這個 test 的 LLM call 會被跳過 → 預期外的 narrative_status)
    try:
        import streamlit as st
        st.session_state.pop(cp._QUOTA_FLAG_KEY, None)
    except Exception:
        pass
    yield db_file
    db._reset_path_cache()


def _seed_finmind_mock(monkeypatch):
    """讓 fetch_taiwan_stock_info 不打 FinMind,直接回 fake facts。"""
    fake_info = {
        "stock_id": "2330", "name": "台積電", "industry": "半導體業",
    }
    monkeypatch.setattr(
        data_fetcher, "ensure_stock_info",
        lambda sid: fake_info if sid == "2330" else None,
    )
    monkeypatch.setattr(
        data_fetcher, "_fetch_all_stock_info",
        lambda: {
            "2330": {
                "stock_id": "2330", "stock_name": "台積電",
                "industry_category": "半導體業", "type": "twse",
            },
        },
    )


# === fetch_taiwan_stock_info ===

def test_fetch_taiwan_stock_info_returns_industry_and_market(tmp_db, monkeypatch):
    """走 ensure_stock_info + _fetch_all_stock_info,組出 industry/market。"""
    _seed_finmind_mock(monkeypatch)
    info = cp.fetch_taiwan_stock_info("2330")
    assert info["industry"] == "半導體業"
    assert info["market"] == "上市"  # type=twse → 上市
    assert info["name"] == "台積電"


def test_fetch_taiwan_stock_info_unknown_returns_empty(tmp_db, monkeypatch):
    """無效股號 → ensure_stock_info 回 None → 全 None facts。"""
    monkeypatch.setattr(data_fetcher, "ensure_stock_info", lambda sid: None)
    info = cp.fetch_taiwan_stock_info("9999")
    assert info["industry"] is None
    assert info["market"] is None


# === generate_with_gemini ===

def test_generate_with_gemini_no_api_key_raises(tmp_db, monkeypatch):
    """沒設 GEMINI_API_KEY → 拋 RuntimeError(讓呼叫端 fallback)。"""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        cp.generate_with_gemini("2330", "台積電", "半導體業")


def test_generate_with_gemini_sdk_unavailable_raises(tmp_db, monkeypatch):
    """google-generativeai 沒裝 → 拋 RuntimeError。"""
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    with pytest.raises(RuntimeError, match="generativeai"):
        cp.generate_with_gemini("2330", "台積電", "半導體業")


def test_generate_with_gemini_strips_code_fence(tmp_db, monkeypatch):
    """LLM 回 ```json ... ``` 包覆的 JSON,_strip_code_fence + json.loads 要過。"""
    cleaned = cp._strip_code_fence(
        '```json\n{"description": "做晶圓代工", "uniqueness": "全球第一",'
        ' "moat": "規模優勢"}\n```'
    )
    assert not cleaned.startswith("```")
    import json
    parsed = json.loads(cleaned)
    assert parsed["description"] == "做晶圓代工"


# === get_company_profile cache 行為 ===

def test_get_company_profile_cache_miss_triggers_finmind_and_llm(
    tmp_db, monkeypatch,
):
    """第一次查 → 打 FinMind + 打 Gemini,寫進 SQLite。"""
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    fake_llm = {
        "description": "晶圓代工龍頭",
        "uniqueness": "領先製程節點",
        "moat": "規模 + 客戶綁定",
    }
    llm_calls = []
    def _fake_gen(sid, name, ind):
        llm_calls.append((sid, name, ind))
        return fake_llm
    monkeypatch.setattr(cp, "generate_with_gemini", _fake_gen)

    profile = cp.get_company_profile("2330")

    assert profile["industry"] == "半導體業"
    assert profile["market"] == "上市"
    assert profile["description"] == "晶圓代工龍頭"
    assert profile["uniqueness"] == "領先製程節點"
    assert profile["moat"] == "規模 + 客戶綁定"
    assert profile["llm_error"] is None
    assert len(llm_calls) == 1
    assert llm_calls[0] == ("2330", "台積電", "半導體業")


def test_get_company_profile_cache_hit_skips_llm(tmp_db, monkeypatch):
    """第二次查同一檔 → 不該再打 Gemini(cache 已有 description)。"""
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    llm_calls = []
    def _fake_gen(sid, name, ind):
        llm_calls.append(sid)
        return {"description": "X", "uniqueness": "Y", "moat": "Z"}
    monkeypatch.setattr(cp, "generate_with_gemini", _fake_gen)

    cp.get_company_profile("2330")  # miss → 打 1 次
    cp.get_company_profile("2330")  # hit → 不該再打
    cp.get_company_profile("2330")  # hit → 不該再打

    assert len(llm_calls) == 1, f"cache hit 應跳過 LLM,實際 {llm_calls}"


def test_get_company_profile_regenerate_forces_llm_recall(tmp_db, monkeypatch):
    """regenerate=True → 即使 cache 有,還是要重打 Gemini。"""
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    call_count = [0]
    def _fake_gen(sid, name, ind):
        call_count[0] += 1
        return {
            "description": f"v{call_count[0]}",
            "uniqueness": f"u{call_count[0]}",
            "moat": f"m{call_count[0]}",
        }
    monkeypatch.setattr(cp, "generate_with_gemini", _fake_gen)

    cp.get_company_profile("2330")  # 第 1 次
    p2 = cp.get_company_profile("2330", regenerate=True)  # 第 2 次強制
    p3 = cp.get_company_profile("2330", regenerate=True)  # 第 3 次強制

    assert call_count[0] == 3
    assert p2["description"] == "v2"
    assert p3["description"] == "v3"


def test_get_company_profile_generic_failure_returns_facts_with_error(
    tmp_db, monkeypatch,
):
    """Gemini API 拋 generic exception(非 quota / 非 config)→ facts 仍寫進
    cache,narrative_status='failed',llm_error 是友善短訊(不含 raw exception)。
    """
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)
    monkeypatch.setattr(
        cp, "generate_with_gemini",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("Network unreachable: connection refused")
        ),
    )

    profile = cp.get_company_profile("2330")
    assert profile["industry"] == "半導體業"
    assert profile["market"] == "上市"
    assert profile["description"] is None
    assert profile["narrative_status"] == "failed"
    assert profile["llm_error"] is not None
    # 不該 dump raw exception 字串給 user
    assert "Network unreachable" not in profile["llm_error"]
    assert "connection refused" not in profile["llm_error"]
    # 是友善訊息(短)
    assert len(profile["llm_error"]) < 50


def test_get_company_profile_quota_exceeded_returns_facts_with_quota_status(
    tmp_db, monkeypatch,
):
    """打 Gemini 撞 429 → narrative_status='quota_exceeded',llm_error 是友善
    訊息(不 dump 整段 google API error)。"""
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)
    # 模擬 google api_core ResourceExhausted 的訊息格式(含 429 + quota)
    monkeypatch.setattr(
        cp, "generate_with_gemini",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError(
                "429 You exceeded your current quota, please check your "
                "plan and billing details. quota_metric: ..."
            )
        ),
    )

    profile = cp.get_company_profile("2330")
    assert profile["industry"] == "半導體業"
    assert profile["description"] is None
    assert profile["narrative_status"] == "quota_exceeded"
    assert profile["llm_error"] is not None
    # 不 dump 原始 google API 錯誤(429 / quota_metric / billing 等技術字)
    assert "429" not in profile["llm_error"]
    assert "quota_metric" not in profile["llm_error"]
    # 是友善繁中訊息
    assert "額度" in profile["llm_error"]


def test_get_company_profile_quota_flag_skips_subsequent_llm_calls(
    tmp_db, monkeypatch,
):
    """打到 429 → session_state quota flag 設;後續同/異 sid 直接跳過 LLM,
    不再撞 429 浪費 RTT。regenerate=True 仍會強制再試。"""
    _seed_finmind_mock(monkeypatch)
    # 第二檔股號的 finmind mock(2317 鴻海)— 補進 mock
    monkeypatch.setattr(
        data_fetcher, "ensure_stock_info",
        lambda sid: (
            {"stock_id": "2330", "name": "台積電", "industry": "半導體業"}
            if sid == "2330" else
            {"stock_id": "2317", "name": "鴻海", "industry": "電腦及週邊設備業"}
            if sid == "2317" else None
        ),
    )
    monkeypatch.setattr(
        data_fetcher, "_fetch_all_stock_info",
        lambda: {
            "2330": {"type": "twse"},
            "2317": {"type": "twse"},
        },
    )
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    call_count = [0]
    def _raiser(*a, **kw):
        call_count[0] += 1
        raise RuntimeError("429 quota exceeded")
    monkeypatch.setattr(cp, "generate_with_gemini", _raiser)

    p1 = cp.get_company_profile("2330")
    p2 = cp.get_company_profile("2317")  # 不同 sid

    assert p1["narrative_status"] == "quota_exceeded"
    assert p2["narrative_status"] == "quota_exceeded"
    # 第一次撞 429,設旗標;第二次直接跳過 → 總共只打 1 次
    assert call_count[0] == 1, (
        f"預期 LLM 只打 1 次(第二次因 quota flag 跳過),實際 {call_count[0]}"
    )

    # regenerate=True 強制再打一次(讓 user 手動重試)
    p3 = cp.get_company_profile("2330", regenerate=True)
    assert p3["narrative_status"] == "quota_exceeded"
    assert call_count[0] == 2, (
        f"regenerate=True 該再打一次,實際 total {call_count[0]}"
    )


def test_get_company_profile_llm_call_false_skips_llm_on_cache_miss(
    tmp_db, monkeypatch,
):
    """llm_call=False 模式 — cache miss 不打 LLM,回 narrative_status='not_loaded'。

    這是 user-button-gated 流程的核心:138 picks 全展開只走 cache-only path,
    0 LLM call。實際打 LLM 的 path 走 llm_call=True(user 點按鈕)。
    """
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)

    llm_call_count = [0]
    def _llm_spy(*a, **kw):
        llm_call_count[0] += 1
        return {"description": "X", "uniqueness": "Y", "moat": "Z"}
    monkeypatch.setattr(cp, "generate_with_gemini", _llm_spy)

    profile = cp.get_company_profile("2330", llm_call=False)

    # facts 仍補(FinMind 是免費快路徑,不算 LLM)
    assert profile["industry"] == "半導體業"
    assert profile["market"] == "上市"
    # description 沒生成
    assert profile["description"] is None
    assert profile["narrative_status"] == "not_loaded"
    # **核心 assert**:LLM 完全沒被呼叫
    assert llm_call_count[0] == 0, (
        f"llm_call=False 模式下不該打 LLM, 實際打了 {llm_call_count[0]} 次"
    )


def test_get_company_profile_llm_call_false_returns_cached_narrative(
    tmp_db, monkeypatch,
):
    """cache hit + llm_call=False → 直接回 cache 內 narrative,不打 LLM。"""
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)
    monkeypatch.setattr(
        cp, "generate_with_gemini",
        lambda *a, **kw: {
            "description": "晶圓代工", "uniqueness": "領先製程", "moat": "規模",
        },
    )

    # 先用 llm_call=True 寫進 cache
    p1 = cp.get_company_profile("2330", llm_call=True)
    assert p1["description"] == "晶圓代工"

    # 改 mock 讓「萬一被叫」就拋錯,守門 — 但 llm_call=False 應跳過
    def _fail(*a, **kw):
        raise AssertionError("llm_call=False 不該叫 LLM,但被叫了")
    monkeypatch.setattr(cp, "generate_with_gemini", _fail)

    p2 = cp.get_company_profile("2330", llm_call=False)
    assert p2["narrative_status"] == "ok"
    assert p2["description"] == "晶圓代工"


def test_get_company_profile_no_gemini_key_returns_facts_only(
    tmp_db, monkeypatch,
):
    """沒設 GEMINI_API_KEY → narrative_status='not_configured',llm_error 提示。"""
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)
    # generate_with_gemini 內部 raise RuntimeError("GEMINI_API_KEY 未設定")
    # → _is_not_configured_error 偵測到 → narrative_status='not_configured'

    profile = cp.get_company_profile("2330")
    assert profile["industry"] == "半導體業"
    assert profile["description"] is None
    assert profile["narrative_status"] == "not_configured"
    assert profile["llm_error"] is not None
    assert "GEMINI_API_KEY" in profile["llm_error"]


def test_get_company_profile_empty_stock_id_returns_empty(tmp_db):
    """空字串股號 → 回 empty profile,不打任何 API。"""
    profile = cp.get_company_profile("")
    assert profile["stock_id"] == ""
    assert profile["industry"] is None
    assert profile["description"] is None


def test_get_company_profile_writes_to_company_profiles_table(
    tmp_db, monkeypatch,
):
    """成功流程 → SQLite company_profiles 表必須有資料。"""
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)
    monkeypatch.setattr(
        cp, "generate_with_gemini",
        lambda *a, **kw: {
            "description": "D", "uniqueness": "U", "moat": "M",
        },
    )

    cp.get_company_profile("2330")

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM company_profiles WHERE stock_id=?", ("2330",),
        ).fetchone()
    assert row is not None
    assert row["industry"] == "半導體業"
    assert row["description"] == "D"
    assert row["finmind_updated_at"] is not None
    assert row["llm_updated_at"] is not None
