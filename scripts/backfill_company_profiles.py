"""排程入口:預先填入 company_profiles 表(FinMind facts + 可選 Gemini LLM 敘述)。

table `company_profiles` 是 lazy on-demand cache,只有使用者點開個股詳情頁時才會
觸發 `get_company_profile()` 寫入。結果就是表常常 0 rows — 在沒有任何 user
session 的 batch 環境(scheduled fetcher、CI)看起來像「死表」。

本 script 兩種模式:

1. **warm-up 模式**(`--llm-call false`,預設):
   走 `get_company_profile(sid, llm_call=False)` → 只填 FinMind facts
   (industry / market / listing_date / foreign_limit),**絕對不打 Gemini**,
   避免吃 LLM 配額。description / uniqueness / moat 保持 NULL。

2. **LLM 模式**(`--llm-call true`,給 GH Actions workflow_dispatch 用):
   走 `get_company_profile(sid, llm_call=True)` → FinMind facts + Gemini
   生成 description/uniqueness/moat。**慢 ~5-10 s/call**,2715 檔全跑要 30 min ~
   1 hr,所以一定要在 GH Actions / 本機長 process 跑(不要在 streamlit
   dispatch task 跑會 timeout)。撞 Gemini 429 quota → fail-fast 整批中斷
   (類似 backfill_financials 對 FinMindQuotaError 的處理)。

CLI
---
::

    # 預設 warm-up TW_TOP_50(向後相容)
    python scripts/backfill_company_profiles.py

    # warm-up pure_stock(~2715 檔,只填 FinMind facts)
    python scripts/backfill_company_profiles.py --universe pure_stock

    # LLM 模式 — 跑 watchlist + LLM(主公自選名單,快)
    python scripts/backfill_company_profiles.py --universe watchlist --llm-call true

    # LLM 模式分批跑(避過 Gemini quota / GH Actions 2 hr 上限)
    # 跑 universe[0:500]:
    python scripts/backfill_company_profiles.py --universe pure_stock \\
        --llm-call true --batch-start 0 --batch-end 500
    # 接著 universe[500:1000]:
    python scripts/backfill_company_profiles.py --universe pure_stock \\
        --llm-call true --batch-start 500 --batch-end 1000

    # 跑完 dump 進 company_profiles.csv(GH workflow 用,讓雲端 reload)
    python scripts/backfill_company_profiles.py --universe pure_stock \\
        --llm-call true --dump-format parquet --upload-release

Exit code
---------
- 0  成功(LLM 模式:LLM 成功率 > 50% 即視為 OK)
- 1  全部失敗 / Gemini quota 爆 / 沒設 GEMINI_API_KEY 在 LLM 模式
- 2  CLI 參數錯誤
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import company_profile as cp  # noqa: E402
from src import database as db  # noqa: E402
from src.universe import TW_TOP_50, load_watchlist, pure_stock_universe  # noqa: E402

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"


def _resolve_universe(name: str) -> list[str]:
    if name == "tw_top_50":
        return [sid for sid, _ in TW_TOP_50]
    if name == "pure_stock":
        return pure_stock_universe()
    if name == "watchlist":
        return [sid for sid, _ in load_watchlist()]
    raise ValueError(f"unknown universe: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="預先填入 company_profiles 表(FinMind facts + 可選 Gemini LLM 敘述)",
    )
    parser.add_argument(
        "--universe",
        choices=["tw_top_50", "pure_stock", "watchlist"],
        default="tw_top_50",
        help="要 warm 哪個 universe(default: tw_top_50)",
    )
    parser.add_argument(
        "--llm-call",
        choices=["true", "false"],
        default="false",
        help=(
            "true = 跑 Gemini LLM 生 description/uniqueness/moat(慢 ~5-10s/call,"
            "撞 quota 自動 fail-fast);false = 只填 FinMind facts(預設,向後相容)"
        ),
    )
    parser.add_argument(
        "--regenerate", action="store_true",
        help="LLM 模式下強制重打 Gemini(忽略 cache 已有 narrative)",
    )
    parser.add_argument("--limit", type=int, default=0, help="限定前 N 檔(0 = 全跑)")
    parser.add_argument(
        "--batch-start", type=int, default=0,
        help=(
            "從 universe 的第 N 檔開始(0-based,inclusive)。"
            "搭配 --batch-end 做分批 dispatch — 例 0/500 跑前 500 檔,500/1000 跑下一批,"
            "避過 Gemini quota 撞牆 / GH Actions 2 hr timeout"
        ),
    )
    parser.add_argument(
        "--batch-end", type=int, default=0,
        help=(
            "跑到 universe 的第 N 檔結束(0-based,exclusive)。0 = 跑到 universe 末尾"
        ),
    )
    parser.add_argument(
        "--max-stocks", type=int, default=0,
        help=(
            "單次 run 上限檔數(0 = 無上限,搭配 --batch-* 已夠用)。"
            "若要保險可設 500,防 Gemini RPD quota 撞牆"
        ),
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help=(
            "每檔間 sleep 秒數(default 0;LLM 模式建議 1.0 以避免 Gemini "
            "15 RPM / 1500 RPD free tier 撞牆)"
        ),
    )
    parser.add_argument(
        "--progress-every", type=int, default=50,
        help="每 N 檔印一次進度(default 50)",
    )
    parser.add_argument(
        "--dump-format", choices=["csv", "parquet"], default=None,
        help="跑完 dump company_profiles 表;不指定 = 不 dump",
    )
    parser.add_argument(
        "--upload-release", action="store_true",
        help="Dump 完上傳到 GH Release(tag=snapshot-company-profiles-YYYY-MM-DD)",
    )
    parser.add_argument(
        "--release-tag", default=None,
        help="覆寫 release tag(預設 snapshot-company-profiles-{YYYY-MM-DD})",
    )
    args = parser.parse_args(argv)

    llm_mode = args.llm_call == "true"

    try:
        sids = _resolve_universe(args.universe)
    except ValueError as e:
        print(f"[BACKFILL-CP] [ERROR] {e}", file=sys.stderr)
        return 2

    if not sids:
        print(
            f"[BACKFILL-CP] universe={args.universe} 為空 — "
            "先跑 daily_fetch.py 初始化 stocks(或檢查 watchlist.txt)",
            flush=True,
        )
        return 0

    # batch range:對 universe(已排序 stable)切 [batch_start, batch_end)
    batch_lo = max(0, args.batch_start)
    batch_hi = args.batch_end if args.batch_end > 0 else len(sids)
    if batch_lo >= len(sids):
        print(
            f"[BACKFILL-CP] batch-start={batch_lo} >= universe size={len(sids)} "
            "→ nothing to do",
            flush=True,
        )
        return 0
    if batch_lo >= batch_hi:
        print(
            f"[BACKFILL-CP] batch-start={batch_lo} >= batch-end={batch_hi} → 區間空,exit",
            file=sys.stderr, flush=True,
        )
        return 2
    sids = sids[batch_lo:batch_hi]

    if args.limit > 0:
        sids = sids[: args.limit]
    if args.max_stocks > 0 and len(sids) > args.max_stocks:
        print(
            f"[BACKFILL-CP] todo={len(sids)} > max-stocks={args.max_stocks},"
            f"截到前 {args.max_stocks} 檔",
            flush=True,
        )
        sids = sids[: args.max_stocks]

    total = len(sids)
    if total == 0:
        print("[BACKFILL-CP] universe 為空,沒事可做", flush=True)
        return 0

    # LLM 模式前置檢查:沒設 GEMINI_API_KEY → 直接退,別跑半天才發現
    if llm_mode:
        from src import config
        if not config.GEMINI_API_KEY:
            print(
                "[BACKFILL-CP] ⚠ --llm-call true 但 GEMINI_API_KEY 未設定,exit",
                file=sys.stderr, flush=True,
            )
            return 1

    mode_label = "LLM 模式(facts + Gemini)" if llm_mode else "warm-up 模式(僅 facts)"
    print(
        f"[BACKFILL-CP] {mode_label}: {total} 檔 (universe={args.universe} "
        f"batch={batch_lo}:{batch_hi})",
        flush=True,
    )

    ok = 0
    failed = 0
    quota_hit = False
    t0 = time.time()

    for i, sid in enumerate(sids, 1):
        try:
            profile = cp.get_company_profile(
                sid,
                llm_call=llm_mode,
                regenerate=args.regenerate if llm_mode else False,
            )
            status = profile.get("narrative_status")

            if llm_mode:
                # LLM 模式:看 narrative_status 判定
                if status == "ok":
                    ok += 1
                elif status == "quota_exceeded":
                    # Gemini 配額爆 — 跟 FinMind 402 同模式,fail-fast 整批中斷
                    print(
                        f"[BACKFILL-CP] ⚠ Gemini quota 爆({sid}),中斷整批 — "
                        "明天 GMT+8 00:00 重置或加 paid quota",
                        file=sys.stderr, flush=True,
                    )
                    logger.warning(
                        "[BACKFILL-CP] Gemini quota exceeded at sid=%s, abort batch",
                        sid,
                    )
                    quota_hit = True
                    break
                elif status == "not_configured":
                    print(
                        f"[BACKFILL-CP] ⚠ GEMINI_API_KEY / SDK 未配置({sid}),中斷",
                        file=sys.stderr, flush=True,
                    )
                    return 1
                else:
                    # "failed" / "empty" / "not_loaded" — 個別失敗,繼續
                    failed += 1
            else:
                # warm-up 模式:看 FinMind facts 是否有寫進
                if profile.get("industry") or profile.get("market"):
                    ok += 1
                else:
                    failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"[BACKFILL-CP] [WARN] {sid} 失敗: {e}", file=sys.stderr, flush=True)
            logger.warning("[BACKFILL-CP] %s exception: %s", sid, e)
            failed += 1

        if i % args.progress_every == 0 or i == total:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate / 60 if rate > 0 else 0
            print(
                f"[BACKFILL-CP] {i}/{total} ok={ok} fail={failed} "
                f"{rate:.2f}/s ETA={eta:.1f}m",
                flush=True,
            )

        if args.sleep > 0:
            time.sleep(args.sleep)

    elapsed = time.time() - t0
    print(
        f"[BACKFILL-CP DONE] 耗時 {elapsed/60:.1f} 分鐘,"
        f"ok={ok} fail={failed} / {total} 檔",
        flush=True,
    )
    logger.info(
        "[BACKFILL-CP DONE] elapsed=%.1fm ok=%d fail=%d total=%d mode=%s",
        elapsed / 60, ok, failed, total,
        "llm" if llm_mode else "warmup",
    )

    if args.dump_format:
        _dump_snapshot(
            fmt=args.dump_format,
            upload_release=args.upload_release,
            release_tag=args.release_tag,
        )

    # exit code:
    # - quota 爆 → 1(workflow 看到紅燈)
    # - LLM 模式失敗 > 50% → 1(可能網路/設定錯)
    # - warm-up 模式有任何成功 → 0(個別 FinMind 失敗不算整批失敗)
    if quota_hit:
        return 1
    if llm_mode and ok == 0:
        return 1
    if llm_mode and failed > total // 2:
        return 1
    return 0 if ok > 0 else 1


def _dump_snapshot(
    fmt: str = "csv",
    *,
    upload_release: bool = False,
    release_tag: str | None = None,
) -> tuple[Path | None, int]:
    """Dump company_profiles 表進 snapshot 檔(CSV 或 Parquet)。

    Returns (out_path, row_count) — DB 空回 (None, 0)。

    Parquet 路徑供 `--upload-release` 用,直接走 GH Release 避開 100MB 上限。
    """
    import pandas as pd

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    with db.get_conn() as conn:
        df = pd.read_sql(
            "SELECT stock_id, industry, market, listing_date, foreign_limit, "
            "description, uniqueness, moat, finmind_updated_at, llm_updated_at "
            "FROM company_profiles "
            "ORDER BY stock_id",
            conn,
        )
    if df.empty:
        print("[BACKFILL-CP] company_profiles 表空,不 dump", flush=True)
        return None, 0

    if fmt == "parquet":
        out = SNAPSHOT_DIR / "company_profiles.parquet"
        try:
            df.to_parquet(out, index=False, compression="zstd")
        except (ImportError, ValueError):
            df.to_parquet(out, index=False, compression="snappy")
    else:
        out = SNAPSHOT_DIR / "company_profiles.csv"
        df.to_csv(out, index=False)

    size_mb = out.stat().st_size / (1024 * 1024)
    print(
        f"[BACKFILL-CP] 寫 {out.name}: {len(df)} 行 ({size_mb:.2f} MB)",
        flush=True,
    )

    if upload_release and out:
        from src.snapshot_release import (
            make_snapshot_tag,
            upload_snapshot_to_release,
        )
        tag = release_tag or make_snapshot_tag("company-profiles")
        notes = f"Backfill company_profiles {len(df)} rows ({out.name})"
        ok = upload_snapshot_to_release(
            tag, [out], notes=notes, snapshot_dir=SNAPSHOT_DIR,
        )
        if ok:
            print(f"[BACKFILL-CP] uploaded → release {tag}", flush=True)
        else:
            print(
                "[BACKFILL-CP] release upload failed(snapshot 仍在 SQLite + 本地檔)",
                flush=True,
            )

    return out, len(df)


if __name__ == "__main__":
    sys.exit(main())
