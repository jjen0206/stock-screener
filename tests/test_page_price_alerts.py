"""「🚨 警報設定」分頁結構性守住測試(不跑 streamlit)。"""
from __future__ import annotations

import inspect
import re

import app


def test_page_price_alerts_function_exists():
    assert hasattr(app, "_page_price_alerts"), "app 缺 _page_price_alerts"
    assert callable(app._page_price_alerts)


def test_price_alerts_in_pages_list():
    assert "🚨 警報設定" in app.PAGES, f"PAGES 缺「🚨 警報設定」:{app.PAGES}"


def test_price_alerts_after_position_management():
    """「🚨 警報設定」應跟在「🛡️ 持倉管理」之後 — 都是風險控管動作。"""
    idx_pos = app.PAGES.index("🛡️ 持倉管理")
    idx_pa = app.PAGES.index("🚨 警報設定")
    assert idx_pa == idx_pos + 1, (
        f"「🚨 警報設定」應緊接「🛡️ 持倉管理」"
        f"(pos at {idx_pos}, alerts at {idx_pa})"
    )


def test_main_dispatch_routes_price_alerts():
    src = inspect.getsource(app)
    pattern = re.compile(
        r'elif\s+page\s*==\s*"🚨 警報設定"\s*:\s*\n\s*_page_price_alerts\(\)'
    )
    assert pattern.search(src), (
        "主路由缺「🚨 警報設定」→ _page_price_alerts() dispatch"
    )


def test_page_source_imports_price_alerts():
    src = inspect.getsource(app._page_price_alerts)
    assert "price_alerts" in src, "page 必須引用 src.price_alerts"


def test_page_source_wires_crud_helpers():
    src = inspect.getsource(app._page_price_alerts)
    for helper in ("add_alert", "list_alerts", "delete_alert"):
        assert helper in src, f"page 必須 wire db.{helper}"


def test_page_source_has_active_and_history_tabs():
    src = inspect.getsource(app._page_price_alerts)
    assert "進行中" in src, "page 必須有「進行中」tab"
    assert "已觸發歷史" in src, "page 必須有「已觸發歷史」tab"


def test_page_source_handles_kill_switch():
    src = inspect.getsource(app._page_price_alerts)
    assert "is_enabled" in src, "page 必須檢查 PRICE_ALERT_ENABLED kill-switch"
