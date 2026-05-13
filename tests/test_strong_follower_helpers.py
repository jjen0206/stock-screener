"""「強者跟蹤」綜合頁 helpers 單元測試
(db.get_top_inst_consensus / db.get_strong_follower_composite)

塞假資料測:
  1. 法人連買共識:三家(外/投/自)同時 net > 0 連續 N 日才入榜
  2. 連 N-1 日不入、連 N 日入、其中一家負一日就被擋
  3. JOIN stocks 帶名稱、LEFT JOIN daily_prices 帶 close
  4. LEFT JOIN daily_picks 帶 ML 分數
  5. inst_net_total 排序正確(大 net 加碼者優先)
  6. 綜合排行交集 = 法人共識 ∩ 千張戶 delta_w > 0
  7. composite_score normalize 後在 [0, 2] 區間
  8. 空表 / 沒資料 → 回空 list 不炸
"""
from __future__ import annotations

import pytest

from src import config, database as db


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "strong_follower.db"
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


def _seed_institutional(rows: list[dict]) -> None:
    """rows 必須帶 stock_id / date / foreign / trust / dealer(net 值,正 = 買超)。"""
    db.upsert_institutional([
        {
            "stock_id": r["stock_id"],
            "date": r["date"],
            "foreign_buy_sell": r.get("foreign", 0),
            "trust_buy_sell": r.get("trust", 0),
            "dealer_buy_sell": r.get("dealer", 0),
            "total_buy_sell": (
                r.get("foreign", 0) + r.get("trust", 0) + r.get("dealer", 0)
            ),
        }
        for r in rows
    ])


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
# get_top_inst_consensus
# ============================================================================

