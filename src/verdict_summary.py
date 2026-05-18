"""每日 verdict banner 聚合器 — 純 data,不依賴 streamlit。

設計重點(2026-05-18 主公拍板):
  - 首頁 + 系統結論頁要顯示「今天系統覺得能不能進場」一行 banner,
    包含 🟢 可進場 / 🟡 觀望 / 🔴 不進場 三個 count + 各自 Top 名單。
  - 整批跑 compute_verdict 在雲端 cold load 太慢(~50 picks × ~200ms collect IO
    ≈ 10s),所以本模組 dump 一份 daily_verdict_summary.csv,
    cron 跑完 commit 到 repo,Streamlit Cloud preload 直接吃 CSV。
  - CSV 採 long format(section + rank):一檔 CSV 同時帶 counts + 3 個 top 名單,
    git diff 看得懂、人類可讀。

公開 API:
  - build_summary(picks_df, trade_date, *, sid_col='sid', name_col='name',
                  compute_verdict_fn=None) -> dict
  - dump_to_csv(summary, path) -> int      # 回 row count
  - load_from_csv(path) -> dict | None     # 找不到 / 過舊 → None

回傳 dict 結構:
    {
        'trade_date': '2026-05-18',
        'counts': {'green': 5, 'yellow': 12, 'red': 3},
        'top_green':  [{sid, name, verdict, verdict_color, score, main_reason}, ...],  # Top 3
        'top_yellow': [...],                                                            # Top 5
        'top_red':    [...],                                                            # Top 3
    }

注意:本模組不 import streamlit,可單獨拿來 cron 用、單元測試也不用 mock st.*。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

import pandas as pd


TOP_GREEN_N = 3
TOP_YELLOW_N = 5
TOP_RED_N = 3

_COLOR_TO_KEY = {"🟢": "green", "🟡": "yellow", "🔴": "red"}
_KEY_TO_COLOR = {v: k for k, v in _COLOR_TO_KEY.items()}


def _default_compute_verdict(sid: str) -> dict:
    """Lazy import — 給 test mock 用一個輕量替身,production 走真實 compute。"""
    from src.individual_stock_verdict import compute_verdict
    return compute_verdict(sid)


def _main_reason(v: dict) -> str:
    """從 verdict dict 抽一句白話主因。

    優先序:
      - 🟢 → 第一條 reasons_pro
      - 🔴 → 第一條 reasons_con
      - 🟡 → action_suggestion(或第一條 reasons_*)
    抓不到 → 空字串(UI 自己處理)。
    """
    color = v.get("verdict_color", "🟡")
    pros = v.get("reasons_pro") or []
    cons = v.get("reasons_con") or []
    if color == "🟢" and pros:
        return str(pros[0])
    if color == "🔴" and cons:
        return str(cons[0])
    # 🟡 或上面拿不到 → action_suggestion fallback
    sug = v.get("action_suggestion") or ""
    if sug:
        return str(sug)
    if pros:
        return str(pros[0])
    if cons:
        return str(cons[0])
    return ""


def _resolve_name(sid: str, hint: str | None) -> str:
    """有 hint 用 hint;沒就嘗試從 SQLite stocks 表撈;失敗 → '—'。"""
    if hint and str(hint).strip():
        return str(hint).strip()
    try:
        from src import database as db
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM stocks WHERE stock_id=?",
                (str(sid).strip(),),
            ).fetchone()
        if row and row["name"]:
            return str(row["name"])
    except Exception:  # noqa: BLE001
        pass
    return "—"


def build_summary(
    picks_df: pd.DataFrame | Iterable[dict] | None,
    trade_date: str,
    *,
    sid_col: str = "sid",
    name_col: str = "name",
    compute_verdict_fn: Callable[[str], dict] | None = None,
    top_green_n: int = TOP_GREEN_N,
    top_yellow_n: int = TOP_YELLOW_N,
    top_red_n: int = TOP_RED_N,
) -> dict:
    """對 picks_df 內每個 sid 算 verdict,聚合成 counts + 3 個 top 名單。

    picks_df:DataFrame(必有 sid_col)或 list[dict]。空 / None → counts 全 0、
        top 全空 list,trade_date 仍會帶出去(讓 load_from_csv 對應 staleness 檢查)。
    sid_col / name_col:欄位 fallback;DataFrame 若無 name_col,從 stocks 表撈。
    compute_verdict_fn:給 test 注入 mock;預設用 individual_stock_verdict.compute_verdict。
    """
    fn = compute_verdict_fn or _default_compute_verdict

    out: dict = {
        "trade_date": str(trade_date),
        "counts": {"green": 0, "yellow": 0, "red": 0},
        "top_green": [],
        "top_yellow": [],
        "top_red": [],
    }

    # 標準化進 list[dict]
    if picks_df is None:
        return out
    if isinstance(picks_df, pd.DataFrame):
        if picks_df.empty:
            return out
        records = picks_df.to_dict("records")
    else:
        records = list(picks_df)
    if not records:
        return out

    # 去重 — 一個 sid 只算一次 verdict(策略可能 fire 多次)
    seen: dict[str, str | None] = {}
    for r in records:
        sid_raw = r.get(sid_col)
        if sid_raw is None:
            continue
        sid = str(sid_raw).strip()
        if not sid or sid in seen:
            continue
        seen[sid] = r.get(name_col)

    if not seen:
        return out

    buckets: dict[str, list[dict]] = {"green": [], "yellow": [], "red": []}

    for sid, name_hint in seen.items():
        try:
            v = fn(sid)
        except Exception:  # noqa: BLE001
            # 單一 sid 算掛 → silent skip,不擋整體
            continue
        if not isinstance(v, dict):
            continue
        if not v.get("enabled", True):
            # kill switch off → 全 skip(整批不算 verdict)
            continue
        color = v.get("verdict_color", "🟡")
        key = _COLOR_TO_KEY.get(color, "yellow")
        verdict = v.get("verdict") or {"green": "可進場", "yellow": "觀望", "red": "不進場"}[key]
        score = v.get("score", 0)
        try:
            score = int(score)
        except Exception:  # noqa: BLE001
            score = 0
        buckets[key].append({
            "sid": sid,
            "name": _resolve_name(sid, name_hint),
            "verdict_color": color,
            "verdict": str(verdict),
            "score": score,
            "main_reason": _main_reason(v),
        })

    for key, lst in buckets.items():
        out["counts"][key] = len(lst)

    # 🟢 / 🟡 ascending main? — score 從高到低,同分 sid 字典序穩定
    def _sort_key(item: dict) -> tuple:
        return (-item["score"], item["sid"])

    out["top_green"] = sorted(buckets["green"], key=_sort_key)[:top_green_n]
    out["top_yellow"] = sorted(buckets["yellow"], key=_sort_key)[:top_yellow_n]
    # 🔴:score 由負最深排到較淺(風險最大優先) — -10 比 -3 嚴重
    out["top_red"] = sorted(
        buckets["red"], key=lambda x: (x["score"], x["sid"]),
    )[:top_red_n]

    return out


_CSV_COLUMNS = [
    "trade_date", "section", "rank", "sid", "name",
    "verdict_color", "verdict", "score", "main_reason", "count",
]


def dump_to_csv(summary: dict, path: str | Path) -> int:
    """把 summary dump 成 long-format CSV。回 row count。

    Long format(每行一筆):
      - section='count': 三筆 — 帶 verdict_color + count(其他欄空)
      - section='top_green' / 'top_yellow' / 'top_red':各 N 筆 — rank/sid/name/...
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    trade_date = summary.get("trade_date", "")
    rows: list[dict] = []

    # counts:三筆固定順序(綠 / 黃 / 紅)
    counts = summary.get("counts") or {}
    for key in ("green", "yellow", "red"):
        rows.append({
            "trade_date": trade_date,
            "section": "count",
            "rank": None,
            "sid": None,
            "name": None,
            "verdict_color": _KEY_TO_COLOR[key],
            "verdict": None,
            "score": None,
            "main_reason": None,
            "count": int(counts.get(key, 0)),
        })

    # top 名單(三段)
    for sec_key in ("top_green", "top_yellow", "top_red"):
        items = summary.get(sec_key) or []
        for i, it in enumerate(items, start=1):
            rows.append({
                "trade_date": trade_date,
                "section": sec_key,
                "rank": i,
                "sid": it.get("sid"),
                "name": it.get("name"),
                "verdict_color": it.get("verdict_color"),
                "verdict": it.get("verdict"),
                "score": it.get("score"),
                "main_reason": it.get("main_reason"),
                "count": None,
            })

    df = pd.DataFrame(rows, columns=_CSV_COLUMNS)
    df.to_csv(path, index=False, encoding="utf-8")
    return len(rows)


