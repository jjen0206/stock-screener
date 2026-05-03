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


def test_get_company_profile_llm_failure_returns_facts_with_error(
    tmp_db, monkeypatch,
):
    """Gemini API 拋 exception → facts 仍寫進 cache,llm_error 帶 user-facing 訊息。"""
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)
    monkeypatch.setattr(
        cp, "generate_with_gemini",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("API quota exceeded")),
    )

    profile = cp.get_company_profile("2330")
    # facts 還是有
    assert profile["industry"] == "半導體業"
    assert profile["market"] == "上市"
    # LLM 失敗 → description None + llm_error 有訊息
    assert profile["description"] is None
    assert profile["llm_error"] is not None
    assert "API quota exceeded" in profile["llm_error"]


def test_get_company_profile_no_gemini_key_returns_facts_only(
    tmp_db, monkeypatch,
):
    """沒設 GEMINI_API_KEY → 只回 facts,description None,llm_error 帶訊息。"""
    _seed_finmind_mock(monkeypatch)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(cp, "_GEMINI_AVAILABLE", True)
    # 這條路徑下 generate_with_gemini 真的會被呼叫(因為 cache 還沒 description),
    # 但 SDK 內 raise RuntimeError("GEMINI_API_KEY 未設定")— 走 fallback 流程

    profile = cp.get_company_profile("2330")
    assert profile["industry"] == "半導體業"
    assert profile["description"] is None
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
