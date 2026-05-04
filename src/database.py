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

import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)

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

    並發策略(救火 cloud `database is locked`):
    - **WAL mode**:讀寫分離 — 1 writer + N reader 不互相 block
      (預設 rollback journal mode 是 reader 跟 writer 互鎖,Streamlit
      Cloud worker 有時跑 ML retrain / preload + 使用者刷頁,wait 不過
      就 OperationalError)
    - **busy_timeout=30000ms**:contention 時等 30 秒(取代立即 raise)
    - **isolation_level=None + 手動 BEGIN/COMMIT**:避開 Python sqlite3
      preset 的 implicit transaction(對 DML 自動 BEGIN 但 COMMIT 時機綁
      在 module-level magic,雲端有時抓不準)

    WAL mode 是 DB 檔屬性,設一次留 -wal/-shm sidecar files;但容器重啟
    這些 sidecar 重建,所以每 boot 都重設(idempotent,免外部 migration)。

    使用範例::

        with get_conn() as conn:
            conn.execute("SELECT * FROM stocks")
    """
    path = _resolve_db_path(db_path)
    # mkdir 已在 _resolve_db_path 內 cache 處理過,這裡不重複
    # timeout=30 是 sqlite3.connect 的 C-level busy timeout(秒),配合
    # PRAGMA busy_timeout(毫秒)雙保險。
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        # WAL 對 in-memory DB 不適用(:memory: 不能 WAL),讀錯不致命
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        # 錯誤時 rollback 釋放 writer lock,讓其他 worker 立刻進得來
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
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
    """
    CREATE TABLE IF NOT EXISTS trades (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_id   TEXT NOT NULL,
        direction  TEXT NOT NULL CHECK(direction IN ('buy', 'sell')),
        price      REAL NOT NULL CHECK(price > 0),
        quantity   INTEGER NOT NULL CHECK(quantity > 0),
        trade_date TEXT NOT NULL,
        note       TEXT,
        created_at TEXT NOT NULL
    )
    """,
    # 個股公司資訊:FinMind facts(industry/market/listing_date/foreign_limit)
    # + LLM 生成 (description/uniqueness/moat) — cache-first lookup,LLM 慢
    # 不影響 boot;regenerate 才會重打 Gemini API。
    """
    CREATE TABLE IF NOT EXISTS company_profiles (
        stock_id           TEXT PRIMARY KEY,
        industry           TEXT,
        market             TEXT,
        listing_date       TEXT,
        foreign_limit      REAL,
        description        TEXT,
        uniqueness         TEXT,
        moat               TEXT,
        finmind_updated_at TEXT,
        llm_updated_at     TEXT
    )
    """,
    # daily_picks:nightly 預跑 run_all_strategies 的結果。App 端 cache 命中即
    # 0ms 回(取代每 rerun 重算 ~338ms)。一行 = 一個 (sid, strategy) 命中,
    # payload 是該 row 完整 dict 的 JSON。
    # universe:'pure_stock' / 'with_etf' / 'top_50' 三種預跑常用 universe。
    # params_hash:'default_v1' 表示 default params,user 改過 sliders 走 runtime。
    """
    CREATE TABLE IF NOT EXISTS daily_picks (
        trade_date  TEXT NOT NULL,
        universe    TEXT NOT NULL,
        strategy    TEXT NOT NULL,
        sid         TEXT NOT NULL,
        score       REAL,
        rank        INTEGER,
        params_hash TEXT NOT NULL,
        payload     TEXT,
        ml_prob     REAL,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (trade_date, universe, strategy, sid, params_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_daily_picks_lookup "
    "ON daily_picks(trade_date, universe, params_hash)",
    # strategy_backtest:每週一 nightly 跑 N=126 日歷史回測,對每個 strategy
    # 統計命中數 / 勝率 / 平均報酬。App 端用 load_latest_strategy_backtest
    # 拿 {strategy: win_rate},算每張 pick 命中策略的算術平均 → 卡片勝率欄。
    """
    CREATE TABLE IF NOT EXISTS strategy_backtest (
        strategy       TEXT NOT NULL,
        period_end     DATE NOT NULL,
        lookback_days  INTEGER NOT NULL,
        target_pct     REAL NOT NULL,
        stop_pct       REAL NOT NULL,
        hold_days      INTEGER NOT NULL,
        n_fires        INTEGER NOT NULL,
        n_wins         INTEGER NOT NULL,
        win_rate       REAL NOT NULL,
        avg_return     REAL,
        computed_at    TEXT NOT NULL,
        PRIMARY KEY (strategy, period_end)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sb_period "
    "ON strategy_backtest(period_end)",
    "CREATE INDEX IF NOT EXISTS idx_daily_prices_date ON daily_prices(date)",
    "CREATE INDEX IF NOT EXISTS idx_institutional_date ON institutional(date)",
    # 加速 screener_long 的 WHERE stock_id=? AND period_type=? ORDER BY period DESC
    "CREATE INDEX IF NOT EXISTS idx_financials_stock_type_period "
    "ON financials(stock_id, period_type, period DESC)",
    # 加速 watchlist 排序顯示
    "CREATE INDEX IF NOT EXISTS idx_watchlist_added_at "
    "ON watchlist(added_at DESC)",
    # 加速 trades 按股號 / 日期查
    "CREATE INDEX IF NOT EXISTS idx_trades_stock_date "
    "ON trades(stock_id, trade_date)",
]


def init_db(db_path: str | Path | None = None) -> None:
    """建立所有資料表(冪等:重複呼叫不會出錯)。"""
    with get_conn(db_path) as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        _migrate_daily_picks_add_ml_prob(conn)


def _migrate_daily_picks_add_ml_prob(conn) -> None:
    """Stage 1 Part 2 schema migration:加 ml_prob REAL 欄位到既有 daily_picks。

    SQLite 沒 ALTER TABLE ADD COLUMN IF NOT EXISTS,改用 PRAGMA 檢查再加。
    冪等(已存在 → no-op)。雲端容器 git pull 帶到既有 .db 時自動補欄,
    舊 daily_picks.csv preload(沒 ml_prob 欄)走 fallback 寫 NULL。
    """
    cols = {
        r["name"] for r in conn.execute(
            "PRAGMA table_info(daily_picks)"
        ).fetchall()
    }
    if "ml_prob" not in cols:
        conn.execute("ALTER TABLE daily_picks ADD COLUMN ml_prob REAL")


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

def _spawn_github_push_thread(push_fn, csv_content: str, label: str) -> None:
    """fire-and-forget thread:用 push_fn(csv_content) 推到 GitHub。

    雲端容器(Streamlit Cloud)會設 GITHUB_PAT,push 後讓 CSV 跨重啟保留。
    本地 dev / tests 沒設 PAT 時 caller 不會呼叫此函式。
    thread 內任何錯誤只 log 不 raise,避免 UI 死掉。

    push_fn: callable(csv_content: str) -> bool — 通常是
             github_sync.push_watchlist_to_github / push_trades_to_github
    label:   log 標籤(watchlist / trades 等)
    """
    def _worker() -> None:
        try:
            push_fn(csv_content)
        except Exception as ex:  # noqa: BLE001
            logger.error("[GH_SYNC] %s thread 內未預期錯誤:%s", label, ex)

    threading.Thread(target=_worker, daemon=True).start()


def _dump_watchlist_snapshot(db_path: str | Path | None) -> None:
    """在 watchlist 變動後把 SQLite 內容 dump 成 CSV(供下次 boot 還原)。

    snapshot 模組自帶 silent skip(snapshot dir 不存在 / DB 不在 repo 內 / load 進行中),
    所以 tests 跟非標準佈署不會誤寫真實 CSV。lazy import 避免 watchlist_snapshot ↔
    database 互相 import。

    若 dump 真的有寫(回傳 ≥0)且環境有設 GITHUB_PAT,會 fire-and-forget 推到 GitHub;
    沒設 PAT 時不開 thread,本機行為完全不變。
    """
    from src.watchlist_snapshot import dump_to_csv, dump_to_string
    n = dump_to_csv(db_path=db_path)
    if n < 0:
        return
    if not os.environ.get("GITHUB_PAT"):
        return
    try:
        csv_content = dump_to_string(db_path=db_path)
    except Exception as ex:  # noqa: BLE001
        logger.error("[GH_SYNC] 取 watchlist csv 字串失敗:%s", ex)
        return
    from src.github_sync import push_watchlist_to_github
    _spawn_github_push_thread(
        push_watchlist_to_github, csv_content, "watchlist",
    )


def _dump_trades_snapshot(db_path: str | Path | None) -> None:
    """在 trades 變動後 dump CSV + 自動 push GitHub(雲端永久化)。

    跟 _dump_watchlist_snapshot 同 pattern:
    - 一律 dump 進本地 trades.csv(讓本機 streamlit 也能 reload 還原)
    - 有 GITHUB_PAT → fire-and-forget thread push 到 watchlist-sync 分支
    - 沒 PAT → 不開 thread,本機行為不變
    """
    try:
        from src import portfolio_snapshot
        n = portfolio_snapshot.dump_to_csv(db_path=db_path)
    except Exception as ex:  # noqa: BLE001
        logger.error("[GH_SYNC] dump trades.csv 失敗:%s", ex)
        return
    if n < 0:
        return
    if not os.environ.get("GITHUB_PAT"):
        return
    # 讀回 csv 內容(string)送進 push_fn
    try:
        from src.portfolio_snapshot import _csv_path
        csv_content = _csv_path().read_text(encoding="utf-8")
    except Exception as ex:  # noqa: BLE001
        logger.error("[GH_SYNC] 讀 trades.csv 字串失敗:%s", ex)
        return
    from src.github_sync import push_trades_to_github
    _spawn_github_push_thread(
        push_trades_to_github, csv_content, "trades",
    )


def add_to_watchlist(
    stock_id: str,
    note: str | None = None,
    db_path: str | Path | None = None,
    added_at: str | None = None,
) -> None:
    """加入關注;若已存在則更新 note(added_at 不變)。

    added_at 預設為當下時間;傳入值用於從 CSV snapshot 還原時保留原時間戳,
    避免每次容器重啟 load 時 added_at 都被改寫成「啟動時間」。
    ON CONFLICT 永遠保留既有 added_at,所以重複呼叫不會誤覆寫。

    成功寫 SQLite 後同步 dump CSV snapshot,讓使用者 ☆ 變動跨容器重啟保留。
    """
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (stock_id, added_at, note)
            VALUES (?, ?, ?)
            ON CONFLICT(stock_id) DO UPDATE SET note=excluded.note
            """,
            (stock_id, added_at or _now_iso(), note),
        )
    _dump_watchlist_snapshot(db_path)


_VALID_STOCK_ID_RE = re.compile(r"^[0-9]{4,6}[A-Z]?$")


def bulk_add_to_watchlist(
    stock_ids: Iterable[str],
    notes: dict[str, str | None] | None = None,
    db_path: str | Path | None = None,
) -> dict[str, int | list[str]]:
    """批量加入關注。一次 SQLite 寫入,完事後只 dump+push 一次。

    輸入經 strip + 大寫,過濾條件:
      - 格式合法(4-6 位數字 + 可選一英文字母,如 2330 / 00878 / 1101A)
      - 同 batch 內去重
      - 已在 watchlist → 計入 dup,**不**更新 note(避免覆寫使用者既有備註)

    Returns:
        {
          "ok": int,          已成功新加(不含 dup)
          "dup": int,         已在 watchlist 的數
          "invalid": int,     格式不合法的數
          "ok_ids": list[str],
          "dup_ids": list[str],
          "invalid_ids": list[str],
        }

    add_to_watchlist 是 N 次 dump+push;這個函式對 N 筆只 dump+push 一次,
    用於 UI 批量貼上場景避免 GitHub spam commits。
    """
    notes = notes or {}
    seen: set[str] = set()
    valid: list[str] = []
    invalid_ids: list[str] = []
    for raw in stock_ids:
        sid = str(raw or "").strip().upper()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        if _VALID_STOCK_ID_RE.match(sid):
            valid.append(sid)
        else:
            invalid_ids.append(sid)

    if not valid:
        return {
            "ok": 0, "dup": 0, "invalid": len(invalid_ids),
            "ok_ids": [], "dup_ids": [], "invalid_ids": invalid_ids,
        }

    placeholders = ",".join(["?"] * len(valid))
    with get_conn(db_path) as conn:
        existing = {
            r[0] for r in conn.execute(
                f"SELECT stock_id FROM watchlist WHERE stock_id IN ({placeholders})",
                valid,
            )
        }
        ok_ids = [s for s in valid if s not in existing]
        dup_ids = [s for s in valid if s in existing]
        if ok_ids:
            now = _now_iso()
            conn.executemany(
                "INSERT INTO watchlist (stock_id, added_at, note) "
                "VALUES (?, ?, ?)",
                [(sid, now, notes.get(sid)) for sid in ok_ids],
            )

    if ok_ids:
        _dump_watchlist_snapshot(db_path)

    return {
        "ok": len(ok_ids), "dup": len(dup_ids), "invalid": len(invalid_ids),
        "ok_ids": ok_ids, "dup_ids": dup_ids, "invalid_ids": invalid_ids,
    }


def remove_from_watchlist(
    stock_id: str,
    db_path: str | Path | None = None,
) -> bool:
    """從關注移除;回 True 表示真的移掉,False 表示原本就不在。

    成功從 SQLite 刪掉後同步 dump CSV snapshot,讓刪除動作跨容器重啟生效。
    """
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM watchlist WHERE stock_id=?", (stock_id,)
        )
        removed = cur.rowcount > 0
    if removed:
        _dump_watchlist_snapshot(db_path)
    return removed


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


