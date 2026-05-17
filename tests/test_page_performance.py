"""「📈 績效分析」分頁結構性守住測試(不跑 streamlit)。

不跑 AppTest — 純檢查 app.py source:
  1. _page_performance function 存在 + callable
  2. "📈 績效分析" 在 PAGES list 內
  3. 主路由有對應 dispatch
  4. page source 引用 performance_analysis + strategy_backtest
  5. 四個 tab 名稱(真實交易 / 策略 attribution / 策略組合回測 / 策略相關性)
  6. 引用 STRATEGY_LABELS(策略 key → 中文)
  7. handle kill-switch is_enabled
"""
from __future__ import annotations

import inspect
import re

import app


def test_page_performance_function_exists():
    assert hasattr(app, "_page_performance"), "app 缺 _page_performance"
    assert callable(app._page_performance)


def test_performance_in_pages_list():
    assert "📈 績效分析" in app.PAGES, f"PAGES 缺「📈 績效分析」:{app.PAGES}"


def test_performance_after_strategy_history():
    """「📈 績效分析」應跟在「📊 策略歷史」之後 — 都是回看績效類。"""
    idx_h = app.PAGES.index("📊 策略歷史")
    idx_p = app.PAGES.index("📈 績效分析")
    assert idx_p == idx_h + 1, (
        f"「📈 績效分析」應緊接「📊 策略歷史」"
        f"(history at {idx_h}, performance at {idx_p})"
    )


def test_main_dispatch_routes_performance():
    src = inspect.getsource(app)
    pattern = re.compile(
        r'elif\s+page\s*==\s*"📈 績效分析"\s*:\s*\n\s*_page_performance\(\)'
    )
    assert pattern.search(src), (
        "主路由缺「📈 績效分析」→ _page_performance() dispatch"
    )


def test_page_source_imports_modules():
    src = inspect.getsource(app._page_performance)
    assert "performance_analysis" in src, (
        "page 必須引用 src.performance_analysis"
    )
    assert "strategy_backtest" in src, (
        "page 必須引用 src.strategy_backtest"
    )


def test_page_source_has_four_tabs():
    src = inspect.getsource(app._page_performance)
    assert "真實交易" in src, "tab 1「真實交易」缺"
    assert "策略 attribution" in src or "attribution" in src, (
        "tab 2「策略 attribution」缺"
    )
    assert "策略組合回測" in src or "組合回測" in src, (
        "tab 3「策略組合回測」缺"
    )
    assert "策略相關性" in src or "相關性" in src, "tab 4「策略相關性」缺"


def test_page_source_wires_kill_switch():
    src = inspect.getsource(app._page_performance)
    assert "is_enabled" in src, (
        "page 必須檢查 PERFORMANCE_ENABLED kill-switch"
    )


def test_page_source_wires_attribution():
    src = inspect.getsource(app._page_performance)
    assert "compute_attribution" in src, (
        "page 必須 wire compute_attribution"
    )
    assert "best_strategy_by_pnl" in src, (
        "page 必須 wire best_strategy_by_pnl 給軍師判讀"
    )


def test_page_source_wires_backtest():
    src = inspect.getsource(app._page_performance)
    assert "backtest_combination" in src, (
        "page 必須 wire strategy_backtest.backtest_combination"
    )
    assert "compute_strategy_correlation" in src, (
        "page 必須 wire compute_strategy_correlation"
    )


def test_page_source_uses_strategy_labels():
    src = inspect.getsource(app._page_performance)
    assert "STRATEGY_LABELS" in src, (
        "page 必須用 STRATEGY_LABELS 把策略 key 轉中文"
    )


def test_page_source_renders_drawdown_curve():
    src = inspect.getsource(app._page_performance)
    assert "drawdown" in src.lower() or "Drawdown" in src, (
        "page 必須 render drawdown 圖"
    )
    assert "compute_drawdown_curve" in src, (
        "page 必須 wire compute_drawdown_curve"
    )
