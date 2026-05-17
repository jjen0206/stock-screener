"""動態停損(Trailing Stop)模組 — B 進場時機強化.

設計原則:
- ATR-based:trailing stop = high_water_mark − ATR × multiplier。
- 永遠 only-up(long)/ only-down(short),不會「鬆動」原停損。
- 開倉首日 high_water_mark = entry_price;之後逐日比較 current_price 更新。
- ATR 從 daily_prices 撈(複用 risk_management._compute_atr_value),失敗 → 不更新。

Kill-switch:env `TRAILING_STOP_ENABLED`(預設 true)。off → 所有 API graceful skip。

公開 API:
- `is_enabled() -> bool`
- `compute_trailing_stop(entry_price, current_price, atr, multiplier=2.0,
                          high_water_mark=None, current_stop=None,
                          side='long') -> dict`
- `update_position_trailing_stop(position_id, db_path=None) -> dict | None`
- `batch_update_trailing_stops(db_path=None) -> dict`  全部 open positions 跑一輪

回傳格式範例(compute_trailing_stop):
    {
        "entry_price": 600.0,
        "current_price": 660.0,
        "high_water_mark": 660.0,  # 更新後
        "atr": 6.0,
        "multiplier": 2.0,
        "new_stop": 648.0,
        "raised": True,
        "rationale": "HWM 600→660,trailing = 660 - 2.0×6.0 = 648 (原 588 → 648)"
    }
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from src import database as db

logger = logging.getLogger(__name__)

_DEFAULT_ATR_DAYS = 14
_DEFAULT_MULTIPLIER = 2.0


def is_enabled() -> bool:
    """讀 env `TRAILING_STOP_ENABLED`(預設 true)。"""
    raw = os.getenv("TRAILING_STOP_ENABLED", "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def compute_trailing_stop(
    entry_price: float,
    current_price: float,
    atr: float,
    multiplier: float = _DEFAULT_MULTIPLIER,
    high_water_mark: float | None = None,
    current_stop: float | None = None,
    side: str = "long",
) -> dict:
    """純算式 trailing stop(無 DB I/O,方便測試)。

    規則(long):
    - new_hwm = max(high_water_mark or entry, current_price)
    - candidate = new_hwm − ATR × multiplier
    - 觸發條件:current_price 漲過 entry × (1 + ATR/entry)(= 至少一 ATR)
      才上移,否則 trailing stop 不上移(但 high_water_mark 仍會更新)。
    - new_stop = max(current_stop or 0, candidate) — only-up。
    - 若 current_stop is None 且 current_price 還沒漲超 entry+ATR
      → new_stop = candidate(直接生效當初始 trailing)。

    short 反向(對稱)。

    回傳 dict,'raised' 表是否真的把 stop 往上移。
    任何 atr <= 0 / entry_price <= 0 → raise ValueError。
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price 必須 > 0,got {entry_price}")
    if atr <= 0:
        raise ValueError(f"atr 必須 > 0,got {atr}")
    if multiplier <= 0:
        raise ValueError(f"multiplier 必須 > 0,got {multiplier}")
    side_l = (side or "long").lower()
    if side_l not in ("long", "short"):
        raise ValueError(f"side 必須 long/short,got {side!r}")

    entry = float(entry_price)
    cp = float(current_price)
    mult = float(multiplier)
    atr_v = float(atr)

    if side_l == "long":
        prev_hwm = float(high_water_mark) if high_water_mark else entry
        new_hwm = max(prev_hwm, cp)
        candidate = new_hwm - atr_v * mult
        # 觸發門檻:current_price 至少漲 1 ATR(避免進場馬上把停損拉上)
        # 不滿足 → 候選仍計算但只在 candidate > current_stop 時生效。
        threshold = entry + atr_v * 1.0
        triggered = cp >= threshold
        old_stop = float(current_stop) if current_stop else 0.0
        # only-up
        if triggered:
            new_stop = max(old_stop, candidate)
        else:
            new_stop = max(old_stop, candidate) if old_stop > 0 else candidate
        # safety:停損不可 >= current_price(否則一推就觸發)
        if new_stop >= cp:
            new_stop = max(old_stop, cp - atr_v * mult * 0.5)
        raised = new_stop > old_stop + 1e-9
        rationale = (
            f"HWM {prev_hwm:.2f}→{new_hwm:.2f},trailing = "
            f"{new_hwm:.2f} - {mult:.1f}×{atr_v:.2f} = {candidate:.2f}"
            f"(原 {old_stop:.2f} → {new_stop:.2f})"
        )
    else:  # short
        prev_hwm = float(high_water_mark) if high_water_mark else entry
        new_hwm = min(prev_hwm, cp)
        candidate = new_hwm + atr_v * mult
        threshold = entry - atr_v * 1.0
        triggered = cp <= threshold
        old_stop = float(current_stop) if current_stop else float("inf")
        if triggered:
            new_stop = min(old_stop, candidate) if old_stop != float("inf") else candidate
        else:
            new_stop = min(old_stop, candidate) if old_stop != float("inf") else candidate
        if new_stop <= cp:
            new_stop = min(old_stop, cp + atr_v * mult * 0.5) if old_stop != float("inf") else cp + atr_v * mult * 0.5
        raised = (old_stop == float("inf")) or (new_stop < old_stop - 1e-9)
        old_stop_disp = old_stop if old_stop != float("inf") else None
        rationale = (
            f"HWM {prev_hwm:.2f}→{new_hwm:.2f},trailing = "
            f"{new_hwm:.2f} + {mult:.1f}×{atr_v:.2f} = {candidate:.2f}"
            f"(原 {old_stop_disp} → {new_stop:.2f})"
        )

    return {
        "entry_price": entry,
        "current_price": cp,
        "high_water_mark": float(new_hwm),
        "atr": atr_v,
        "multiplier": mult,
        "new_stop": float(new_stop),
        "raised": bool(raised),
        "side": side_l,
        "rationale": rationale,
    }


