"""月報 Telegram + Discord 推播 — 上個月各策略表現排名。

每月 1 號台北時間 09:00（UTC 01:00）由 .github/workflows/monthly-strategy-
report.yml 跑:
  1. init_db + preload_snapshots from data/twse_snapshot/*.csv
  2. 抓上個月(M-1)所有 pick_outcomes,按 strategy group 算 N/WR/AvgD5/Sharpe
  3. 抓上個月大盤(TAIEX)同期 return → baseline
  4. format_monthly_report_for_telegram → 推 Telegram + Discord

跟既有 weekly brief 隔離 — 用「上個月日曆月份」窗口,不是 rolling 30 天,
讓主公月初看到的是「4 月整月」這種乾淨切點。

Usage:
    python scripts/monthly_strategy_report.py                  # 跑「上個月」
    python scripts/monthly_strategy_report.py --month 2026-04  # 指定月份(覆寫)
    python scripts/monthly_strategy_report.py --dry-run        # 印不送
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.discord_notifier import send_discord_message  # noqa: E402
from src.notifier import send_telegram_message  # noqa: E402

try:
    from src.strategies import STRATEGY_LABELS  # noqa: E402
except Exception:  # noqa: BLE001
    STRATEGY_LABELS = {}


def _previous_month_range(today: date | None = None) -> tuple[str, str, str]:
    """回 (start_iso, end_iso, label) — 上個月第一天 / 最後一天 / 'YYYY-MM'。

    today=2026-05-01 → ('2026-04-01', '2026-04-30', '2026-04')
    today=2026-01-15 → ('2025-12-01', '2025-12-31', '2025-12')
    """
    today = today or date.today()
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev.isoformat(), last_prev.isoformat(), f"{first_prev.year:04d}-{first_prev.month:02d}"


def _parse_month_arg(month: str) -> tuple[str, str, str]:
    """'2026-04' → ('2026-04-01', '2026-04-30', '2026-04')。"""
    y, m = month.split("-")
    yi, mi = int(y), int(m)
    first = date(yi, mi, 1)
    # 月底:下個月 1 號 - 1 天
    next_first = date(yi + (1 if mi == 12 else 0), 1 if mi == 12 else mi + 1, 1)
    last = next_first - timedelta(days=1)
    return first.isoformat(), last.isoformat(), f"{yi:04d}-{mi:02d}"


def _query_strategy_stats(
    conn: sqlite3.Connection, start_iso: str, end_iso: str,
) -> list[dict[str, Any]]:
    """跑 SQL 抓上個月 pick_outcomes,按 strategy 聚合。

    回每 strategy: name / n / wr / avg_d5 / std_d5 / sharpe_d5。
    Sharpe 用 daily-equivalent 近似:mean / std(無風險假設 0);std=0/N<2 → None。
    """
    try:
        rows = conn.execute(
            """
            SELECT strategy, return_d5
            FROM pick_outcomes
            WHERE pick_date >= ?
              AND pick_date <= ?
              AND return_d5 IS NOT NULL
            ORDER BY strategy ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    # Group in Python — SQLite stock build 沒有 SQRT,Sharpe 算在這裡
    grouped: dict[str, list[float]] = {}
    for r in rows:
        grouped.setdefault(r["strategy"], []).append(float(r["return_d5"]))

    out: list[dict[str, Any]] = []
    for name, vals in grouped.items():
        n = len(vals)
        wins = sum(1 for v in vals if v > 0)
        avg_d5 = sum(vals) / n if n > 0 else None
        wr = wins / n if n > 0 else None
        std_d5 = 0.0
        if n >= 2 and avg_d5 is not None:
            var = sum((v - avg_d5) ** 2 for v in vals) / (n - 1)
            std_d5 = math.sqrt(var)
        sharpe = None
        if n >= 2 and std_d5 > 1e-9 and avg_d5 is not None:
            sharpe = avg_d5 / std_d5
        out.append({
            "name": name,
            "label": STRATEGY_LABELS.get(name, name),
            "n": n,
            "wr": wr,
            "avg_d5": avg_d5,
            "std_d5": std_d5,
            "sharpe_d5": sharpe,
        })
    out.sort(key=lambda s: (s["avg_d5"] or -99), reverse=True)
    return out