# === 健康檢查 / 篩選 helpers ===

def cache_health_summary(db_path: str | Path | None = None) -> dict:
    """回 daily_prices 歷史天數分布(給 UI / 推播訊息加註「歷史不足」用)。

    回 {
        "total_stocks": int,        # stocks 表 TW 市場總數
        "with_prices": int,         # daily_prices 有任何資料的個股數
        "buckets": {
            "<14": int,   # 任何策略都跑不了
            "14-19": int, # 可跑量價KD
            "20-59": int, # 可跑乖離率
            "60+": int,   # 可跑全策略(含 MA60)
        },
    }
    """
    with get_conn(db_path) as conn:
        total_stocks = conn.execute(
            "SELECT COUNT(*) AS c FROM stocks WHERE market='TW'"
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT stock_id, COUNT(*) AS cnt FROM daily_prices GROUP BY stock_id"
        ).fetchall()

    buckets = {"<14": 0, "14-19": 0, "20-59": 0, "60+": 0}
    for r in rows:
        cnt = r["cnt"]
        if cnt >= 60:
            buckets["60+"] += 1
        elif cnt >= 20:
            buckets["20-59"] += 1
        elif cnt >= 14:
            buckets["14-19"] += 1
        else:
            buckets["<14"] += 1

    return {
        "total_stocks": int(total_stocks),
        "with_prices": len(rows),
        "buckets": buckets,
    }


