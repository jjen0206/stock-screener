"""Cold-load profiling script — 量短線頁三階段 wallclock + 內部 timing。

跑法:
    python scripts/profile_short_page.py

輸出:
    每階段 wallclock(整個 at.run 進到出)+ app.py 內 _tic/_toc 細項。
    把結果貼回 issue / docs/profiling_results.md。

階段:
    1. cold load:第一次 at.run() — boot + dashboard
    2. switch to short:active_page='🔥 短線' 後再 run
    3. submit screening:short_submitted=True 後再 run

注意:本 script 跑的是 streamlit AppTest(headless),不會開瀏覽器。
測 wallclock 跟用戶實際體感接近,但少了 browser render / network round-trip。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# 讓 AppTest run app.py 時找得到 src/(以本檔的 parent.parent 為 project root)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# AppTest 需要 streamlit context — import 後即可
from streamlit.testing.v1 import AppTest

APP_PATH = str(_ROOT / "app.py")


def _read_timing(at) -> dict[str, float]:
    """從 AppTest session_state 撈 _timing — SafeSessionState 沒 .get(),需 try/except。"""
    try:
        timing = at.session_state["_timing"]
    except (KeyError, AttributeError):
        return {}
    return dict(timing) if timing else {}


def _print_phase(name: str, wallclock_s: float, internal: dict[str, float]) -> None:
    print(f"\n=== {name} (wallclock: {wallclock_s*1000:.0f}ms) ===")
    if not internal:
        print("  (no internal timing — _toc 沒寫進 session_state)")
        return
    items = sorted(internal.items(), key=lambda kv: -kv[1])
    for k, v in items:
        print(f"  {k:<35s} {v*1000:7.1f}ms")


def main() -> None:
    print(f"[profile] APP_PATH = {APP_PATH}")
    at = AppTest.from_file(APP_PATH, default_timeout=60)

    # === Phase 1: cold load ===
    t0 = time.perf_counter()
    at.run()
    t1 = time.perf_counter()
    if at.exception:
        print("[profile] [ERR]cold load raised:")
        for e in at.exception:
            print(f"  {e.value!s}")
        return
    timing1 = _read_timing(at)
    _print_phase("Phase 1: cold load (default page = 首頁)", t1 - t0, timing1)

    # === Phase 2: switch to 短線頁 ===
    # active_page 是 app.py 自己維護的 session key,但 segmented_control 的
    # widget state(key="nav_segmented")會覆蓋它 — 必須直接設 widget key
    at.session_state["nav_segmented"] = "🔥 短線"
    at.session_state["active_page"] = "🔥 短線"
    # 清 timing dict 讓下一輪 rerun 蓋掉
    at.session_state["_timing"] = {}
    t2 = time.perf_counter()
    at.run()
    t3 = time.perf_counter()
    if at.exception:
        print("[profile] [ERR]switch to short raised:")
        for e in at.exception:
            print(f"  {e.value!s}")
        return
    timing2 = _read_timing(at)
    _print_phase("Phase 2: switch → 短線頁(尚未執行選股)", t3 - t2, timing2)

    # === Phase 3: submit 執行選股 ===
    # 走「快速:50 檔大型股」universe,免依賴 SQLite 歷史
    try:
        sb = at.selectbox(key=None)  # universe selectbox 沒 key,只能 by index
    except Exception:
        sb = None
    # 找 universe selectbox(label = "選股範圍")
    universe_sb = None
    for s in at.selectbox:
        if "選股範圍" in (s.label or ""):
            universe_sb = s
            break
    if universe_sb is None:
        print("[profile] [WARN] 找不到「選股範圍」selectbox,fallback 走原 default")
    else:
        universe_sb.set_value("快速:50 檔大型股")

    # 找「執行選股」按鈕
    submit_btn = None
    for b in at.button:
        if "執行選股" in (b.label or ""):
            submit_btn = b
            break
    if submit_btn is None:
        print("[profile] [ERR]找不到「執行選股」按鈕")
        return

    at.session_state["_timing"] = {}
    t4 = time.perf_counter()
    submit_btn.click()
    at.run()
    t5 = time.perf_counter()
    if at.exception:
        print("[profile] [ERR]submit raised:")
        for e in at.exception:
            print(f"  {e.value!s}")
        return
    timing3 = _read_timing(at)
    _print_phase("Phase 3: 執行選股(50 檔大型股 universe)", t5 - t4, timing3)

    print("\n=== Summary ===")
    print(f"Phase 1 (cold load):       {(t1-t0)*1000:7.0f}ms")
    print(f"Phase 2 (switch → short):  {(t3-t2)*1000:7.0f}ms")
    print(f"Phase 3 (執行選股):         {(t5-t4)*1000:7.0f}ms")


if __name__ == "__main__":
    main()
