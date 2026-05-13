"""排程入口:把過去 N 天的 daily_picks 跑出實際 1/3/5/10 日報酬,寫進 pick_outcomes。

設計:
    pick_outcomes 給「昨日複盤」section + weekly backtest 報表用。
    跟 strategy_backtest(per-strategy aggregate over 126 天)不同 —
    本表是 per-pick fire,粒度更細(每張 pick 的實際表現)。

    取數:
    - 對 daily_picks WHERE trade_date >= today - N 天的每筆 (sid, strategy)
    - entry_close 從 payload JSON 取(precompute 時存的策略當天 close)
    - 未來窗口報酬:從 daily_prices 撈 pick_date 之後第 1/3/5/10 個交易日
      close,算 (future_close - entry_close) / entry_close * 100
    - hit_target:d1~d10 區間最高價 / entry - 1 >= 3%
    - stopped_out:d1~d10 區間最低價 / entry - 1 <= -3%

    報酬窗口未到位(例如昨天的 pick 只能算 d1,不能算 d10)→ 該欄寫 NULL,
    下次重跑時 UPSERT 覆蓋(視為新資料補進去)。

CLI:
    python scripts/backtest_picks.py --days 15
    python scripts/backtest_picks.py --days 30 --dump-csv

Exit code:
    0 = 成功 evaluate 至少一筆
    1 = daily_picks 表空 / 全失敗
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


# 讓本檔從任何 cwd 執行都能 import src.*
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402


# 報酬窗口(交易日數)
_RETURN_WINDOWS = (1, 3, 5, 10)
# Target / stop 門檻(主公拍板:固定 +3% / -3%)
_TARGET_PCT = 3.0
_STOP_PCT = -3.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _list_recent_trading_dates(days: int) -> list[str]:
    """從 daily_prices 撈最近 N 個交易日(去重 + 升序)。"""
    if days <= 0:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_prices "
            "WHERE stock_id != 'TAIEX' "
            "ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()
    return sorted(r["date"] for r in rows)


def _fetch_pick_rows(pick_dates: Iterable[str]) -> list[dict]:
    """撈這些日期的 daily_picks(包含 payload JSON)— per (date, sid, strategy)。

    去重:同 (date, sid, strategy) 不同 universe 只取一筆(以 universe='pure_stock'
    優先,否則任一)— 因為 pick_outcomes 的 PK 不含 universe。同 sid 同策略不論
    在哪個 universe 內被命中,實際 entry/return 都一樣。
    """
    pick_dates = list(pick_dates)
    if not pick_dates:
        return []
    placeholders = ",".join("?" * len(pick_dates))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT trade_date, sid, strategy, universe, payload
            FROM daily_picks
            WHERE trade_date IN ({placeholders})
              AND params_hash = 'default_v1'
            """,
            pick_dates,
        ).fetchall()
    # 去重:同 (date, sid, strategy) 取第一筆(pure_stock 優先)
    seen: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (r["trade_date"], r["sid"], r["strategy"])
        existing = seen.get(key)
        if existing is None or (
            r["universe"] == "pure_stock" and existing["universe"] != "pure_stock"
        ):
            seen[key] = dict(r)
    return list(seen.values())


