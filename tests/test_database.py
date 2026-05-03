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