def _query_taiex_baseline(
    conn: sqlite3.Connection, start_iso: str, end_iso: str,
) -> float | None:
    """大盤 TAIEX 上個月同期報酬 %(月底收盤 / 月初前一交易日收盤 - 1)× 100。

    回 None 時 = 資料不足 / TAIEX 缺資料。
    """
    try:
        # 月初前 1 交易日 close(把「進場」對齊 strategy entry 那種感覺)
        row_start = conn.execute(
            """
            SELECT close FROM daily_prices
            WHERE stock_id = 'TAIEX' AND date < ?
            ORDER BY date DESC LIMIT 1
            """,
            (start_iso,),
        ).fetchone()
        # 月底最後一個 TAIEX 交易日 close
        row_end = conn.execute(
            """
            SELECT close FROM daily_prices
            WHERE stock_id = 'TAIEX' AND date >= ? AND date <= ?
            ORDER BY date DESC LIMIT 1
            """,
            (start_iso, end_iso),
        ).fetchone()
    except sqlite3.OperationalError:
        return None

    if not row_start or not row_end:
        return None
    try:
        start_close = float(row_start["close"])
        end_close = float(row_end["close"])
    except (TypeError, ValueError):
        return None
    if start_close <= 0:
        return None
    return (end_close / start_close - 1) * 100.0