def test_inst_consensus_requires_all_three_positive_n_days(tmp_db):
    """N=2 時:連 2 日三家都 > 0 才入榜;任何一家負一日就擋。"""
    _seed_stocks([
        {"stock_id": "2330", "name": "台積電"},
        {"stock_id": "2317", "name": "鴻海"},
        {"stock_id": "1101", "name": "台泥"},
        {"stock_id": "2454", "name": "聯發科"},
    ])
    _seed_institutional([
        # 2330 ✅ 連 2 日三家全正
        {"stock_id": "2330", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "2330", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
        # 2317 ❌ D-1 投信負,D 全正(只 1 日共識)
        {"stock_id": "2317", "date": "2026-05-07",
         "foreign": 100, "trust": -10, "dealer": 30},
        {"stock_id": "2317", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
        # 1101 ❌ 兩日都正但自營商 0(NOT > 0)
        {"stock_id": "1101", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 0},
        {"stock_id": "1101", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
        # 2454 ✅ 連 2 日三家全正 (較小 net)
        {"stock_id": "2454", "date": "2026-05-07",
         "foreign": 10, "trust": 5, "dealer": 3},
        {"stock_id": "2454", "date": "2026-05-08",
         "foreign": 20, "trust": 8, "dealer": 5},
    ])

    rows = db.get_top_inst_consensus(min_days=2, limit=10)
    sids = [r["sid"] for r in rows]
    assert "2330" in sids and "2454" in sids, "2330 / 2454 應入榜"
    assert "2317" not in sids, "2317 投信前一日負,擋"
    assert "1101" not in sids, "1101 自營商 = 0 不算共識,擋"

    # 排序:net_total 大者在前。2330 = 510, 2454 = 51
    assert sids[0] == "2330"
    assert sids[1] == "2454"


def test_inst_consensus_returns_meta_fields(tmp_db):
    """回傳 dict 帶完整 schema:sid/name/close/consensus_days/inst_net_total/
    last_date/ml_prob。"""
    _seed_stocks([{"stock_id": "2330", "name": "台積電"}])
    _seed_institutional([
        {"stock_id": "2330", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "2330", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
    ])
    _seed_prices([
        {"stock_id": "2330", "date": "2026-05-08", "close": 1010.0},
    ])
    _seed_ml_prob("2330", "2026-05-07", 0.77)

    rows = db.get_top_inst_consensus(min_days=2, limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["sid"] == "2330"
    assert r["name"] == "台積電"
    assert r["close"] == 1010.0
    assert r["consensus_days"] == 2
    # inst_net_total = sum of 6 numbers = 510
    assert r["inst_net_total"] == 510
    assert r["last_date"] == "2026-05-08"
    assert r["ml_prob"] == pytest.approx(0.77)


def test_inst_consensus_left_join_handles_missing_meta(tmp_db):
    """institutional 有 sid 但 stocks / daily_prices / daily_picks 沒對應 → 仍出列。"""
    # 故意不 seed stocks / prices / ml,只給 institutional
    _seed_institutional([
        {"stock_id": "9999", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "9999", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
    ])
    rows = db.get_top_inst_consensus(min_days=2, limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["sid"] == "9999"
    assert r["name"] is None
    assert r["close"] is None
    assert r["ml_prob"] is None


def test_inst_consensus_picks_latest_n_days_not_arbitrary(tmp_db):
    """每 sid 取最新 N 日的 institutional,舊資料有負不影響(只看最新 N 日)。"""
    _seed_stocks([{"stock_id": "2330", "name": "台積電"}])
    _seed_institutional([
        # 舊資料,有負(應該被忽略,不影響最新 N 日判斷)
        {"stock_id": "2330", "date": "2026-04-01",
         "foreign": -100, "trust": -50, "dealer": -30},
        {"stock_id": "2330", "date": "2026-04-02",
         "foreign": -100, "trust": -50, "dealer": -30},
        # 最新 2 日全正(min_days=2 該入榜)
        {"stock_id": "2330", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "2330", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
    ])
    rows = db.get_top_inst_consensus(min_days=2, limit=10)
    assert len(rows) == 1, "只看最新 2 日,舊負資料不該擋"


def test_inst_consensus_min_days_3_strict(tmp_db):
    """min_days=3 時,只有最新 2 日共識的 sid 不入榜。"""
    _seed_stocks([{"stock_id": "2330", "name": "台積電"}])
    _seed_institutional([
        {"stock_id": "2330", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "2330", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
    ])
    # min_days=3 但只有 2 日 → 該空
    rows = db.get_top_inst_consensus(min_days=3, limit=10)
    assert rows == []


def test_inst_consensus_empty_when_no_data(tmp_db):
    """institutional 表空 → 空 list 不炸。"""
    assert db.get_top_inst_consensus(min_days=2, limit=10) == []


def test_inst_consensus_respects_limit(tmp_db):
    """limit 截斷生效。"""
    _seed_stocks([
        {"stock_id": f"99{i:02d}", "name": f"X{i}"} for i in range(5)
    ])
    _seed_institutional([
        {"stock_id": f"99{i:02d}", "date": "2026-05-07",
         "foreign": (i + 1) * 100, "trust": (i + 1) * 50,
         "dealer": (i + 1) * 30}
        for i in range(5)
    ] + [
        {"stock_id": f"99{i:02d}", "date": "2026-05-08",
         "foreign": (i + 1) * 110, "trust": (i + 1) * 55,
         "dealer": (i + 1) * 35}
        for i in range(5)
    ])
    rows = db.get_top_inst_consensus(min_days=2, limit=3)
    assert len(rows) == 3


# ============================================================================
# get_strong_follower_composite
# ============================================================================

def test_composite_intersects_inst_consensus_and_holders_growth(tmp_db):
    """交集 = 法人共識 ∩ 最新一週 holders_delta_w > 0。"""
    _seed_stocks([
        {"stock_id": "2330", "name": "台積電"},
        {"stock_id": "2317", "name": "鴻海"},
        {"stock_id": "1101", "name": "台泥"},
    ])
    _seed_institutional([
        # 2330 ✅ 法人共識
        {"stock_id": "2330", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "2330", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
        # 2317 ✅ 法人共識
        {"stock_id": "2317", "date": "2026-05-07",
         "foreign": 50, "trust": 20, "dealer": 10},
        {"stock_id": "2317", "date": "2026-05-08",
         "foreign": 80, "trust": 30, "dealer": 20},
        # 1101 ❌ 法人不共識(自營商負)
        {"stock_id": "1101", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": -10},
        {"stock_id": "1101", "date": "2026-05-08",
         "foreign": 100, "trust": 50, "dealer": -10},
    ])
    _seed_concentration([
        # 2330 千張戶有增 ✅
        {"sid": "2330", "week_end": "2026-05-08",
         "h1k": 2000, "holders_pct": 0.10, "holders_delta_w": 100},
        # 2317 千張戶持平(delta=0)→ 該擋(NOT > 0)
        {"sid": "2317", "week_end": "2026-05-08",
         "h1k": 1500, "holders_pct": 0.08, "holders_delta_w": 0},
        # 1101 千張戶增加但法人不共識 → 該擋
        {"sid": "1101", "week_end": "2026-05-08",
         "h1k": 800, "holders_pct": 0.05, "holders_delta_w": 50},
    ])

    rows = db.get_strong_follower_composite(min_inst_days=2, limit=10)
    sids = [r["sid"] for r in rows]
    assert sids == ["2330"], (
        f"只 2330 同時滿足法人共識 + 千張戶增加,實際 {sids}"
    )
    r = rows[0]
    assert r["name"] == "台積電"
    assert r["inst_net_total"] == 510
    assert r["holders_delta_w"] == 100
    assert r["consensus_days"] == 2
    # composite_score 在 [0, 2] 區間(只 1 筆時兩個 rank 都 = 1/1 = 1,加總 = 2)
    assert r["composite_score"] is not None
    assert 0.0 < r["composite_score"] <= 2.0


def test_composite_scores_multiple_intersected_sids(tmp_db):
    """≥ 2 檔交集時,composite_score 在 (0, 2] 區間,且總和 desc 排序合理。"""
    _seed_stocks([
        {"stock_id": "2330", "name": "台積電"},
        {"stock_id": "2317", "name": "鴻海"},
        {"stock_id": "2454", "name": "聯發科"},
    ])
    _seed_institutional([
        # 三檔法人共識,net 總額不同
        {"stock_id": "2330", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "2330", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},   # net_total = 510
        {"stock_id": "2317", "date": "2026-05-07",
         "foreign": 50, "trust": 20, "dealer": 10},
        {"stock_id": "2317", "date": "2026-05-08",
         "foreign": 80, "trust": 30, "dealer": 20},   # net_total = 210
        {"stock_id": "2454", "date": "2026-05-07",
         "foreign": 10, "trust": 5, "dealer": 3},
        {"stock_id": "2454", "date": "2026-05-08",
         "foreign": 20, "trust": 8, "dealer": 5},     # net_total = 51
    ])
    _seed_concentration([
        # 三檔千張戶都正,delta 不同
        {"sid": "2330", "week_end": "2026-05-08",
         "h1k": 2000, "holders_pct": 0.10, "holders_delta_w": 50},
        {"sid": "2317", "week_end": "2026-05-08",
         "h1k": 1500, "holders_pct": 0.08, "holders_delta_w": 300},
        {"sid": "2454", "week_end": "2026-05-08",
         "h1k": 1200, "holders_pct": 0.07, "holders_delta_w": 10},
    ])

    rows = db.get_strong_follower_composite(min_inst_days=2, limit=10)
    sids = [r["sid"] for r in rows]
    assert set(sids) == {"2330", "2317", "2454"}
    # composite_score 都在 (0, 2] 區間
    for r in rows:
        assert 0.0 < r["composite_score"] <= 2.0
    # 2317(法人 rank=2, 千張 rank=3) vs 2330(法人 rank=3, 千張 rank=2):平手
    # 2454 兩個都最小,該排最後
    assert sids[-1] == "2454"


def test_composite_empty_when_no_intersection(tmp_db):
    """法人共識集和千張戶增加集無交集 → 空 list。"""
    _seed_stocks([{"stock_id": "2330", "name": "台積電"}])
    _seed_institutional([
        # 法人共識
        {"stock_id": "2330", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "2330", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
    ])
    # 千張戶資料 delta = 負 → 不交集
    _seed_concentration([
        {"sid": "2330", "week_end": "2026-05-08",
         "h1k": 2000, "holders_pct": 0.10, "holders_delta_w": -10},
    ])
    assert db.get_strong_follower_composite(min_inst_days=2, limit=10) == []


def test_composite_empty_when_no_tables(tmp_db):
    """兩個來源表都空 → 空 list 不炸。"""
    assert db.get_strong_follower_composite(min_inst_days=2, limit=10) == []