def preload_snapshots(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, int]:
    """從 snapshot_dir 讀 6 個 CSV 並 upsert 進 SQLite。

    給 streamlit cloud boot + GitHub Actions workflow 共用 — workflow runner
    是 fresh container,checkout 後 SQLite 空,沒這個 preload 會看到 daily_prices
    cache 空 → 短線篩選 0 picks。

    snapshot_dir None → PROJECT_ROOT/data/twse_snapshot/。
    db_path None → config.DATABASE_PATH。

    Loads(順序保留依賴關係:stocks 先行,daily_prices/institutional 後續):
      stocks.csv / daily_metrics.csv / financials_quarterly.csv /
      daily_prices.csv / institutional.csv / taiex.csv

    回 {csv stem: rows loaded} 給 caller 用 log。任何 csv 不存在 → skip。

    watchlist 不在這個 helper(streamlit 專屬,workflow 不需要)。
    """
    import pandas as pd

    if snapshot_dir is None:
        snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    snapshot_dir = Path(snapshot_dir)

    counts: dict[str, int] = {}
    if not snapshot_dir.exists():
        return counts

    init_db(db_path)

    # 1. stocks(含 industry)
    stocks_csv = snapshot_dir / "stocks.csv"
    if stocks_csv.exists():
        df = pd.read_csv(stocks_csv, dtype={"stock_id": str})
        rows: list[dict] = []
        for _, r in df.iterrows():
            rows.append({
                "stock_id": str(r["stock_id"]),
                "name": str(r["name"]) if pd.notna(r.get("name")) else "",
                "industry": (
                    str(r["industry"]) if pd.notna(r.get("industry")) else None
                ),
                "market": "TW",
            })
        if rows:
            upsert_stocks(rows, db_path=db_path)
            counts["stocks"] = len(rows)

    # 2. daily_metrics
    metrics_csv = snapshot_dir / "daily_metrics.csv"
    if metrics_csv.exists():
        df = pd.read_csv(metrics_csv, dtype={"stock_id": str})
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "stock_id": str(r["stock_id"]),
                "date": str(r["date"]),
                "close": float(r["close"]) if pd.notna(r.get("close")) else None,
                "pe": float(r["pe"]) if pd.notna(r.get("pe")) else None,
                "pb": float(r["pb"]) if pd.notna(r.get("pb")) else None,
                "dividend_yield": (
                    float(r["dividend_yield"])
                    if pd.notna(r.get("dividend_yield")) else None
                ),
            })
        if rows:
            upsert_daily_metrics(rows, db_path=db_path)
            counts["daily_metrics"] = len(rows)

    # 3. financials.quarterly
    fin_csv = snapshot_dir / "financials_quarterly.csv"
    if fin_csv.exists():
        df = pd.read_csv(fin_csv, dtype={"stock_id": str})
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "stock_id": str(r["stock_id"]),
                "period_type": "quarterly",
                "period": str(r["period"]),
                "revenue": (
                    float(r["revenue"]) if pd.notna(r.get("revenue")) else None
                ),
                "revenue_yoy": (
                    float(r["revenue_yoy"])
                    if pd.notna(r.get("revenue_yoy")) else None
                ),
                "eps": float(r["eps"]) if pd.notna(r.get("eps")) else None,
                "roe": float(r["roe"]) if pd.notna(r.get("roe")) else None,
            })
        if rows:
            upsert_financials(rows, db_path=db_path)
            counts["financials_quarterly"] = len(rows)

    # 4. daily_prices(從 backfill_history 產生的 ~130K 行 snapshot)
    prices_csv = snapshot_dir / "daily_prices.csv"
    if prices_csv.exists():
        df = pd.read_csv(prices_csv, dtype={"stock_id": str})
        records = df.to_dict("records")
        for r in records:
            for k, v in list(r.items()):
                if pd.isna(v):
                    r[k] = None
        if records:
            upsert_daily_prices(records, db_path=db_path)
            counts["daily_prices"] = len(records)

    # 5. institutional
    inst_csv = snapshot_dir / "institutional.csv"
    if inst_csv.exists():
        df = pd.read_csv(inst_csv, dtype={"stock_id": str})
        records = df.to_dict("records")
        for r in records:
            for k, v in list(r.items()):
                if pd.isna(v):
                    r[k] = None
        if records:
            upsert_institutional(records, db_path=db_path)
            counts["institutional"] = len(records)

    # 6. TAIEX(獨立 csv,from weekly_market_update)
    taiex_csv = snapshot_dir / "taiex.csv"
    if taiex_csv.exists():
        df = pd.read_csv(taiex_csv, dtype={"stock_id": str})
        records = df.to_dict("records")
        for r in records:
            for k, v in list(r.items()):
                if pd.isna(v):
                    r[k] = None
        if records:
            upsert_daily_prices(records, db_path=db_path)
            counts["taiex"] = len(records)

    # 7. trades(P&L 紀錄)— delegate 給 portfolio_snapshot,只在表空時灌
    # 避免覆蓋本機使用者新加的交易
    try:
        from src.portfolio_snapshot import load_from_csv as _load_trades
        n_trades = _load_trades(snapshot_dir=snapshot_dir, db_path=db_path)
        if n_trades > 0:
            counts["trades"] = n_trades
    except Exception as e:  # noqa: BLE001
        logger.warning("[PRELOAD] trades load 失敗:%s", e)

    # 8. daily_picks(precompute 預跑結果)— nightly workflow dump 進 repo,
    # Cloud 容器 git pull 拿到 CSV → 這裡灌進 SQLite,App 端 _run_all_strategies_cached
    # 命中 → 0ms 回。CSV 沒有就跳過(precompute step 失敗 / 第一次部署沒檔)。
    picks_csv = snapshot_dir / "daily_picks.csv"
    if picks_csv.exists():
        df = pd.read_csv(picks_csv, dtype={"sid": str, "trade_date": str})
        # backward-compat:舊 CSV 沒 ml_prob 欄,補欄填 NULL
        if "ml_prob" not in df.columns:
            df["ml_prob"] = None
        records = df.to_dict("records")
        for r in records:
            for k, v in list(r.items()):
                if pd.isna(v):
                    r[k] = None
        if records:
            with get_conn(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO daily_picks (
                        trade_date, universe, strategy, sid, score, rank,
                        params_hash, payload, ml_prob, computed_at
                    ) VALUES (
                        :trade_date, :universe, :strategy, :sid, :score, :rank,
                        :params_hash, :payload, :ml_prob, :computed_at
                    )
                    ON CONFLICT(trade_date, universe, strategy, sid, params_hash)
                    DO UPDATE SET
                        score=excluded.score, rank=excluded.rank,
                        payload=excluded.payload,
                        ml_prob=excluded.ml_prob,
                        computed_at=excluded.computed_at
                    """,
                    records,
                )
            counts["daily_picks"] = len(records)

    # 9. strategy_backtest(週一 nightly 跑歷史回測結果)— App 端 enrich
    # 卡片勝率欄用。只 dump 週一一次,週二-週五 CSV 不變。
    sb_csv = snapshot_dir / "strategy_backtest.csv"
    if sb_csv.exists():
        df = pd.read_csv(sb_csv, dtype={"strategy": str, "period_end": str})
        records = df.to_dict("records")
        for r in records:
            for k, v in list(r.items()):
                if pd.isna(v):
                    r[k] = None
        if records:
            with get_conn(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO strategy_backtest (
                        strategy, period_end, lookback_days, target_pct, stop_pct,
                        hold_days, n_fires, n_wins, win_rate, avg_return, computed_at
                    ) VALUES (
                        :strategy, :period_end, :lookback_days, :target_pct, :stop_pct,
                        :hold_days, :n_fires, :n_wins, :win_rate, :avg_return, :computed_at
                    )
                    ON CONFLICT(strategy, period_end) DO UPDATE SET
                        lookback_days=excluded.lookback_days,
                        target_pct=excluded.target_pct, stop_pct=excluded.stop_pct,
                        hold_days=excluded.hold_days,
                        n_fires=excluded.n_fires, n_wins=excluded.n_wins,
                        win_rate=excluded.win_rate, avg_return=excluded.avg_return,
                        computed_at=excluded.computed_at
                    """,
                    records,
                )
            counts["strategy_backtest"] = len(records)

    return counts


