"""
SQLite 快取資料庫模組。

設計原則:
- 所有從 FinMind / yfinance 抓回來的資料都進入此 DB。
- 後續查詢一律「先查 DB,缺的才打 API」(由 data_fetcher 處理快取邏輯)。
- 用 PRIMARY KEY 防重複,寫入一律 upsert(INSERT ... ON CONFLICT DO UPDATE)。

資料表:
- stocks         股票主表(代號 / 名稱 / 市場 / 產業)
- daily_prices   日線價格(OHLCV + 成交金額 / 筆數 / 漲跌)
- institutional  三大法人買賣超(已 pivot 成單筆/股/日)
- financials     財報(月營收 + 季 EPS / ROE,以 period_type 區分)
- dividend       年度配息(現金股利 + 股票股利 + 除息日,長線選股用)
- daily_metrics  當日 PE / PB / 殖利率(TWSE OpenAPI 免費版)
- watchlist      使用者自選關注股(代號 / 加入時間 / 備註)
- sync_log       各股票各 dataset 的已同步日期區間,用於快取判斷

DB 路徑來源:
- 預設讀 src.config.DATABASE_PATH(由 .env 載入)
- 測試可用 monkeypatch 改 config.DATABASE_PATH 改成 tmp_path,不需傳參數
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from src import config


# === 連線與初始化 ===

_resolved_path_cache: dict[str, Path] = {}


def _resolve_db_path(db_path: str | Path | None = None) -> Path:
    """把相對路徑轉為以 PROJECT_ROOT 為基準的絕對路徑。

    用 module-level cache 避免每次 get_conn 都 mkdir(profile 顯示 mkdir 是 hot path:
    8000+ 次 query 等於 8000+ 次 mkdir = 0.35s 純 overhead)。
    """
    raw = str(db_path) if db_path is not None else str(config.DATABASE_PATH)
    cached = _resolved_path_cache.get(raw)
    if cached is not None:
        return cached
    p = Path(raw)
    if not p.is_absolute():
        p = config.PROJECT_ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    _resolved_path_cache[raw] = p
    return p


def _reset_path_cache() -> None:
    """測試用:測試切 DATABASE_PATH 時清掉 cache。"""
    _resolved_path_cache.clear()


@contextmanager
def get_conn(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """取得 SQLite 連線(context manager,自動 commit / close)。

    使用範例::

        with get_conn() as conn:
            conn.execute("SELECT * FROM stocks")
    """
    path = _resolve_db_path(db_path)
    # mkdir 已在 _resolve_db_path 內 cache 處理過,這裡不重複
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# === Schema ===
SCHEMA: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS stocks (
        stock_id   TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        market     TEXT NOT NULL DEFAULT 'TW',
        industry   TEXT,
        type       TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_prices (
        stock_id         TEXT NOT NULL,
        date             TEXT NOT NULL,
        open             REAL,
        high             REAL,
        low              REAL,
        close            REAL,
        volume           INTEGER,
        trading_money    REAL,
        trading_turnover INTEGER,
        spread           REAL,
        PRIMARY KEY (stock_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS institutional (
        stock_id         TEXT NOT NULL,
        date             TEXT NOT NULL,
        foreign_buy_sell INTEGER DEFAULT 0,
        trust_buy_sell   INTEGER DEFAULT 0,
        dealer_buy_sell  INTEGER DEFAULT 0,
        total_buy_sell   INTEGER DEFAULT 0,
        PRIMARY KEY (stock_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financials (
        stock_id    TEXT NOT NULL,
        period_type TEXT NOT NULL,
        period      TEXT NOT NULL,
        revenue     REAL,
        revenue_yoy REAL,
        eps         REAL,
        roe         REAL,
        PRIMARY KEY (stock_id, period_type, period)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dividend (
        stock_id         TEXT NOT NULL,
        year             INTEGER NOT NULL,
        cash_dividend    REAL DEFAULT 0,
        stock_dividend   REAL DEFAULT 0,
        ex_dividend_date TEXT,
        PRIMARY KEY (stock_id, year)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_metrics (
        stock_id       TEXT NOT NULL,
        date           TEXT NOT NULL,
        close          REAL,
        pe             REAL,
        pb             REAL,
        dividend_yield REAL,
        PRIMARY KEY (stock_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        stock_id TEXT PRIMARY KEY,
        added_at TEXT NOT NULL,
        note     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_log (
        stock_id   TEXT NOT NULL,
        dataset    TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date   TEXT NOT NULL,
        synced_at  TEXT NOT NULL,
        PRIMARY KEY (stock_id, dataset)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_daily_prices_date ON daily_prices(date)",
    "CREATE INDEX IF NOT EXISTS idx_institutional_date ON institutional(date)",
    # 加速 screener_long 的 WHERE stock_id=? AND period_type=? ORDER BY period DESC
    "CREATE INDEX IF NOT EXISTS idx_financials_stock_type_period "
    "ON financials(stock_id, period_type, period DESC)",
    # 加速 watchlist 排序顯示
    "CREATE INDEX IF NOT EXISTS idx_watchlist_added_at "
    "ON watchlist(added_at DESC)",
]


def init_db(db_path: str | Path | None = None) -> None:
    """建立所有資料表(冪等:重複呼叫不會出錯)。"""
    with get_conn(db_path) as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# === Upsert helpers ===

def upsert_stocks(rows: Iterable[dict], db_path: str | Path | None = None) -> int:
    """寫入 / 更新股票主表。

    rows 每筆需有 stock_id, name;market 預設 'TW',其餘可選。
    回傳寫入筆數。
    """
    rows_list = list(rows)
    if not rows_list:
        return 0
    now = _now_iso()
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO stocks (stock_id, name, market, industry, type, updated_at)
            VALUES (:stock_id, :name, :market, :industry, :type, :updated_at)
            ON CONFLICT(stock_id) DO UPDATE SET
                name=excluded.name,
                market=excluded.market,
                industry=excluded.industry,
                type=excluded.type,
                updated_at=excluded.updated_at
            """,
            [
                {
                    "stock_id": r["stock_id"],
                    "name": r["name"],
                    "market": r.get("market", "TW"),
                    "industry": r.get("industry"),
                    "type": r.get("type"),
                    "updated_at": now,
                }
                for r in rows_list
            ],
        )
    return len(rows_list)


