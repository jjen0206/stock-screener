"""『高信心精選』三維交集 helper(db.get_strong_follower_premium)單元測試。

對應 _page_strong_follower Tab 4「✨ 高信心精選」設計:
  1. 三維命中:法人連買 ≥ N 日 + 千張戶週增 ≥ K + ML 過 per-strategy 門檻
  2. 缺一維 → 不入榜
  3. top_n 截斷
  4. composite_score desc 排序
  5. 空結果 graceful(無資料 / 表不存在)
  6. 無 ML cache → fallback 2D + reason_text 省略 ML

Test fixture 用 production schema(db.init_db),不自編 CREATE TABLE
(對齊既有 test_strong_follower_helpers.py pattern)。
"""
from __future__ import annotations

import pytest

from src import config, database as db


# ============================================================================
# fixtures
# ============================================================================

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """每 case 獨立 SQLite,init_db 建好 production schema 後 yield。"""
    db_file = tmp_path / "premium.db"
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
    """rows:stock_id / date / foreign / trust / dealer(net,正 = 買超)。"""
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


def _seed_ml_prob(
    sid: str,
    trade_date: str,
    ml_prob: float,
    strategy: str = "ma_alignment",
) -> None:
    """Seed daily_picks(strategy 預設 ma_alignment, threshold 0.55)。"""
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_picks
                (trade_date, universe, strategy, sid, score, rank,
                 params_hash, payload, ml_prob, computed_at)
            VALUES (?, 'pure_stock', ?, ?, 0.5, 1,
                    'default_v1', '{}', ?, '2026-05-09T00:00:00+00:00')
            """,
            (trade_date, strategy, sid, ml_prob),
        )


def _seed_3d_hit(sid: str, name: str, *, inst_net_base: int = 100,
                 delta_w: int = 100, ml: float | None = 0.80) -> None:
    """便利:塞「三維都過」一檔(法人 2 日全正、千張週增、ML 過 strategy 門檻)。"""
    _seed_stocks([{"stock_id": sid, "name": name}])
    _seed_institutional([
        {"stock_id": sid, "date": "2026-05-06",
         "foreign": inst_net_base, "trust": 50, "dealer": 30},
        {"stock_id": sid, "date": "2026-05-07",
         "foreign": inst_net_base, "trust": 50, "dealer": 30},
        {"stock_id": sid, "date": "2026-05-08",
         "foreign": inst_net_base * 2, "trust": 80, "dealer": 50},
    ])
    _seed_concentration([
        {"sid": sid, "week_end": "2026-05-08",
         "h1k": 2000, "holders_pct": 0.10, "holders_delta_w": delta_w},
    ])
    if ml is not None:
        _seed_ml_prob(sid, "2026-05-07", ml, strategy="ma_alignment")


# ============================================================================
# 三維交集命中
# ============================================================================

def test_premium_three_dim_hit_enters_top_picks(tmp_db):
    """三維都命中:法人連 3 日全正 + 千張戶週增 + ML 過 ma_alignment 0.55 → 入榜。"""
    _seed_3d_hit("2330", "台積電", inst_net_base=100, delta_w=100, ml=0.80)

    rows = db.get_strong_follower_premium(
        min_inst_days=3, min_delta_w=1, top_n=10,
    )
    assert len(rows) == 1, f"三維都過該入榜,實際 {rows}"
    r = rows[0]
    assert r["sid"] == "2330"
    assert r["name"] == "台積電"
    assert r["consensus_days"] == 3
    assert r["holders_delta_w"] == 100
    assert r["ml_prob"] == pytest.approx(0.80)
    assert r["composite_score"] is not None
    assert 0.0 < r["composite_score"] <= 1.0
    # reason_text 包含三個訊號
    assert "三大法人連買" in r["reason_text"]
    assert "千張戶週增" in r["reason_text"]
    assert "ML" in r["reason_text"]


def test_premium_drops_when_inst_consensus_missing(tmp_db):
    """法人共識不過(任一日有非正)→ 不入榜,即使其餘兩維都過。"""
    _seed_stocks([{"stock_id": "1101", "name": "台泥"}])
    _seed_institutional([
        # D-2 投信 0 → 不算共識
        {"stock_id": "1101", "date": "2026-05-06",
         "foreign": 100, "trust": 0, "dealer": 30},
        {"stock_id": "1101", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "1101", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
    ])
    _seed_concentration([
        {"sid": "1101", "week_end": "2026-05-08",
         "h1k": 2000, "holders_pct": 0.10, "holders_delta_w": 50},
    ])
    _seed_ml_prob("1101", "2026-05-07", 0.80)

    rows = db.get_strong_follower_premium(min_inst_days=3, top_n=10)
    assert rows == [], "法人共識缺一日,該擋"


def test_premium_drops_when_holders_not_growing(tmp_db):
    """千張戶 delta_w 不足(< min_delta_w)→ 不入榜。"""
    _seed_3d_hit("2317", "鴻海", delta_w=0, ml=0.80)
    rows = db.get_strong_follower_premium(
        min_inst_days=3, min_delta_w=1, top_n=10,
    )
    assert rows == [], "千張戶持平,該擋"


def test_premium_drops_when_ml_below_threshold(tmp_db):
    """ML score 過低(< per-strategy threshold)→ 不入榜(3D 模式)。"""
    # ma_alignment threshold = 0.55, ML = 0.40 該擋。
    # 但 DB 有其他 sid 帶 ML 過門檻 → has_ml_cache=True → 3D mode active
    _seed_3d_hit("2454", "聯發科", delta_w=50, ml=0.40)
    # 另一檔 ML 過門檻撐起 has_ml_cache（但這檔法人不過,不會入榜）:
    _seed_ml_prob("9999", "2026-05-07", 0.90)

    rows = db.get_strong_follower_premium(
        min_inst_days=3, min_delta_w=1, top_n=10,
    )
    assert rows == [], "ML 過低,該擋(3D 模式 ML 是 INNER JOIN 必須過門檻)"


def test_premium_top_n_caps_results(tmp_db):
    """top_n 截斷生效。"""
    # 塞 5 檔三維都命中,top_n=3 應截為 3
    for i in range(5):
        sid = f"99{i:02d}"
        _seed_3d_hit(
            sid, f"X{i}", inst_net_base=(i + 1) * 100, delta_w=(i + 1) * 10,
            ml=0.70 + i * 0.02,
        )
    rows = db.get_strong_follower_premium(
        min_inst_days=3, min_delta_w=1, top_n=3,
    )
    assert len(rows) == 3


def test_premium_sorts_by_composite_desc(tmp_db):
    """≥ 2 檔交集時,composite_score desc 排序;最高分排第一。"""
    # 三檔都過,A 最強(三維最高)、C 最弱
    _seed_3d_hit("AAAA", "A強", inst_net_base=500, delta_w=500, ml=0.95)
    _seed_3d_hit("BBBB", "B中", inst_net_base=200, delta_w=200, ml=0.75)
    _seed_3d_hit("CCCC", "C弱", inst_net_base=100, delta_w=10, ml=0.60)

    rows = db.get_strong_follower_premium(
        min_inst_days=3, min_delta_w=1, top_n=10,
    )
    sids = [r["sid"] for r in rows]
    assert sids[0] == "AAAA", "A 三維都最強,該排第一"
    assert sids[-1] == "CCCC", "C 三維都最弱,該排最後"
    # composite_score 嚴格遞減(或相等)
    scores = [r["composite_score"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_premium_empty_when_no_data(tmp_db):
    """三個來源表都空 → 空 list 不炸。"""
    assert db.get_strong_follower_premium(top_n=10) == []


def test_premium_fallback_to_2d_when_no_ml_cache(tmp_db):
    """DB 完全沒 ML cache → fallback 2D,reason_text 省略 ML 段。"""
    # 三維中只塞前 2 維,完全不塞 daily_picks.ml_prob
    _seed_stocks([{"stock_id": "2330", "name": "台積電"}])
    _seed_institutional([
        {"stock_id": "2330", "date": "2026-05-06",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "2330", "date": "2026-05-07",
         "foreign": 100, "trust": 50, "dealer": 30},
        {"stock_id": "2330", "date": "2026-05-08",
         "foreign": 200, "trust": 80, "dealer": 50},
    ])
    _seed_concentration([
        {"sid": "2330", "week_end": "2026-05-08",
         "h1k": 2000, "holders_pct": 0.10, "holders_delta_w": 50},
    ])

    rows = db.get_strong_follower_premium(
        min_inst_days=3, min_delta_w=1, top_n=10,
    )
    assert len(rows) == 1, "前 2 維過 + 無 ML cache,該 fallback 入榜"
    r = rows[0]
    assert r["sid"] == "2330"
    assert r["ml_prob"] is None, "fallback 模式 ML 應為 NULL"
    # reason_text 含前兩段、不含 ML
    assert "三大法人連買" in r["reason_text"]
    assert "千張戶週增" in r["reason_text"]
    assert "ML" not in r["reason_text"]


def test_premium_returns_meta_fields(tmp_db):
    """回傳 dict 帶完整 schema(對齊 UI 需要欄位)。"""
    _seed_3d_hit("2330", "台積電", inst_net_base=100, delta_w=80, ml=0.77)
    _seed_prices([
        {"stock_id": "2330", "date": "2026-05-08", "close": 1010.0},
    ])

    rows = db.get_strong_follower_premium(
        min_inst_days=3, min_delta_w=1, top_n=10,
    )
    assert len(rows) == 1
    r = rows[0]
    # schema 必含欄位(_render_table 用)
    expected_keys = {
        "sid", "name", "close", "consensus_days", "inst_net_total",
        "holders_delta_w", "holders_1000up_count", "ml_prob",
        "composite_score", "last_date", "reason_text",
    }
    assert expected_keys.issubset(r.keys())
    assert r["close"] == 1010.0
    assert r["holders_1000up_count"] == 2000
