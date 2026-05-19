"""Watchlist priority — 推播時把主公 watchlist 命中的 picks 排最前 + 加 ⭐ 標。

只動排序 + render flag,不影響 picks 本身的計算 / 評分邏輯。
Watchlist 為空時行為等同舊版(保留原排序,僅多注一個 is_watchlist=False flag)。

設計重點:
- pick dict 的股號可能在 'sid' 或 'stock_id' 兩種 key(短線 / premium / movers 都是
  'sid';短線 DataFrame 版用 'stock_id'),helper 同時支援。
- watchlist 載入失敗(DB 還沒 init / 例外)→ 回空 set,推播照常進行不擋。
- sort key 第二維用既有 rank,保留 watchlist 命中 / 未命中各自的內部順序。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _pick_sid(pick: dict) -> str:
    """從 pick dict 取股號,支援 sid / stock_id 兩種 key。空 / None → ''。"""
    sid = pick.get("sid") or pick.get("stock_id") or ""
    return str(sid).strip()


def load_watchlist(db_path: str | Path | None = None) -> set[str]:
    """載入 watchlist set(stock_id 集合),for sort key lookup。

    從 SQLite watchlist 表撈。失敗 → 回空 set 不擋推播(降級為「watchlist 為空」,
    舊版行為 = 排序不變)。
    """
    try:
        from src import database as db
        rows = db.get_watchlist(db_path=db_path)
        return {
            str(r.get("stock_id") or "").strip()
            for r in rows
            if r.get("stock_id")
        }
    except Exception:  # noqa: BLE001
        logger.exception(
            "[WATCHLIST_PRIORITY] load_watchlist 失敗,fallback 空 set"
        )
        return set()


def annotate_watchlist_picks(
    picks: list[dict], watchlist: set[str],
) -> list[dict]:
    """In-place 注入 pick['is_watchlist'] flag(bool)。回傳同 list 供 chain。

    用於 format_*_block 判斷要不要加 ⭐ 前綴。watchlist 為空 → 全 False。
    """
    if not picks:
        return picks
    for p in picks:
        p["is_watchlist"] = _pick_sid(p) in watchlist
    return picks


def watchlist_sort_key(
    pick: dict, watchlist: set[str],
) -> tuple[int, int]:
    """排序 key — (0 命中 / 1 不命中, 既有 rank 升冪)。

    第一維把 watchlist 命中的壓到最前;第二維用 pick['rank'](由 caller 上游
    score-sort 後寫入的 1..N)保留原順序。沒 rank → 視為大數壓底。

    用既有 rank 而非 -score,原因:rank 已經是 caller 排好的最終結果,
    再用 score 容易抓錯欄位(短線用 ml_prob × weight × theme,
    premium 用 ml_prob,movers 用 holders_delta_w 不同維度)。
    """
    in_wl = _pick_sid(pick) in watchlist
    rank = pick.get("rank")
    try:
        rank_val = int(rank) if rank is not None else 10_000
    except (TypeError, ValueError):
        rank_val = 10_000
    return (0 if in_wl else 1, rank_val)


def prioritize_watchlist(
    picks: list[dict],
    db_path: str | Path | None = None,
) -> list[dict]:
    """便利 helper:load watchlist → annotate → sort → 回新 list。

    Args:
        picks: 已由 caller 排好順序的 picks(每筆建議帶 rank,沒 rank 也能跑)。
        db_path: 測試用 override(走 src.database.get_watchlist 同套介面)。

    Returns:
        重排後的新 list(命中 watchlist 排最前,其餘維持既有順序)。
        每筆 pick in-place 帶 'is_watchlist' bool flag。
        rank 由 caller 視需要重新分配(本 helper 不動 rank)。

    watchlist 為空 → 排序不變,僅多注 is_watchlist=False flag(避免 regression)。
    """
    if not picks:
        return picks
    watchlist = load_watchlist(db_path=db_path)
    annotate_watchlist_picks(picks, watchlist)
    if not watchlist:
        return picks
    return sorted(picks, key=lambda p: watchlist_sort_key(p, watchlist))


def any_watchlist_hit(*pick_groups: list[dict] | None) -> bool:
    """檢查多組 picks 是否至少一筆 is_watchlist=True(用於 caption 顯不顯示)。"""
    for group in pick_groups:
        if not group:
            continue
        for p in group:
            if p.get("is_watchlist"):
                return True
    return False
