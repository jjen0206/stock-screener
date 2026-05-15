"""警示股 picks **標註 + 軟降權** 模組(2026-05-15 主公拍板;同日 amendment:
拿掉 hard exclude,改成 annotate-only — 軍師不替主公做隱藏決定)。

設計原則:
  **主動提示風險,但不替主公做隱藏決定**。隱藏會讓主公失去判斷的機會 —
  違約股偶爾反彈很猛、有時主公有特殊資訊想接刀,系統沒資格替他擋。
  改用「強弱軟降權 + UI 顯眼 badge」讓主公自己看到自己決定。

兩層處理(全 soft,不 hard exclude):
  1. **annotate**: 全部命中 active warning 的 pick 都加上 `warnings` 欄位
       供 UI badge / 推播 caption 顯示
  2. **soft 降權(分嚴重等級)**:
       - SEVERE: default_settlement / full_cash → ml_prob × 0.3(嚴重的自動沉到底,
         但仍出現在推薦中讓主公看到)
       - SOFT  : attention / disposition / method_changed → ml_prob × 0.7

對外介面:
  annotate_warned_stocks(conn, picks, as_of=None)
      in-place 把 active warnings 注入每筆 pick 的 'warnings' 欄位。
      回 list of annotated sids(僅給 log 用,不改 picks 順序也不過濾)。

  apply_soft_warning_penalty(conn, picks, as_of=None,
                             severe_multiplier=0.3, soft_multiplier=0.7)
      in-place 把命中 SEVERE 的 ml_prob × 0.3,SOFT 的 × 0.7。
      回 list of penalized sids。

  format_warning_caption(annotated_picks)
      組「⚠️ 推薦中含 N 檔警示股 (違約X 全額Y 注意Z ...)」caption。

Kill-switch:
  WARNING_ANNOTATE_ENABLED=false → annotate + penalty 全跳過(主公出事 escape hatch)
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date as _date

logger = logging.getLogger(__name__)


# === 警示分類常數 ===

# SEVERE — 嚴重(違約 / 全額交割)→ soft 降權 ×0.3 自動沉到推薦末段,
# 但仍會出現在 picks 中讓主公自行判斷(2026-05-15 amendment:不替主公隱藏)
SEVERE_PENALTY_TYPES: tuple[str, ...] = ("default_settlement", "full_cash")

# SOFT — 一般(注意 / 處置 / 變更交易方法)→ soft 降權 ×0.7
SOFT_PENALTY_TYPES: tuple[str, ...] = (
    "attention", "disposition", "method_changed",
)

# 全部會被 annotate / penalty 的類別(union,給 _query_active_warnings 用)
ALL_WARNING_TYPES: tuple[str, ...] = SEVERE_PENALTY_TYPES + SOFT_PENALTY_TYPES

# 中文 label(給 caption / UI 顯示用)
WARNING_TYPE_LABELS: dict[str, str] = {
    "default_settlement": "違約交割",
    "full_cash": "全額交割",
    "attention": "注意股",
    "disposition": "處置股",
    "method_changed": "變更交易方法",
}


def _is_enabled() -> bool:
    """讀 WARNING_ANNOTATE_ENABLED env(預設 true)。

    主公出事 kill-switch:設成 'false' / '0' / 'no' 就讓整個 module 退化 no-op,
    避免推播鏈被警示資料源故障(TWSE 改 HTML 結構等)拖死。
    """
    val = os.environ.get("WARNING_ANNOTATE_ENABLED", "true").strip().lower()
    return val not in ("false", "0", "no", "off", "")


def _today_iso() -> str:
    return _date.today().isoformat()


def _query_active_warnings(
    conn: sqlite3.Connection,
    sids: list[str],
    warning_types: list[str],
    as_of: str,
) -> dict[str, list[dict]]:
    """純 SQL 撈該批 sids 在 as_of 仍生效的警示(指定類別)。

    生效 = effective_to IS NULL OR effective_to >= as_of。

    回 {sid: [warning_dict, ...]}(沒命中的 sid key 不在 dict 裡)。
    """
    if not sids or not warning_types:
        return {}
    sid_ph = ",".join("?" * len(sids))
    wt_ph = ",".join("?" * len(warning_types))
    sql = (
        "SELECT stock_id, warning_type, announced_date, "
        "       effective_to, reason "
        "FROM stock_warnings "
        f"WHERE stock_id IN ({sid_ph}) "
        f"AND warning_type IN ({wt_ph}) "
        "AND (effective_to IS NULL OR effective_to >= ?)"
    )
    params = list(sids) + list(warning_types) + [as_of]
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # 表不存在(舊 DB 還沒跑過 init_db / 還沒抓警示)→ 沒警示可比對
        logger.warning("[WARNINGS_FILTER] query 失敗(表可能不存在): %s", e)
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        try:
            sid = r["stock_id"]
            wt = r["warning_type"]
            ad = r["announced_date"]
            et = r["effective_to"]
            reason = r["reason"]
        except (TypeError, IndexError):
            sid, wt, ad, et, reason = r
        out.setdefault(sid, []).append({
            "warning_type": wt,
            "announced_date": ad,
            "effective_to": et,
            "reason": reason,
        })
    return out


def annotate_warned_stocks(
    conn: sqlite3.Connection,
    picks: list[dict],
    as_of: str | None = None,
) -> list[str]:
    """In-place 把 active warnings 注入每筆 pick 的 'warnings' 欄位。

    **不過濾、不改 picks 順序**(2026-05-15 amendment:不替主公隱藏決定)。
    純標註,讓 UI badge / 推播 caption / soft penalty 後續 step 用。

    Args:
        conn: 已開啟的 sqlite3 connection
        picks: List[dict],每筆需有 'sid' key
        as_of: 'YYYY-MM-DD' 判定生效用,預設 today

    Returns:
        被 annotate 的 sids list(僅給 log / monitoring 用)。
        picks 本身 in-place 被改:命中的多了 'warnings' 欄位,
        值為 [{warning_type, announced_date, effective_to, reason}, ...]。

    Kill-switch:
        WARNING_ANNOTATE_ENABLED=false → 直接回 [],不修改 picks。
    """
    if not picks:
        return []
    if not _is_enabled():
        logger.info("[WARNINGS_FILTER] WARNING_ANNOTATE_ENABLED=false,跳過 annotate")
        return []

    if as_of is None:
        as_of = _today_iso()

    sids = [str(p["sid"]) for p in picks if p.get("sid")]
    warned = _query_active_warnings(
        conn, sids, list(ALL_WARNING_TYPES), as_of,
    )

    annotated: list[str] = []
    for p in picks:
        sid = str(p.get("sid", ""))
        hits = warned.get(sid)
        if not hits:
            continue
        p["warnings"] = hits  # in-place inject
        annotated.append(sid)
    return annotated


def apply_soft_warning_penalty(
    conn: sqlite3.Connection,
    picks: list[dict],
    as_of: str | None = None,
    severe_multiplier: float = 0.3,
    soft_multiplier: float = 0.7,
) -> list[str]:
    """對命中 warning 的 picks in-place 把 ml_prob × 對應 multiplier。

    嚴重等級分流(2026-05-15 amendment:取代原 hard exclude):
      - SEVERE_PENALTY_TYPES (default_settlement / full_cash) → ×0.3
        嚴重風險自動沉到推薦末段,但仍顯示讓主公自己看
      - SOFT_PENALTY_TYPES (attention / disposition / method_changed) → ×0.7

    同一 pick 命中多類別 → 取**最低 multiplier**(SEVERE > SOFT 嚴重時用 SEVERE),
    避免 SEVERE 被 SOFT 「稀釋」。

    Args:
        conn: sqlite3 connection
        picks: List[dict],需有 'sid' + 'ml_prob' key
        as_of: 'YYYY-MM-DD' 判定生效,預設 today
        severe_multiplier: 嚴重等級 ml_prob 倍率,預設 0.3
        soft_multiplier: 一般等級 ml_prob 倍率,預設 0.7

    Returns:
        List of (penalized) sids 給 log / caption 用。

    Kill-switch:
        WARNING_ANNOTATE_ENABLED=false → no-op,回 []。
    """
    if not picks:
        return []
    if not _is_enabled():
        return []

    if as_of is None:
        as_of = _today_iso()

    sids = [str(p["sid"]) for p in picks if p.get("sid")]
    warned = _query_active_warnings(
        conn, sids, list(ALL_WARNING_TYPES), as_of,
    )

    penalized: list[str] = []
    for p in picks:
        sid = str(p.get("sid", ""))
        hits = warned.get(sid)
        if not hits:
            continue
        # 取最嚴重 multiplier(SEVERE 命中即用 0.3,否則 SOFT 用 0.7)
        wt_set = {w["warning_type"] for w in hits}
        if wt_set & set(SEVERE_PENALTY_TYPES):
            mult = float(severe_multiplier)
            tier = "severe"
        elif wt_set & set(SOFT_PENALTY_TYPES):
            mult = float(soft_multiplier)
            tier = "soft"
        else:
            continue  # 不該走到(_query 已限定 ALL_WARNING_TYPES)

        ml = p.get("ml_prob")
        p["warning_penalty_tier"] = tier
        p["warning_penalty_multiplier"] = mult
        p["warning_types"] = sorted(wt_set)
        if ml is None:
            penalized.append(sid)
            continue
        try:
            p["ml_prob"] = float(ml) * mult
            penalized.append(sid)
        except (TypeError, ValueError):
            pass  # ml_prob 不是數值就跳過,不擋整批
    return penalized


def format_warning_caption(annotated_picks: list[dict]) -> str:
    """組「⚠️ 推薦中含 N 檔警示股 (違約X 全額Y 注意Z ...)」caption。

    用於推播訊息頂部 / Streamlit 頁面 banner,提示主公推薦中有警示但**沒被擋**,
    自己看到自己決定。

    Args:
        annotated_picks: List of pick dict,需含 'warnings' 欄位
            (annotate_warned_stocks 注入)。沒 warnings 的 pick 不算數。

    Returns:
        caption string(empty 時 caller 自行 graceful skip 不顯)。
    """
    if not annotated_picks:
        return ""
    # 統計每類別的 sid 數(同 sid 多類別計入各類)
    counts: dict[str, set[str]] = {}
    for p in annotated_picks:
        sid = str(p.get("sid", ""))
        ws = p.get("warnings") or []
        if not ws:
            continue
        for w in ws:
            wt = w.get("warning_type", "unknown")
            counts.setdefault(wt, set()).add(sid)
    if not counts:
        return ""
    # n = 含警示的 unique sid 數(同 sid 多類別只算一檔)
    all_warned_sids: set[str] = set()
    for s in counts.values():
        all_warned_sids |= s
    n = len(all_warned_sids)
    # 排序:SEVERE 優先,次按 SOFT 順序;餘類別末段
    parts: list[str] = []
    ordered_types = list(SEVERE_PENALTY_TYPES) + list(SOFT_PENALTY_TYPES)
    for wt in ordered_types:
        if wt in counts:
            label = WARNING_TYPE_LABELS.get(wt, wt)
            parts.append(f"{label}{len(counts[wt])}")
    for wt, sids in counts.items():
        if wt not in ordered_types:
            label = WARNING_TYPE_LABELS.get(wt, wt)
            parts.append(f"{label}{len(sids)}")
    suffix = f" ({' '.join(parts)})" if parts else ""
    return (
        f"⚠️ 推薦中含 {n} 檔警示股{suffix}"
        " — 風險已標註,進場與否主公自行判斷"
    )


__all__ = [
    "SEVERE_PENALTY_TYPES",
    "SOFT_PENALTY_TYPES",
    "ALL_WARNING_TYPES",
    "WARNING_TYPE_LABELS",
    "annotate_warned_stocks",
    "apply_soft_warning_penalty",
    "format_warning_caption",
]
