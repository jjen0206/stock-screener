"""Unit tests for src.industry_filter helpers."""
from __future__ import annotations

from src.industry_filter import (
    INDUSTRY_CANONICAL_MAP,
    canonicalize_industry,
    filter_picks_by_industry,
    get_available_industries,
)


def test_canonical_map_merges_tse_tpex_twins():
    assert canonicalize_industry("生技醫療業") == "生技醫療"
    assert canonicalize_industry("生技醫療") == "生技醫療"
    assert canonicalize_industry("綠能環保類") == "綠能環保"
    assert canonicalize_industry("綠能環保") == "綠能環保"
    assert canonicalize_industry("金融業") == "金融保險"
    assert canonicalize_industry("金融保險") == "金融保險"
    assert canonicalize_industry("其他電子業") == "其他電子"
    assert canonicalize_industry("其他電子類") == "其他電子"


def test_canonical_map_passthrough_for_unknown():
    assert canonicalize_industry("半導體業") == "半導體業"
    assert canonicalize_industry("食品工業") == "食品工業"


def test_canonical_map_handles_empty_and_none():
    assert canonicalize_industry("") == ""
    assert canonicalize_industry(None) == ""


def test_filter_empty_selected_returns_all():
    rows = [
        {"sid": "2330", "industry": "半導體業"},
        {"sid": "1101", "industry": "水泥工業"},
    ]
    assert filter_picks_by_industry(rows, []) == rows
    assert filter_picks_by_industry(rows, None) == rows


def test_filter_multi_select_or_logic():
    rows = [
        {"sid": "2330", "industry": "半導體業"},
        {"sid": "1101", "industry": "水泥工業"},
        {"sid": "2882", "industry": "金融業"},
    ]
    out = filter_picks_by_industry(rows, ["半導體業", "金融保險"])
    sids = sorted(r["sid"] for r in out)
    assert sids == ["2330", "2882"]


def test_filter_uses_canonical_form_for_matching():
    rows = [
        {"sid": "2330", "industry": "生技醫療業"},
        {"sid": "4128", "industry": "生技醫療"},
        {"sid": "1101", "industry": "水泥工業"},
    ]
    out = filter_picks_by_industry(rows, ["生技醫療"])
    sids = sorted(r["sid"] for r in out)
    assert sids == ["2330", "4128"]


def test_filter_tolerates_missing_industry_field():
    rows = [
        {"sid": "2330", "industry": "半導體業"},
        {"sid": "9999"},
        {"sid": "8888", "industry": None},
        {"sid": "7777", "industry": ""},
    ]
    out = filter_picks_by_industry(rows, ["半導體業"])
    assert [r["sid"] for r in out] == ["2330"]
    assert filter_picks_by_industry(rows, []) == rows


def test_get_available_industries_dedupes_and_sorts():
    rows = [
        {"industry": "半導體業"},
        {"industry": "金融業"},
        {"industry": "金融保險"},
        {"industry": "綠能環保類"},
        {"industry": "綠能環保"},
        {"industry": ""},
        {},
    ]
    out = get_available_industries(rows)
    assert "金融保險" in out
    assert "金融業" not in out
    assert out.count("綠能環保") == 1
    assert out == sorted(out)


def test_canonical_map_keys_unique_and_nonempty():
    assert all(k for k in INDUSTRY_CANONICAL_MAP)
    assert all(v for v in INDUSTRY_CANONICAL_MAP.values())
