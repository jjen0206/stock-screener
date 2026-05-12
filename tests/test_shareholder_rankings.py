"""跨 sid 千張大戶排行 helper(db.get_top_shareholder_movers /
get_top_shareholder_concentration / get_consecutive_shareholder_increases)
單元測試 — 給「👥 大戶入場」頁用。

塞假資料測:
  1. 各 helper 排序 / filter 正確
  2. JOIN stocks 帶名稱
  3. LEFT JOIN daily_prices 帶最新 close
  4. LEFT JOIN daily_picks 帶 ML 分數
  5. 連續增加在資料只 1 週時回空(符合上線初期預期)
"""
from __future__ import annotations

import pytest

from src import config, database as db


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "rank.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    yield tmp_path
    db._reset_path_cache()


def _seed_stocks(rows: list[dict]) -> None:
    db.upsert_stocks([
        {"stock_id": r["stock_id"], "name": r.get("name", "")} for r in rows
    ])


def _seed_prices(prices: list[dict]) -> None:
    with db.get_conn() as conn:
        for r in prices:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_prices
                  (stock_id, date, open, high, low, close, volume, trading_money)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["stock_id"], r["date"],
                    r.get("open", r["close"]),
                    r.get("high", r["close"]),
                    r.get("low", r["close"]),
                    r["close"],
                    r.get("volume", 1000),
                    r.get("trading_money", r["close"] * r.get("volume", 1000)),
                ),
            )


def _seed_concentration(rows: list[dict]) -> None:
    db.upsert_shareholder_concentration([
        {
            "sid": r["sid"],
            "week_end": r["week_end"],
            "holders_1000up_count": r.get("h1k", 1000),
            "total_holders": r.get("total_holders", 100000),
            "holders_pct": r.get("holders_pct"),
            "holders_delta_w": r.get("holders_delta_w"),
        }
        for r in rows
    ])


