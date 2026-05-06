"""Paper trading 追蹤 — 驗證 Stage 2B v2 ML 過濾在實盤是否有效。

純 paper tracking,**不影響真實 trades 表**。寫入 paper_trades 表(schema 在
src/database.py 內定義),App 端「🧪 實測追蹤」頁 render 時自動 evaluate active
trades。

判定規則跟 backtest.simulate_outcome 同口徑:
- 進場 D = entry_date,entry_price = D 收盤
- target_price = entry × (1 + target_pct)
- stop_price = entry × (1 - stop_pct)
- 逐日掃 D+1 ~ D+hold_days:
    - 該日 low ≤ stop_price → 'lose', return = -stop_pct,exit on that day
    - 該日 high ≥ target_price → 'win', return = +target_pct,exit on that day
    - 同日兩邊都觸 → 保守 'lose'(intra-day path 不可知)
- 超過 hold_days 都沒觸 → timeout
    - 收盤 > entry → 'timeout_win', return = (close-entry)/entry
    - 收盤 ≤ entry → 'timeout_lose', return = (close-entry)/entry
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from src import database as db

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _next_trading_dates(
    sid: str, after_date: str, n: int,
    db_path: str | Path | None = None,
) -> list[str]:
    """回 sid 在 after_date 之後最多 n 個交易日(該 sid 自己有資料的日子)。

    若資料不足 n 個 → 回實際有的(< n 個)。
    """
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT date FROM daily_prices "
            "WHERE stock_id=? AND date > ? "
            "ORDER BY date ASC LIMIT ?",
            (sid, after_date, n),
        ).fetchall()
    return [r["date"] for r in rows]


def add_paper_trade(
    sid: str,
    name: str | None,
    entry_date: str,
    entry_price: float,
    matched_strategies: Iterable[str] | None = None,
    ml_prob: float | None = None,
    target_pct: float = 0.05,
    stop_pct: float = 0.03,
    hold_days: int = 5,
    db_path: str | Path | None = None,
) -> int | None:
    """寫入 paper trade。同 (sid, entry_date) 已存在 → 回 None(冪等)。

    target_price / stop_price 從 entry_price + (target_pct, stop_pct) 算。
    expected_exit_date 從 daily_prices 找 entry_date 之後第 hold_days 個交易
    日;若該 sid 之後沒這麼多天資料 → 留 NULL(evaluate 時動態查)。

    回新建 row id;UNIQUE conflict → 回 None。
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be > 0,got {entry_price}")
    if hold_days <= 0:
        raise ValueError(f"hold_days must be > 0,got {hold_days}")
    if target_pct <= 0 or stop_pct <= 0:
        raise ValueError("target_pct / stop_pct must be > 0 (decimal e.g. 0.05)")

    target_price = entry_price * (1 + target_pct)
    stop_price = entry_price * (1 - stop_pct)
    matched_json = (
        json.dumps(list(matched_strategies), ensure_ascii=False)
        if matched_strategies else None
    )

    # 找 expected_exit_date(若資料不足留 NULL,evaluate 端再算)
    future_dates = _next_trading_dates(sid, entry_date, hold_days, db_path=db_path)
    expected_exit = future_dates[-1] if len(future_dates) >= hold_days else None

    now = _now_iso()
    with db.get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_trades
                (sid, name, entry_date, entry_price, matched_strategies,
                 ml_prob, target_price, stop_price, current_stop,
                 trailing_level, hold_days,
                 expected_exit_date, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'active', ?)
            """,
            (sid, name, entry_date, entry_price, matched_json,
             ml_prob, target_price, stop_price, stop_price,
             hold_days,
             expected_exit, now),
        )
        return cur.lastrowid if cur.rowcount > 0 else None


def bulk_add_paper_trades(
    rows: Iterable[dict],
    entry_date: str,
    target_pct: float = 0.05,
    stop_pct: float = 0.03,
    hold_days: int = 5,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """批量寫入 paper trades。已追蹤(UNIQUE 衝突)→ 算進 skipped 不加。

    rows 預期 dict 含 stock_id / close;optional name / matched_strategies /
    ml_prob。invalid(沒 sid / close ≤ 0 / NaN)→ errors。

    回 {added, skipped, errors}。
    """
    n_added = 0
    n_skipped = 0
    n_errors = 0
    for r in rows:
        sid = str(r.get("stock_id", "") or "").strip()
        if not sid:
            n_errors += 1
            continue
        close = r.get("close")
        try:
            close_val = float(close) if close is not None else 0.0
        except (TypeError, ValueError):
            n_errors += 1
            continue
        if close_val <= 0 or close_val != close_val:  # NaN check
            n_errors += 1
            continue

        name = str(r.get("name", "") or "")
        matched = r.get("matched_strategies") or []
        if isinstance(matched, str):
            try:
                matched = json.loads(matched)
            except (TypeError, ValueError):
                matched = []
        ml_prob = r.get("ml_prob")
        try:
            ml_prob_val = (
                float(ml_prob) if ml_prob is not None
                and float(ml_prob) == float(ml_prob)  # not NaN
                else None
            )
        except (TypeError, ValueError):
            ml_prob_val = None

        try:
            new_id = add_paper_trade(
                sid=sid, name=name, entry_date=entry_date,
                entry_price=close_val,
                matched_strategies=list(matched),
                ml_prob=ml_prob_val,
                target_pct=target_pct, stop_pct=stop_pct,
                hold_days=hold_days, db_path=db_path,
            )
            if new_id:
                n_added += 1
            else:
                n_skipped += 1  # UNIQUE conflict(已追蹤)
        except ValueError:
            n_errors += 1
    return {"added": n_added, "skipped": n_skipped, "errors": n_errors}


def already_tracked(
    sid: str, entry_date: str, db_path: str | Path | None = None,
) -> bool:
    """同 sid 同日已加追蹤過 → True。給 UI 灰掉重複 button。"""
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM paper_trades WHERE sid=? AND entry_date=? LIMIT 1",
            (sid, entry_date),
        ).fetchone()
    return row is not None


def _update_trailing_stop(
    entry_price: float,
    current_stop: float,
    trailing_level: int,
    current_close: float,
) -> tuple[float, int]:
    """根據浮動報酬上移停損(主公拍板的「動態停損 / 移動停利」)。

    Trail 觸發點(都根據 close,只升不降 — 確保停損單調上移):
      pct ≥ 8%  → level 3:鎖 5%  → stop = entry × 1.05
      pct ≥ 5%  → level 2:鎖 2%  → stop = entry × 1.02
      pct ≥ 3%  → level 1:保本    → stop = entry
      pct < 3% → 維持原 current_stop / level 不動

    回 (new_current_stop, new_trailing_level)。冪等(已升到更高 level 不會降)。
    """
    if entry_price <= 0:
        return current_stop, trailing_level
    pct = (current_close - entry_price) / entry_price

    new_stop = current_stop
    new_level = trailing_level
    if pct >= 0.08 and trailing_level < 3:
        new_stop = entry_price * 1.05
        new_level = 3
    elif pct >= 0.05 and trailing_level < 2:
        new_stop = entry_price * 1.02
        new_level = 2
    elif pct >= 0.03 and trailing_level < 1:
        new_stop = entry_price
        new_level = 1
    return new_stop, new_level


def _evaluate_one(
    sid: str,
    entry_date: str,
    entry_price: float,
    target_price: float,
    initial_stop: float,
    hold_days: int,
    db_path: str | Path | None = None,
) -> dict | None:
    """模擬該 trade 進場後 hold_days 天 outcome。資料不足 → 回 None(active 不變)。

    Trailing 邏輯(2026-05-06 加):每天用 close 算浮動報酬 → _update_trailing_stop
    上移 current_stop。若任何一天 low ≤ current_stop → 出場用 current_stop 計算
    return_pct(可能 -3% / 0% / +2% / +5% 取決於當時的 trailing_level)。

    每次呼叫從 initial_stop / level=0 開始,deterministic — 同樣價格歷史
    結果一致,不依賴 DB 內 current_stop / trailing_level 上次的 snapshot。

    回 dict {status, return_pct, exit_date, exit_price, current_stop,
            trailing_level};資料不足回 None。
    """
    future_dates = _next_trading_dates(
        sid, entry_date, hold_days, db_path=db_path,
    )
    if len(future_dates) < hold_days:
        # 資料不足,active 不變(下次 evaluate 再試)
        return None

    last_date = future_dates[-1]
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT date, high, low, close FROM daily_prices "
            "WHERE stock_id=? AND date > ? AND date <= ? "
            "ORDER BY date ASC",
            (sid, entry_date, last_date),
        ).fetchall()

    if len(rows) < hold_days:
        return None

    last_close = entry_price
    last_seen_date = entry_date
    cs = float(initial_stop)
    tl = 0
    for r in rows[:hold_days]:
        d = r["date"]
        high = float(r["high"] or 0)
        low = float(r["low"] or 0)
        close = float(r["close"] or 0)
        last_close = close
        last_seen_date = d

        # 1. 先用「昨日 close 後設定的 current_stop」偵測今日 stop 觸發
        # (real-world 邏輯:trailing 由前一日收盤後更新,當日生效)
        hit_stop = low <= cs
        hit_target = high >= target_price
        # 同日兩邊都觸 → 保守視為先觸停損
        if hit_stop:
            return {
                "status": "lose",
                "return_pct": (cs - entry_price) / entry_price,
                "exit_date": d,
                "exit_price": cs,
                "current_stop": cs,
                "trailing_level": tl,
            }
        if hit_target:
            return {
                "status": "win",
                "return_pct": (target_price - entry_price) / entry_price,
                "exit_date": d,
                "exit_price": target_price,
                "current_stop": cs,
                "trailing_level": tl,
            }

        # 2. 今日沒出場 → 用今日 close 更新 trailing(只升不降),供明日生效
        cs, tl = _update_trailing_stop(entry_price, cs, tl, close)

    # hold_days 結束都沒觸 → timeout
    if last_close <= 0:
        return {
            "status": "timeout_lose", "return_pct": 0.0,
            "exit_date": last_seen_date, "exit_price": last_close,
            "current_stop": cs, "trailing_level": tl,
        }
    final_return = (last_close - entry_price) / entry_price
    status = "timeout_win" if final_return > 0 else "timeout_lose"
    return {
        "status": status,
        "return_pct": final_return,
        "exit_date": last_seen_date,
        "exit_price": last_close,
        "current_stop": cs,
        "trailing_level": tl,
    }


def evaluate_active_trades(db_path: str | Path | None = None) -> int:
    """掃 status='active' 的全部紀錄,可結算的更新成 win/lose/timeout_*。

    回更新筆數(資料不足 / 仍進行中的不算)。出場時同步寫 current_stop /
    trailing_level 反映出場當下的 trailing snapshot。
    """
    with db.get_conn(db_path) as conn:
        actives = conn.execute(
            "SELECT id, sid, entry_date, entry_price, "
            "target_price, stop_price, hold_days "
            "FROM paper_trades WHERE status='active'"
        ).fetchall()

    if not actives:
        return 0

    n_updated = 0
    now = _now_iso()
    with db.get_conn(db_path) as conn:
        for row in actives:
            outcome = _evaluate_one(
                row["sid"], row["entry_date"], float(row["entry_price"]),
                float(row["target_price"]), float(row["stop_price"]),
                int(row["hold_days"]), db_path=db_path,
            )
            if outcome is None:
                continue  # 資料不足,active 不變
            conn.execute(
                "UPDATE paper_trades SET "
                "status=?, return_pct=?, "
                "actual_exit_date=?, actual_exit_price=?, "
                "current_stop=?, trailing_level=?, updated_at=? "
                "WHERE id=?",
                (outcome["status"], outcome["return_pct"],
                 outcome["exit_date"], outcome["exit_price"],
                 outcome["current_stop"], outcome["trailing_level"], now,
                 row["id"]),
            )
            n_updated += 1
    return n_updated


def _row_to_dict(row) -> dict:
    """sqlite3.Row → dict + parse matched_strategies JSON。"""
    d = dict(row)
    raw = d.get("matched_strategies")
    if raw:
        try:
            d["matched_strategies"] = json.loads(raw)
        except (TypeError, ValueError):
            d["matched_strategies"] = []
    else:
        d["matched_strategies"] = []
    return d


def list_active_trades(
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    """status='active' 全部 — render Section 2 用。最舊優先(先進場先出)。"""
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status='active' "
            "ORDER BY entry_date ASC, id ASC"
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([_row_to_dict(r) for r in rows])


def list_settled_trades(
    since: str | None = None,
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    """status != 'active' 全部。since='YYYY-MM-DD' → 只回該日後進場的。"""
    sql = (
        "SELECT * FROM paper_trades WHERE status != 'active' "
    )
    args: list = []
    if since:
        sql += "AND entry_date >= ? "
        args.append(since)
    sql += "ORDER BY entry_date DESC, id DESC"
    with db.get_conn(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([_row_to_dict(r) for r in rows])


def _is_win(status: str) -> bool:
    return status in ("win", "timeout_win")


def compute_stats(settled_df: pd.DataFrame) -> dict:
    """從已結算 DataFrame 算統計:WR / avg_return / max_loss_streak / by_strategy。

    by_strategy:用 matched_strategies JSON 把同 pick 算到所有命中策略上(同
    pick 命中 N 個策略 → N 個策略各自 +1)。
    """
    if settled_df is None or settled_df.empty:
        return {
            "n_settled": 0,
            "n_wins": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "max_loss_streak": 0,
            "by_strategy": {},
        }

    n = len(settled_df)
    wins = sum(1 for s in settled_df["status"] if _is_win(s))
    avg_ret = float(settled_df["return_pct"].fillna(0).mean())

    # max_loss_streak:依 entry_date 升冪走一遍,最長連續非 win
    sorted_df = settled_df.sort_values("entry_date", ascending=True)
    cur_streak = 0
    max_streak = 0
    for s in sorted_df["status"]:
        if _is_win(s):
            cur_streak = 0
        else:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)

    # by_strategy:每 pick 拆到 matched_strategies 內每個 strategy
    by_strategy: dict[str, dict] = {}
    for _, row in settled_df.iterrows():
        matched = row.get("matched_strategies") or []
        if isinstance(matched, str):
            try:
                matched = json.loads(matched)
            except (TypeError, ValueError):
                matched = []
        is_w = _is_win(row.get("status", ""))
        ret = float(row.get("return_pct") or 0.0)
        for s in matched:
            stat = by_strategy.setdefault(s, {
                "n": 0, "wins": 0, "total_return": 0.0,
            })
            stat["n"] += 1
            stat["total_return"] += ret
            if is_w:
                stat["wins"] += 1

    for s, stat in by_strategy.items():
        stat["win_rate"] = stat["wins"] / stat["n"] if stat["n"] else 0.0
        stat["avg_return"] = (
            stat["total_return"] / stat["n"] if stat["n"] else 0.0
        )

    return {
        "n_settled": n,
        "n_wins": wins,
        "win_rate": wins / n if n else 0.0,
        "avg_return": avg_ret,
        "max_loss_streak": max_streak,
        "by_strategy": by_strategy,
    }


__all__ = [
    "add_paper_trade",
    "bulk_add_paper_trades",
    "already_tracked",
    "evaluate_active_trades",
    "list_active_trades",
    "list_settled_trades",
    "compute_stats",
]
