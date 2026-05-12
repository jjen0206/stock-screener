"""Industry filter helpers for short-term picks.

Pre-filter universe pattern: pick industry tags first, strategies only run
on sids inside those industries. Canonicalizes TSE/TPEx twin labels so the
same industry shows up once in the UI.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable


INDUSTRY_CANONICAL_MAP: dict[str, str] = {
    "生技醫療業": "生技醫療",
    "油電燃氣業": "油電燃氣",
    "金融業": "金融保險",
    "其他電子業": "其他電子",
    "居家生活類": "居家生活",
    "運動休閒類": "運動休閒",
    "數位雲端類": "數位雲端",
    "綠能環保類": "綠能環保",
    "其他電子類": "其他電子",
}


# Top 15 canonical industries by stock count, computed from data/cache.db
# on 2026-05-12. Hardcoded because the stocks table changes rarely; refresh
# this list when stocks table mass-updates (new listings batch).
MAINSTREAM_INDUSTRIES: list[str] = [
    "ETF",
    "電子零組件業",
    "半導體業",
    "生技醫療",
    "光電業",
    "其他",
    "電腦及週邊設備業",
    "上櫃ETF",
    "其他電子",
    "電機機械",
    "通信網路業",
    "建材營造",
    "金融保險",
    "紡織纖維",
    "鋼鐵工業",
]


def canonicalize_industry(raw: str | None) -> str:
    if not raw:
        return ""
    return INDUSTRY_CANONICAL_MAP.get(raw, raw)


def get_available_industries(rows: Iterable[dict]) -> list[str]:
    seen: set[str] = set()
    for r in rows or []:
        ind = canonicalize_industry(r.get("industry") if isinstance(r, dict) else "")
        if ind:
            seen.add(ind)
    return sorted(seen)


def get_other_industries(
    all_industries: Iterable[str], mainstream: Iterable[str] = MAINSTREAM_INDUSTRIES
) -> list[str]:
    """Return the canonical industries that are NOT in mainstream, sorted."""
    main_set = set(mainstream)
    return sorted({i for i in (all_industries or []) if i and i not in main_set})


def filter_sids_by_industry(
    sids: list[int],
    selected: list[str] | None,
    conn: sqlite3.Connection,
) -> list[int]:
    """Pre-filter universe: keep only sids whose canonical industry is selected.

    Empty / None selection = no filter, return sids unchanged.
    """
    if not selected or not sids:
        return list(sids or [])
    selected_set = set(selected)

    # Single round-trip: pull (sid, industry) for the candidate sids only.
    placeholders = ",".join("?" * len(sids))
    cur = conn.execute(
        f"SELECT sid, industry FROM stocks WHERE sid IN ({placeholders})",
        [str(s) for s in sids],
    )
    by_sid: dict[str, str] = {row[0]: row[1] or "" for row in cur.fetchall()}

    out: list[int] = []
    for sid in sids:
        ind = by_sid.get(str(sid), "")
        if canonicalize_industry(ind) in selected_set:
            out.append(sid)
    return out


def filter_picks_by_industry(
    rows: list[dict], selected: list[str] | None
) -> list[dict]:
    """Post-filter helper. Kept for backward compat; new code should pre-filter
    universe via filter_sids_by_industry."""
    if not selected:
        return list(rows or [])
    selected_set = set(selected)
    return [
        r
        for r in (rows or [])
        if canonicalize_industry((r or {}).get("industry", "")) in selected_set
    ]