def load_from_csv(path: str | Path) -> dict | None:
    """讀 CSV → 還原 summary dict。檔不存在 / 解析失敗 → None(caller fallback)。"""
    path = Path(path)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, dtype={"sid": str, "trade_date": str})
    except Exception:  # noqa: BLE001
        return None
    if df.empty:
        return None

    # trade_date 取第一筆(整檔同日,但 cron 偶爾 race 可能混 — 取 max 保險)
    trade_date = str(df["trade_date"].max()) if "trade_date" in df.columns else ""

    out: dict = {
        "trade_date": trade_date,
        "counts": {"green": 0, "yellow": 0, "red": 0},
        "top_green": [],
        "top_yellow": [],
        "top_red": [],
    }

    # counts
    count_rows = df[df["section"] == "count"]
    for _, r in count_rows.iterrows():
        color = r.get("verdict_color")
        key = _COLOR_TO_KEY.get(str(color), None)
        if key is None:
            continue
        try:
            out["counts"][key] = int(r["count"])
        except Exception:  # noqa: BLE001
            out["counts"][key] = 0

    # top 三段
    for sec_key in ("top_green", "top_yellow", "top_red"):
        sub = df[df["section"] == sec_key].sort_values("rank")
        items: list[dict] = []
        for _, r in sub.iterrows():
            try:
                score = int(r["score"]) if pd.notna(r["score"]) else 0
            except Exception:  # noqa: BLE001
                score = 0
            items.append({
                "sid": str(r["sid"]) if pd.notna(r.get("sid")) else "",
                "name": str(r["name"]) if pd.notna(r.get("name")) else "—",
                "verdict_color": (
                    str(r["verdict_color"])
                    if pd.notna(r.get("verdict_color")) else ""
                ),
                "verdict": (
                    str(r["verdict"]) if pd.notna(r.get("verdict")) else ""
                ),
                "score": score,
                "main_reason": (
                    str(r["main_reason"]) if pd.notna(r.get("main_reason")) else ""
                ),
            })
        out[sec_key] = items

    return out


__all__ = [
    "build_summary",
    "dump_to_csv",
    "load_from_csv",
    "TOP_GREEN_N",
    "TOP_YELLOW_N",
    "TOP_RED_N",
]
