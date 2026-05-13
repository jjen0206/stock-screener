"""「📊 強者跟蹤」regime banner + bear top-n 收緊結構性守住測試。

純 inspect.getsource + regex,不 mock streamlit、不跑真頁面,對齊既有
test_page_strong_follower_wire pattern。

守住:
1. _page_strong_follower source 含 compute_regime() 呼叫
2. source 含 3 種 banner 文案標籤(大盤偏多 / 大盤盤整 / 大盤偏空)
3. bear 時把 premium top_n 降到 5(避免主公追高)
4. premium tab 的 get_strong_follower_premium 用 dynamic top_n 變數(不是 hardcode 10)
"""
from __future__ import annotations

import inspect
import re

import app


def test_page_strong_follower_calls_compute_regime():
    """source 必須呼叫 compute_regime() 取得當前大盤狀態。"""
    src = inspect.getsource(app._page_strong_follower)
    assert "compute_regime(" in src, (
        "_page_strong_follower 沒呼叫 compute_regime() — regime banner 斷掉"
    )


def test_page_strong_follower_imports_market_regime():
    """compute_regime 必須有 import,避免 NameError。可接受 module-level 或
    function 內 lazy import 兩種模式。"""
    page_src = inspect.getsource(app._page_strong_follower)
    module_src = inspect.getsource(app)
    has_module_import = re.search(
        r"from\s+src\.market_regime\s+import\s*\(?[^)]*compute_regime",
        module_src,
    )
    has_inline_import = re.search(
        r"from\s+src\.market_regime\s+import\s*\(?[^)]*compute_regime",
        page_src,
    )
    assert has_module_import or has_inline_import, (
        "compute_regime 必須有 import(module-level 或 page 內 lazy 都可)"
    )


def test_page_strong_follower_has_three_regime_banner_labels():
    """三種大盤狀態(bull / range / bear)各對應一段中文標籤。

    主公拍板:bull = 偏多 / range = 盤整 / bear = 偏空。
    label 用「大盤」前綴,避免跟 strategy 'bull/bear/range' 字面衝突。
    """
    src = inspect.getsource(app._page_strong_follower)
    for label in ("大盤偏多", "大盤盤整", "大盤偏空"):
        assert label in src, (
            f"_page_strong_follower source 缺 regime banner 標籤「{label}」"
        )


def test_page_strong_follower_bear_warning_text():
    """bear 時要有強烈警示「小心追高」字樣(主公拍板的視覺強警語)。"""
    src = inspect.getsource(app._page_strong_follower)
    assert "小心追高" in src, (
        "bear 時 banner 缺「小心追高」警示(主公拍板強警語)"
    )


def test_page_strong_follower_bear_trims_premium_top_n():
    """bear 時 premium top_n 必須降到 5,bull/range 維持 10。

    抓 source 內 'if ... bear ... else 10' 或顯式 '5 if ... bear ... else 10' 結構。
    """
    src = inspect.getsource(app._page_strong_follower)
    # 容許多種寫法:三元運算子 / if-else block / 直接賦值
    # 核心要件:bear 對應 5、else 對應 10
    has_ternary = re.search(
        r"5\s+if\s+\w+\s*==\s*['\"]bear['\"]\s+else\s+10",
        src,
    )
    has_explicit_5_10 = ("top_n=5" in src or "top_n = 5" in src) and (
        "top_n=10" in src or "top_n = 10" in src
    )
    assert has_ternary or has_explicit_5_10, (
        "_page_strong_follower 沒看到 bear → 5 / else → 10 的 top_n 切換"
    )


def test_page_strong_follower_premium_uses_dynamic_top_n():
    """get_strong_follower_premium(...) 的 top_n 必須是變數,不是 hardcode 10。

    這樣才能配合 regime banner 動態收緊。抓「真正的 helper call」(帶
    min_inst_days= kwarg 的那段),避開 docstring 內 placeholder 文字。
    """
    src = inspect.getsource(app._page_strong_follower)
    # 抓真正的 helper call(必帶 min_inst_days= kwarg → 跟 docstring 區隔)
    match = re.search(
        r"get_strong_follower_premium\s*\(\s*min_inst_days\s*=(.*?)\)",
        src,
        re.DOTALL,
    )
    assert match, (
        "找不到 get_strong_follower_premium(min_inst_days=...) 呼叫"
    )
    args_blob = match.group(1)
    # 不能是 top_n=10 hardcode
    assert "top_n=10" not in args_blob, (
        "get_strong_follower_premium(top_n=10) hardcode,regime 無法動態收緊"
    )
    # 必須有 top_n=<some_variable> 形式(辨識 ASCII 識別符)
    assert re.search(r"top_n\s*=\s*[A-Za-z_]\w*", args_blob), (
        "get_strong_follower_premium 必須傳 top_n=<變數> 才能配合 regime 切換"
    )
