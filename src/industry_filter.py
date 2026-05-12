"""Industry filter helpers for short-term picks.

Phase 1: uses existing DB `industry` column (no AI topic classification).
Canonicalizes TSE/TPEx twin labels so the same industry shows up once
in the UI multiselect.
"""
from __future__ import annotations

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


def filter_picks_by_industry(
    rows: list[dict], selected: list[str] | None
) -> list[dict]:
    if not selected:
        return list(rows or [])
    selected_set = set(selected)
    return [
        r
        for r in (rows or [])
        if canonicalize_industry((r or {}).get("industry", "")) in selected_set
    ]
