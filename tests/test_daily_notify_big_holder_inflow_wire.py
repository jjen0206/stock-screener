"""Structural guards for daily_notify → notify_top_picks → big_holder_inflow path.

守住「big_holder_inflow 命中的 picks 真的會進 Telegram 推播」這條 wire,純
inspect.getsource + regex,不 mock streamlit、不跑 strategy 真邏輯。

要守的不變量:
1. scripts/daily_notify.py 必須 import notify_top_picks(否則整支 cron 沒 picks)
2. scripts/daily_notify.py 必須呼叫 notify_top_picks(...)
3. src.notifier._select_top_picks 必須呼叫 run_all_strategies(...)
4. 該 run_all_strategies(...) call 不能傳 enabled=... 篩掉 big_holder_inflow
   (用 None / 不傳 → run_all_strategies 預設跑全部 strategies,包含千張戶進場)
5. big_holder_inflow 仍掛在 ALL_STRATEGIES(雙重保險,跟 test_big_holder_inflow_wire 互補)
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from src import notifier as notifier_mod
from src import strategies as strat


_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# 1. daily_notify 接到 notify_top_picks
# ============================================================================

def test_daily_notify_imports_notify_top_picks():
    """scripts/daily_notify.py 必須 import notify_top_picks。"""
    src = (_ROOT / "scripts" / "daily_notify.py").read_text(encoding="utf-8")
    assert re.search(
        r"from\s+src\.notifier\s+import\s*\(?[^)]*notify_top_picks",
        src,
    ), "daily_notify.py 必須 from src.notifier import notify_top_picks"


def test_daily_notify_calls_notify_top_picks():
    """daily_notify.main() 必須真的 call notify_top_picks(...)。"""
    src = (_ROOT / "scripts" / "daily_notify.py").read_text(encoding="utf-8")
    assert "notify_top_picks(" in src, (
        "daily_notify.py 沒有呼叫 notify_top_picks(...),cron 不會推任何 picks"
    )


# ============================================================================
# 2. notify_top_picks → _select_top_picks → run_all_strategies(no enabled filter)
# ============================================================================

def test_select_top_picks_calls_run_all_strategies():
    """_select_top_picks 必須呼叫 run_all_strategies(...)。"""
    fn_src = inspect.getsource(notifier_mod._select_top_picks)
    assert "run_all_strategies(" in fn_src, (
        "_select_top_picks 沒呼叫 run_all_strategies(...) — "
        "策略聚合 wire path 斷掉"
    )


def test_select_top_picks_does_not_filter_strategies():
    """確認 _select_top_picks 內 run_all_strategies(...) call 沒傳 enabled=...

    若傳 enabled=[白名單] 把 big_holder_inflow 漏掉 → 籌碼策略命中的 picks
    永遠不會進 Telegram 推播。預設不傳 enabled → run_all_strategies 跑全部。
    """
    fn_src = inspect.getsource(notifier_mod._select_top_picks)
    # 抓 run_all_strategies( ... ) 的呼叫(handle 跨行 args)
    match = re.search(
        r"run_all_strategies\s*\((.*?)\)",
        fn_src,
        re.DOTALL,
    )
    assert match, "找不到 run_all_strategies(...) 呼叫"
    args_blob = match.group(1)
    # 不能傳 enabled=... (None 或無此 arg 都 OK,代表跑全部 strategies)
    assert "enabled=" not in args_blob, (
        "_select_top_picks 內 run_all_strategies(...) 傳了 enabled=... 參數,"
        "可能漏掉 big_holder_inflow 籌碼策略 — 移除該參數讓全部 strategies 都跑"
    )


# ============================================================================
# 3. big_holder_inflow 仍在 ALL_STRATEGIES(交叉守護)
# ============================================================================

def test_big_holder_inflow_in_all_strategies_for_notify_wire():
    """ALL_STRATEGIES 必須含 big_holder_inflow,否則 run_all_strategies 永遠
    跑不到該策略,daily-notify 看不到千張戶進場 picks。"""
    assert "big_holder_inflow" in strat.ALL_STRATEGIES, (
        "ALL_STRATEGIES 缺 big_holder_inflow — daily-notify 不會推該策略 picks"
    )
