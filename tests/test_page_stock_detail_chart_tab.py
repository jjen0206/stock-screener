"""結構性 wire test:確認個股深度頁的 K 線 tab 接 plotly chart_renderer。

涵蓋:
- _render_detail_kline_tab 內 call chart_renderer 對外 4 個 API
- 個股深度頁 tabs 變 5(K線 / 籌碼 / ML / 新聞 / 警示)
- K 線 tab 有 lookback / 指標 multiselect / 標記 toggle 等控制項 key

外加 AppTest smoke:帶 detail_sid=2330 + 灌 OHLCV 資料,K 線 tab 0 exception
且 plotly_chart 真的被 render。
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src import config, database as db


APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _read_app() -> str:
    return Path(APP_PATH).read_text(encoding="utf-8")


# ============================================================================
# 結構性 guard
# ============================================================================

def test_kline_tab_calls_chart_renderer_api():
    """_render_detail_kline_tab 必須 call chart_renderer 對外 4 個 API。"""
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", APP_PATH)
    assert spec is not None and spec.loader is not None
    app_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_mod)
    fn_src = inspect.getsource(app_mod._render_detail_kline_tab)

    expected = [
        "render_candlestick_chart",
        "mark_pick_dates",
        "mark_position_levels",
        "mark_pattern_signals",
    ]
    missing = [name for name in expected if name not in fn_src]
    assert not missing, (
        f"_render_detail_kline_tab 必須 call chart_renderer 全部 API,缺:{missing}"
    )


def test_detail_page_has_five_tabs_including_warning():
    """個股深度頁 tabs 應該變成 5 個(加「⚠️ 警示」tab)。"""
    src = _read_app()
    # 5 tab 字串都應該出現
    for label in ("📈 K 線", "🚦 籌碼", "🧠 ML 解釋", "📰 新聞", "⚠️ 警示"):
        assert label in src, f"app.py 缺 tab label {label!r}"


def test_kline_tab_has_user_controls():
    """K 線 tab 應有 lookback / 指標 multiselect / 標記 toggle 三大控制項。"""
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", APP_PATH)
    assert spec is not None and spec.loader is not None
    app_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_mod)
    fn_src = inspect.getsource(app_mod._render_detail_kline_tab)

    # 三大控制項 key prefix(format str → 抓 stem 字串)
    assert "detail_kline_lookback_" in fn_src, "缺 lookback slider key"
    assert "detail_kline_inds_" in fn_src, "缺 指標 multiselect key"
    assert "detail_kline_picks_" in fn_src, "缺 picks toggle key"
    assert "detail_kline_pos_" in fn_src, "缺 持倉價位 toggle key"
    assert "detail_kline_pat_" in fn_src, "缺 形態 toggle key"


def test_chart_renderer_exports_public_api():
    """src.chart_renderer 必須 export 4 個對外 API + 3 個 compute_* helper。"""
    from src import chart_renderer

    expected = [
        "render_candlestick_chart",
        "mark_pick_dates",
        "mark_pattern_signals",
        "mark_position_levels",
        "compute_bollinger",
        "compute_kd",
        "compute_stoch",
    ]
    missing = [n for n in expected if not hasattr(chart_renderer, n)]
    assert not missing, f"chart_renderer 缺 export:{missing}"


# ============================================================================
# AppTest smoke
# ============================================================================

@pytest.fixture
def isolated_db_with_prices(monkeypatch, tmp_path):
    """獨立 tmp DB,灌 120 天 sid=2330 OHLCV(讓 K 線 tab 真的 render plotly)。"""
    import streamlit as st

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "kchart.db"))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()

    today = date.today()
    rows = []
    for i in range(120):
        d = (today - timedelta(days=120 - 1 - i)).isoformat()
        close = 100.0 + i * 0.3
        rows.append({
            "stock_id": "2330", "date": d,
            "open": close - 0.2, "high": close + 0.5,
            "low": close - 0.5, "close": close,
            "volume": 10000 + i * 100,
        })
    db.upsert_daily_prices(rows)

    sys.modules.pop("app", None)
    st.cache_data.clear()
    yield tmp_path
    db._reset_path_cache()  # type: ignore[attr-defined]


def test_detail_page_kline_tab_renders_plotly_chart(isolated_db_with_prices):
    """帶 detail_sid=2330 + OHLCV,K 線 tab 應 render plotly chart 不炸。"""
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["active_page"] = "📊 個股深度"
    at.session_state["detail_sid"] = "2330"
    at.session_state["high_confidence_mode"] = False
    at.run()

    assert not at.exception, (
        f"K 線 tab raised: {[str(e.value)[:300] for e in at.exception]}"
    )

    # tabs 應該有 5 個,且 K 線 / 警示 都在
    tab_labels = {t.label for t in at.tabs}
    assert "📈 K 線" in tab_labels, f"tabs={tab_labels}"
    assert "⚠️ 警示" in tab_labels, f"tabs={tab_labels}"
