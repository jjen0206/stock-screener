"""「📊 強者跟蹤」regime banner E2E smoke。

不 mock streamlit、用 AppTest 跑真頁面;seed TAIEX daily_prices 強迫
compute_regime 回特定 regime → 驗證對應 banner 真的渲染出來。

守住:
1. bull regime → at.success 含「大盤偏多」
2. bear regime → at.error 含「大盤偏空,小心追高」
3. 4 個 tab(法人共識榜 / 千張大戶進場榜 / 綜合排行 / 高信心精選)仍正常 render
4. 整頁 0 exception
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _exc_msgs(at: AppTest) -> str:
    return "\n".join(str(e.value) for e in at.exception)


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """乾淨 tmp DB,init schema 後不灌 CSV snapshot(避免 TAIEX 預設灌進來)。"""
    import streamlit as st
    from src import config, database as db

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "sf_regime.db"))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()

    # 把 preload_snapshots wrap 成 no-op,確保 TAIEX 由 test 自己控制 seed
    monkeypatch.setattr(db, "preload_snapshots", lambda *a, **kw: {})

    sys.modules.pop("app", None)
    st.cache_data.clear()
    yield tmp_path
    db._reset_path_cache()


def _seed_taiex(closes: list[float], anchor: str = "2026-05-13") -> None:
    """灌 TAIEX 60+ 日 daily_prices,closes[0] 對應最新交易日 anchor。"""
    from datetime import date, timedelta
    from src import database as db
    d = date.fromisoformat(anchor)
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "stock_id": "TAIEX",
            "date": (d - timedelta(days=i)).isoformat(),
            "open": c, "high": c, "low": c, "close": c,
            "volume": 1, "trading_money": c,
        })
    with db.get_conn() as conn:
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO daily_prices "
                "(stock_id, date, open, high, low, close, volume, trading_money) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (r["stock_id"], r["date"], r["open"], r["high"], r["low"],
                 r["close"], r["volume"], r["trading_money"]),
            )


def test_strong_follower_bull_regime_banner_renders(isolated_db):
    """close > MA20 > MA60 → bull → 🟢 大盤偏多 banner 顯示。"""
    # 60 天 closes:遠 → 近,值單調遞增 → 最新 close 最高 → bull
    closes = list(range(15000, 18000, 50))[:60]  # 60 天
    closes_desc = list(reversed(closes))  # closes_desc[0] = 最新最高
    _seed_taiex(closes_desc)

    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["active_page"] = "📊 強者跟蹤"
    at.session_state["high_confidence_mode"] = False
    at.run()
    assert not at.exception, f"page boot 炸: {_exc_msgs(at)}"

    success_text = "\n".join(str(s.value) for s in at.success)
    assert "大盤偏多" in success_text, (
        f"bull regime 沒看到「大盤偏多」success banner;at.success={success_text}"
    )


def test_strong_follower_bear_regime_banner_renders(isolated_db):
    """close < MA20 < MA60 → bear → 🔴 大盤偏空,小心追高 banner 顯示。"""
    # closes_desc[0] = 最新最低 → bear
    closes = list(range(15000, 18000, 50))[:60]
    _seed_taiex(closes)  # 不 reverse → closes[0]=15000 為最新

    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["active_page"] = "📊 強者跟蹤"
    at.session_state["high_confidence_mode"] = False
    at.run()
    assert not at.exception, f"page boot 炸: {_exc_msgs(at)}"

    error_text = "\n".join(str(e.value) for e in at.error)
    assert "大盤偏空" in error_text and "小心追高" in error_text, (
        f"bear regime 沒看到「大盤偏空,小心追高」error banner;at.error={error_text}"
    )


def test_strong_follower_four_tabs_still_render(isolated_db):
    """regime banner 加上去後,4 個既有 tab 仍要正常 render(no regression)。"""
    closes = list(range(15000, 18000, 50))[:60]
    _seed_taiex(list(reversed(closes)))

    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["active_page"] = "📊 強者跟蹤"
    at.session_state["high_confidence_mode"] = False
    at.run()
    assert not at.exception, f"page boot 炸: {_exc_msgs(at)}"

    tab_labels = [t.label for t in at.tabs]
    expected = {
        "🏛️ 法人共識榜", "🐋 千張大戶進場榜",
        "🎯 綜合排行", "✨ 高信心精選",
    }
    missing = expected - set(tab_labels)
    assert not missing, (
        f"強者跟蹤缺 tab: {missing}; 實際 tabs={tab_labels}"
    )
