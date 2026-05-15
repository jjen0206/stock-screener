"""src/database.py 個股深度頁 6 helpers 單元測試。

涵蓋:
- get_stock_kline_with_indicators:OHLCV + MA20/60 + BB 計算、空表 fallback
- get_inst_history:三大法人 DESC 排序、覆蓋率不足空 list
- get_shareholder_history:千張戶歷史 ASC、空 list
- get_news_for_sid:近 N 日新聞、DESC 排序、cutoff 過濾
- get_pick_history_for_sid:daily_picks LEFT JOIN pick_outcomes(含待結算)
- get_shap_for_sid_latest:JSON 解碼 + None fallback

production schema 透過 db.init_db() 建立(跟 test_database.py 一致),
不用 mock,tmp_path 隔離。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import pytest

from src import config, database as db


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    return db_file


def _seed_prices(sid: str, n: int = 90, base: float = 100.0) -> None:
    """灌 n 天連續日線(從 today 倒數),價格遞增以便 MA / BB 算得出來。"""
    today = date.today()
    rows = []
    for i in range(n):
        d = (today - timedelta(days=n - 1 - i)).isoformat()
        close = base + i * 0.5
        rows.append({
            "stock_id": sid, "date": d,
            "open": close - 0.2, "high": close + 0.5,
            "low": close - 0.5, "close": close,
            "volume": 10000 + i * 100,
        })
    db.upsert_daily_prices(rows)


# === get_stock_kline_with_indicators ===


def test_kline_returns_ohlcv_plus_indicators(tmp_db):
    # 灌 150 天 → fetch_days = 60 + 80 = 140 全撈到,tail 60 前已有
    # 80 天歷史(夠 MA60 算到 tail 第一行)
    _seed_prices("2330", n=150)
    out = db.get_stock_kline_with_indicators("2330", days=60)
    assert isinstance(out, pd.DataFrame)
    assert len(out) == 60
    # 欄位齊
    for col in ("date", "open", "high", "low", "close", "volume",
                "ma20", "ma60", "bb_upper", "bb_mid", "bb_lower"):
        assert col in out.columns, f"missing col: {col}"
    # tail 60 內 MA60 應每行都算得出(歷史足)
    assert out["ma60"].notna().all(), "MA60 should be fully populated"
    # BB upper > mid > lower(遞增價格 + std > 0)
    last = out.iloc[-1]
    assert last["bb_upper"] > last["bb_mid"] > last["bb_lower"]


def test_kline_empty_returns_empty_df(tmp_db):
    out = db.get_stock_kline_with_indicators("9999", days=60)
    assert isinstance(out, pd.DataFrame)
    assert len(out) == 0


# === get_inst_history ===


def test_inst_history_desc_sorted(tmp_db):
    db.upsert_institutional([
        {"stock_id": "2330", "date": "2026-05-10",
         "foreign_buy_sell": 1000, "trust_buy_sell": 200,
         "dealer_buy_sell": -50, "total_buy_sell": 1150},
        {"stock_id": "2330", "date": "2026-05-12",
         "foreign_buy_sell": -300, "trust_buy_sell": 100,
         "dealer_buy_sell": 0, "total_buy_sell": -200},
        {"stock_id": "2330", "date": "2026-05-11",
         "foreign_buy_sell": 500, "trust_buy_sell": 0,
         "dealer_buy_sell": 0, "total_buy_sell": 500},
    ])
    out = db.get_inst_history("2330", days=7)
    assert len(out) == 3
    # DESC by date
    assert out[0]["date"] == "2026-05-12"
    assert out[1]["date"] == "2026-05-11"
    assert out[2]["date"] == "2026-05-10"
    assert out[0]["foreign_buy_sell"] == -300


def test_inst_history_empty_for_uncovered_sid(tmp_db):
    assert db.get_inst_history("9999") == []


# === get_shareholder_history ===


def test_shareholder_history_asc_for_chart(tmp_db):
    db.upsert_shareholder_concentration([
        {"sid": "2330", "week_end": "2026-04-25",
         "holders_1000up_count": 1000, "total_holders": 500000,
         "holders_pct": 0.002, "holders_delta_w": 10},
        {"sid": "2330", "week_end": "2026-05-09",
         "holders_1000up_count": 1020, "total_holders": 500000,
         "holders_pct": 0.002, "holders_delta_w": 15},
        {"sid": "2330", "week_end": "2026-05-02",
         "holders_1000up_count": 1005, "total_holders": 500000,
         "holders_pct": 0.002, "holders_delta_w": 5},
    ])
    out = db.get_shareholder_history("2330", weeks=12)
    # 函式 reverse 成 ASC 給 UI 畫 bar chart 直接吃
    assert [r["week_end"] for r in out] == [
        "2026-04-25", "2026-05-02", "2026-05-09",
    ]
    assert out[-1]["holders_1000up_count"] == 1020


def test_shareholder_history_empty(tmp_db):
    assert db.get_shareholder_history("9999") == []


# === get_news_for_sid ===


def test_news_for_sid_filters_by_cutoff_and_desc(tmp_db):
    today = date.today()
    rows_to_insert = [
        # 在 cutoff 內
        {
            "publish_date": (today - timedelta(days=1)).isoformat(),
            "publish_time": "143000",
            "subject": "新聞 A(昨日)",
        },
        {
            "publish_date": (today - timedelta(days=3)).isoformat(),
            "publish_time": "100000",
            "subject": "新聞 B(3 天前)",
        },
        # cutoff 外 — days=7 → 不入結果
        {
            "publish_date": (today - timedelta(days=30)).isoformat(),
            "publish_time": "090000",
            "subject": "新聞 C(30 天前,排除)",
        },
    ]
    with db.get_conn() as conn:
        for i, r in enumerate(rows_to_insert):
            conn.execute(
                "INSERT INTO news (sid, publish_date, publish_time, subject, "
                "url_hash, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("2330", r["publish_date"], r["publish_time"], r["subject"],
                 f"hash_{i}", "2026-05-15T00:00:00"),
            )

    out = db.get_news_for_sid("2330", days=7)
    assert len(out) == 2  # cutoff 排除「30 天前」
    # DESC by publish_date
    assert out[0]["subject"] == "新聞 A(昨日)"
    assert out[1]["subject"] == "新聞 B(3 天前)"


# === get_pick_history_for_sid ===


def test_pick_history_left_join_pending_outcome(tmp_db):
    # 兩筆 pick:一筆有 outcome,一筆 d5 未到(LEFT JOIN 留 None)
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO daily_picks (trade_date, universe, strategy, sid, "
            "score, rank, params_hash, payload, ml_prob, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-05-01", "TW", "volume_kd", "2330",
                 80.0, 1, "h1", "{}", 0.7, "2026-05-01T00:00:00"),
                ("2026-05-13", "TW", "macd_cross", "2330",
                 75.0, 2, "h1", "{}", 0.6, "2026-05-13T00:00:00"),
            ],
        )
    db.dump_pick_outcomes([
        {"pick_date": "2026-05-01", "sid": "2330",
         "strategy": "volume_kd", "entry_close": 600.0,
         "return_d1": 0.01, "return_d3": 0.02, "return_d5": 0.035,
         "return_d10": 0.05, "hit_target": 1, "stopped_out": 0,
         "evaluated_at": "2026-05-08T00:00:00"},
    ])

    out = db.get_pick_history_for_sid("2330")
    assert len(out) == 2
    # DESC by pick_date — 2026-05-13(無 outcome)在前
    assert out[0]["pick_date"] == "2026-05-13"
    assert out[0]["return_d5"] is None  # 待結算
    assert out[1]["pick_date"] == "2026-05-01"
    assert out[1]["return_d5"] == pytest.approx(0.035)
    assert out[1]["hit_target"] == 1


def test_pick_history_empty(tmp_db):
    assert db.get_pick_history_for_sid("9999") == []


# === get_shap_for_sid_latest ===


def test_shap_for_sid_latest_decodes_json(tmp_db):
    top = [
        {"feature": "ma20_dist", "value": 0.05,
         "contribution": 0.12, "contribution_pct": 0.30, "direction": "+"},
        {"feature": "rsi14", "value": 60.0,
         "contribution": 0.08, "contribution_pct": 0.20, "direction": "+"},
    ]
    db.save_shap_explanation("2026-05-01", "2330", "volume_kd", top)
    # 較新的覆蓋,應該回新的
    top2 = [{"feature": "macd_hist", "value": 1.2,
             "contribution": 0.15, "contribution_pct": 0.40,
             "direction": "+"}]
    db.save_shap_explanation("2026-05-13", "2330", "general", top2)

    out = db.get_shap_for_sid_latest("2330")
    assert out is not None
    assert out["pick_date"] == "2026-05-13"
    assert out["strategy"] == "general"
    assert out["top_features"][0]["feature"] == "macd_hist"


def test_shap_for_sid_latest_none_when_missing(tmp_db):
    assert db.get_shap_for_sid_latest("9999") is None