def _fetch_future_prices(
    sids: list[str], pick_dates: list[str],
) -> dict[str, list[dict]]:
    """對每個 (sid, pick_date) 範圍內可能用到的 future 交易日資料一次撈出。

    回 {sid: [{date, close, high, low}, ...]} 升序。caller 自己根據 pick_date
    定位 d1/d3/d5/d10 在 list 內位置。

    撈最廣的範圍:從 min(pick_dates) 到 max(pick_dates) + 30 天(留 buffer 給 d10)。
    交易日 ~ 5 個自然日 7 個 → 10 個交易日 ~ 14 個自然日,30 天夠安全。
    """
    if not sids or not pick_dates:
        return {}
    from datetime import date as _date, timedelta
    min_pick = min(pick_dates)
    max_pick = max(pick_dates)
    # 取 30 天 buffer 涵蓋 d10
    end = (_date.fromisoformat(max_pick) + timedelta(days=30)).isoformat()
    placeholders = ",".join("?" * len(sids))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT stock_id, date, close, high, low
            FROM daily_prices
            WHERE stock_id IN ({placeholders})
              AND date >= ? AND date <= ?
            ORDER BY stock_id, date
            """,
            sids + [min_pick, end],
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["stock_id"], []).append(dict(r))
    return out


def _compute_outcome(
    pick_date: str,
    entry_close: float | None,
    sid_prices: list[dict],
) -> dict:
    """算單筆 (pick_date, sid) 的 d1/d3/d5/d10 報酬 + hit_target/stopped_out。

    sid_prices 是該 sid 的時序 list[dict{date, close, high, low}]。

    報酬窗口未到位(例 pick 是昨天,只能算 d1)→ 該欄 None;hit_target /
    stopped_out 至少要有 d1 才會算(0 否則 NULL)。
    """
    out: dict[str, float | None] = {
        "return_d1": None, "return_d3": None,
        "return_d5": None, "return_d10": None,
        "hit_target": None, "stopped_out": None,
    }
    if entry_close is None or entry_close <= 0 or not sid_prices:
        return out

    # pick_date 之後的交易日 list(升序)
    future = [p for p in sid_prices if p["date"] > pick_date]
    if not future:
        return out

    for window in _RETURN_WINDOWS:
        if len(future) >= window:
            future_close = future[window - 1]["close"]
            if future_close is None:
                continue
            ret = (float(future_close) - entry_close) / entry_close * 100.0
            out[f"return_d{window}"] = round(ret, 4)

    # hit_target / stopped_out:看 d1~d10 區間是否有 high 達 +3% 或 low 觸 -3%
    window_slice = future[:_RETURN_WINDOWS[-1]]  # 取前 10 個交易日
    if window_slice:
        out["hit_target"] = 0.0
        out["stopped_out"] = 0.0
        target_price = entry_close * (1 + _TARGET_PCT / 100.0)
        stop_price = entry_close * (1 + _STOP_PCT / 100.0)
        for p in window_slice:
            high = p.get("high")
            low = p.get("low")
            if high is not None and float(high) >= target_price:
                out["hit_target"] = 1.0
            if low is not None and float(low) <= stop_price:
                out["stopped_out"] = 1.0
    return out


def evaluate_picks(days: int) -> int:
    """跑過去 N 個交易日的 picks,把實際報酬寫進 pick_outcomes。回 written rows。"""
    db.init_db()
    pick_dates = _list_recent_trading_dates(days)
    if not pick_dates:
        print(
            "[BACKTEST] daily_prices 無歷史,沒交易日可 evaluate",
            flush=True,
        )
        return 0

    pick_rows = _fetch_pick_rows(pick_dates)
    if not pick_rows:
        print(
            f"[BACKTEST] daily_picks 表內 {pick_dates[0]} ~ {pick_dates[-1]} "
            f"沒任何資料(precompute 還沒跑過?)",
            flush=True,
        )
        return 0

    sids = sorted({r["sid"] for r in pick_rows})
    print(
        f"[BACKTEST] evaluate {len(pick_rows)} pick rows "
        f"({len(sids)} unique sids,日期 {pick_dates[0]} ~ {pick_dates[-1]})",
        flush=True,
    )

    price_map = _fetch_future_prices(sids, pick_dates)
    now = _now_iso()
    out_rows: list[dict] = []
    skipped_no_payload = 0

    for r in pick_rows:
        payload_raw = r.get("payload")
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        entry_close = payload.get("close")
        if entry_close is None:
            # fallback:從 daily_prices 撈 pick_date 當天 close
            sid_prices = price_map.get(r["sid"], [])
            same_day = next(
                (p for p in sid_prices if p["date"] == r["trade_date"]),
                None,
            )
            if same_day:
                entry_close = same_day.get("close")
        if entry_close is None:
            skipped_no_payload += 1
            continue
        try:
            entry_close = float(entry_close)
        except (TypeError, ValueError):
            skipped_no_payload += 1
            continue

        outcome = _compute_outcome(
            r["trade_date"], entry_close, price_map.get(r["sid"], []),
        )
        out_rows.append({
            "pick_date": r["trade_date"],
            "sid": r["sid"],
            "strategy": r["strategy"],
            "entry_close": entry_close,
            "return_d1": outcome["return_d1"],
            "return_d3": outcome["return_d3"],
            "return_d5": outcome["return_d5"],
            "return_d10": outcome["return_d10"],
            "hit_target": outcome["hit_target"],
            "stopped_out": outcome["stopped_out"],
            "evaluated_at": now,
        })

    if skipped_no_payload:
        print(
            f"[BACKTEST] {skipped_no_payload} 筆 payload 缺 close 跳過",
            flush=True,
        )

    written = db.dump_pick_outcomes(out_rows)
    print(f"[BACKTEST] UPSERT pick_outcomes: {written} rows", flush=True)
    return written


def dump_pick_outcomes_csv(snapshot_dir: Path | None = None) -> int:
    """把 pick_outcomes 全表 dump 成 CSV(weekly workflow git push 用)。

    跟 daily_picks.csv 同 pattern — 讓雲端容器 boot 時透過 preload_snapshots
    拿回最新 outcomes,讓 daily-notify 的「昨日複盤」section 直接吃 SQLite。

    回 row count;空表 → 不寫 CSV,回 0。
    """
    import pandas as pd

    if snapshot_dir is None:
        snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT pick_date, sid, strategy, entry_close,
                   return_d1, return_d3, return_d5, return_d10,
                   hit_target, stopped_out, evaluated_at
            FROM pick_outcomes
            ORDER BY pick_date DESC, sid, strategy
            """
        ).fetchall()

    if not rows:
        print("[BACKTEST] pick_outcomes 表空,不 dump CSV", flush=True)
        return 0

    df = pd.DataFrame([dict(r) for r in rows])
    csv_path = snapshot_dir / "pick_outcomes.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(
        f"[BACKTEST] dump CSV → {csv_path} ({len(rows)} rows)",
        flush=True,
    )
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser(description="weekly backtest:把 daily_picks 跑出實際報酬")
    p.add_argument(
        "--days", type=int, default=15,
        help="倒推 N 個交易日 evaluate(default 15,涵蓋 d10 + 一週 buffer)",
    )
    p.add_argument(
        "--dump-csv", action="store_true",
        help="evaluate 完 dump pick_outcomes.csv 到 snapshot dir(weekly workflow 用)",
    )
    args = p.parse_args()

    if args.days <= 0:
        print("[BACKTEST] --days 必須 > 0", flush=True)
        return 1

    db.init_db()
    # workflow runner fresh container,SQLite 空 → preload snapshot CSV
    # 才有歷史 daily_prices / daily_picks 可 evaluate。
    preload = db.preload_snapshots()
    if preload:
        print(f"[BACKTEST] preload snapshots: {preload}", flush=True)

    written = evaluate_picks(args.days)
    if written == 0:
        print("[BACKTEST] 0 rows evaluated(daily_picks 空 / 全 skip)", flush=True)
        return 1

    if args.dump_csv:
        dump_pick_outcomes_csv()

    print(f"[BACKTEST] DONE rows_written={written}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