def upsert_daily_prices(rows: Iterable[dict], db_path: str | Path | None = None) -> int:
    """寫入 / 更新日線。rows 已是統一格式(stock_id, date, open, high, low, close, volume, ...)。"""
    rows_list = list(rows)
    if not rows_list:
        return 0
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO daily_prices
                (stock_id, date, open, high, low, close, volume,
                 trading_money, trading_turnover, spread)
            VALUES
                (:stock_id, :date, :open, :high, :low, :close, :volume,
                 :trading_money, :trading_turnover, :spread)
            ON CONFLICT(stock_id, date) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                trading_money=excluded.trading_money,
                trading_turnover=excluded.trading_turnover,
                spread=excluded.spread
            """,
            [
                {
                    "stock_id": r["stock_id"],
                    "date": r["date"],
                    "open": r.get("open"),
                    "high": r.get("high"),
                    "low": r.get("low"),
                    "close": r.get("close"),
                    "volume": r.get("volume"),
                    "trading_money": r.get("trading_money"),
                    "trading_turnover": r.get("trading_turnover"),
                    "spread": r.get("spread"),
                }
                for r in rows_list
            ],
        )
    return len(rows_list)


def upsert_institutional(rows: Iterable[dict], db_path: str | Path | None = None) -> int:
    """寫入 / 更新三大法人(已 pivot 成單筆/股/日)。"""
    rows_list = list(rows)
    if not rows_list:
        return 0
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO institutional
                (stock_id, date, foreign_buy_sell, trust_buy_sell,
                 dealer_buy_sell, total_buy_sell)
            VALUES
                (:stock_id, :date, :foreign_buy_sell, :trust_buy_sell,
                 :dealer_buy_sell, :total_buy_sell)
            ON CONFLICT(stock_id, date) DO UPDATE SET
                foreign_buy_sell=excluded.foreign_buy_sell,
                trust_buy_sell=excluded.trust_buy_sell,
                dealer_buy_sell=excluded.dealer_buy_sell,
                total_buy_sell=excluded.total_buy_sell
            """,
            [
                {
                    "stock_id": r["stock_id"],
                    "date": r["date"],
                    "foreign_buy_sell": r.get("foreign_buy_sell", 0) or 0,
                    "trust_buy_sell": r.get("trust_buy_sell", 0) or 0,
                    "dealer_buy_sell": r.get("dealer_buy_sell", 0) or 0,
                    "total_buy_sell": r.get("total_buy_sell", 0) or 0,
                }
                for r in rows_list
            ],
        )
    return len(rows_list)


def upsert_daily_metrics(
    rows: Iterable[dict],
    db_path: str | Path | None = None,
) -> int:
    """寫入 / 更新當日 PE / PB / 殖利率(來源 TWSE OpenAPI BWIBBU_d)。"""
    rows_list = list(rows)
    if not rows_list:
        return 0
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO daily_metrics
                (stock_id, date, close, pe, pb, dividend_yield)
            VALUES
                (:stock_id, :date, :close, :pe, :pb, :dividend_yield)
            ON CONFLICT(stock_id, date) DO UPDATE SET
                close=excluded.close,
                pe=excluded.pe,
                pb=excluded.pb,
                dividend_yield=excluded.dividend_yield
            """,
            [
                {
                    "stock_id": r["stock_id"],
                    "date": r["date"],
                    "close": r.get("close"),
                    "pe": r.get("pe"),
                    "pb": r.get("pb"),
                    "dividend_yield": r.get("dividend_yield"),
                }
                for r in rows_list
            ],
        )
    return len(rows_list)


def upsert_dividend(rows: Iterable[dict], db_path: str | Path | None = None) -> int:
    """寫入 / 更新年度配息。

    rows 每筆需有 stock_id, year;cash_dividend / stock_dividend / ex_dividend_date 可選。
    """
    rows_list = list(rows)
    if not rows_list:
        return 0
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO dividend
                (stock_id, year, cash_dividend, stock_dividend, ex_dividend_date)
            VALUES
                (:stock_id, :year, :cash_dividend, :stock_dividend, :ex_dividend_date)
            ON CONFLICT(stock_id, year) DO UPDATE SET
                cash_dividend=excluded.cash_dividend,
                stock_dividend=excluded.stock_dividend,
                ex_dividend_date=excluded.ex_dividend_date
            """,
            [
                {
                    "stock_id": r["stock_id"],
                    "year": int(r["year"]),
                    "cash_dividend": r.get("cash_dividend") or 0,
                    "stock_dividend": r.get("stock_dividend") or 0,
                    "ex_dividend_date": r.get("ex_dividend_date"),
                }
                for r in rows_list
            ],
        )
    return len(rows_list)