def get_latest_trading_date(
    db_path: str | Path | None = None,
) -> str | None:
    """SQLite daily_prices 內最新一筆 date(ISO string,YYYY-MM-DD)。

    給 daily_notify / streamlit 用,週末 / 假日 today() 沒當日 close 時改用
    這個當篩選日期(避免「今日無入選」誤判)。

    daily_prices 空 → 回 None,caller 自己 fallback today。
    """
    with get_conn(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM daily_prices"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return row["d"] if row and row["d"] else None


def stocks_with_min_history(
    min_days: int = 60, db_path: str | Path | None = None,
) -> list[str]:
    """回 stock_id 清單(只含 daily_prices 天數 >= min_days 的 TW 個股)。

    給「僅有充足歷史的股」selectbox option 用 — 過濾 cache 還沒回補完的個股,
    避免全市場 2700 檔大多 1-2 天 → 全部 skip → 0 入選的鬼扯結果。
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT stock_id FROM daily_prices "
            "GROUP BY stock_id HAVING COUNT(*) >= ?",
            (min_days,),
        ).fetchall()
    return [r["stock_id"] for r in rows]


# === daily_picks:預跑 strategies 結果(nightly precompute) ===

def dump_daily_picks(
    trade_date: str,
    universe_key: str,
    agg: dict[str, dict],
    params_hash: str = "default_v1",
    db_path: str | Path | None = None,
    ml_probs: dict[str, float | None] | None = None,
) -> int:
    """把 run_all_strategies 結果 bulk insert 進 daily_picks。

    Args:
        trade_date: 'YYYY-MM-DD'
        universe_key: 'pure_stock' / 'with_etf' / 'top_50' 之一(precompute 用)
        agg: run_all_strategies 回傳的 dict[sid, {name, signals, details}]
        params_hash: 'default_v1' 表示 default params(預跑路徑)
        db_path: 預設用 config.DATABASE_PATH
        ml_probs: optional {sid: prob | None} — 同 sid 的所有 strategy 行
            會寫同一個 ml_prob(per-sid 不 per-strategy)。None 不寫(stage 1
            Part 2 之前的呼叫者 backward compat)。

    一個 (sid, strategy) 變一行;score/rank 留 None(目前不用,留欄位給未
    來擴充);payload = 該策略 row dict 的 JSON(name + close + 各 indicator)。
    用 ON CONFLICT REPLACE 重跑同 (date, universe, strategy, sid, params_hash)
    時覆蓋舊值。

    回 inserted row count(若 agg 全空回 0)。
    """
    import json as _json

    if not agg:
        return 0

    now = _now_iso()
    rows: list[dict] = []
    for sid, info in agg.items():
        prob = (ml_probs or {}).get(sid) if ml_probs else None
        for strategy_key, row_dict in (info.get("details") or {}).items():
            # row_dict 內可能含 numpy/pandas 類型,json.dumps 用 default=str
            payload = _json.dumps(row_dict, ensure_ascii=False, default=str)
            rows.append({
                "trade_date": trade_date,
                "universe": universe_key,
                "strategy": strategy_key,
                "sid": str(sid),
                "score": None,
                "rank": None,
                "params_hash": params_hash,
                "payload": payload,
                "ml_prob": prob,
                "computed_at": now,
            })

    if not rows:
        return 0

    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO daily_picks (
                trade_date, universe, strategy, sid, score, rank,
                params_hash, payload, ml_prob, computed_at
            ) VALUES (
                :trade_date, :universe, :strategy, :sid, :score, :rank,
                :params_hash, :payload, :ml_prob, :computed_at
            )
            ON CONFLICT(trade_date, universe, strategy, sid, params_hash)
            DO UPDATE SET
                score=excluded.score, rank=excluded.rank,
                payload=excluded.payload,
                ml_prob=excluded.ml_prob,
                computed_at=excluded.computed_at
            """,
            rows,
        )
    return len(rows)


