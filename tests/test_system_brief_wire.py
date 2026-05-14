"""「📋 系統結論」分頁 + weekly brief cron 結構性守住測試。

純結構性 — 不 mock streamlit、不跑 cron、用 inspect.getsource 看 function body
有沒有互相 call 對。對齊 test_page_strategy_history_wire.py pattern。

守住:
1. app._page_system_brief 存在
2. "📋 系統結論" 在 app.PAGES 且排在 "⚙️ 系統" 前
3. 主路由 if/elif 對 "📋 系統結論" dispatch
4. _page_system_brief 內呼叫 build_system_brief
5. src.system_brief 暴露 build_system_brief 和 format_brief_for_telegram
6. scripts/send_weekly_brief.py 同時 import 兩個 helper（避免漂移）
7. .github/workflows/weekly-brief.yml 有 cron + 跑 send_weekly_brief.py
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import app


# ============================================================================
# 1. Function 存在
# ============================================================================

def test_page_system_brief_function_exists():
    """app._page_system_brief 必須是 module-level callable。"""
    assert hasattr(app, "_page_system_brief")
    assert callable(app._page_system_brief)


# ============================================================================
# 2. PAGES 註冊 + 排序
# ============================================================================

def test_system_brief_in_pages_list():
    """「📋 系統結論」必須在 PAGES 內。"""
    assert "📋 系統結論" in app.PAGES, (
        f"PAGES 缺「📋 系統結論」:{app.PAGES}"
    )


def test_system_brief_before_system_page():
    """「📋 系統結論」必須排在「⚙️ 系統」之前
    (結論 → 系統健康度,概覽 → 細節)。
    """
    idx_brief = app.PAGES.index("📋 系統結論")
    idx_system = app.PAGES.index("⚙️ 系統")
    assert idx_brief < idx_system, (
        f"「📋 系統結論」必須排在「⚙️ 系統」前 "
        f"(brief at {idx_brief}, system at {idx_system})"
    )


# ============================================================================
# 3. Dispatch 路由
# ============================================================================

def test_main_dispatch_routes_system_brief():
    """主路由 if/elif chain 必須對「📋 系統結論」dispatch 到 _page_system_brief。"""
    src = inspect.getsource(app)
    pattern = re.compile(
        r'elif\s+page\s*==\s*"📋 系統結論"\s*:\s*\n\s*_page_system_brief\(\)'
    )
    assert pattern.search(src), (
        "主路由缺「📋 系統結論」→ _page_system_brief() dispatch"
    )


# ============================================================================
# 4. _page_system_brief 呼叫 build_system_brief
# ============================================================================

def test_page_calls_build_system_brief():
    """_page_system_brief 必須 call build_system_brief
    (否則 page render 沒拿軍師結論 → 空白頁)。
    """
    src = inspect.getsource(app._page_system_brief)
    assert "build_system_brief" in src, (
        "_page_system_brief 沒 call build_system_brief — page 拿不到結論"
    )


# ============================================================================
# 5. src.system_brief 暴露兩個 helper
# ============================================================================

def test_system_brief_module_exports_both_helpers():
    """src/system_brief.py 必須暴露 build_system_brief + format_brief_for_telegram
    (Streamlit page 用前者,weekly cron 用後者,缺一就斷)。
    """
    from src import system_brief as sb
    assert hasattr(sb, "build_system_brief")
    assert callable(sb.build_system_brief)
    assert hasattr(sb, "format_brief_for_telegram")
    assert callable(sb.format_brief_for_telegram)


# ============================================================================
# 6. scripts/send_weekly_brief.py 用兩個 helper
# ============================================================================

def test_weekly_brief_script_imports_helpers():
    """scripts/send_weekly_brief.py source 必須 import 兩個 helper +
    呼叫 send_telegram_message + send_discord_message,
    確保 caller 跟 callee 都對得上(避免簽名漂移)。
    """
    script_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "send_weekly_brief.py"
    )
    assert script_path.exists(), "scripts/send_weekly_brief.py 不見了"
    src = script_path.read_text(encoding="utf-8")
    assert "build_system_brief" in src, "weekly brief script 缺 build_system_brief import"
    assert "format_brief_for_telegram" in src, "weekly brief script 缺 format_brief_for_telegram import"
    assert "send_telegram_message" in src, "weekly brief script 缺 telegram 推播"
    assert "send_discord_message" in src, "weekly brief script 缺 discord 推播"


# ============================================================================
# 7. weekly-brief.yml workflow 存在 + cron 對
# ============================================================================

def test_weekly_brief_workflow_exists():
    """.github/workflows/weekly-brief.yml 必須存在,含 cron + 跑 send_weekly_brief.py。"""
    yml_path = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows"
        / "weekly-brief.yml"
    )
    assert yml_path.exists(), ".github/workflows/weekly-brief.yml 不見了"
    content = yml_path.read_text(encoding="utf-8")
    # cron 對齊主公規格:0 2 * * 0(週日 UTC 02:00 = 台灣 10:00)
    assert "cron:" in content
    assert '"0 2 * * 0"' in content or "'0 2 * * 0'" in content, (
        "weekly-brief cron 必須是 0 2 * * 0 (週日 10:00 TW)"
    )
    assert "send_weekly_brief.py" in content, (
        "workflow 沒跑 send_weekly_brief.py"
    )