def _seed_ml_prob(sid: str, trade_date: str, ml_prob: float) -> None:
    """灌一筆 daily_picks 進去測 ML 分數 LEFT JOIN。"""
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_picks
                (trade_date, universe, strategy, sid, score, rank,
                 params_hash, payload, ml_prob, computed_at)
            VALUES (?, 'pure_stock', 'test_strategy', ?, 0.5, 1,
                    'default_v1', '{}', ?, '2026-05-09T00:00:00+00:00')
            """,
            (trade_date, sid, ml_prob),
        )


# ============================================================================
# get_top_shareholder_movers
# ============================================================================

def test_movers_orders_by_delta_w_desc_filters_positive(tmp_db):
    """delta_w 正排序 + filter > 0(NULL / 0 / 負應該被擋掉)。"""
    _seed_stocks([
        {"stock_id": "2330", "name": "台積電"},
        {"stock_id": "2317", "name": "鴻海"},
        {"stock_id": "1101", "name": "台泥"},
        {"stock_id": "2454", "name": "聯發科"},
        {"stock_id": "0050", "name": "ETF"},
    ])
    _seed_concentration([
        {"sid": "2330", "week_end": "2026-05-08", "h1k": 2000,
         "holders_pct": 0.10, "holders_delta_w": 50},
        {"sid": "2317", "week_end": "2026-05-08", "h1k": 1500,
         "holders_pct": 0.08, "holders_delta_w": 200},  # max delta
        {"sid": "1101", "week_end": "2026-05-08", "h1k": 800,
         "holders_pct": 0.05, "holders_delta_w": -10},  # 負 → 擋
        {"sid": "2454", "week_end": "2026-05-08", "h1k": 1200,
         "holders_pct": 0.07, "holders_delta_w": 0},  # 0 → 擋
        {"sid": "0050", "week_end": "2026-05-08", "h1k": 500,
         "holders_pct": 0.03, "holders_delta_w": None},  # NULL → 擋
    ])

    rows = db.get_top_shareholder_movers(limit=10)
    assert len(rows) == 2, f"只該 2 檔 delta>0,實際 {len(rows)}"
    # 排序:2317 (200) > 2330 (50)
    assert [r["sid"] for r in rows] == ["2317", "2330"]
    # JOIN stocks 帶名稱
    assert rows[0]["name"] == "鴻海"
    assert rows[1]["name"] == "台積電"


def test_movers_left_join_close_from_daily_prices(tmp_db):
    """LEFT JOIN daily_prices 最新一日 close。沒價格資料 → None。"""
    _seed_stocks([{"stock_id": "2330", "name": "台積電"}])
    _seed_prices([
        {"stock_id": "2330", "date": "2026-05-06", "close": 1000},
        {"stock_id": "2330", "date": "2026-05-07", "close": 1010},  # 最新
    ])
    _seed_concentration([
        {"sid": "2330", "week_end": "2026-05-08", "h1k": 2000,
         "holders_pct": 0.10, "holders_delta_w": 50},
        # 2317 沒名稱、沒價格,只有持股資料
        {"sid": "9999", "week_end": "2026-05-08", "h1k": 100,
         "holders_pct": 0.01, "holders_delta_w": 20},
    ])

    rows = db.get_top_shareholder_movers(limit=10)
    by_sid = {r["sid"]: r for r in rows}
    assert by_sid["2330"]["close"] == 1010, "該取最新交易日 close"
    assert by_sid["9999"]["close"] is None, "沒價格資料 → None,不該炸"
    assert by_sid["9999"]["name"] is None, "沒 stocks 對應 → name=None"


def test_movers_left_join_ml_prob(tmp_db):
    """LEFT JOIN daily_picks 最新 ml_prob。沒 ml 資料 → None。"""
    _seed_stocks([
        {"stock_id": "2330", "name": "台積電"},
        {"stock_id": "2317", "name": "鴻海"},
    ])
    _seed_concentration([
        {"sid": "2330", "week_end": "2026-05-08", "h1k": 2000,
         "holders_pct": 0.10, "holders_delta_w": 100},
        {"sid": "2317", "week_end": "2026-05-08", "h1k": 1500,
         "holders_pct": 0.08, "holders_delta_w": 50},
    ])
    _seed_ml_prob("2330", "2026-05-07", 0.85)
    # 2317 沒灌 ml

    rows = db.get_top_shareholder_movers(limit=10)
    by_sid = {r["sid"]: r for r in rows}
    assert by_sid["2330"]["ml_prob"] == pytest.approx(0.85)
    assert by_sid["2317"]["ml_prob"] is None


def test_movers_respects_limit(tmp_db):
    """limit 參數正確截斷。"""
    _seed_stocks([{"stock_id": f"99{i:02d}", "name": f"X{i}"} for i in range(5)])
    _seed_concentration([
        {"sid": f"99{i:02d}", "week_end": "2026-05-08",
         "h1k": 1000, "holders_pct": 0.05, "holders_delta_w": 100 - i}
        for i in range(5)
    ])
    rows = db.get_top_shareholder_movers(limit=3)
    assert len(rows) == 3


def test_movers_empty_when_no_data(tmp_db):
    """空表 → 空 list,不炸。"""
    assert db.get_top_shareholder_movers(limit=30) == []


# ============================================================================
# get_top_shareholder_concentration
# ============================================================================

def test_concentration_orders_by_pct_desc(tmp_db):
    """holders_pct 排序正確。"""
    _seed_stocks([
        {"stock_id": "2330", "name": "台積電"},
        {"stock_id": "2317", "name": "鴻海"},
        {"stock_id": "1101", "name": "台泥"},
    ])
    _seed_concentration([
        {"sid": "2330", "week_end": "2026-05-08", "h1k": 2000,
         "holders_pct": 0.05, "holders_delta_w": 10},
        {"sid": "2317", "week_end": "2026-05-08", "h1k": 1500,
         "holders_pct": 0.20, "holders_delta_w": -5},  # 高占比、負 delta 也該入榜
        {"sid": "1101", "week_end": "2026-05-08", "h1k": 800,
         "holders_pct": 0.12, "holders_delta_w": 20},
    ])

    rows = db.get_top_shareholder_concentration(limit=10)
    assert [r["sid"] for r in rows] == ["2317", "1101", "2330"]
    # 占比 desc — 不受 delta_w 正負影響
    assert rows[0]["holders_pct"] == pytest.approx(0.20)


def test_concentration_join_stocks_brings_name(tmp_db):
    _seed_stocks([{"stock_id": "2330", "name": "台積電"}])
    _seed_concentration([
        {"sid": "2330", "week_end": "2026-05-08", "h1k": 2000,
         "holders_pct": 0.15, "holders_delta_w": 30},
    ])
    rows = db.get_top_shareholder_concentration(limit=10)
    assert len(rows) == 1
    assert rows[0]["name"] == "台積電"


def test_concentration_empty_when_pct_all_null(tmp_db):
    """所有 holders_pct 為 NULL → 空(WHERE 過濾)。"""
    _seed_concentration([
        {"sid": "2330", "week_end": "2026-05-08", "h1k": 2000,
         "holders_pct": None, "holders_delta_w": 30},
    ])
    rows = db.get_top_shareholder_concentration(limit=10)
    assert rows == []


# ============================================================================
# get_consecutive_shareholder_increases
# ============================================================================

def test_consecutive_returns_empty_when_only_one_week(tmp_db):
    """只一週資料 → weeks=2 永遠回空(符合上線初期預期)。"""
    _seed_stocks([{"stock_id": "2330", "name": "台積電"}])
    _seed_concentration([
        {"sid": "2330", "week_end": "2026-05-08", "h1k": 2000,
         "holders_pct": 0.10, "holders_delta_w": 50},
    ])
    rows = db.get_consecutive_shareholder_increases(weeks=2, limit=30)
    assert rows == []


def test_consecutive_picks_sids_with_2_weeks_positive(tmp_db):
    """連 2 週 delta > 0 入選;有一週 ≤ 0 不入選。"""
    _seed_stocks([
        {"stock_id": "2330", "name": "台積電"},
        {"stock_id": "2317", "name": "鴻海"},
        {"stock_id": "1101", "name": "台泥"},
    ])
    _seed_concentration([
        # 2330 連 2 週都正 → 入選
        {"sid": "2330", "week_end": "2026-05-01", "h1k": 1950,
         "holders_pct": 0.09, "holders_delta_w": 30},
        {"sid": "2330", "week_end": "2026-05-08", "h1k": 2000,
         "holders_pct": 0.10, "holders_delta_w": 50},
        # 2317 上週負、本週正 → 不入選
        {"sid": "2317", "week_end": "2026-05-01", "h1k": 1510,
         "holders_pct": 0.08, "holders_delta_w": -20},
        {"sid": "2317", "week_end": "2026-05-08", "h1k": 1530,
         "holders_pct": 0.08, "holders_delta_w": 20},
        # 1101 連 2 週都正 → 入選
        {"sid": "1101", "week_end": "2026-05-01", "h1k": 800,
         "holders_pct": 0.05, "holders_delta_w": 10},
        {"sid": "1101", "week_end": "2026-05-08", "h1k": 810,
         "holders_pct": 0.05, "holders_delta_w": 10},
    ])

    rows = db.get_consecutive_shareholder_increases(weeks=2, limit=30)
    assert {r["sid"] for r in rows} == {"2330", "1101"}, (
        f"應只 2330+1101 連 2 週正,實際 {[r['sid'] for r in rows]}"
    )
    # 回最新一週的 row(week_end=2026-05-08)
    by_sid = {r["sid"]: r for r in rows}
    assert by_sid["2330"]["week_end"] == "2026-05-08"
    assert by_sid["2330"]["holders_delta_w"] == 50
    assert by_sid["2330"]["name"] == "台積電"


def test_consecutive_picks_sids_with_3_weeks_positive(tmp_db):
    """連 3 週 delta > 0;只 2 連的不入(weeks=3)。"""
    _seed_stocks([
        {"stock_id": "2330", "name": "台積電"},
        {"stock_id": "2317", "name": "鴻海"},
    ])
    _seed_concentration([
        # 2330 連 3 週都正
        {"sid": "2330", "week_end": "2026-04-24", "h1k": 1900,
         "holders_pct": 0.09, "holders_delta_w": 20},
        {"sid": "2330", "week_end": "2026-05-01", "h1k": 1950,
         "holders_pct": 0.09, "holders_delta_w": 30},
        {"sid": "2330", "week_end": "2026-05-08", "h1k": 2000,
         "holders_pct": 0.10, "holders_delta_w": 50},
        # 2317 只 2 連
        {"sid": "2317", "week_end": "2026-04-24", "h1k": 1500,
         "holders_pct": 0.08, "holders_delta_w": -5},
        {"sid": "2317", "week_end": "2026-05-01", "h1k": 1510,
         "holders_pct": 0.08, "holders_delta_w": 10},
        {"sid": "2317", "week_end": "2026-05-08", "h1k": 1525,
         "holders_pct": 0.08, "holders_delta_w": 15},
    ])

    rows = db.get_consecutive_shareholder_increases(weeks=3, limit=30)
    assert {r["sid"] for r in rows} == {"2330"}


def test_consecutive_orders_by_delta_w_desc(tmp_db):
    """多檔入選時依 delta_w desc 排。"""
    _seed_stocks([
        {"stock_id": "2330", "name": "台積電"},
        {"stock_id": "1101", "name": "台泥"},
    ])
    _seed_concentration([
        {"sid": "2330", "week_end": "2026-05-01", "h1k": 1950,
         "holders_pct": 0.09, "holders_delta_w": 30},
        {"sid": "2330", "week_end": "2026-05-08", "h1k": 2000,
         "holders_pct": 0.10, "holders_delta_w": 50},
        {"sid": "1101", "week_end": "2026-05-01", "h1k": 800,
         "holders_pct": 0.05, "holders_delta_w": 10},
        {"sid": "1101", "week_end": "2026-05-08", "h1k": 815,
         "holders_pct": 0.05, "holders_delta_w": 15},
    ])
    rows = db.get_consecutive_shareholder_increases(weeks=2, limit=30)
    assert [r["sid"] for r in rows] == ["2330", "1101"]
