"""獲利了結 / 停損達標警報 — B 進場時機強化.

設計原則:
- 對 open user_positions 算當前 P&L %,看是否達 take_profit / stop_loss / trailing_stop。
- 部分了結建議:漲 +5% 建議賣 1/3、+10% 再賣 1/3、留 1/3 跑趨勢。
- 不下單,只回 alert list 讓 notifier / app 顯示。
- short 部位對稱(漲 = 對你不利)。

Kill-switch:env `TAKE_PROFIT_ALERT_ENABLED`(預設 true)。off → check 回空 list。

公開 API:
- `is_enabled() -> bool`
- `check_take_profit_hit(db_path=None) -> list[dict]`
- `partial_exit_suggestion(pnl_pct) -> Optional[dict]`

每個 alert dict:
    {
        "position_id": int,
        "sid": str,
        "entry_price": float,
        "current_price": float,
        "pnl_pct": float,           # 正 = 賺
        "kind": "take_profit" | "stop_loss" | "trailing_stop"
                | "partial_exit_5" | "partial_exit_10",
        "severity": "info" | "warn" | "danger",
        "message": str,              # 人類可讀
        "suggested_action": str,     # "賣 1/3" / "全平倉" 等
    }
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from src import database as db

logger = logging.getLogger(__name__)

# 部分了結門檻(主公規矩):
# +5% 賣 1/3,+10% 再賣 1/3,留 1/3 跑趨勢。
_PARTIAL_EXIT_TIER_1_PCT = 5.0
_PARTIAL_EXIT_TIER_2_PCT = 10.0


def is_enabled() -> bool:
    """讀 env `TAKE_PROFIT_ALERT_ENABLED`(預設 true)。"""
    raw = os.getenv("TAKE_PROFIT_ALERT_ENABLED", "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def partial_exit_suggestion(pnl_pct: float) -> Optional[dict]:
    """從 pnl_pct 算分批了結建議。回 None 表示未達門檻。

    +5% ≤ pnl < +10%  → kind=partial_exit_5  「建議賣 1/3 鎖利」
    +10% ≤ pnl        → kind=partial_exit_10 「建議再賣 1/3,留 1/3 跑趨勢」
    """
    try:
        p = float(pnl_pct)
    except (TypeError, ValueError):
        return None
    if p < _PARTIAL_EXIT_TIER_1_PCT:
        return None
    if p >= _PARTIAL_EXIT_TIER_2_PCT:
        return {
            "kind": "partial_exit_10",
            "tier": 2,
            "threshold_pct": _PARTIAL_EXIT_TIER_2_PCT,
            "suggested_action": "再賣 1/3,留 1/3 跑趨勢",
            "message": (
                f"獲利 {p:+.1f}%,建議再賣 1/3(累計賣 2/3)"
                "，留 1/3 跑趨勢 + 拉 trailing stop"
            ),
        }
    return {
        "kind": "partial_exit_5",
        "tier": 1,
        "threshold_pct": _PARTIAL_EXIT_TIER_1_PCT,
        "suggested_action": "賣 1/3 鎖利",
        "message": (
            f"獲利 {p:+.1f}%,建議賣 1/3 鎖利(剩 2/3 等 +10% 再分批)"
        ),
    }


def _compute_pnl_pct(
    entry: float, current: float, side: str = "long",
) -> float:
    """算 pnl 百分比(正 = 對你有利)。entry/current <= 0 → 0。"""
    if entry <= 0 or current <= 0:
        return 0.0
    if side == "short":
        return (entry - current) / entry * 100.0
    return (current - entry) / entry * 100.0


def check_take_profit_hit(
    db_path: str | Path | None = None,
) -> list[dict]:
    """掃所有 open user_positions,回達標 alert list。

    Order(severity 高的先):
      1. stop_loss 達標 → severity=danger
      2. take_profit 達標 → severity=info
      3. trailing_stop 達標(若 trailing_stop > stop_loss)→ severity=warn
      4. partial_exit_10 → severity=warn
      5. partial_exit_5 → severity=info

    同一個 position 可同時觸發多個(分開 alert,讓 UI / notifier 分行顯示)。
    無達標 / 無持倉 / kill-switch off → []。
    """
    if not is_enabled():
        return []
    try:
        positions = db.get_open_positions(db_path=db_path)
    except Exception:  # noqa: BLE001
        logger.exception("[TP_ALERT] get_open_positions failed")
        return []
    if not positions:
        return []

    # 撈 current price (batch)
    sids = [p["stock_id"] for p in positions]
    px_map: dict[str, float] = {}
    try:
        with db.get_conn(db_path) as conn:
            placeholders = ",".join(["?"] * len(sids))
            rows = conn.execute(
                "SELECT stock_id, close FROM daily_prices WHERE stock_id IN "
                f"({placeholders}) AND date = ("
                "  SELECT MAX(date) FROM daily_prices dp2 "
                "  WHERE dp2.stock_id = daily_prices.stock_id"
                ")",
                sids,
            ).fetchall()
            for r in rows:
                if r["close"] is not None:
                    px_map[r["stock_id"]] = float(r["close"])
    except Exception:  # noqa: BLE001
        logger.exception("[TP_ALERT] fetch current price failed")
        return []

    alerts: list[dict] = []
    for p in positions:
        sid = p["stock_id"]
        cp = px_map.get(sid)
        if cp is None or cp <= 0:
            continue
        try:
            entry = float(p["entry_price"])
        except (TypeError, ValueError):
            continue
        side = (p.get("side") or "long").lower()
        pnl_pct = _compute_pnl_pct(entry, cp, side=side)
        sl = p.get("stop_loss")
        tp = p.get("take_profit")
        trail = p.get("trailing_stop")
        pid = int(p["id"])

        base = {
            "position_id": pid, "sid": sid,
            "entry_price": entry, "current_price": cp,
            "pnl_pct": pnl_pct, "side": side,
        }

        # 1. stop loss (long: cp <= sl;short: cp >= sl)
        if sl is not None:
            try:
                slf = float(sl)
                if slf > 0:
                    hit = cp <= slf if side == "long" else cp >= slf
                    if hit:
                        alerts.append({
                            **base,
                            "kind": "stop_loss",
                            "severity": "danger",
                            "message": (
                                f"🚨 {sid} 達停損({cp:.2f} {'≤' if side == 'long' else '≥'} "
                                f"{slf:.2f}),pnl {pnl_pct:+.1f}%"
                            ),
                            "suggested_action": "全平倉 — 停損紀律",
                        })
            except (TypeError, ValueError):
                pass

        # 2. take_profit
        if tp is not None:
            try:
                tpf = float(tp)
                if tpf > 0:
                    hit = cp >= tpf if side == "long" else cp <= tpf
                    if hit:
                        alerts.append({
                            **base,
                            "kind": "take_profit",
                            "severity": "info",
                            "message": (
                                f"🎯 {sid} 達停利({cp:.2f} {'≥' if side == 'long' else '≤'} "
                                f"{tpf:.2f}),pnl {pnl_pct:+.1f}%"
                            ),
                            "suggested_action": "全平倉 / 或拉 trailing stop 跑趨勢",
                        })
            except (TypeError, ValueError):
                pass

        # 3. trailing_stop(只在 trailing > stop_loss 時當獨立警報,
        #    否則跟 stop_loss 重疊)
        if trail is not None:
            try:
                trf = float(trail)
                if trf > 0:
                    overlap_sl = sl is not None and abs(float(sl) - trf) < 1e-6
                    if not overlap_sl:
                        hit = cp <= trf if side == "long" else cp >= trf
                        if hit:
                            alerts.append({
                                **base,
                                "kind": "trailing_stop",
                                "severity": "warn",
                                "message": (
                                    f"⚠️ {sid} 達動態停損({cp:.2f} "
                                    f"{'≤' if side == 'long' else '≥'} {trf:.2f}),"
                                    f"pnl {pnl_pct:+.1f}%"
                                ),
                                "suggested_action": "鎖利平倉 — trailing 觸發",
                            })
            except (TypeError, ValueError):
                pass

        # 4. partial exit 建議(只在賺錢時觸發)
        if pnl_pct >= _PARTIAL_EXIT_TIER_1_PCT:
            adv = partial_exit_suggestion(pnl_pct)
            if adv:
                sev = "warn" if adv["tier"] == 2 else "info"
                alerts.append({
                    **base,
                    "kind": adv["kind"],
                    "severity": sev,
                    "message": f"💰 {sid} {adv['message']}",
                    "suggested_action": adv["suggested_action"],
                    "tier": adv["tier"],
                })

    # severity 順序:danger > warn > info,內部按 pnl_pct desc
    sev_rank = {"danger": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda a: (sev_rank.get(a["severity"], 9), -a["pnl_pct"]))
    return alerts


__all__ = [
    "is_enabled",
    "check_take_profit_hit",
    "partial_exit_suggestion",
]
