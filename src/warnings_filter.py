"""警示股 picks 過濾模組(2026-05-15 主公拍板加入,違約交割教訓 root cause)。

設計分兩層:
  1. **硬擋**(default): default_settlement(違約交割)+ full_cash(全額交割)
       — 這兩類「真會卡停損」的警示直接從 picks 剔除,不給跑分機會
  2. **soft 降權**: attention(注意)/ disposition(處置)/ method_changed
       (變更交易方法但非全額)— 留在 picks 但 _compute_pick_score 乘 0.7 讓
       排序自然往後沉,主公仍能看到該檔但不會優先推進去

對外介面:
  exclude_warned_stocks(conn, picks, warning_types=None, as_of=None)
      回 (kept, excluded) tuple,excluded 帶 reason 給 caption / log 用

  apply_soft_warning_penalty(conn, picks, soft_warning_types=None,
                             multiplier=0.7, as_of=None)
      in-place 把命中 soft warning 的 pick 的 ml_prob × multiplier(讓
      _compute_pick_score 算分數時自然降權)。回 list of penalized sids。

Kill-switch:
  WARNING_FILTER_ENABLED 環境變數 = 'false' → 整個 module 行為退化成 no-op
  (出事時主公能立刻關掉不用 redeploy)。預設 'true' = 啟用。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date as _date

logger = logging.getLogger(__name__)


# === 警示分類常數 ===

# 硬擋(picks 直接剔除)— 真的會卡停損的兩類:違約交割 + 全額交割
HARD_EXCLUDE_TYPES: tuple[str, ...] = ("default_settlement", "full_cash")

# soft 降權(留在 picks 但 score 乘 multiplier)— 注意 / 處置 / 變更方法
SOFT_PENALTY_TYPES: tuple[str, ...] = (
    "attention", "disposition", "method_changed",
)

# 中文 label(給 caption / UI 顯示用)
WARNING_TYPE_LABELS: dict[str, str] = {
    "default_settlement": "違約交割",
    "full_cash": "全額交割",
    "attention": "注意股",
    "disposition": "處置股",
    "method_changed": "變更交易方法",
}


def _is_enabled() -> bool:
    """讀 WARNING_FILTER_ENABLED env(預設 true)。

    主公出事 kill-switch:設成 'false' / '0' / 'no' 就讓整個 module 退化 no-op,
    避免推播鏈被警示資料源故障(TWSE 改 HTML 結構等)拖死。
    """
    val = os.environ.get("WARNING_FILTER_ENABLED", "true").strip().lower()
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

    回 {sid: [warning_dict, ...]} — 沒命中的 sid key 不會在 dict 裡。
    抽出 _query 函式讓 caller(exclude / apply_penalty)共用 query plan。
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
        # 支援 sqlite3.Row(欄位名 access)和 tuple(下標 access)
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


def exclude_warned_stocks(
    conn: sqlite3.Connection,
    picks: list[dict],
    warning_types: list[str] | None = None,
    as_of: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """從 picks 把命中指定 warning_types 且仍生效的剔除。

    Args:
        conn: 已開啟的 sqlite3 connection(caller 自己管 with-block / commit)
        picks: List[dict],每筆需有 'sid' key(其他欄位原樣保留)
        warning_types: 要硬擋的警示類別。預設 HARD_EXCLUDE_TYPES
            (default_settlement + full_cash)
        as_of: 'YYYY-MM-DD' 判定生效用,預設 today

    Returns:
        (kept, excluded) — kept 是仍能進 picks 的;excluded 每筆額外多兩個 key:
            'warning_type'(主要命中類別,多 type 取第一筆)
            'warning_reason'(命中原因說明,給 log / caption 用)

    Kill-switch:
        WARNING_FILTER_ENABLED=false → 直接回 (picks, []),no-op pass-through。
    """
    if not picks:
        return [], []
    if not _is_enabled():
        logger.info("[WARNINGS_FILTER] WARNING_FILTER_ENABLED=false,跳過硬擋")
        return list(picks), []

    if warning_types is None:
        warning_types = list(HARD_EXCLUDE_TYPES)
    if as_of is None:
        as_of = _today_iso()

    sids = [str(p["sid"]) for p in picks if p.get("sid")]
    warned = _query_active_warnings(conn, sids, list(warning_types), as_of)

    kept: list[dict] = []
    excluded: list[dict] = []
    for p in picks:
        sid = str(p.get("sid", ""))
        hits = warned.get(sid)
        if not hits:
            kept.append(p)
            continue
        first = hits[0]
        ex_row = dict(p)  # shallow copy 避免污染原 dict
        ex_row["warning_type"] = first["warning_type"]
        ex_row["warning_reason"] = (
            first.get("reason") or WARNING_TYPE_LABELS.get(
                first["warning_type"], first["warning_type"],
            )
        )
        excluded.append(ex_row)
    return kept, excluded


def apply_soft_warning_penalty(
    conn: sqlite3.Connection,
    picks: list[dict],
    soft_warning_types: list[str] | None = None,
    multiplier: float = 0.7,
    as_of: str | None = None,
) -> list[str]:
    """對命中 soft warning 的 picks in-place 把 ml_prob 乘 multiplier。

    讓後續 _compute_pick_score 排序時自然往後沉(weighted_ml = ml_prob × avg_w
    所以乘 0.7 等同把 ml_prob 降 30%,排序自動往後)。

    Args:
        conn: sqlite3 connection
        picks: List[dict],需有 'sid' + 'ml_prob' key
        soft_warning_types: 預設 SOFT_PENALTY_TYPES
            (attention + disposition + method_changed)
        multiplier: ml_prob × multiplier。預設 0.7
        as_of: 'YYYY-MM-DD' 判定生效,預設 today

    Returns:
        List of sids 被降權(給 log / caption 用,不改 picks 順序)。

    Kill-switch:
        WARNING_FILTER_ENABLED=false → no-op,回 []。
    """
    if not picks:
        return []
    if not _is_enabled():
        return []

    if soft_warning_types is None:
        soft_warning_types = list(SOFT_PENALTY_TYPES)
    if as_of is None:
        as_of = _today_iso()

    sids = [str(p["sid"]) for p in picks if p.get("sid")]
    warned = _query_active_warnings(conn, sids, list(soft_warning_types), as_of)

    penalized: list[str] = []
    for p in picks:
        sid = str(p.get("sid", ""))
        if sid not in warned:
            continue
        ml = p.get("ml_prob")
        if ml is None:
            # 沒 ml_prob 就沒法乘,但仍標記 soft_warning 給 UI / caption 用
            p["soft_warning_types"] = [
                w["warning_type"] for w in warned[sid]
            ]
            penalized.append(sid)
            continue
        try:
            p["ml_prob"] = float(ml) * float(multiplier)
            p["soft_warning_penalty_applied"] = True
            p["soft_warning_types"] = [
                w["warning_type"] for w in warned[sid]
            ]
            penalized.append(sid)
        except (TypeError, ValueError):
            pass  # ml_prob 不是數值就跳過,不擋整批
    return penalized


def format_excluded_caption(excluded: list[dict]) -> str:
    """組「✅ 已濾掉 N 檔警示股 (違約X 全額Y)」caption,給推播訊息用。

    excluded 為 exclude_warned_stocks 的第二回傳值。
    None / empty → 回空字串(caller 自己判 falsy graceful skip)。
    """
    if not excluded:
        return ""
    n = len(excluded)
    counts: dict[str, int] = {}
    for ex in excluded:
        wt = ex.get("warning_type", "unknown")
        counts[wt] = counts.get(wt, 0) + 1
    parts = []
    # 按 HARD_EXCLUDE_TYPES 順序輸出,讓 caption 穩定
    for wt in HARD_EXCLUDE_TYPES:
        if wt in counts:
            label = WARNING_TYPE_LABELS.get(wt, wt)
            parts.append(f"{label}{counts[wt]}")
    # 其他不在 HARD_EXCLUDE_TYPES 的(理論上不會,但保險)
    for wt, c in counts.items():
        if wt not in HARD_EXCLUDE_TYPES:
            label = WARNING_TYPE_LABELS.get(wt, wt)
            parts.append(f"{label}{c}")
    suffix = f" ({' '.join(parts)})" if parts else ""
    return f"✅ 已濾掉 {n} 檔警示股{suffix}"


__all__ = [
    "HARD_EXCLUDE_TYPES",
    "SOFT_PENALTY_TYPES",
    "WARNING_TYPE_LABELS",
    "exclude_warned_stocks",
    "apply_soft_warning_penalty",
    "format_excluded_caption",
]
