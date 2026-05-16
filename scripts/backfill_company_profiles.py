"""排程入口:預先填入 company_profiles 表的 FinMind facts(不打 LLM)。

table `company_profiles` 是 lazy on-demand cache,只有使用者點開個股詳情頁時才會
觸發 `get_company_profile()` 寫入。結果就是表常常 0 rows — 在沒有任何 user
session 的 batch 環境(scheduled fetcher、CI)看起來像「死表」。

本 script 提供一個輕量 warm-up:
- 走 `get_company_profile(sid, llm_call=False)` → 只填 FinMind facts
  (industry / market / listing_date / foreign_limit),**絕對不打 Gemini**,
  避免吃 LLM 配額。
- description / uniqueness / moat 保持 NULL,等使用者在 UI 主動點才會走
  LLM path。

CLI:
    # 預設跑 TW_TOP_50
    python scripts/backfill_company_profiles.py

    # 跑 pure_stock_universe(~2000 檔,慢但更完整)
    python scripts/backfill_company_profiles.py --universe pure_stock

    # 限定前 N 檔(debug)
    python scripts/backfill_company_profiles.py --limit 5

Exit code:
  0 = 成功(包含部分失敗,只要有成功就算 OK)
  1 = 全部失敗
  2 = 參數錯誤
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import company_profile as cp  # noqa: E402
from src.universe import TW_TOP_50, pure_stock_universe  # noqa: E402


def _resolve_universe(name: str) -> list[str]:
    if name == "tw_top_50":
        return [sid for sid, _ in TW_TOP_50]
    if name == "pure_stock":
        return pure_stock_universe()
    raise ValueError(f"unknown universe: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="warm-up company_profiles FinMind facts")
    parser.add_argument(
        "--universe",
        choices=["tw_top_50", "pure_stock"],
        default="tw_top_50",
        help="要 warm 哪個 universe(default: tw_top_50)",
    )
    parser.add_argument("--limit", type=int, default=0, help="限定前 N 檔(0 = 全跑)")
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="每檔間 sleep 秒數(default 0,FinMind 已有自己的 rate limit)",
    )
    args = parser.parse_args()

    try:
        sids = _resolve_universe(args.universe)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    if args.limit > 0:
        sids = sids[: args.limit]

    total = len(sids)
    if total == 0:
        print("[WARN] universe 為空,沒事可做")
        return 0

    print(f"[INFO] warm-up company_profiles facts: {total} 檔 ({args.universe})")
    ok = 0
    failed = 0
    for i, sid in enumerate(sids, 1):
        try:
            # llm_call=False → 只寫 FinMind facts,不打 Gemini
            profile = cp.get_company_profile(sid, llm_call=False, regenerate=False)
            if profile.get("industry") or profile.get("market"):
                ok += 1
            else:
                failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] {sid} 失敗: {e}", file=sys.stderr)
            failed += 1
        if i % 10 == 0 or i == total:
            print(f"[PROG] {i}/{total} ok={ok} fail={failed}")
        if args.sleep > 0:
            time.sleep(args.sleep)

    print(f"[DONE] ok={ok} fail={failed} total={total}")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
