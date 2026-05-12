"""Regression test for filter_sids_by_industry against real-schema stocks table.

Origin: 2026-05-13 主公在 Streamlit Cloud 短線推薦頁選「半導體業 + 光電業」
撞 sqlite3.OperationalError。root cause:filter_sids_by_industry 用 `sid`
欄位名,但生產 schema 的 stocks 表 PK 是 `stock_id`。

這支測試:
- 用真實 schema (stock_id, name, industry, market, type, updated_at) 建 in-memory db
- 跑出問題的組合「半導體業 + 光電業」
- 同時跑單一 / 空 / 三組 / 未知 sid 等 edge cases

No mock — 直接 run helper against real sqlite3.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.industry_filter import filter_sids_by_industry


@pytest.fixture
def real_schema_conn():
    """In-memory SQLite with production-shape stocks table."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE stocks (
            stock_id   TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            market     TEXT NOT NULL DEFAULT 'TW',
            industry   TEXT,
            type       TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO stocks (stock_id, name, market, industry, type, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("2330", "台積電", "TW", "半導體業", "Common Stock", "2026-05-13"),
            ("2454", "聯發科", "TW", "半導體業", "Common Stock", "2026-05-13"),
            ("3008", "大立光", "TW", "光電業", "Common Stock", "2026-05-13"),
            ("2317", "鴻海", "TW", "其他電子業", "Common Stock", "2026-05-13"),
            ("1101", "台泥", "TW", "水泥工業", "Common Stock", "2026-05-13"),
            ("2882", "國泰金", "TW", "金融保險", "Common Stock", "2026-05-13"),
        ],
    )
    conn.commit()
    return conn


def test_semiconductor_plus_optoelectronics_does_not_throw(real_schema_conn):
    """主公撞到的組合 — 不能炸 OperationalError。"""
    sids = [2330, 2454, 3008, 2317, 1101, 2882]
    out = filter_sids_by_industry(sids, ["半導體業", "光電業"], real_schema_conn)
    assert sorted(out) == [2330, 2454, 3008]


def test_single_industry_against_real_schema(real_schema_conn):
    sids = [2330, 2454, 3008, 2317]
    out = filter_sids_by_industry(sids, ["半導體業"], real_schema_conn)
    assert sorted(out) == [2330, 2454]


def test_three_industries_against_real_schema(real_schema_conn):
    sids = [2330, 3008, 2317, 1101, 2882]
    out = filter_sids_by_industry(
        sids, ["半導體業", "光電業", "金融保險"], real_schema_conn,
    )
    assert sorted(out) == [2330, 2882, 3008]


def test_empty_selection_returns_all_against_real_schema(real_schema_conn):
    sids = [2330, 2454]
    assert filter_sids_by_industry(sids, [], real_schema_conn) == sids
    assert filter_sids_by_industry(sids, None, real_schema_conn) == sids


def test_unknown_sid_dropped_against_real_schema(real_schema_conn):
    """Sid 不在 stocks 表 → 不會進 result(也不能炸)。"""
    sids = [2330, 99999]
    out = filter_sids_by_industry(sids, ["半導體業"], real_schema_conn)
    assert out == [2330]


def test_large_sid_list_still_works(real_schema_conn):
    """確認 IN (?, ?, ..., ?) 展開法在 ~500 sids 也能跑(SQLite 預設 999 ok)。"""
    sids = list(range(1000, 1500)) + [2330, 3008]
    out = filter_sids_by_industry(sids, ["半導體業", "光電業"], real_schema_conn)
    assert sorted(out) == [2330, 3008]
