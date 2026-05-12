"""Unit tests for src.industry_filter helpers."""
from __future__ import annotations

import sqlite3

import pytest

from src.industry_filter import (
    INDUSTRY_CANONICAL_MAP,
    MAINSTREAM_INDUSTRIES,
    canonicalize_industry,
    filter_picks_by_industry,
    filter_sids_by_industry,
    get_available_industries,
    get_other_industries,
)


@pytest.fixture
def stocks_conn():
    """In-memory SQLite with a tiny stocks table for pre-filter tests."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE stocks (sid TEXT PRIMARY KEY, industry TEXT)")
    conn.executemany(
        "INSERT INTO stocks (sid, industry) VALUES (?, ?)",
        [
            ("2330", "半導體業"),
            ("1101", "水泥工業"),
            ("2882", "金融業"),
            ("2891", "金融保險"),
            ("4128", "生技醫療"),
            ("4142", "生技醫療業"),
            ("9999", None),
        ],
    )
    conn.commit()
    return conn


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


# ============================================================================
# Pre-filter universe tests (new pattern)
# ============================================================================


def test_mainstream_industries_size_and_canonical():
    """MAINSTREAM_INDUSTRIES 必須長度 15 且每個都是 canonical 形式。"""
    assert len(MAINSTREAM_INDUSTRIES) == 15
    # canonical form means: passing through canonicalize_industry doesn't change it
    for ind in MAINSTREAM_INDUSTRIES:
        assert canonicalize_industry(ind) == ind, (
            f"{ind!r} in MAINSTREAM_INDUSTRIES is not in canonical form"
        )
    # no duplicates
    assert len(set(MAINSTREAM_INDUSTRIES)) == 15


def test_filter_sids_empty_selected_returns_all(stocks_conn):
    sids = [2330, 1101, 2882]
    assert filter_sids_by_industry(sids, [], stocks_conn) == sids
    assert filter_sids_by_industry(sids, None, stocks_conn) == sids


def test_filter_sids_multi_select_or_logic(stocks_conn):
    sids = [2330, 1101, 2882, 4128]
    out = filter_sids_by_industry(sids, ["半導體業", "金融保險"], stocks_conn)
    assert sorted(out) == [2330, 2882]


def test_filter_sids_canonical_merges_twins(stocks_conn):
    """selecting '生技醫療' should grab both '生技醫療' and '生技醫療業' rows;
    selecting '金融保險' should grab both '金融業' and '金融保險' rows."""
    sids = [2330, 4128, 4142, 1101, 2882, 2891]
    out_bio = filter_sids_by_industry(sids, ["生技醫療"], stocks_conn)
    assert sorted(out_bio) == [4128, 4142]
    out_fin = filter_sids_by_industry(sids, ["金融保險"], stocks_conn)
    assert sorted(out_fin) == [2882, 2891]


def test_filter_sids_drops_unknown_and_null_industry(stocks_conn):
    """sids without industry data (or industry=NULL) must not pass the filter
    unless we don't filter at all."""
    sids = [2330, 9999, 12345]  # 12345 doesn't exist; 9999 has NULL industry
    out = filter_sids_by_industry(sids, ["半導體業"], stocks_conn)
    assert out == [2330]


def test_get_other_industries_excludes_mainstream():
    all_inds = ["半導體業", "電子零組件業", "水泥工業", "塑膠工業", "ETF"]
    other = get_other_industries(all_inds, MAINSTREAM_INDUSTRIES)
    assert "半導體業" not in other  # mainstream
    assert "電子零組件業" not in other  # mainstream
    assert "水泥工業" in other
    assert "塑膠工業" in other
    assert "ETF" in other  # ETF 已從 mainstream 排除,落到 other
    assert other == sorted(other)
