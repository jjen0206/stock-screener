"""src/strategies.py::screen_big_holder_inflow 單元測試。

Phase 1(目前資料只有 1 週):
- holders_delta_w > 0 + 落在 P80 以上 → 命中
- delta_w <= 0 / NULL → 不命中
- 沒 shareholder 資料的 sid → 不命中(graceful skip)
- 首週全 NULL → return empty(對齊 ML predict 在資料不足時的 graceful skip)
"""
from __future__ import annotations

import pytest

from src import config, database as db
from src import strategies as strat


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "big_holder.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def _seed_stocks_and_prices(sids: list[str], date: str = "2026-05-08") -> None:
    """為一批 sid 灌最低限度的 stocks + daily_prices(close 讓 enrich 不爆)。"""
    db.upsert_stocks([
        {"stock_id": s, "name": f"股{s}", "market": "TW"}
        for s in sids
    ])
    db.upsert_daily_prices([
        {
            "stock_id": s, "date": date,
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0, "volume": 1000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        }
        for s in sids
    ])


def _seed_concentration(
    rows: list[tuple[str, int | None]],
    week_end: str = "2026-05-09",  # 週五
) -> None:
    """rows = [(sid, holders_delta_w), ...]。other fields 用合理 dummy。"""
    db.upsert_shareholder_concentration([
        {
            "sid": sid,
            "week_end": week_end,
            "holders_1000up_count": 1000 + (delta or 0),
            "total_holders": 100_000,
            "holders_pct": 0.01,
            "holders_delta_w": delta,
        }
        for sid, delta in rows
    ])


# === Phase 1 case 1:Top 20% 命中 ===

def test_big_holder_inflow_top20_passes(tmp_db):
    """灌 10 檔 delta_w = 1..10 → P80 = 8.2 → delta >= 8.2 才命中(9, 10)。"""
    sids = [f"100{i}" for i in range(10)]
    _seed_stocks_and_prices(sids)
    _seed_concentration([(sids[i], i + 1) for i in range(10)])

    df = strat.screen_big_holder_inflow("2026-05-09", stock_ids=sids)
    # P80 of [1..10] = 8.2 → threshold 8.2 → delta in {9, 10}
    hit_sids = set(df["stock_id"].tolist())
    assert hit_sids == {sids[8], sids[9]}, (
        f"P80 hit expected {{sids[8], sids[9]}}, got {hit_sids}"
    )
    # 命中 row delta_w 欄位該對得上
    for _, row in df.iterrows():
        assert row["holders_delta_w"] in (9, 10)
        assert row["week_end"] == "2026-05-09"


# === Phase 1 case 2:Bottom 80% 不命中 ===

def test_big_holder_inflow_bottom80_excluded(tmp_db):
    """灌 10 檔 delta_w = 1..10 → delta = 1..8 都不該命中。"""
    sids = [f"100{i}" for i in range(10)]
    _seed_stocks_and_prices(sids)
    _seed_concentration([(sids[i], i + 1) for i in range(10)])

    df = strat.screen_big_holder_inflow("2026-05-09", stock_ids=sids)
    hit_sids = set(df["stock_id"].tolist())
    for low_sid in sids[:8]:
        assert low_sid not in hit_sids


# === Phase 1 case 3:delta_w <= 0 不命中 ===

def test_big_holder_inflow_negative_or_zero_delta_excluded(tmp_db):
    """5 檔有正 delta(命中候選),3 檔負 delta + 1 檔 0 delta 都不該入 candidates。"""
    sids = ["P1", "P2", "P3", "P4", "P5", "N1", "N2", "N3", "Z1"]
    _seed_stocks_and_prices(sids)
    _seed_concentration([
        ("P1", 5), ("P2", 10), ("P3", 15), ("P4", 20), ("P5", 25),
        ("N1", -5), ("N2", -10), ("N3", -3),
        ("Z1", 0),
    ])

    df = strat.screen_big_holder_inflow("2026-05-09", stock_ids=sids)
    hit_sids = set(df["stock_id"].tolist())
    # 負值 / 零都不該命中
    for s in ["N1", "N2", "N3", "Z1"]:
        assert s not in hit_sids
    # candidates = [5, 10, 15, 20, 25],P80 = 21 → 只有 P5 (25) 命中
    assert hit_sids == {"P5"}


# === Phase 1 case 4:首週全 NULL → return [](graceful skip) ===

def test_big_holder_inflow_all_null_delta_returns_empty(tmp_db):
    """首週 holders_delta_w 全 NULL → return 空 DF,不報錯。"""
    sids = ["A1", "A2", "A3"]
    _seed_stocks_and_prices(sids)
    _seed_concentration([(s, None) for s in sids])

    df = strat.screen_big_holder_inflow("2026-05-09", stock_ids=sids)
    assert df.empty
    # schema 還是要齊全(對齊其他策略 empty 行為)
    for col in ["stock_id", "name", "close", "holders_delta_w", "week_end"]:
        assert col in df.columns


# === Phase 1 case 5:沒 shareholder 資料的 sid → 不命中(不算空訊號) ===

def test_big_holder_inflow_missing_sid_not_hit(tmp_db):
    """有 SC 表但給 stock_ids 多帶幾檔沒灌資料的 sid → 那些 sid 不命中。"""
    in_table = ["IN1", "IN2", "IN3"]
    not_in_table = ["NIL1", "NIL2"]
    _seed_stocks_and_prices(in_table + not_in_table)
    _seed_concentration([("IN1", 10), ("IN2", 20), ("IN3", 30)])

    df = strat.screen_big_holder_inflow(
        "2026-05-09", stock_ids=in_table + not_in_table,
    )
    hit_sids = set(df["stock_id"].tolist())
    for s in not_in_table:
        assert s not in hit_sids


# === Phase 1 case 6:空 stock_ids → return 空 DF ===

def test_big_holder_inflow_empty_stock_ids_returns_empty(tmp_db):
    """stock_ids=[] 邊界 → 直接 return 空 DF,不去 query。"""
    df = strat.screen_big_holder_inflow("2026-05-09", stock_ids=[])
    assert df.empty


# === Phase 1 case 7:shareholder_concentration 完全沒資料 → return 空 DF ===

def test_big_holder_inflow_no_data_returns_empty(tmp_db):
    """SC 表完全沒資料(MAX(week_end) IS NULL)→ return 空 DF graceful。"""
    sids = ["EMPTY1", "EMPTY2"]
    _seed_stocks_and_prices(sids)
    # 不灌 SC 資料

    df = strat.screen_big_holder_inflow("2026-05-09", stock_ids=sids)
    assert df.empty


# === Phase 1 case 8:不同 percentile 參數可調 ===

def test_big_holder_inflow_custom_percentile(tmp_db):
    """percentile=0.5 → top 50% 命中(5, 6, 7, 8, 9, 10)。
    確保 params override 機制有作用。"""
    sids = [f"200{i}" for i in range(10)]
    _seed_stocks_and_prices(sids)
    _seed_concentration([(sids[i], i + 1) for i in range(10)])

    df = strat.screen_big_holder_inflow(
        "2026-05-09",
        params={"percentile": 0.5},
        stock_ids=sids,
    )
    hit_sids = set(df["stock_id"].tolist())
    # P50 of [1..10] = 5.5 → delta in {6,7,8,9,10} → sids[5..9]
    assert hit_sids == set(sids[5:])