def build_monthly_report(
    conn: sqlite3.Connection,
    month: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """組月報 dict。caller 負責開 conn。

    month=None → 自動算「上個月」。否則 'YYYY-MM' 覆寫。
    """
    if month:
        start_iso, end_iso, label = _parse_month_arg(month)
    else:
        start_iso, end_iso, label = _previous_month_range(today)

    stats = _query_strategy_stats(conn, start_iso, end_iso)
    taiex_ret = _query_taiex_baseline(conn, start_iso, end_iso)
    return {
        "month": label,
        "start_date": start_iso,
        "end_date": end_iso,
        "stats": stats,
        "taiex_return_pct": taiex_ret,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def format_monthly_report_for_telegram(report: dict[str, Any]) -> str:
    """月報 markdown(< 4096 字)。

    強策略 Top 3(按 AvgD5 高→低)/ 弱策略 Bottom 3 + 軍師建議。
    沒資料 → 1 條中性提示 + baseline。
    """
    lines: list[str] = []
    month = report.get("month", "—")
    lines.append(f"📅 *{month} 月報* · Phase 2 觀察期")
    lines.append("")

    stats = report.get("stats") or []
    taiex = report.get("taiex_return_pct")
    taiex_str = f"{taiex:+.2f}%" if taiex is not None else "—"

    if not stats:
        lines.append("📭 本月無 pick_outcomes 資料(可能 cron 未跑或樣本不足)")
        lines.append("")
        lines.append(f"📊 大盤(TAIEX)月報酬:{taiex_str}")
        return "\n".join(lines).rstrip()

    total_n = sum(s["n"] for s in stats)
    avg_wr = (
        sum((s["wr"] or 0) * s["n"] for s in stats) / total_n
        if total_n > 0 else None
    )
    avg_ret = (
        sum((s["avg_d5"] or 0) * s["n"] for s in stats) / total_n
        if total_n > 0 else None
    )

    lines.append("📊 *整體表現*")
    lines.append(
        f"N={total_n} · WR {int((avg_wr or 0) * 100)}% · D5 {(avg_ret or 0):+.2f}% "
        f"· TAIEX {taiex_str}"
    )
    lines.append("")

    # 強策略 Top 3(N >= 3 才列,避免單筆 dominate)
    strong = [s for s in stats if s["n"] >= 3]
    strong_sorted = sorted(strong, key=lambda s: (s["avg_d5"] or -99), reverse=True)[:3]
    if strong_sorted:
        lines.append("🔥 *強策略 Top 3*")
        for i, s in enumerate(strong_sorted, 1):
            wr_pct = int((s["wr"] or 0) * 100)
            sharpe = s.get("sharpe_d5")
            sharpe_str = f"Sharpe {sharpe:.2f}" if sharpe is not None else "Sharpe —"
            lines.append(
                f"{i}. {s['label']} · D5 {(s['avg_d5'] or 0):+.2f}% / "
                f"WR {wr_pct}% / N={s['n']} / {sharpe_str}"
            )
        lines.append("")

    # 弱策略 Bottom 3(N >= 3)
    weak_sorted = sorted(strong, key=lambda s: (s["avg_d5"] or 99))[:3]
    if weak_sorted and weak_sorted != strong_sorted:
        lines.append("🥶 *弱策略 Bottom 3*")
        for i, s in enumerate(weak_sorted, 1):
            wr_pct = int((s["wr"] or 0) * 100)
            lines.append(
                f"{i}. {s['label']} · D5 {(s['avg_d5'] or 0):+.2f}% / "
                f"WR {wr_pct}% / N={s['n']}"
            )
        lines.append("")

    # 軍師建議:跟大盤 baseline 比
    recs: list[str] = []
    if taiex is not None and avg_ret is not None:
        if avg_ret > taiex + 1.0:
            recs.append(
                f"🎯 系統整體超越 TAIEX {avg_ret - taiex:+.2f}%,本月有 alpha"
            )
        elif avg_ret < taiex - 1.0:
            recs.append(
                f"⚠️ 系統整體落後 TAIEX {avg_ret - taiex:+.2f}%,檢視弱策略權重"
            )
        else:
            recs.append("📊 系統整體與 TAIEX 接近(±1%),無明顯 alpha/落後")
    if strong_sorted:
        top = strong_sorted[0]
        recs.append(
            f"💡 本月最強:{top['label']}(N={top['n']}),下月可加碼觀察"
        )
    if weak_sorted and weak_sorted != strong_sorted:
        bot = weak_sorted[0]
        recs.append(
            f"🛑 本月最弱:{bot['label']}(N={bot['n']}),建議暫停或降權"
        )

    if recs:
        lines.append("🎖️ *軍師建議*")
        for r in recs:
            lines.append(f"• {r}")
        lines.append("")

    text = "\n".join(lines).rstrip()
    if len(text) > 4000:
        text = text[:3990] + "\n…(截斷)"
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="月報 Telegram brief 推播")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只印不送 Telegram / Discord",
    )
    parser.add_argument(
        "--month", type=str, default=None,
        help="覆寫月份 YYYY-MM(預設『上個月』)",
    )
    args = parser.parse_args()

    db.init_db()
    try:
        db.preload_snapshots()
    except Exception as e:  # noqa: BLE001
        print(f"[MONTHLY-REPORT] preload_snapshots failed: {e}", file=sys.stderr)

    with db.get_conn() as conn:
        report = build_monthly_report(conn, month=args.month)

    text = format_monthly_report_for_telegram(report)
    print("=" * 60)
    print(f"MONTHLY REPORT TEXT (len={len(text)}):")
    print("=" * 60)
    print(text)
    print("=" * 60)

    if args.dry_run:
        print("[MONTHLY-REPORT] --dry-run, 跳過推播")
        return 0

    tg_ok = send_telegram_message(text)
    dc_ok = send_discord_message(text)
    print(f"[MONTHLY-REPORT] Telegram={'OK' if tg_ok else 'FAIL'} / "
          f"Discord={'OK' if dc_ok else 'FAIL'}")

    if not (tg_ok or dc_ok):
        print("[MONTHLY-REPORT] 推播全失敗 — 確認 secrets 是否設定", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
