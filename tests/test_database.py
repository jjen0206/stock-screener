"""src/database.py 單元測試。

每個測試用 tmp_path 建立獨立 DB,不污染 data/cache.db。
透過 monkeypatch 改 src.config.DATABASE_PATH 來切換。
"""
from __future__ import annotations

import sqlite3

import pytest

from src import config, database as db


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """每個測試一份乾淨 DB。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


# === init_db ===

def test_init_db_creates_all_tables(tmp_db):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert {
        "stocks",
        "daily_prices",
        "institutional",
        "financials",
        "sync_log",
    }.issubset(names)


def test_init_db_idempotent(tmp_db):
    """重複呼叫 init_db 不會出錯,且資料保留。"""
    db.upsert_stocks([{"stock_id": "2330", "name": "台積電"}])
    db.init_db()
    db.init_db()
    with db.get_conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM stocks").fetchone()["c"]
    assert cnt == 1


# === upsert_stocks ===

def test_upsert_stocks_insert_and_update(tmp_db):
    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "industry": "半導體"}])
    db.upsert_stocks([{"stock_id": "2330", "name": "TSMC", "industry": "Semiconductor"}])
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM stocks WHERE stock_id='2330'").fetchone()
    assert row["name"] == "TSMC"
    assert row["industry"] == "Semiconductor"
    assert row["market"] == "TW"


def test_upsert_stocks_empty_returns_zero(tmp_db):
    assert db.upsert_stocks([]) == 0


# === upsert_daily_prices ===

def test_upsert_daily_prices_conflict_updates(tmp_db):
    base = {
        "stock_id": "2330", "date": "2024-01-02",
        "open": 593.0, "high": 595.0, "low": 587.0, "close": 593.0,
        "volume": 26841832, "trading_money": 1.58e10,
        "trading_turnover": 30821, "spread": 4.0,
    }
    db.upsert_daily_prices([base])
    # 同 (stock_id, date) 改 close,應該覆蓋
    updated = {**base, "close": 600.0}
    db.upsert_daily_prices([updated])
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM daily_prices WHERE stock_id='2330' AND date='2024-01-02'"
        ).fetchone()
    assert row["close"] == 600.0
    # PK 不應重複
    with db.get_conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM daily_prices").fetchone()["c"]
    assert cnt == 1


# === upsert_institutional ===

def test_upsert_institutional(tmp_db):
    db.upsert_institutional([
        {
            "stock_id": "2330", "date": "2024-01-02",
            "foreign_buy_sell": 100000, "trust_buy_sell": 5000,
            "dealer_buy_sell": -2000, "total_buy_sell": 103000,
        }
    ])
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM institutional WHERE stock_id='2330' AND date='2024-01-02'"
        ).fetchone()
    assert row["foreign_buy_sell"] == 100000
    assert row["dealer_buy_sell"] == -2000
    assert row["total_buy_sell"] == 103000


# === upsert_financials ===

def test_upsert_financials_monthly_and_quarterly(tmp_db):
    db.upsert_financials([
        {
            "stock_id": "2330", "period_type": "monthly_revenue",
            "period": "2024-01", "revenue": 1.95e11, "revenue_yoy": 0.12,
        },
        {
            "stock_id": "2330", "period_type": "quarterly",
            "period": "2024-Q1", "eps": 8.5, "roe": 27.3,
        },
    ])
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM financials WHERE stock_id='2330' ORDER BY period_type, period"
        ).fetchall()
    assert len(rows) == 2
    by_type = {r["period_type"]: r for r in rows}
    assert by_type["monthly_revenue"]["revenue"] == pytest.approx(1.95e11)
    assert by_type["quarterly"]["eps"] == pytest.approx(8.5)
    assert by_type["quarterly"]["roe"] == pytest.approx(27.3)


def test_upsert_financials_coalesce(tmp_db):
    """同一個 (stock_id, period_type, period) 多次 upsert,空值不覆蓋既有值。"""
    db.upsert_financials([
        {"stock_id": "2330", "period_type": "quarterly", "period": "2024-Q1",
         "eps": 8.5, "roe": 27.3},
    ])
    # 第二次只有 eps,roe 留空 → 不該把舊的 27.3 蓋掉
    db.upsert_financials([
        {"stock_id": "2330", "period_type": "quarterly", "period": "2024-Q1",
         "eps": 9.0},
    ])
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM financials WHERE stock_id='2330' AND period='2024-Q1'"
        ).fetchone()
    assert row["eps"] == pytest.approx(9.0)
    assert row["roe"] == pytest.approx(27.3)


# === sync_log ===

def test_sync_log_initial_state(tmp_db):
    assert db.get_synced_range("2330", "TaiwanStockPrice") is None


def test_sync_log_update_and_extend(tmp_db):
    db.update_synced_range("2330", "TaiwanStockPrice", "2024-01-01", "2024-01-31")
    assert db.get_synced_range("2330", "TaiwanStockPrice") == (
        "2024-01-01", "2024-01-31"
    )
    # 擴大尾端
    db.update_synced_range("2330", "TaiwanStockPrice", "2024-01-15", "2024-02-15")
    assert db.get_synced_range("2330", "TaiwanStockPrice") == (
        "2024-01-01", "2024-02-15"
    )
    # 擴大頭端
    db.update_synced_range("2330", "TaiwanStockPrice", "2023-12-01", "2024-01-10")
    assert db.get_synced_range("2330", "TaiwanStockPrice") == (
        "2023-12-01", "2024-02-15"
    )


def test_sync_log_isolated_per_stock_and_dataset(tmp_db):
    db.update_synced_range("2330", "TaiwanStockPrice", "2024-01-01", "2024-01-31")
    db.update_synced_range("2330", "TaiwanStockMonthRevenue", "2023-01-01", "2023-12-31")
    db.update_synced_range("2454", "TaiwanStockPrice", "2024-02-01", "2024-02-29")
    assert db.get_synced_range("2330", "TaiwanStockPrice") == (
        "2024-01-01", "2024-01-31"
    )
    assert db.get_synced_range("2330", "TaiwanStockMonthRevenue") == (
        "2023-01-01", "2023-12-31"
    )
    assert db.get_synced_range("2454", "TaiwanStockPrice") == (
        "2024-02-01", "2024-02-29"
    )


# === get_conn 行為 ===

def test_get_conn_creates_parent_dir(monkeypatch, tmp_path):
    nested = tmp_path / "sub" / "deeper" / "x.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(nested))
    db.init_db()
    assert nested.exists()


def test_get_conn_returns_row_factory(tmp_db):
    """確認 row_factory 為 sqlite3.Row(可用 dict 風格存取)。"""
    db.upsert_stocks([{"stock_id": "2330", "name": "台積電"}])
    with db.get_conn() as conn:
        assert conn.row_factory is sqlite3.Row
        row = conn.execute("SELECT * FROM stocks LIMIT 1").fetchone()
        assert row["stock_id"] == "2330"


# === cache_health_summary / stocks_with_min_history ===

def _seed_history(sids_with_days: dict[str, int]) -> None:
    """灌入指定股票的 daily_prices,各 N 天(date 從今往回推)。"""
    from datetime import date, timedelta
    today = date(2026, 4, 28)
    rows = []
    for sid, n in sids_with_days.items():
        for i in range(n):
            d = (today - timedelta(days=i)).isoformat()
            rows.append({
                "stock_id": sid, "date": d,
                "open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0,
                "volume": 1000,
                "trading_money": None, "trading_turnover": None, "spread": 0.0,
            })
    db.upsert_daily_prices(rows)


def test_cache_health_summary_buckets(tmp_db):
    """正確按天數分桶。"""
    db.upsert_stocks([
        {"stock_id": s, "name": "X", "market": "TW"}
        for s in ["A", "B", "C", "D", "E"]
    ])
    _seed_history({
        "A": 5,    # <14
        "B": 13,   # <14
        "C": 18,   # 14-19
        "D": 30,   # 20-59
        "E": 90,   # 60+
    })
    h = db.cache_health_summary()
    assert h["total_stocks"] == 5
    assert h["with_prices"] == 5
    assert h["buckets"] == {"<14": 2, "14-19": 1, "20-59": 1, "60+": 1}


def test_cache_health_summary_empty(tmp_db):
    """空 DB 不爆 — 全桶歸零。"""
    h = db.cache_health_summary()
    assert h["total_stocks"] == 0
    assert h["with_prices"] == 0
    assert h["buckets"] == {"<14": 0, "14-19": 0, "20-59": 0, "60+": 0}


def test_cache_health_summary_only_other_market(tmp_db):
    """non-TW 不算入 total_stocks(但仍計入 with_prices)。"""
    db.upsert_stocks([
        {"stock_id": "AAPL", "name": "Apple", "market": "US"},
    ])
    _seed_history({"AAPL": 5})
    h = db.cache_health_summary()
    assert h["total_stocks"] == 0  # market='TW' 過濾
    assert h["with_prices"] == 1   # daily_prices 不分 market


def test_stocks_with_min_history_filter(tmp_db):
    """只回傳達到天數門檻的個股。"""
    db.upsert_stocks([
        {"stock_id": s, "name": "X", "market": "TW"}
        for s in ["A", "B", "C"]
    ])
    _seed_history({"A": 30, "B": 60, "C": 120})
    assert sorted(db.stocks_with_min_history(60)) == ["B", "C"]
    assert sorted(db.stocks_with_min_history(20)) == ["A", "B", "C"]
    assert db.stocks_with_min_history(200) == []


def test_stocks_with_min_history_default_60(tmp_db):
    """預設 60 天門檻。"""
    db.upsert_stocks([
        {"stock_id": "A", "name": "X", "market": "TW"},
        {"stock_id": "B", "name": "Y", "market": "TW"},
    ])
    _seed_history({"A": 59, "B": 60})
    assert db.stocks_with_min_history() == ["B"]


# === trades / P&L ===

def test_trades_table_created(tmp_db):
    """init_db 包含 trades 表 + 對應 index。"""
    with db.get_conn() as conn:
        names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        idx_names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "trades" in names
    assert "idx_trades_stock_date" in idx_names


def test_add_trade_returns_id_and_persists(tmp_db):
    """add_trade INSERT 後,get_trades 拿得到。"""
    tid = db.add_trade("2330", "buy", 600.0, 2, "2026-04-01", note="第一筆")
    assert tid > 0
    trades = db.get_trades("2330")
    assert len(trades) == 1
    t = trades[0]
    assert t["stock_id"] == "2330"
    assert t["direction"] == "buy"
    assert t["price"] == 600.0
    assert t["quantity"] == 2
    assert t["trade_date"] == "2026-04-01"
    assert t["note"] == "第一筆"
    assert t["created_at"]  # 自動填


def test_add_trade_invalid_inputs_raise(tmp_db):
    """direction / quantity / price 不合法 → ValueError。"""
    with pytest.raises(ValueError):
        db.add_trade("2330", "short", 600.0, 1, "2026-04-01")
    with pytest.raises(ValueError):
        db.add_trade("2330", "buy", 600.0, 0, "2026-04-01")
    with pytest.raises(ValueError):
        db.add_trade("2330", "buy", -1.0, 1, "2026-04-01")


def test_delete_trade(tmp_db):
    """delete_trade 刪掉指定 id;不存在 id 回 False。"""
    tid = db.add_trade("2330", "buy", 600.0, 1, "2026-04-01")
    assert db.delete_trade(tid) is True
    assert db.get_trades("2330") == []
    assert db.delete_trade(99999) is False


def test_get_position_weighted_avg_cost(tmp_db):
    """混合 buy 算 weighted average cost。
    買 1 張 @ 600 + 買 2 張 @ 700 → avg = (1×600 + 2×700) / 3 = 666.67
    """
    db.add_trade("2330", "buy", 600.0, 1, "2026-04-01")
    db.add_trade("2330", "buy", 700.0, 2, "2026-04-02")

    pos = db.get_position("2330")
    assert pos["quantity"] == 3
    assert abs(pos["avg_cost"] - (1 * 600 + 2 * 700) / 3) < 1e-6
    assert pos["realized_pnl"] == 0.0
    assert pos["total_buy_amount"] == 600 + 1400


def test_get_position_realized_pnl_after_sell(tmp_db):
    """買 2 張 @ 600,賣 1 張 @ 700 → realized = (700 - 600) × 1 = 100。
    剩餘 1 張仍 avg_cost=600(賣不影響 avg)。
    """
    db.add_trade("2330", "buy", 600.0, 2, "2026-04-01")
    db.add_trade("2330", "sell", 700.0, 1, "2026-04-05")

    pos = db.get_position("2330")
    assert pos["quantity"] == 1
    assert abs(pos["avg_cost"] - 600.0) < 1e-6
    assert abs(pos["realized_pnl"] - 100.0) < 1e-6
    assert pos["total_sell_amount"] == 700.0


def test_get_unrealized_pnl(tmp_db):
    """avg_cost=600,qty=2,current_price=650 → unrealized = (650-600)×2 = 100。"""
    db.add_trade("2330", "buy", 600.0, 2, "2026-04-01")
    assert abs(db.get_unrealized_pnl("2330", 650.0) - 100.0) < 1e-6


def test_get_unrealized_pnl_no_position_zero(tmp_db):
    """沒倉位(qty=0)→ 未實現 0,不論 current_price。"""
    db.add_trade("2330", "buy", 600.0, 1, "2026-04-01")
    db.add_trade("2330", "sell", 650.0, 1, "2026-04-05")
    # 全清倉
    pos = db.get_position("2330")
    assert pos["quantity"] == 0
    assert db.get_unrealized_pnl("2330", 700.0) == 0.0


def test_get_trades_filter_by_stock(tmp_db):
    """get_trades 支援 stock_id 過濾;None → 全部。"""
    db.add_trade("2330", "buy", 600.0, 1, "2026-04-01")
    db.add_trade("2454", "buy", 1000.0, 1, "2026-04-02")
    db.add_trade("2330", "sell", 650.0, 1, "2026-04-03")

    assert len(db.get_trades("2330")) == 2
    assert len(db.get_trades("2454")) == 1
    assert len(db.get_trades()) == 3


# === daily_picks(precompute 預跑 strategies)===

def _fake_agg_2sids() -> dict[str, dict]:
    """構造跟 run_all_strategies 同 schema 的假 agg。"""
    return {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD", "多頭排列"],
            "details": {
                "volume_kd": {
                    "stock_id": "2330", "name": "台積電",
                    "close": 600.0, "atr14": 12.0, "matched_at": "2026-05-04",
                },
                "ma_alignment": {
                    "stock_id": "2330", "name": "台積電",
                    "close": 600.0, "ma5": 595.0, "ma20": 580.0, "ma60": 540.0,
                },
            },
        },
        "2317": {
            "name": "鴻海",
            "signals": ["量價KD"],
            "details": {
                "volume_kd": {
                    "stock_id": "2317", "name": "鴻海",
                    "close": 200.0, "atr14": 5.0, "matched_at": "2026-05-04",
                },
            },
        },
    }


def test_daily_picks_schema_exists(tmp_db):
    """init_db 後 daily_picks 表 + index 存在,欄位齊全。"""
    with db.get_conn() as conn:
        # 表存在
        names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "daily_picks" in names

        # 欄位齊全
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(daily_picks)"
            ).fetchall()
        }
        assert cols == {
            "trade_date", "universe", "strategy", "sid",
            "score", "rank", "params_hash", "payload",
            "ml_prob",  # Stage 1 Part 2 加
            "computed_at",
        }

        # 查詢 index 存在
        idx_names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_daily_picks_lookup" in idx_names


def test_dump_daily_picks_then_load_roundtrip(tmp_db):
    """dump → load 還原成同 schema agg dict,signals 用中文標籤。"""
    fake_agg = _fake_agg_2sids()
    n = db.dump_daily_picks("2026-05-04", "pure_stock", fake_agg)
    assert n == 3  # 2330 兩個策略 + 2317 一個

    loaded = db.load_daily_picks("2026-05-04", "pure_stock")
    assert loaded is not None
    assert sorted(loaded.keys()) == ["2317", "2330"]

    # 2330 兩個策略命中 + signals 是中文標籤
    assert sorted(loaded["2330"]["details"].keys()) == ["ma_alignment", "volume_kd"]
    assert "量價KD" in loaded["2330"]["signals"]
    assert "多頭排列" in loaded["2330"]["signals"]
    assert loaded["2330"]["name"] == "台積電"

    # 2317 只一個策略
    assert sorted(loaded["2317"]["details"].keys()) == ["volume_kd"]
    assert loaded["2317"]["signals"] == ["量價KD"]

    # payload 還原成 dict + 數值
    payload = loaded["2330"]["details"]["volume_kd"]
    assert payload["stock_id"] == "2330"
    assert payload["close"] == 600.0
    assert payload["atr14"] == 12.0


def test_load_daily_picks_cache_miss_returns_none(tmp_db):
    """daily_picks 沒這 (date, universe, params_hash) → 回 None(讓 caller fallback)。"""
    assert db.load_daily_picks("2026-05-04", "pure_stock") is None
    # 灌一筆別 universe 的,查我要的 universe 仍 miss
    db.dump_daily_picks("2026-05-04", "top_50", _fake_agg_2sids())
    assert db.load_daily_picks("2026-05-04", "pure_stock") is None
    # 不同 params_hash 也 miss
    assert db.load_daily_picks("2026-05-04", "top_50", "custom_v1") is None


def test_dump_daily_picks_on_conflict_replaces(tmp_db):
    """同 (date, universe, strategy, sid, params_hash) 重新 dump → 用新值覆蓋。"""
    db.dump_daily_picks("2026-05-04", "pure_stock", _fake_agg_2sids())
    # 改 close 重 dump
    new_agg = _fake_agg_2sids()
    new_agg["2330"]["details"]["volume_kd"]["close"] = 999.99
    db.dump_daily_picks("2026-05-04", "pure_stock", new_agg)

    loaded = db.load_daily_picks("2026-05-04", "pure_stock")
    assert loaded["2330"]["details"]["volume_kd"]["close"] == 999.99
    # 沒爆出兩倍 row
    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM daily_picks").fetchone()["c"]
    assert n == 3


def test_clear_daily_picks_for_date_only_affects_target_date(tmp_db):
    """清今天不該影響昨天。"""
    db.dump_daily_picks("2026-05-03", "pure_stock", _fake_agg_2sids())
    db.dump_daily_picks("2026-05-04", "pure_stock", _fake_agg_2sids())
    assert db.clear_daily_picks_for_date("2026-05-04") == 3
    # 5-04 沒了,5-03 還在
    assert db.load_daily_picks("2026-05-04", "pure_stock") is None
    assert db.load_daily_picks("2026-05-03", "pure_stock") is not None


def test_dump_daily_picks_empty_agg_returns_zero(tmp_db):
    """空 agg → 不該炸 + 回 0。"""
    assert db.dump_daily_picks("2026-05-04", "pure_stock", {}) == 0


def test_dump_daily_picks_separates_universes(tmp_db):
    """同 date 但不同 universe 各自獨立,load 不串(避免 pure_stock 撈到 top_50 結果)。"""
    db.dump_daily_picks("2026-05-04", "pure_stock", _fake_agg_2sids())
    only_2330 = {
        "2330": _fake_agg_2sids()["2330"],
    }
    db.dump_daily_picks("2026-05-04", "top_50", only_2330)

    pure = db.load_daily_picks("2026-05-04", "pure_stock")
    top50 = db.load_daily_picks("2026-05-04", "top_50")
    assert sorted(pure.keys()) == ["2317", "2330"]   # pure_stock 兩檔
    assert list(top50.keys()) == ["2330"]            # top_50 只有 2330


def test_daily_picks_roundtrip_with_ml_prob(tmp_db):
    """ml_probs 傳進 dump → load_daily_picks 還原時 agg[sid]['ml_prob'] 對。"""
    db.dump_daily_picks(
        "2026-05-04", "pure_stock", _fake_agg_2sids(),
        ml_probs={"2330": 0.72, "2317": 0.45},
    )
    loaded = db.load_daily_picks("2026-05-04", "pure_stock")
    assert loaded["2330"]["ml_prob"] == pytest.approx(0.72)
    assert loaded["2317"]["ml_prob"] == pytest.approx(0.45)


def test_daily_picks_roundtrip_without_ml_prob_returns_none(tmp_db):
    """不傳 ml_probs(舊版 caller 路徑)→ load 回傳 ml_prob=None,不炸。"""
    db.dump_daily_picks("2026-05-04", "pure_stock", _fake_agg_2sids())
    loaded = db.load_daily_picks("2026-05-04", "pure_stock")
    assert loaded["2330"]["ml_prob"] is None
    assert loaded["2317"]["ml_prob"] is None


def test_preload_daily_picks_csv_without_ml_prob_column(tmp_db, tmp_path):
    """舊 daily_picks.csv 沒 ml_prob 欄(Stage 1 之前的 commit) → preload 不
    噴錯,DB 內 ml_prob 補 NULL。給雲端 git pull 拿到舊 CSV 但 schema 已更
    新的 backward-compat 守門。"""
    import pandas as pd
    csv_path = tmp_path / "daily_picks.csv"
    pd.DataFrame([
        {
            "trade_date": "2026-05-04", "universe": "pure_stock",
            "strategy": "volume_kd", "sid": "2330",
            "score": None, "rank": None, "params_hash": "default_v1",
            "payload": '{"close": 600.0, "name": "台積電"}',
            "computed_at": "2026-05-04T00:00:00+00:00",
        },
    ]).to_csv(csv_path, index=False)
    counts = db.preload_snapshots(snapshot_dir=tmp_path)
    assert counts.get("daily_picks") == 1
    loaded = db.load_daily_picks("2026-05-04", "pure_stock")
    assert loaded is not None
    assert loaded["2330"]["ml_prob"] is None  # 舊 CSV 補 NULL


# === pick_outcomes(M4 weekly backtest)===

def test_pick_outcomes_schema_exists(tmp_db):
    """init_db 後 pick_outcomes 表 + index 存在,欄位齊全。"""
    with db.get_conn() as conn:
        names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "pick_outcomes" in names

        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(pick_outcomes)"
            ).fetchall()
        }
        assert cols == {
            "pick_date", "sid", "strategy", "entry_close",
            "return_d1", "return_d3", "return_d5", "return_d10",
            "hit_target", "stopped_out", "evaluated_at",
        }

        idx_names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_pick_outcomes_date" in idx_names


def _fake_outcome_row(
    pick_date: str = "2026-05-08",
    sid: str = "2330",
    strategy: str = "volume_kd",
    **overrides,
) -> dict:
    base = {
        "pick_date": pick_date, "sid": sid, "strategy": strategy,
        "entry_close": 600.0,
        "return_d1": 1.0, "return_d3": 2.5,
        "return_d5": 3.2, "return_d10": None,
        "hit_target": 1.0, "stopped_out": 0.0,
        "evaluated_at": "2026-05-13T14:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_dump_pick_outcomes_roundtrip(tmp_db):
    """dump → get_pick_outcomes_for_date roundtrip,欄位值對齊。"""
    rows = [
        _fake_outcome_row(),
        _fake_outcome_row(strategy="ma_alignment", return_d1=0.5),
        _fake_outcome_row(sid="2317", entry_close=200.0, return_d1=-0.8),
    ]
    n = db.dump_pick_outcomes(rows)
    assert n == 3

    loaded = db.get_pick_outcomes_for_date("2026-05-08")
    assert len(loaded) == 3
    # 排序穩定:sid asc, strategy asc
    sids = [r["sid"] for r in loaded]
    assert sids == ["2317", "2330", "2330"]

    # 找 2330 / volume_kd 那筆
    target = next(
        r for r in loaded if r["sid"] == "2330" and r["strategy"] == "volume_kd"
    )
    assert target["entry_close"] == pytest.approx(600.0)
    assert target["return_d1"] == pytest.approx(1.0)
    assert target["return_d10"] is None
    assert target["hit_target"] == pytest.approx(1.0)


def test_dump_pick_outcomes_on_conflict_replaces(tmp_db):
    """同 (pick_date, sid, strategy) 重 dump → 覆蓋舊值(報酬窗口拉長後重算)。"""
    db.dump_pick_outcomes([_fake_outcome_row(return_d10=None)])
    # 補上 d10
    db.dump_pick_outcomes([_fake_outcome_row(return_d10=5.5)])

    loaded = db.get_pick_outcomes_for_date("2026-05-08")
    assert len(loaded) == 1  # 沒重複插入
    assert loaded[0]["return_d10"] == pytest.approx(5.5)


def test_dump_pick_outcomes_empty_returns_zero(tmp_db):
    assert db.dump_pick_outcomes([]) == 0


def test_get_pick_outcomes_for_date_empty(tmp_db):
    """沒資料 → 空 list,不炸。"""
    assert db.get_pick_outcomes_for_date("2026-05-08") == []


def test_get_last_evaluated_pick_date(tmp_db):
    """回最近一筆有 return_d1 的 pick_date(沒資料 → None)。"""
    assert db.get_last_evaluated_pick_date() is None

    # 一筆 return_d1 NULL 的(尚未 evaluate)→ 不算入
    db.dump_pick_outcomes([
        _fake_outcome_row(pick_date="2026-05-12", return_d1=None),
    ])
    assert db.get_last_evaluated_pick_date() is None

    db.dump_pick_outcomes([
        _fake_outcome_row(pick_date="2026-05-08", return_d1=1.0),
        _fake_outcome_row(pick_date="2026-05-11", return_d1=0.5),
    ])
    assert db.get_last_evaluated_pick_date() == "2026-05-11"


def test_preload_pick_outcomes_csv(tmp_db, tmp_path):
    """pick_outcomes.csv 走 preload → DB 內可查到。"""
    import pandas as pd
    csv_path = tmp_path / "pick_outcomes.csv"
    pd.DataFrame([
        _fake_outcome_row(),
        _fake_outcome_row(sid="2317", entry_close=200.0, return_d1=-0.8),
    ]).to_csv(csv_path, index=False)
    counts = db.preload_snapshots(snapshot_dir=tmp_path)
    assert counts.get("pick_outcomes") == 2
    loaded = db.get_pick_outcomes_for_date("2026-05-08")
    assert len(loaded) == 2


# === Strategy history aggregation helpers(📊 策略歷史頁面)===

def test_get_strategy_history_stats_aggregates_by_strategy(tmp_db):
    """3 筆混合策略 → 按 strategy group,N / avg_d5 / hit_rate / stop_rate 對齊。"""
    db.dump_pick_outcomes([
        _fake_outcome_row(sid="2330", strategy="volume_kd",
                          return_d5=0.04, hit_target=1.0, stopped_out=0.0),
        _fake_outcome_row(sid="2317", strategy="volume_kd",
                          return_d5=-0.02, hit_target=0.0, stopped_out=1.0),
        _fake_outcome_row(sid="2330", strategy="ma_alignment",
                          return_d5=0.06, hit_target=1.0, stopped_out=0.0),
    ])
    stats = db.get_strategy_history_stats()
    by_name = {s["strategy"]: s for s in stats}
    assert by_name["volume_kd"]["n"] == 2
    assert by_name["volume_kd"]["avg_d5"] == pytest.approx(0.01)
    assert by_name["volume_kd"]["hit_rate"] == pytest.approx(0.5)
    assert by_name["volume_kd"]["stop_rate"] == pytest.approx(0.5)
    assert by_name["ma_alignment"]["n"] == 1
    assert by_name["ma_alignment"]["avg_d5"] == pytest.approx(0.06)
    # avg_d5 desc → ma_alignment 排在 volume_kd 前面
    assert [s["strategy"] for s in stats] == ["ma_alignment", "volume_kd"]


def test_get_strategy_history_stats_filters_by_since(tmp_db):
    """since='2026-05-09' → 只算 2026-05-09 之後的 rows。"""
    db.dump_pick_outcomes([
        _fake_outcome_row(pick_date="2026-05-05", return_d5=0.02),
        _fake_outcome_row(pick_date="2026-05-10", return_d5=0.06),
    ])
    stats_all = db.get_strategy_history_stats()
    assert stats_all[0]["n"] == 2

    stats_since = db.get_strategy_history_stats(since="2026-05-09")
    assert stats_since[0]["n"] == 1
    assert stats_since[0]["avg_d5"] == pytest.approx(0.06)


def test_get_pick_outcomes_by_date_groups_per_day(tmp_db):
    """3 個 pick_date → group by day,N 跟 avg 對。"""
    db.dump_pick_outcomes([
        _fake_outcome_row(pick_date="2026-05-08", sid="2330",
                          return_d1=0.02, return_d5=0.04),
        _fake_outcome_row(pick_date="2026-05-08", sid="2317",
                          return_d1=-0.01, return_d5=0.02),
        _fake_outcome_row(pick_date="2026-05-09", sid="2330",
                          return_d1=0.03, return_d5=0.05),
    ])
    by_date = db.get_pick_outcomes_by_date(days=10)
    # pick_date desc
    assert [r["pick_date"] for r in by_date] == ["2026-05-09", "2026-05-08"]
    d_08 = next(r for r in by_date if r["pick_date"] == "2026-05-08")
    assert d_08["n"] == 2
    assert d_08["avg_d1"] == pytest.approx(0.005)
    assert d_08["avg_d5"] == pytest.approx(0.03)


def test_get_pick_outcomes_by_date_respects_limit(tmp_db):
    """days=2 → 只回 2 個最近的 pick_date。"""
    db.dump_pick_outcomes([
        _fake_outcome_row(pick_date="2026-05-05"),
        _fake_outcome_row(pick_date="2026-05-06"),
        _fake_outcome_row(pick_date="2026-05-07"),
    ])
    by_date = db.get_pick_outcomes_by_date(days=2)
    assert len(by_date) == 2
    assert [r["pick_date"] for r in by_date] == ["2026-05-07", "2026-05-06"]


def test_get_pick_outcomes_raw_filters(tmp_db):
    """strategy + since filter 連用,只回符合的 rows。"""
    db.dump_pick_outcomes([
        _fake_outcome_row(pick_date="2026-05-05", sid="2330",
                          strategy="volume_kd"),
        _fake_outcome_row(pick_date="2026-05-10", sid="2317",
                          strategy="volume_kd"),
        _fake_outcome_row(pick_date="2026-05-10", sid="2330",
                          strategy="ma_alignment"),
    ])
    raw = db.get_pick_outcomes_raw(
        since="2026-05-08", strategy="volume_kd",
    )
    assert len(raw) == 1
    assert raw[0]["sid"] == "2317"
    assert raw[0]["pick_date"] == "2026-05-10"


def test_get_strategy_history_stats_empty(tmp_db):
    """空表 → 空 list,不炸。"""
    assert db.get_strategy_history_stats() == []
    assert db.get_pick_outcomes_by_date() == []
    assert db.get_pick_outcomes_raw() == []