def _fetch_current_price(
    conn, sid: str,
) -> float | None:
    row = conn.execute(
        "SELECT close FROM daily_prices WHERE stock_id=? "
        "ORDER BY date DESC LIMIT 1",
        (str(sid).strip(),),
    ).fetchone()
    if row is None or row["close"] is None:
        return None
    return float(row["close"])


def update_position_trailing_stop(
    position_id: int,
    db_path: str | Path | None = None,
    atr_days: int = _DEFAULT_ATR_DAYS,
    multiplier: float = _DEFAULT_MULTIPLIER,
) -> dict | None:
    """單筆 user_positions 算 trailing stop + UPSERT 回 DB。

    Flow:
      1. 撈 user_positions row(open 才更新)。
      2. 撈最新 daily_prices.close → current_price。
      3. 算 ATR(複用 risk_management._compute_atr_value)。
      4. compute_trailing_stop → 算新 stop / hwm。
      5. UPDATE user_positions SET trailing_stop=?, high_water_mark=?。
         同時若 raised → 把 stop_loss 也更新(現在這支才是「實際停損」)。
    回 dict(compute_trailing_stop 結果 + 'position_id' + 'updated': bool)。
    open 已平倉 / current_price/ATR 失敗 → 回 None。
    """
    if not is_enabled():
        return None
    from src.risk_management import _compute_atr_value
    db.init_db(db_path)
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM user_positions WHERE id=? AND is_open=1",
            (int(position_id),),
        ).fetchone()
        if row is None:
            return None
        rec = dict(row)
        sid = rec["stock_id"]
        cp = _fetch_current_price(conn, sid)
        if cp is None:
            return None
    atr_val, _n = _compute_atr_value(sid, period=atr_days, db_path=db_path)
    if atr_val is None:
        return None
    try:
        result = compute_trailing_stop(
            entry_price=float(rec["entry_price"]),
            current_price=cp,
            atr=atr_val,
            multiplier=multiplier,
            high_water_mark=rec.get("high_water_mark"),
            current_stop=rec.get("trailing_stop") or rec.get("stop_loss"),
            side=rec.get("side") or "long",
        )
    except ValueError:
        logger.exception("[TRAILING] compute failed pid=%s", position_id)
        return None

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.get_conn(db_path) as conn:
        # 永遠更新 high_water_mark + trailing_stop。
        # stop_loss 只在 raised=True 時更新(避免把 user 手動較緊的停損鬆掉)。
        if result["raised"]:
            conn.execute(
                "UPDATE user_positions SET "
                "high_water_mark=?, trailing_stop=?, stop_loss=?, updated_at=? "
                "WHERE id=?",
                (
                    result["high_water_mark"], result["new_stop"],
                    result["new_stop"], now, int(position_id),
                ),
            )
        else:
            conn.execute(
                "UPDATE user_positions SET "
                "high_water_mark=?, trailing_stop=?, updated_at=? "
                "WHERE id=?",
                (
                    result["high_water_mark"], result["new_stop"],
                    now, int(position_id),
                ),
            )
    return {**result, "position_id": int(position_id), "updated": True}


def batch_update_trailing_stops(
    db_path: str | Path | None = None,
    atr_days: int = _DEFAULT_ATR_DAYS,
    multiplier: float = _DEFAULT_MULTIPLIER,
) -> dict:
    """對所有 open user_positions 跑 update_position_trailing_stop。

    回傳統計 dict:
        {
            "checked": int,           # 嘗試的筆數
            "updated": int,            # 真的 raised 的筆數
            "skipped_no_data": int,    # 沒 current_price / ATR
            "errors": int,
            "raised_positions": [      # 每筆 raised 的細節(給 notifier 顯示)
                {position_id, sid, old_stop, new_stop, hwm}
            ],
        }

    Kill-switch off / 無 open positions → 全 0 + 空 list。
    """
    summary = {
        "checked": 0, "updated": 0, "skipped_no_data": 0, "errors": 0,
        "raised_positions": [],
    }
    if not is_enabled():
        return summary
    try:
        positions = db.get_open_positions(db_path=db_path)
    except Exception:  # noqa: BLE001
        logger.exception("[TRAILING] get_open_positions failed")
        return summary
    summary["checked"] = len(positions)
    for p in positions:
        pid = int(p["id"])
        try:
            res = update_position_trailing_stop(
                pid, db_path=db_path,
                atr_days=atr_days, multiplier=multiplier,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[TRAILING] update failed pid=%s", pid)
            summary["errors"] += 1
            continue
        if res is None:
            summary["skipped_no_data"] += 1
            continue
        if res.get("raised"):
            summary["updated"] += 1
            summary["raised_positions"].append({
                "position_id": pid,
                "sid": p["stock_id"],
                "entry_price": float(p["entry_price"]),
                "current_price": res["current_price"],
                "old_stop": float(p.get("trailing_stop") or p.get("stop_loss") or 0),
                "new_stop": res["new_stop"],
                "hwm": res["high_water_mark"],
            })
    return summary


__all__ = [
    "is_enabled",
    "compute_trailing_stop",
    "update_position_trailing_stop",
    "batch_update_trailing_stops",
]
