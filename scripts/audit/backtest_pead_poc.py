"""PEAD (earnings_surprise_followthrough) PoC backtest.

Why a separate script (not via backtest_combination):
  該策略剛 wire,daily_picks 還沒寫過任何 picks,沒有歷史記錄可走既有
  backtest_combination 路徑。本 PoC 直接 walk 歷史 trading dates,呼叫
  screen_earnings_surprise_followthrough 模擬「當日 dashboard 命中」,
  再算 entry/exit ROI。Backfill 完整後再走 strict 路徑。

PoC gate(放鬆):樣本 ≥ 5 + WR > 35%(任務 spec)
"""
from __future__ import annotations

import sys
from datetime import date as _date, timedelta as _td

from src import database as db
from src.strategies import screen_earnings_surprise_followthrough
from src.strategy_backtest import _entry_price, _resolve_exit_price


def iter_trading_dates(start: str, end: str) -> list[str]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_prices "
            "WHERE date BETWEEN ? AND ? ORDER BY date ASC",
            (start, end),
        ).fetchall()
    return [r["date"] for r in rows]


def main(start: str = "2025-01-15", end: str = "2026-05-12") -> int:
    """walk trading days,每日跑 PEAD 策略,記錄第一次命中(避同 announce 多日重複)。

    HOLDING_DAYS=5(策略 spec)。WR 用扣除滑價前的 raw return(PoC)。
    """
    HOLDING_DAYS = 5
    # PoC override:本機 snapshot daily_prices 多數 sid 只有 65 天,正式 spec
    # avg_volume_lookback=60 太貼邊 → 拉到 30 提升樣本可用性。Backfill 完整後
    # cron 跑用 default(60)。
    POC_PARAMS = {"avg_volume_lookback": 30}

    # 撈 2025-01-15 ~ 2026-05-12 所有交易日
    trading_dates = iter_trading_dates(start, end)
    print(f"[PoC] trading dates: {len(trading_dates)} ({start} ~ {end})")

    # 限定 universe = 有 announce_date 資料的 168 個 sids(其餘沒資料不會命中)
    with db.get_conn() as conn:
        sids = [
            r["stock_id"] for r in conn.execute(
                "SELECT DISTINCT stock_id FROM financials "
                "WHERE period_type='quarterly' AND announce_date IS NOT NULL"
            ).fetchall()
        ]
    print(f"[PoC] universe (sids with announce_date): {len(sids)}")

    # 記錄每 (sid, announce_date) 第一次命中(避同 announce 連 5 天重複算)
    seen: set[tuple[str, str]] = set()
    trades: list[dict] = []
    coverage_dates_with_picks = 0

    for d in trading_dates:
        try:
            df = screen_earnings_surprise_followthrough(
                d, params=POC_PARAMS, stock_ids=sids,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  [{d}] error: {e}")
            continue
        if df.empty:
            continue
        coverage_dates_with_picks += 1
        for _, row in df.iterrows():
            sid = str(row["stock_id"])
            ann = str(row["announce_date"])
            key = (sid, ann)
            if key in seen:
                continue
            seen.add(key)
            with db.get_conn() as conn:
                entry = _entry_price(conn, sid, d)
                if entry is None or entry <= 0:
                    continue
                exit_px, exit_date = _resolve_exit_price(
                    conn, sid, d, HOLDING_DAYS,
                )
                if exit_px is None:
                    continue
            ret_pct = (exit_px - entry) / entry * 100.0
            trades.append({
                "pick_date": d,
                "sid": sid,
                "announce_date": ann,
                "days_after": int(row["days_after_announce"]),
                "eps_yoy": (
                    float(row["eps_yoy"])
                    if row["eps_yoy"] is not None else None
                ),
                "gap_open_pct": (
                    float(row["gap_open_pct"])
                    if row["gap_open_pct"] is not None else None
                ),
                "score": float(row["score"]),
                "entry": entry,
                "exit": exit_px,
                "exit_date": exit_date,
                "ret_pct": ret_pct,
            })

    n = len(trades)
    print()
    print(f"[PoC] trades (dedup by (sid, announce_date)): {n}")
    print(f"[PoC] dates with picks: {coverage_dates_with_picks} / {len(trading_dates)}")

    if n == 0:
        print("[PoC] gate FAILED: 樣本 = 0 (太少資料 / backfill 未完)")
        return 1

    wins = sum(1 for t in trades if t["ret_pct"] > 0)
    avg = sum(t["ret_pct"] for t in trades) / n
    wr = wins / n
    total = sum(t["ret_pct"] for t in trades)
    print(f"[PoC] win_rate: {wr:.2%}  ({wins}/{n})")
    print(f"[PoC] avg_return: {avg:+.2f}%")
    print(f"[PoC] total_return (sum): {total:+.2f}%")

    # 分路徑 stats
    eps_only = [t for t in trades if t["eps_yoy"] and t["eps_yoy"] > 50]
    gap_only = [t for t in trades if t["gap_open_pct"] and t["gap_open_pct"] > 3]
    print(
        f"[PoC] eps_yoy path: {len(eps_only)} fires, "
        f"WR {sum(1 for t in eps_only if t['ret_pct']>0)/max(len(eps_only),1):.2%}, "
        f"avg {sum(t['ret_pct'] for t in eps_only)/max(len(eps_only),1):+.2f}%"
    )
    print(
        f"[PoC] gap path:     {len(gap_only)} fires, "
        f"WR {sum(1 for t in gap_only if t['ret_pct']>0)/max(len(gap_only),1):.2%}, "
        f"avg {sum(t['ret_pct'] for t in gap_only)/max(len(gap_only),1):+.2f}%"
    )

    # 樣本展示
    print("\n[PoC] sample (top 10 by score):")
    for t in sorted(trades, key=lambda x: -x["score"])[:10]:
        print(
            f"  {t['pick_date']} {t['sid']} announce={t['announce_date']} "
            f"d+{t['days_after']} eps_yoy={t['eps_yoy']} gap={t['gap_open_pct']} "
            f"score={t['score']:.2f} ret={t['ret_pct']:+.2f}%"
        )

    # PoC gate(放鬆)
    gate_ok = n >= 5 and wr > 0.35
    if gate_ok:
        print(f"\n[PoC] gate PASS (n={n} >= 5, WR={wr:.2%} > 35%)")
        return 0
    else:
        print(f"\n[PoC] gate FAIL (n={n}, WR={wr:.2%})")
        return 2


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-15"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-05-12"
    sys.exit(main(start, end))