def load_daily_picks(
    trade_date: str,
    universe_key: str,
    params_hash: str = "default_v1",
    db_path: str | Path | None = None,
) -> dict[str, dict] | None:
    """從 daily_picks 撈回 agg dict(跟 run_all_strategies 同 schema)。

    回:
        - None:cache miss(這 (date, universe, params_hash) 組合無資料)
        - dict[sid, {name, signals, details}]:重組成跟 run_all_strategies 一樣

    signals 欄位用 STRATEGY_LABELS 還原中文標籤(跟 run_all_strategies 一致)。
    """
    import json as _json

    init_db(db_path)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT strategy, sid, payload, ml_prob FROM daily_picks
            WHERE trade_date=? AND universe=? AND params_hash=?
            """,
            (trade_date, universe_key, params_hash),
        ).fetchall()

    if not rows:
        return None

    # lazy import 避免循環依賴(strategies 也 import database)
    from src.strategies import STRATEGY_LABELS

    agg: dict[str, dict] = {}
    for r in rows:
        sid = r["sid"]
        strategy = r["strategy"]
        try:
            payload = _json.loads(r["payload"]) if r["payload"] else {}
        except (TypeError, _json.JSONDecodeError):
            payload = {}
        if sid not in agg:
            agg[sid] = {
                "name": payload.get("name", ""),
                "signals": [],
                "details": {},
                # ml_prob per-sid(同 sid 多策略 row 共用同一值,取第一個 row 就行)
                "ml_prob": r["ml_prob"],
            }
        # 用 STRATEGY_LABELS 拿中文標籤;未知 strategy key 退回原 key
        agg[sid]["signals"].append(STRATEGY_LABELS.get(strategy, strategy))
        agg[sid]["details"][strategy] = payload
    return agg


def clear_daily_picks_for_date(
    trade_date: str,
    db_path: str | Path | None = None,
) -> int:
    """清掉某日的 daily_picks(precompute 重跑時先清避免遺留舊 universe/params)。
    回 deleted row count。
    """
    init_db(db_path)
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM daily_picks WHERE trade_date=?", (trade_date,),
        )
        return cur.rowcount


# === strategy_backtest:歷史回測勝率(週一 nightly 跑) ===

def dump_strategy_backtest(
    rows: list[dict],
    db_path: str | Path | None = None,
) -> int:
    """bulk insert strategy_backtest 結果,ON CONFLICT REPLACE。

    rows 每筆需含:strategy, period_end, lookback_days, target_pct, stop_pct,
    hold_days, n_fires, n_wins, win_rate, avg_return(可 None),computed_at。

    回 inserted row count。
    """
    if not rows:
        return 0
    init_db(db_path)
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO strategy_backtest (
                strategy, period_end, lookback_days, target_pct, stop_pct,
                hold_days, n_fires, n_wins, win_rate, avg_return, computed_at
            ) VALUES (
                :strategy, :period_end, :lookback_days, :target_pct, :stop_pct,
                :hold_days, :n_fires, :n_wins, :win_rate, :avg_return, :computed_at
            )
            ON CONFLICT(strategy, period_end) DO UPDATE SET
                lookback_days=excluded.lookback_days,
                target_pct=excluded.target_pct, stop_pct=excluded.stop_pct,
                hold_days=excluded.hold_days,
                n_fires=excluded.n_fires, n_wins=excluded.n_wins,
                win_rate=excluded.win_rate, avg_return=excluded.avg_return,
                computed_at=excluded.computed_at
            """,
            rows,
        )
    return len(rows)