def upsert_financials(rows: Iterable[dict], db_path: str | Path | None = None) -> int:
    """寫入 / 更新財報(月營收 + 季 EPS/ROE)。"""
    rows_list = list(rows)
    if not rows_list:
        return 0
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO financials
                (stock_id, period_type, period, revenue, revenue_yoy, eps, roe)
            VALUES
                (:stock_id, :period_type, :period, :revenue, :revenue_yoy, :eps, :roe)
            ON CONFLICT(stock_id, period_type, period) DO UPDATE SET
                revenue=COALESCE(excluded.revenue, financials.revenue),
                revenue_yoy=COALESCE(excluded.revenue_yoy, financials.revenue_yoy),
                eps=COALESCE(excluded.eps, financials.eps),
                roe=COALESCE(excluded.roe, financials.roe)
            """,
            [
                {
                    "stock_id": r["stock_id"],
                    "period_type": r["period_type"],
                    "period": r["period"],
                    "revenue": r.get("revenue"),
                    "revenue_yoy": r.get("revenue_yoy"),
                    "eps": r.get("eps"),
                    "roe": r.get("roe"),
                }
                for r in rows_list
            ],
        )
    return len(rows_list)


# === watchlist helpers(自選股清單) ===

def add_to_watchlist(
    stock_id: str,
    note: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """加入關注;若已存在則更新 note(added_at 不變)。"""
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (stock_id, added_at, note)
            VALUES (?, ?, ?)
            ON CONFLICT(stock_id) DO UPDATE SET note=excluded.note
            """,
            (stock_id, _now_iso(), note),
        )


def remove_from_watchlist(
    stock_id: str,
    db_path: str | Path | None = None,
) -> bool:
    """從關注移除;回 True 表示真的移掉,False 表示原本就不在。"""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM watchlist WHERE stock_id=?", (stock_id,)
        )
        return cur.rowcount > 0


def is_in_watchlist(
    stock_id: str,
    db_path: str | Path | None = None,
) -> bool:
    """是否已在關注清單。"""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM watchlist WHERE stock_id=? LIMIT 1",
            (stock_id,),
        ).fetchone()
    return row is not None


def get_watchlist(db_path: str | Path | None = None) -> list[dict]:
    """取整個關注清單(按 added_at 倒序)。"""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT stock_id, added_at, note FROM watchlist "
            "ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# === sync_log helpers(快取核心) ===

def get_synced_range(
    stock_id: str,
    dataset: str,
    db_path: str | Path | None = None,
) -> tuple[str, str] | None:
    """回傳此 (stock_id, dataset) 已同步的 (start, end) 區間;從未同步則 None。"""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT start_date, end_date FROM sync_log WHERE stock_id=? AND dataset=?",
            (stock_id, dataset),
        ).fetchone()
    return (row["start_date"], row["end_date"]) if row else None


def update_synced_range(
    stock_id: str,
    dataset: str,
    start: str,
    end: str,
    db_path: str | Path | None = None,
) -> None:
    """更新 (stock_id, dataset) 的已同步區間,自動跟既有區間取聯集。"""
    with get_conn(db_path) as conn:
        old = conn.execute(
            "SELECT start_date, end_date FROM sync_log WHERE stock_id=? AND dataset=?",
            (stock_id, dataset),
        ).fetchone()
        new_start = min(old["start_date"], start) if old else start
        new_end = max(old["end_date"], end) if old else end
        conn.execute(
            """
            INSERT INTO sync_log (stock_id, dataset, start_date, end_date, synced_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(stock_id, dataset) DO UPDATE SET
                start_date=excluded.start_date,
                end_date=excluded.end_date,
                synced_at=excluded.synced_at
            """,
            (stock_id, dataset, new_start, new_end, _now_iso()),
        )


__all__ = [
    "get_conn",
    "init_db",
    "upsert_stocks",
    "upsert_daily_prices",
    "upsert_institutional",
    "upsert_financials",
    "upsert_dividend",
    "upsert_daily_metrics",
    "add_to_watchlist",
    "remove_from_watchlist",
    "is_in_watchlist",
    "get_watchlist",
    "get_synced_range",
    "update_synced_range",
    "SCHEMA",
]
