"""排程入口:預跑 daily_picks 內每 sid 的 compute_verdict,聚合成 banner CSV。

跑時機:daily-notify.yml workflow 內、daily_notify step 之後(daily_picks 已寫入)。
產出:data/twse_snapshot/daily_verdict_summary.csv,雲端 Streamlit Cloud
boot 後 verdict banner 直接吃此 CSV 不用每次 user 開首頁重算 ~50 picks × ~200ms。

來源 universe:
  1. daily_picks 表 trade_date = latest 的所有 sid(去重)
  2. + watchlist 表所有 sid(主公自選關注 — 即使沒進策略也要算 verdict)

Exit code:
  0 = 成功(CSV 已寫,row count ≥ 3)
  1 = 失敗(daily_picks 表空 且 watchlist 空 → 沒 sid 可算)

CLI:
  python scripts/precompute_verdicts.py            # 跑當日 latest
  python scripts/precompute_verdicts.py --date 2026-05-18
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.verdict_summary import build_summary, dump_to_csv  # noqa: E402


def _collect_universe(trade_date: str) -> list[dict]:
    """daily_picks (trade_date) ∪ watchlist。回 list[dict] with sid + name。"""
    sid_to_name: dict[str, str | None] = {}

    # 1) daily_picks 命中 sid(各 strategy 去重)
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT p.sid, s.name FROM daily_picks p "
                "LEFT JOIN stocks s ON s.stock_id = p.sid "
                "WHERE p.trade_date=?",
                (trade_date,),
            ).fetchall()
        for r in rows:
            sid = str(r["sid"]).strip()
            if sid:
                sid_to_name[sid] = r["name"]
    except Exception as e:  # noqa: BLE001
        print(
            f"[PRECOMPUTE-VERDICT] daily_picks 查詢失敗 {type(e).__name__}: {e}",
            flush=True,
        )

    # 2) watchlist(主公自選 — 即使沒進策略也算 verdict 進 banner)
    try:
        items = db.get_watchlist() or []
        for it in items:
            sid = str(it.get("stock_id", "")).strip()
            if not sid:
                continue
            if sid not in sid_to_name:
                # name 從 stocks 表 lookup,verdict_summary._resolve_name 會 fallback
                sid_to_name[sid] = None
    except Exception as e:  # noqa: BLE001
        print(
            f"[PRECOMPUTE-VERDICT] watchlist 查詢失敗 {type(e).__name__}: {e}",
            flush=True,
        )

    return [{"sid": sid, "name": name} for sid, name in sid_to_name.items()]


def main() -> int:
    p = argparse.ArgumentParser(description="預跑 daily_verdict_summary CSV")
    p.add_argument(
        "--date",
        help="目標日期 YYYY-MM-DD;留空 = SQLite 內 daily_prices MAX(date)",
    )
    args = p.parse_args()

    db.init_db()
    # 雲端 / GH runner fresh container,先 preload snapshot(才有 daily_picks)
    preload = db.preload_snapshots()
    if preload:
        print(
            f"[PRECOMPUTE-VERDICT] preload snapshots: {preload}",
            flush=True,
        )

    if args.date:
        trade_date = args.date
    else:
        trade_date = db.get_latest_trading_date()
        if not trade_date:
            from datetime import date as _date
            trade_date = _date.today().isoformat()
            print(
                f"[PRECOMPUTE-VERDICT] 警告:SQLite 無歷史,用 today={trade_date}",
                flush=True,
            )

    universe = _collect_universe(trade_date)
    if not universe:
        print(
            "[PRECOMPUTE-VERDICT] universe 空(daily_picks + watchlist 皆無)",
            flush=True,
        )
        return 1

    print(
        f"[PRECOMPUTE-VERDICT] trade_date={trade_date} "
        f"universe={len(universe)} sids",
        flush=True,
    )

    t0 = time.perf_counter()
    summary = build_summary(
        universe,  # list[dict] {sid, name}
        trade_date,
    )
    elapsed = time.perf_counter() - t0

    counts = summary["counts"]
    print(
        f"[PRECOMPUTE-VERDICT] verdict counts: "
        f"🟢 {counts['green']} / 🟡 {counts['yellow']} / 🔴 {counts['red']} "
        f"(elapsed {elapsed:.1f}s)",
        flush=True,
    )

    snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    csv_path = snapshot_dir / "daily_verdict_summary.csv"
    rows_written = dump_to_csv(summary, csv_path)
    print(
        f"[PRECOMPUTE-VERDICT] dump CSV → {csv_path} ({rows_written} rows)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