def load_latest_strategy_backtest(
    db_path: str | Path | None = None,
) -> dict[str, float]:
    """回 {strategy_name: win_rate},每個 strategy 取最新 period_end 的 win_rate。

    給 App 端 _enrich_with_win_rate 用 — 不論 backtest 跑過多少次,只看最新。
    無資料 → 空 dict。
    """
    init_db(db_path)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT strategy, win_rate FROM strategy_backtest
            WHERE (strategy, period_end) IN (
                SELECT strategy, MAX(period_end) FROM strategy_backtest
                GROUP BY strategy
            )
            """
        ).fetchall()
    return {r["strategy"]: float(r["win_rate"]) for r in rows}


def load_strategy_backtest_for_period(
    period_end: str,
    db_path: str | Path | None = None,
):
    """撈某個 period_end 的全部 strategy 結果,回 pd.DataFrame(空表回空 DataFrame)。"""
    import pandas as _pd

    init_db(db_path)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_backtest WHERE period_end=? "
            "ORDER BY win_rate DESC",
            (period_end,),
        ).fetchall()
    if not rows:
        return _pd.DataFrame()
    return _pd.DataFrame([dict(r) for r in rows])


# === Trades / P&L tracking ===

def add_trade(
    stock_id: str,
    direction: str,
    price: float,
    quantity: int,
    trade_date: str,
    note: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    """新增一筆交易,回 lastrowid。

    direction: 'buy' / 'sell'
    quantity: 張數(必須 > 0)
    price: 每張價格(必須 > 0)
    trade_date: 'YYYY-MM-DD'
    """
    if direction not in ("buy", "sell"):
        raise ValueError(f"direction 必須是 'buy' 或 'sell',got {direction!r}")
    if quantity <= 0:
        raise ValueError(f"quantity 必須 > 0,got {quantity}")
    if price <= 0:
        raise ValueError(f"price 必須 > 0,got {price}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO trades "
            "(stock_id, direction, price, quantity, trade_date, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stock_id, direction, price, quantity, trade_date, note, now),
        )
        new_id = int(cur.lastrowid)
    # dump CSV + 雲端 GitHub auto-push(沒 PAT 時不開 thread)
    _dump_trades_snapshot(db_path)
    return new_id


def delete_trade(trade_id: int, db_path: str | Path | None = None) -> bool:
    """刪一筆交易,回 True 若有刪掉、False 若 id 不存在。"""
    with get_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
        deleted = cur.rowcount > 0
    if deleted:
        # dump CSV + 雲端 GitHub auto-push
        _dump_trades_snapshot(db_path)
    return deleted


def get_trades(
    stock_id: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """list trades,可 filter by stock_id;按 trade_date desc, id desc 排序。"""
    with get_conn(db_path) as conn:
        if stock_id is not None:
            rows = conn.execute(
                "SELECT * FROM trades WHERE stock_id=? "
                "ORDER BY trade_date DESC, id DESC",
                (stock_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY trade_date DESC, id DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_position(
    stock_id: str,
    db_path: str | Path | None = None,
) -> dict:
    """算該股當前 position(weighted average cost)+ 已實現 P&L。

    Weighted average 算法:
    - buy:新 avg_cost = (舊 qty × 舊 avg + q × p) / (舊 qty + q)
    - sell:realized_pnl += (sell_price - 當下 avg_cost) × q,
      avg_cost 維持不變(剩下的 lot 仍同成本)
    - 全清倉(qty=0)後再 buy → avg_cost 從新 buy 重算

    回 dict:
        - quantity: 當前持有張數
        - avg_cost: 加權平均成本(qty=0 時回 0.0)
        - realized_pnl: 已實現損益(累計)
        - total_buy_amount: 累計買入金額
        - total_sell_amount: 累計賣出金額
        - num_trades: 總筆數
    """
    trades = get_trades(stock_id, db_path)
    # 處理時要按時間升序(早的先,影響 avg_cost 累計)
    trades.sort(key=lambda t: (t["trade_date"], t["id"]))

    qty = 0
    avg_cost = 0.0
    realized = 0.0
    total_buy = 0.0
    total_sell = 0.0

    for t in trades:
        d = t["direction"]
        p = float(t["price"])
        q = int(t["quantity"])
        if d == "buy":
            new_qty = qty + q
            if new_qty > 0:
                avg_cost = (qty * avg_cost + q * p) / new_qty
            qty = new_qty
            total_buy += p * q
        else:  # sell(已在 add_trade 驗過 buy/sell)
            realized += (p - avg_cost) * q
            qty -= q
            total_sell += p * q

    return {
        "stock_id": stock_id,
        "quantity": qty,
        "avg_cost": avg_cost if qty > 0 else 0.0,
        "realized_pnl": realized,
        "total_buy_amount": total_buy,
        "total_sell_amount": total_sell,
        "num_trades": len(trades),
    }


def get_unrealized_pnl(
    stock_id: str,
    current_price: float,
    db_path: str | Path | None = None,
) -> float:
    """當前未實現 P&L = (current_price - avg_cost) × qty。
    qty<=0(沒持倉)→ 回 0。
    """
    pos = get_position(stock_id, db_path)
    if pos["quantity"] <= 0:
        return 0.0
    return (current_price - pos["avg_cost"]) * pos["quantity"]


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
    "bulk_add_to_watchlist",
    "remove_from_watchlist",
    "is_in_watchlist",
    "get_watchlist",
    "get_synced_range",
    "update_synced_range",
    "cache_health_summary",
    "preload_snapshots",
    "get_latest_trading_date",
    "add_trade",
    "delete_trade",
    "get_trades",
    "get_position",
    "get_unrealized_pnl",
    "stocks_with_min_history",
    "dump_daily_picks",
    "load_daily_picks",
    "clear_daily_picks_for_date",
    "dump_strategy_backtest",
    "load_latest_strategy_backtest",
    "load_strategy_backtest_for_period",
    "SCHEMA",
]
