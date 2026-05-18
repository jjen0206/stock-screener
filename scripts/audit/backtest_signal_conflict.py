"""throwaway audit:60-day signal-conflict penalty 對勝率影響對比。

對 pick_outcomes 內最近 lookback 天的 picks,按 (pick_date, sid) group 出
「該 sid 該日命中哪些策略」,再依 STRATEGY_NATURE 判斷:
  - solo          → 單一策略命中(沒共識可言)
  - consensus_ok  → 多策略 + 無 reversal×trend 衝突(NEW 仍給 bonus)
  - conflict      → 多策略 + reversal×trend 衝突(NEW 不給 bonus)

對每群算 WR (return_d5 > 0)、AvgRet d5(扣 0.6% round-trip cost)、fires 數。

核心問題:conflict 群若 WR 比 consensus_ok 群明顯低 → 衝突檢有意義
(原本給的 bonus 是「假共識」)。

執行:
    python scripts/audit/backtest_signal_conflict.py --lookback 60 --db <path>
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.consensus import STRATEGY_NATURE, has_signal_conflict  # noqa: E402


# 成本(round-trip,主公口徑):0.6% — 跟 bias_convergence rescue 用的口徑一致
COST_ROUND_TRIP_PCT = 0.6


def load_outcomes(db_path: str, lookback: int) -> list[dict]:
    """從 pick_outcomes 拉最近 lookback 天的 picks(含 return_d5)。

    用 lookback 天為視窗(以最新 pick_date 為基準往回算)— pick_outcomes 的
    pick_date 就是 trade_date,return_d5 已 evaluate 完。沒 d5 報酬(太新)
    的列直接濾掉。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT pick_date, sid, strategy, entry_close, return_d5
        FROM pick_outcomes
        WHERE pick_date >= date(
            (SELECT MAX(pick_date) FROM pick_outcomes), ?
        )
          AND return_d5 IS NOT NULL
        ORDER BY pick_date, sid, strategy
        """,
        (f"-{lookback} days",),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def group_by_pick(rows: list[dict]) -> list[dict]:
    """同 (pick_date, sid) 合併成一個 pick(命中 N 個策略 → 一張 pick)。

    return_d5 在同一 pick 內所有 strategy row 都相同(同 sid 同日 entry/exit)。
    取第一個即可。
    """
    grouped: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["pick_date"], r["sid"])
        if key not in grouped:
            grouped[key] = {
                "pick_date": r["pick_date"],
                "sid": r["sid"],
                "strategies": [],
                "return_d5": r["return_d5"],
            }
        grouped[key]["strategies"].append(r["strategy"])
    return list(grouped.values())


def classify(pick: dict) -> str:
    """3 群分類:solo / consensus_ok / conflict。"""
    if len(pick["strategies"]) < 2:
        return "solo"
    if has_signal_conflict(pick["strategies"]):
        return "conflict"
    return "consensus_ok"


def stats(picks: list[dict]) -> dict:
    """fires / wins / WR / avg_ret(扣成本)。"""
    if not picks:
        return {"fires": 0, "wins": 0, "wr": 0.0, "avg_ret_cost_adj": 0.0}
    fires = len(picks)
    rets = [p["return_d5"] for p in picks]
    # return_d5 在 DB 是百分比(scripts/backtest_picks.py:13 算 * 100),
    # 扣成本 = ret(%) - 0.6(%);WR 改成「扣成本後是否 > 0」更貼真實
    wins_cost_adj = sum(1 for r in rets if (r - COST_ROUND_TRIP_PCT) > 0)
    avg_ret_cost_adj = (sum(rets) / fires) - COST_ROUND_TRIP_PCT
    return {
        "fires": fires,
        "wins": wins_cost_adj,
        "wr": wins_cost_adj / fires,
        "avg_ret_cost_adj": avg_ret_cost_adj,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--lookback", type=int, default=60)
    p.add_argument(
        "--db",
        default="D:/Claude-workspace/projects/stock-screener/data/cache.db",
    )
    args = p.parse_args()

    rows = load_outcomes(args.db, args.lookback)
    if not rows:
        print(f"[CONFLICT] pick_outcomes 內最近 {args.lookback} 天無資料")
        return 1

    picks = group_by_pick(rows)
    by_group: dict[str, list[dict]] = defaultdict(list)
    for pk in picks:
        by_group[classify(pk)].append(pk)

    # 命中策略數分布
    n_hit_dist: dict[int, int] = defaultdict(int)
    for pk in picks:
        n_hit_dist[len(pk["strategies"])] += 1

    print(f"\n=== Signal Conflict Penalty Backtest (lookback={args.lookback}d) ===")
    print(f"資料源: {args.db}")
    print(f"pick_outcomes rows: {len(rows)}")
    print(f"unique (date, sid) picks: {len(picks)}")
    print("\n命中策略數分布(unique picks 內):")
    for n in sorted(n_hit_dist):
        print(f"  {n} 策略: {n_hit_dist[n]} 張")

    print("\n" + "=" * 84)
    print(f"成本口徑: round-trip {COST_ROUND_TRIP_PCT}% — WR/AvgRet 皆扣成本")
    print("=" * 84)
    print(f"{'Group':<36} {'Fires':>8} {'Wins':>6} {'WR':>8} {'AvgRet':>10}")
    print("-" * 84)

    order = [
        ("solo (count=1, 無共識)", "solo"),
        ("consensus_ok (NEW 仍給 bonus)", "consensus_ok"),
        ("conflict (NEW 不給 bonus)", "conflict"),
    ]
    group_stats: dict[str, dict] = {}
    for label, key in order:
        s = stats(by_group.get(key, []))
        group_stats[key] = s
        print(
            f"{label:<36} {s['fires']:>8d} {s['wins']:>6d} "
            f"{s['wr'] * 100:>7.1f}% {s['avg_ret_cost_adj']:>+9.2f}%"
        )

    # 對比 OLD vs NEW
    print("\n" + "=" * 84)
    print("OLD (no conflict check) vs NEW (conflict check)")
    print("=" * 84)
    old_multi = by_group.get("consensus_ok", []) + by_group.get("conflict", [])
    new_multi = by_group.get("consensus_ok", [])

    s_old = stats(old_multi)
    s_new = stats(new_multi)
    print(
        f"{'OLD: 所有多策略 picks 都吃 bonus':<36} "
        f"{s_old['fires']:>8d} {s_old['wins']:>6d} "
        f"{s_old['wr'] * 100:>7.1f}% {s_old['avg_ret_cost_adj']:>+9.2f}%"
    )
    print(
        f"{'NEW: 衝突 picks 不吃 bonus':<36} "
        f"{s_new['fires']:>8d} {s_new['wins']:>6d} "
        f"{s_new['wr'] * 100:>7.1f}% {s_new['avg_ret_cost_adj']:>+9.2f}%"
    )
    wr_delta = (s_new["wr"] - s_old["wr"]) * 100
    ret_delta = s_new["avg_ret_cost_adj"] - s_old["avg_ret_cost_adj"]
    print(
        f"\ndelta WR (NEW - OLD):     {wr_delta:+.2f}pp"
        f"\ndelta AvgRet (NEW - OLD): {ret_delta:+.2f}pp"
    )

    # spec gate:WR 提升 ≥ 0.5pp + AvgRet 改善 → merge
    print("\n" + "=" * 84)
    print("Spec Gate:WR 提升 ≥ 0.5pp + AvgRet 改善 → merge")
    print("=" * 84)
    wr_ok = wr_delta >= 0.5
    ret_ok = ret_delta > 0.0
    if wr_ok and ret_ok:
        print("✅ PASS — 衝突檢有正效益,merge")
    elif wr_delta >= 0 and ret_delta >= 0:
        print("△ MARGINAL — 沒衰退但改善 < 0.5pp,可 merge 但效果有限")
    else:
        print(
            f"❌ FAIL — WR_ok={wr_ok} ret_ok={ret_ok} — 不要 merge,先看哪些被誤砍"
        )

    # 列出 conflict picks 細項(讓人類審查)
    print(f"\nConflict picks 細項(共 {len(by_group.get('conflict', []))} 張):")
    for pk in sorted(
        by_group.get("conflict", []), key=lambda x: x["return_d5"],
    )[:20]:
        nature_dist = defaultdict(int)
        for s in pk["strategies"]:
            nature_dist[STRATEGY_NATURE.get(s, "neutral")] += 1
        print(
            f"  {pk['pick_date']} {pk['sid']:>6} ret={pk['return_d5']:+6.2f}% "
            f"strats={pk['strategies']} nature={dict(nature_dist)}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
