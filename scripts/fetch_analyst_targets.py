"""法人(券商研究員)目標價抓取 CLI(A+B 雙來源:yfinance + Gemini news)。

使用範例:
    # 平日:抓 watchlist + 今日 picks(快、quota 內)
    python scripts/fetch_analyst_targets.py --scope=watchlist
    python scripts/fetch_analyst_targets.py --scope=picks

    # 週日:全市場(~1500 檔,跑 ~30-60 分鐘,quota 內)
    python scripts/fetch_analyst_targets.py --scope=all

    # 限制檔數(debug)
    python scripts/fetch_analyst_targets.py --scope=watchlist --limit 5

排程方式:
    - daily-notify.yml 平日跑 watchlist + picks(notify step 之前)
    - weekly-targets.yml 週日跑 all 全市場(snapshot commit + push)

Exit code:
    0 = 成功(命中或無命中都算成功;空清單也 0)
    1 = scope 解析失敗 / SQLite 出錯
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 讓本檔從任何 cwd 執行都能 import src.*
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src import analyst_targets as at  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402


def _get_watchlist_sids() -> list[str]:
    return [w["stock_id"] for w in db.get_watchlist()]


def _get_today_picks_sids() -> list[str]:
    """從 daily_picks 撈最新一筆 trade_date 的所有 sid(去重)。

    daily_picks 的 trade_date / universe / params_hash 在 nightly precompute 寫入。
    這裡優先 'pure_stock' universe + 'default_v1' params_hash;沒就 fallback 全表。
    """
    db.init_db()
    with db.get_conn() as conn:
        latest = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_picks"
        ).fetchone()
        if not latest or not latest["d"]:
            return []
        rows = conn.execute(
            "SELECT DISTINCT sid FROM daily_picks "
            "WHERE trade_date=? "
            "ORDER BY sid",
            (latest["d"],),
        ).fetchall()
    return [r["sid"] for r in rows]


def _get_all_pure_stock_sids() -> list[str]:
    """全市場 pure_stock universe(過濾 ETF / 債券)。

    走既有 universe.pure_stock_universe(min_history=20) — 跟 notifier
    _select_top_picks default 一致。
    """
    from src.universe import pure_stock_universe
    return pure_stock_universe(min_history=20)


def _name_for_sid(sid: str) -> str:
    """從 stocks 表撈 name;沒有回 ""(給 Gemini fallback prompt 用)。"""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM stocks WHERE stock_id=? LIMIT 1", (sid,),
        ).fetchone()
    return row["name"] if row else ""


def main() -> int:
    p = argparse.ArgumentParser(
        description="抓法人目標價(A=yfinance / B=Gemini news fallback) → SQLite analyst_targets",
    )
    p.add_argument(
        "--scope", required=True,
        choices=["watchlist", "picks", "all"],
        help="watchlist:自選股 / picks:今日 daily_picks / all:全市場 pure_stock",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="最多抓幾檔(debug 用,預設不限)",
    )
    p.add_argument(
        "--no-gemini", action="store_true",
        help="禁用 B 來源(Gemini fallback)— 只試 yfinance 失敗就 skip",
    )
    p.add_argument(
        "--sleep", type=float, default=0.5,
        help="每檔之間 sleep 秒數,避免被 Yahoo throttle(default 0.5)",
    )
    args = p.parse_args()

    setup_file_logging("fetch_analyst_targets", mirror_print=True)

    db.preload_snapshots()  # GitHub Actions runner 有 fresh SQLite,需先 preload

    if args.scope == "watchlist":
        sids = _get_watchlist_sids()
    elif args.scope == "picks":
        sids = _get_today_picks_sids()
    else:
        sids = _get_all_pure_stock_sids()

    if args.limit:
        sids = sids[: args.limit]

    print(
        f"[ANALYST] scope={args.scope} 共 {len(sids)} 檔,"
        f"use_gemini_fallback={not args.no_gemini}",
        flush=True,
    )
    if not sids:
        print("[ANALYST] 0 檔,結束(scope 對應清單為空)", flush=True)
        return 0

    n_yf = n_gemini = n_fail = 0
    # 收集每筆 upsert 的 change_info,batch 結束後一次 push 異動推播
    # (主公拍板 2026-05-08:|Δ%| ≥ 5% 且 sid ∈ 6 類聯集才推 + 同日同方向防重複)
    changes_for_alert: list[dict] = []
    for i, sid in enumerate(sids, start=1):
        name = _name_for_sid(sid)
        try:
            data = at.fetch_and_store(
                sid=sid, name=name,
                use_gemini_fallback=not args.no_gemini,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(sids)}] {sid} 失敗:{e}", flush=True)
            n_fail += 1
            continue

        if data and data.get("_change_info"):
            changes_for_alert.append(data["_change_info"])

        if data is None:
            n_fail += 1
            if i % 50 == 0 or i == len(sids):
                print(
                    f"  [{i}/{len(sids)}] 進度:{n_yf}A / {n_gemini}B / "
                    f"{n_fail} 失敗",
                    flush=True,
                )
        elif data["source"] == "yfinance":
            n_yf += 1
            print(
                f"  [{i}/{len(sids)}] {sid} {name} ✅ yfinance "
                f"target_mean={data['target_mean']:.0f} "
                f"({data.get('num_analysts')} analysts)",
                flush=True,
            )
        else:
            n_gemini += 1
            print(
                f"  [{i}/{len(sids)}] {sid} {name} 🤖 gemini_news "
                f"target_mean={data['target_mean']:.0f}",
                flush=True,
            )

        if args.sleep > 0:
            time.sleep(args.sleep)

    print(
        f"\n[ANALYST] 完成:A(yfinance) {n_yf} / B(gemini) {n_gemini} / "
        f"失敗 {n_fail} / 總 {len(sids)}",
        flush=True,
    )

    # === 異動推播(主公拍板 2026-05-08)===
    # 對所有 upsert 完的 changes,跑一次 notify_target_changes:
    # 內部 filter sid ∈ 6 類聯集 + |Δ%| ≥ 5% + 同日同方向未推過 → push TG/Discord
    if changes_for_alert:
        try:
            from src.analyst_target_alerts import notify_target_changes
            result = notify_target_changes(changes_for_alert)
            print(
                f"[ANALYST-ALERT] 異動推播 — 收集 {len(changes_for_alert)} "
                f"筆 change → 通過 filter {result['n_eligible']} 筆 → "
                f"TG={result['n_pushed_telegram']} / "
                f"Discord={result['n_pushed_discord']}",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            # 異動推播失敗不該影響 fetch CLI 的 exit code
            print(f"[ANALYST-ALERT] 推播 step 失敗(忽略):{e}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
