"""src/strategies.py::screen_big_holder_inflow 單元測試。

涵蓋:
- Phase 1 fallback(歷史不足時走 P80 Top 20% absolute)— 既有 8 case
- Phase 2(滾動 4 週 mean+1σ 突破)— 新增 4 case
- delta_w <= 0 / NULL → 不命中
- 沒 shareholder 資料的 sid → 不命中(graceful skip)
- 全部缺資料 → return empty(對齊 ML predict 在資料不足時的 graceful skip)
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


def _seed_concentration_multi(
    sid_to_weeks: dict[str, list[tuple[str, int | None]]],
) -> None:
    """sid_to_weeks = {sid: [(week_end, delta), ...]}。多週 fixture。"""
    rows = []
    for sid, weeks in sid_to_weeks.items():
        for week_end, delta in weeks:
            rows.append({
                "sid": sid,
                "week_end": week_end,
                "holders_1000up_count": 1000 + (delta or 0),
                "total_holders": 100_000,
                "holders_pct": 0.01,
                "holders_delta_w": delta,
            })
    db.upsert_shareholder_concentration(rows)


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


# ============================================================================
# Phase 2 cases — rolling 4-week mean + sigma breakout
# ============================================================================

# 5 週連續週五 week_end(舊 → 新)
_FIVE_WEEKS = [
    "2026-04-10", "2026-04-17", "2026-04-24", "2026-05-01", "2026-05-08",
]


# === Phase 2 case 1:5 週資料 + 本週 > mean+1σ → 命中 ===

def test_phase2_hit_breaks_mean_plus_sigma(tmp_db):
    """1 檔 sid 前 4 週 delta = [5,10,15,20],本週 50 → μ=12.5、σ≈6.45、
    threshold≈18.95 → 50 > 18.95 → 命中。"""
    sid = "PH1"
    _seed_stocks_and_prices([sid], date=_FIVE_WEEKS[-1])
    _seed_concentration_multi({
        sid: [
            (_FIVE_WEEKS[0], 5),
            (_FIVE_WEEKS[1], 10),
            (_FIVE_WEEKS[2], 15),
            (_FIVE_WEEKS[3], 20),
            (_FIVE_WEEKS[4], 50),
        ],
    })

    df = strat.screen_big_holder_inflow(_FIVE_WEEKS[-1], stock_ids=[sid])
    assert not df.empty
    assert set(df["stock_id"].tolist()) == {sid}
    assert df.iloc[0]["holders_delta_w"] == 50
    assert df.iloc[0]["week_end"] == _FIVE_WEEKS[-1]


# === Phase 2 case 2:5 週資料 + 本週 ≤ mean+1σ → 不命中 ===

def test_phase2_miss_within_mean_plus_sigma(tmp_db):
    """1 檔 sid 前 4 週 delta = [5,10,15,20],本週 15 → 15 < threshold≈18.95
    → 不命中(走 Phase 2,前期 ≥ rolling_weeks=4 不 fallback)。"""
    sid = "PH2"
    _seed_stocks_and_prices([sid], date=_FIVE_WEEKS[-1])
    _seed_concentration_multi({
        sid: [
            (_FIVE_WEEKS[0], 5),
            (_FIVE_WEEKS[1], 10),
            (_FIVE_WEEKS[2], 15),
            (_FIVE_WEEKS[3], 20),
            (_FIVE_WEEKS[4], 15),
        ],
    })

    df = strat.screen_big_holder_inflow(_FIVE_WEEKS[-1], stock_ids=[sid])
    assert df.empty, (
        "本週 15 雖然 > 0 但未超過 μ+1σ(≈18.95);Phase 2 不該命中、"
        "且不可 fallback 到 Phase 1(歷史已足夠)"
    )


# === Phase 2 case 3:歷史不足 5 週 → fallback Phase 1 ===

def test_phase2_fallback_to_phase1_on_short_history(tmp_db):
    """3 檔 sid 只有 4 週(本週 + 前 3 週,前期僅 3 個 < rolling_weeks=4)→
    全部走 Phase 1 fallback。本週 delta = [10, 20, 30] → P80=26 → 只有 30 命中。"""
    sids = ["S1", "S2", "S3"]
    _seed_stocks_and_prices(sids, date=_FIVE_WEEKS[-1])
    # 只塞 4 個 week_end(latest + 前 3 → 前期 deltas 只有 3 個 < 4)
    four_weeks = _FIVE_WEEKS[1:]  # 04-17 ~ 05-08
    deltas_per_sid = {"S1": 10, "S2": 20, "S3": 30}
    payload: dict[str, list[tuple[str, int | None]]] = {}
    for sid, latest_delta in deltas_per_sid.items():
        payload[sid] = [
            (four_weeks[0], 1),   # 前期 1
            (four_weeks[1], 1),   # 前期 2
            (four_weeks[2], 1),   # 前期 3 — 只 3 個 < rolling_weeks(4)
            (four_weeks[3], latest_delta),  # 本週(latest)
        ]
    _seed_concentration_multi(payload)

    df = strat.screen_big_holder_inflow(_FIVE_WEEKS[-1], stock_ids=sids)
    hit_sids = set(df["stock_id"].tolist())
    # P80 of [10, 20, 30] = 26.0 → 只有 S3 (30) >= 26
    assert hit_sids == {"S3"}, (
        f"Phase 1 fallback 應該只有 S3 命中(P80=26),實際 {hit_sids}"
    )


# === Phase 2 case 4:混合場景 — 部分 sid Phase 2 命中,部分 Phase 1 fallback ===

def test_phase2_mixed_with_phase1_fallback(tmp_db):
    """A 檔有 5 週資料 + 本週突破 → Phase 2 命中。
    B/C 檔只有 3 週(latest + 前 2)→ fallback,B 大 C 小 → P80 取 B。"""
    sids = ["A", "B", "C"]
    _seed_stocks_and_prices(sids, date=_FIVE_WEEKS[-1])
    _seed_concentration_multi({
        # A:5 週 → Phase 2,本週 100 遠超 μ+1σ
        "A": [
            (_FIVE_WEEKS[0], 1),
            (_FIVE_WEEKS[1], 2),
            (_FIVE_WEEKS[2], 1),
            (_FIVE_WEEKS[3], 2),
            (_FIVE_WEEKS[4], 100),
        ],
        # B:只 3 週(前期 2 個 < 4)→ fallback,本週 50
        "B": [
            (_FIVE_WEEKS[2], 1),
            (_FIVE_WEEKS[3], 1),
            (_FIVE_WEEKS[4], 50),
        ],
        # C:只 3 週 → fallback,本週 10
        "C": [
            (_FIVE_WEEKS[2], 1),
            (_FIVE_WEEKS[3], 1),
            (_FIVE_WEEKS[4], 10),
        ],
    })

    df = strat.screen_big_holder_inflow(_FIVE_WEEKS[-1], stock_ids=sids)
    hit_sids = set(df["stock_id"].tolist())
    # A:Phase 2 命中(μ=1.5、σ≈0.577、threshold≈2.08,本週 100 遠超)
    # B/C:Phase 1 fallback,候選 = [50, 10] → P80=42 → 只 B 命中
    assert "A" in hit_sids, "A 應該走 Phase 2 命中"
    assert "B" in hit_sids, "B 應該走 Phase 1 fallback 命中(P80 內 Top 20%)"
    assert "C" not in hit_sids, "C 雖然 fallback 但 delta 太低,不該命中"
