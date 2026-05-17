"""「🛡️ 持倉管理」分頁結構性守住測試。

純結構性 — 不 mock streamlit、不跑頁面、用 inspect.getsource 看 function body
有沒有 wire 對。對齊 test_page_strategy_history_wire.py 的 pattern。

守住:
1. `app._page_position_management` 存在
2. `"🛡️ 持倉管理"` 在 `app.PAGES`
3. 主路由 dispatch 對「🛡️ 持倉管理」走 _page_position_management
4. Source 引用 position_sizing + risk_management
5. Source 引用 add_position / close_position / get_all_positions(CRUD)
"""
from __future__ import annotations

import inspect
import re

import app


def test_page_position_management_function_exists():
    assert hasattr(app, "_page_position_management"), (
        "app 缺 _page_position_management function"
    )
    assert callable(app._page_position_management)


def test_position_management_in_pages_list():
    assert "🛡️ 持倉管理" in app.PAGES, (
        f"PAGES 缺「🛡️ 持倉管理」:{app.PAGES}"
    )


def test_position_management_after_trades():
    """「🛡️ 持倉管理」應跟「💼 交易紀錄」相鄰(主公管 P&L 跟管倉是連著看的)。"""
    idx_trades = app.PAGES.index("💼 交易紀錄")
    idx_pos = app.PAGES.index("🛡️ 持倉管理")
    assert idx_pos == idx_trades + 1, (
        f"「🛡️ 持倉管理」應緊接「💼 交易紀錄」"
        f"(trades at {idx_trades}, position at {idx_pos})"
    )


def test_main_dispatch_routes_position_management():
    src = inspect.getsource(app)
    pattern = re.compile(
        r'elif\s+page\s*==\s*"🛡️ 持倉管理"\s*:\s*\n\s*_page_position_management\(\)'
    )
    assert pattern.search(src), (
        "主路由缺「🛡️ 持倉管理」→ _page_position_management() dispatch"
    )


def test_page_source_uses_position_sizing_and_risk_management():
    src = inspect.getsource(app._page_position_management)
    assert "position_sizing" in src, "page 必須引用 position_sizing 模組"
    assert "risk_management" in src, "page 必須引用 risk_management 模組"


def test_page_source_wires_crud_helpers():
    src = inspect.getsource(app._page_position_management)
    for helper in ("add_position", "close_position", "get_all_positions"):
        assert helper in src, f"page 必須 wire db.{helper}"


def test_page_source_wires_drawdown_and_concentration():
    src = inspect.getsource(app._page_position_management)
    assert "drawdown_pct" in src, "page 必須調用 drawdown_pct"
    assert "check_single_concentration" in src, "page 必須調用 check_single_concentration"


def test_page_source_handles_kill_switches():
    src = inspect.getsource(app._page_position_management)
    assert "is_enabled" in src, "page 必須檢查 kill-switch"
