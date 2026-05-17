"""src/ai_assistant.py 單元測試(2026-05-17 加,C 任務)。

涵蓋:
  - kill-switch env AI_ASSISTANT_ENABLED
  - collect_stock_context 拉得到價量 / 法人 / 警示 / news / picks / positions
  - build_stock_prompt 含必要欄位
  - ask_about_stock graceful 處理 Gemini 失敗
  - ask_about_market 不爆
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from src import ai_assistant, database as db  # noqa: E402

# tmp_db fixture 從 conftest 來


# === fixture seeders ===

def _seed_basic(sid: str = "2330", name: str = "台積電") -> None:
    """寫一檔 stock + 30 天 daily_prices + 法人 + 警示 + 一則新聞。"""
    with db.get_conn() as conn:
        # stocks
        conn.execute(
            "INSERT OR REPLACE INTO stocks (stock_id, name, market, "
            "industry, updated_at) VALUES (?, ?, 'TW', '半導體', ?)",
            (sid, name, datetime.now(timezone.utc).isoformat()),
        )
        # 30 day prices
        for i in range(30, 0, -1):
            d = f"2026-04-{i:02d}" if i <= 30 else f"2026-05-{i-30:02d}"
            d = f"2026-04-{i:02d}"
            close = 500 + i * 2
            conn.execute(
                "INSERT INTO daily_prices (stock_id, date, open, high, low, "
                "close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, d, close, close + 5, close - 5, close, 10000 + i * 100),
            )
        # 法人 3 日
        for i, d in enumerate(["2026-04-28", "2026-04-29", "2026-04-30"]):
            conn.execute(
                "INSERT INTO institutional (stock_id, date, "
                "foreign_buy_sell, trust_buy_sell, dealer_buy_sell) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, d, 1000 + i * 100, 500, 100),
            )
        # 警示一筆(生效中)
        conn.execute(
            "INSERT INTO stock_warnings (stock_id, warning_type, "
            "announced_date, fetched_at) VALUES (?, 'attention', "
            "'2026-04-15', ?)",
            (sid, datetime.now(timezone.utc).isoformat()),
        )
        # 一則新聞
        conn.execute(
            "INSERT INTO news (sid, publish_date, subject, url_hash, "
            "fetched_at) VALUES (?, '2026-04-25', '澄清新聞報導', 'hash1', ?)",
            (sid, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


# === kill-switch ===

def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("AI_ASSISTANT_ENABLED", raising=False)
    assert ai_assistant.is_enabled() is True


def test_is_enabled_false_when_disabled(monkeypatch):
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "false")
    assert ai_assistant.is_enabled() is False


def test_ask_stock_returns_disabled_payload_when_killed(monkeypatch, tmp_db):
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "false")
    res = ai_assistant.ask_about_stock("2330")
    assert res["ok"] is False
    assert "下班" in res["answer"] or "disabled" in res["answer"].lower()


def test_ask_market_returns_disabled_payload_when_killed(monkeypatch, tmp_db):
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "false")
    res = ai_assistant.ask_about_market()
    assert res["ok"] is False


# === collect_stock_context ===

def test_collect_stock_context_pulls_all(tmp_db):
    _seed_basic("2330", "台積電")
    ctx = ai_assistant.collect_stock_context("2330")
    assert ctx["meta"]["sid"] == "2330"
    assert ctx["meta"]["name"] == "台積電"
    assert ctx["price"]
    assert ctx["price"]["close"] > 0
    # 法人 3 日合計外資 1000+1100+1200 = 3300
    assert ctx["institutional"]["foreign_3d"] == 3300
    # 警示 1 筆
    assert len(ctx["warnings"]) == 1
    assert ctx["warnings"][0]["type"] == "attention"
    # 新聞 1 則
    assert len(ctx["news"]) == 1


def test_collect_stock_context_unknown_sid_returns_empty_price(tmp_db):
    ctx = ai_assistant.collect_stock_context("0000")
    # 應該回有 meta 但 price 是空 dict
    assert ctx["meta"]["sid"] == "0000"
    assert ctx["price"] == {}


# === prompt builder ===

def test_build_stock_prompt_contains_required(tmp_db):
    _seed_basic("2330", "台積電")
    ctx = ai_assistant.collect_stock_context("2330")
    prompt = ai_assistant.build_stock_prompt("2330", "技術面如何", ctx)
    # 軍師人設
    assert "軍師" in prompt
    # sid + 公司名
    assert "2330" in prompt
    assert "台積電" in prompt
    # 主公提問
    assert "技術面如何" in prompt
    # 法人區塊
    assert "法人" in prompt
    # 警示區塊
    assert "警示" in prompt


def test_build_stock_prompt_uses_default_question_when_empty(tmp_db):
    _seed_basic("2330", "台積電")
    ctx = ai_assistant.collect_stock_context("2330")
    prompt = ai_assistant.build_stock_prompt("2330", "", ctx)
    # 預設問題
    assert "綜合判讀" in prompt or "建議" in prompt


def test_build_market_prompt_basic():
    ctx = {
        "regime": {"label": "多頭", "regime": "bull"},
        "us_sentiment": {"label": "risk_on"},
        "theme_heat_top": [{"theme": "AI", "data": {}}],
        "picks_today": 5,
        "active_warnings_count": 3,
    }
    prompt = ai_assistant.build_market_prompt("今天能進場嗎", ctx)
    assert "軍師" in prompt
    assert "多頭" in prompt
    assert "今天能進場嗎" in prompt


# === ask_about_stock 端到端 ===

def test_ask_about_stock_no_data_returns_friendly_msg(monkeypatch, tmp_db):
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "true")
    res = ai_assistant.ask_about_stock("9999")
    assert res["ok"] is False
    assert "找不到" in res["answer"]


def test_ask_about_stock_gemini_failure_graceful(monkeypatch, tmp_db):
    """Gemini call 拋 → 回 ok=False + answer 含 fallback,不 raise。"""
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "true")
    _seed_basic("2330", "台積電")

    def _boom(prompt):
        raise RuntimeError("network down")

    monkeypatch.setattr(ai_assistant, "_call_gemini", _boom)
    res = ai_assistant.ask_about_stock("2330", "怎麼樣")
    assert res["ok"] is False
    assert "無法回應" in res["answer"]
    # context_summary 還是有東西(代表資料有抓到)
    assert res["context_summary"]


def test_ask_about_stock_happy_path_with_mock(monkeypatch, tmp_db):
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "true")
    _seed_basic("2330", "台積電")
    monkeypatch.setattr(
        ai_assistant, "_call_gemini",
        lambda prompt: "📊 技術面偏多。⚠️ 僅供研究。",
    )
    res = ai_assistant.ask_about_stock("2330")
    assert res["ok"] is True
    assert "技術面偏多" in res["answer"]
    assert res["model"] == ai_assistant.GEMINI_MODEL


def test_ask_about_stock_empty_sid(monkeypatch, tmp_db):
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "true")
    res = ai_assistant.ask_about_stock("  ")
    assert res["ok"] is False
    assert res["error"] == "empty_sid"


# === ask_about_market 端到端 ===

def test_ask_about_market_happy_path_with_mock(monkeypatch, tmp_db):
    monkeypatch.setenv("AI_ASSISTANT_ENABLED", "true")
    monkeypatch.setattr(
        ai_assistant, "_call_gemini",
        lambda prompt: "📊 大盤偏多。",
    )
    res = ai_assistant.ask_about_market("如何")
    assert res["ok"] is True
    assert "大盤偏多" in res["answer"]
