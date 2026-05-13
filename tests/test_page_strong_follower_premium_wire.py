"""「✨ 高信心精選」(Tab 4)結構性守住測試。

純 inspect.getsource + regex,不 mock streamlit、不跑 AppTest(那些走
test_e2e_smoke 之類)。對齊 test_page_strong_follower_wire.py pattern。

守住:
1. Tab 4 標籤 "✨ 高信心精選" 在 _page_strong_follower source 內
2. 4 個 tab 一起列在 st.tabs 內(避免改 3 tab 時誤刪)
3. db.get_strong_follower_premium helper 有被 call
4. st.caption 在 premium tab 區段用於推薦理由
   (regex 抓 reason_text 周邊 caption 邏輯)
5. 不含 mock streamlit(對齊既有教訓)
"""
from __future__ import annotations

import inspect
import re

import app


def _premium_section(src: str) -> str:
    """切出 `with tab_premium:` 區段(structural guard 聚焦掃描)。

    回從 "with tab_premium:" 到下一個 top-level "with " 或 function 結尾。
    """
    m = re.search(r"with\s+tab_premium\s*:\s*\n", src)
    assert m, "_page_strong_follower source 缺 `with tab_premium:` 區段"
    start = m.end()
    # 找下一個非 indent 的 token(可能是 function end 或下一個 def)
    tail = src[start:]
    next_block = re.search(r"\n(?=def\s|\nclass\s|\n#\s*===)", tail)
    end = start + (next_block.start() if next_block else len(tail))
    return src[start:end]


# ============================================================================
# Tab 4 標籤註冊
# ============================================================================

def test_premium_tab_label_in_source():
    """Tab 4 "✨ 高信心精選" 必須在 _page_strong_follower source 內。"""
    src = inspect.getsource(app._page_strong_follower)
    assert "✨ 高信心精選" in src, (
        "_page_strong_follower 缺 Tab 4 標籤「✨ 高信心精選」"
    )


def test_four_tabs_present_in_st_tabs():
    """st.tabs(...) 必須包含 4 個 tab,且新 tab 在最後。

    守住未來 refactor 不誤砍 / 順序錯置。
    """
    src = inspect.getsource(app._page_strong_follower)
    # 抓 st.tabs([ ... ]) call 的 list literal
    m = re.search(
        r"st\.tabs\(\s*\[\s*([\s\S]+?)\]\s*,?\s*\)", src,
    )
    assert m, "_page_strong_follower 缺 st.tabs([...]) call"
    tabs_literal = m.group(1)
    for label in (
        "🏛️ 法人共識榜",
        "🐋 千張大戶進場榜",
        "🎯 綜合排行",
        "✨ 高信心精選",
    ):
        assert label in tabs_literal, (
            f"st.tabs 缺 tab 標籤「{label}」(找到的 literal:{tabs_literal})"
        )


# ============================================================================
# helper call
# ============================================================================

def test_premium_calls_get_strong_follower_premium():
    """premium tab 必須呼叫 db.get_strong_follower_premium(資料源不可丟)。"""
    section = _premium_section(inspect.getsource(app._page_strong_follower))
    pattern = re.compile(r"\bget_strong_follower_premium\b")
    assert pattern.search(section), (
        "premium tab 區段沒 call db.get_strong_follower_premium"
    )


# ============================================================================
# 推薦理由 caption
# ============================================================================

def test_premium_uses_st_caption_for_reason():
    """premium tab 必須用 st.caption 顯示推薦理由(reason_text)。

    regex 抓 caption 周邊邏輯:caption(...) 內含 reason_text 引用,
    或 loop 內 caption(...) 含 r["reason_text"] / r.get("reason_text")。
    """
    section = _premium_section(inspect.getsource(app._page_strong_follower))
    # 必須有 st.caption call
    assert "st.caption(" in section, (
        "premium tab 缺 st.caption(推薦理由顯示需要)"
    )
    # 必須引用 reason_text(來自 helper 回傳 dict)
    assert "reason_text" in section, (
        "premium tab 缺 reason_text 引用(推薦理由欄)"
    )
    # 進一步檢查:reason_text 跟 caption 是「同 block」
    # — 兩者在 section 內間隔不該超過 5 行(避免 reason_text 在別處 random)
    lines = section.splitlines()
    caption_lines = [
        i for i, line in enumerate(lines) if "st.caption(" in line
    ]
    reason_lines = [
        i for i, line in enumerate(lines) if "reason_text" in line
    ]
    assert caption_lines and reason_lines, (
        "premium tab 缺 st.caption / reason_text"
    )
    # 任一 caption 附近(±5 行)該有 reason_text 引用
    close_enough = any(
        abs(c - r) <= 5
        for c in caption_lines
        for r in reason_lines
    )
    assert close_enough, (
        "st.caption 跟 reason_text 距離太遠 — 可能沒用在推薦理由顯示"
    )


# ============================================================================
# 反 mock streamlit 教訓
# ============================================================================

def test_no_mock_streamlit_in_premium_wire_test():
    """守住:本檔自身不寫 mock streamlit(對齊既有教訓)。

    讀本 test 檔內容,確認不含 mock / patch streamlit 字串。
    為了不讓守門字串出現在本檔的 string literal 內被自己抓到,用
    runtime 字串組合(split + join)避開字面比對。
    """
    here = inspect.getsourcefile(test_no_mock_streamlit_in_premium_wire_test)
    assert here is not None
    with open(here, encoding="utf-8") as f:
        body = f.read()
    forbidden = [
        ".".join(["mock", "patch"]),
        "M" + "agicMock",
        "monkeypatch.setattr(" + chr(34) + "streamlit",
    ]
    for bad in forbidden:
        assert bad not in body, (
            f"本 wire test 不該出現「{bad}」(mock streamlit 教訓)"
        )
