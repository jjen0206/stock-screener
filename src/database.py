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

from src import config

logger = logging.getLogger(__name__)


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
    # paper_trades:純 paper trading 紀錄,驗證 Stage 2B v2 ML 過濾在實盤是否
    # 有效。每筆 (sid, entry_date) 唯一,UI page 「🧪 實測追蹤」加進。
    # status active 在 evaluate_active_trades 時掃 daily_prices 滾動更新。
    """
    CREATE TABLE IF NOT EXISTS paper_trades (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        sid                 TEXT NOT NULL,
        name                TEXT,
        entry_date          TEXT NOT NULL,
        entry_price         REAL NOT NULL CHECK(entry_price > 0),
        matched_strategies  TEXT,
        ml_prob             REAL,
        target_price        REAL NOT NULL,
        stop_price          REAL NOT NULL,
        current_stop        REAL,
        trailing_level      INTEGER NOT NULL DEFAULT 0,
        hold_days           INTEGER NOT NULL DEFAULT 5,
        expected_exit_date  TEXT,
        actual_exit_date    TEXT,
        actual_exit_price   REAL,
        status              TEXT NOT NULL CHECK(status IN
                            ('active', 'win', 'lose', 'timeout_win', 'timeout_lose')),
        return_pct          REAL,
        notes               TEXT,
        created_at          TEXT NOT NULL,
        updated_at          TEXT,
        UNIQUE(sid, entry_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_trades_status "
    "ON paper_trades(status, entry_date DESC)",
    # news:TWSE 重大訊息(t187ap04_L OpenAPI 抓全市場)。每小時 cron 抓
    # 一次,白名單過濾後推 Telegram + Discord。url_hash UNIQUE 防重複。
    """
    CREATE TABLE IF NOT EXISTS news (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        sid             TEXT NOT NULL,
        company_name    TEXT,
        publish_date    TEXT NOT NULL,    -- YYYY-MM-DD(已從民國年轉)
        publish_time    TEXT,             -- HHMMSS
        subject         TEXT NOT NULL,
        article_no      TEXT,             -- 條款編號 "第8款" 等
        description     TEXT,             -- 說明全文
        fact_date       TEXT,             -- 事實發生日 YYYY-MM-DD
        url_hash        TEXT NOT NULL UNIQUE,
        sent_telegram   INTEGER NOT NULL DEFAULT 0,
        sent_discord    INTEGER NOT NULL DEFAULT 0,
        fetched_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_news_sent "
    "ON news(sent_telegram, sent_discord, publish_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_news_sid_date "
    "ON news(sid, publish_date DESC)",
    # analyst_targets:法人(券商研究員)目標價共識。A+B 雙來源
    # source='yfinance' 直接拿 Yahoo Finance Analyst Estimates
    # source='gemini_news' 拿不到 yfinance 時走 Gemini 解析新聞
    # PK 加 source → 同一 sid 兩種來源各保一筆,UI 顯時優先 yfinance(較準)
    # previous_target_mean / previous_fetched_at(2026-05-08 加):
    #   每次 upsert 時把當前 target_mean / fetched_at 存成 previous,讓
    #   推播 / picks Δ 可比對「這次 vs 上一次」變動,不需另開 history table。
    """
    CREATE TABLE IF NOT EXISTS analyst_targets (
        stock_id              TEXT NOT NULL,
        target_mean           REAL,
        target_median         REAL,
        target_high           REAL,
        target_low            REAL,
        num_analysts          INTEGER,
        source                TEXT NOT NULL,
        fetched_at            TEXT NOT NULL,
        previous_target_mean  REAL,
        previous_fetched_at   TEXT,
        PRIMARY KEY (stock_id, source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_analyst_targets_sid "
    "ON analyst_targets(stock_id)",
    # analyst_target_alerts:法人異動推播去重(同 sid 同日同方向只推一次)
    """
    CREATE TABLE IF NOT EXISTS analyst_target_alerts (
        sid             TEXT NOT NULL,
        alert_date      TEXT NOT NULL,
        direction       TEXT NOT NULL CHECK(direction IN ('up', 'down')),
        sent_telegram   INTEGER NOT NULL DEFAULT 0,
        sent_discord    INTEGER NOT NULL DEFAULT 0,
        old_target      REAL,
        new_target      REAL,
        created_at      TEXT NOT NULL,
        PRIMARY KEY (sid, alert_date, direction)
    )
    """,
    # alert_dedup:盤中觸發告警去重(同 sid 同 alert_type 同日只推一次)
    # 由 scripts/intraday_alerts.py 寫入,30 分鐘 cron 反覆掃描時用來避免反覆轟炸。
    # alert_type:'stop_loss' / 'entry_zone' / 'breakout'。
    """
    CREATE TABLE IF NOT EXISTS alert_dedup (
        sid          TEXT NOT NULL,
        alert_type   TEXT NOT NULL,
        alert_date   TEXT NOT NULL,
        sent_at      TEXT NOT NULL,
        ref_price    REAL,
        threshold    REAL,
        PRIMARY KEY (sid, alert_type, alert_date)
    )
    """,
    # target_hit_log:現價達法人共識目標價推播,7 日冷卻防重推
    """
    CREATE TABLE IF NOT EXISTS target_hit_log (
        sid              TEXT NOT NULL,
        hit_date         TEXT NOT NULL,
        close            REAL NOT NULL,
        target_consensus REAL NOT NULL,
        sent_telegram    INTEGER NOT NULL DEFAULT 0,
        sent_discord     INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL,
        PRIMARY KEY (sid, hit_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_target_hit_log_sid_date "
    "ON target_hit_log(sid, hit_date DESC)",
    # shareholder_concentration:TDCC 股權分散表「持股 ≥ 1000 張」週快照
    # 每週六凌晨從 TDCC opendata 抓上週五公布,獨立 workflow(別塞 daily-fetch)
    # holders_delta_w 是「本週 - 上週」的人數變化(fetcher 算好寫入,App 端零成本讀)
    # MVP:不納入 ML,只當 Telegram 推播 + Streamlit 長線卡附加資訊
    """
    CREATE TABLE IF NOT EXISTS shareholder_concentration (
        sid                   TEXT NOT NULL,
        week_end              TEXT NOT NULL,    -- YYYY-MM-DD,週五日期
        holders_1000up_count  INTEGER NOT NULL, -- 持股 ≥ 1000 張的股東人數
        total_holders         INTEGER NOT NULL, -- 全部股東人數(全分級加總)
        holders_pct           REAL,             -- 千張戶 / 總股東(0-1)
        holders_delta_w       INTEGER,          -- 本週 - 上週(人)
        fetched_at            TEXT NOT NULL,
        PRIMARY KEY (sid, week_end)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shareholder_concentration_week "
    "ON shareholder_concentration(week_end DESC)",
    # pick_outcomes:把 daily_picks 的每筆策略 fire 跑出實際 1/3/5/10 日報酬,
    # 給 daily-notify「昨日複盤」section 跟 weekly backtest 用。
    # 每筆 (pick_date, sid, strategy) 唯一,UPSERT(報酬窗口拉長後可覆蓋舊值)。
    # hit_target / stopped_out 0/1 表是否在 d1~d10 內觸 +3% / -3% — 用 REAL 方便
    # AVG() 算命中率(主公拍板 schema 用 REAL)。
    """
    CREATE TABLE IF NOT EXISTS pick_outcomes (
        pick_date    TEXT NOT NULL,    -- 推薦日 YYYY-MM-DD
        sid          TEXT NOT NULL,
        strategy     TEXT NOT NULL,    -- 命中策略 key(對應 STRATEGY_LABELS)
        entry_close  REAL,             -- 推薦日收盤價
        return_d1    REAL,             -- 隔交易日收盤報酬 %(close_d1/entry - 1)
        return_d3    REAL,             -- 3 交易日累積
        return_d5    REAL,             -- 5 交易日累積
        return_d10   REAL,             -- 10 交易日累積
        hit_target   REAL,             -- 0/1:d1~d10 區間 high 達 +3%
        stopped_out  REAL,             -- 0/1:d1~d10 區間 low 觸 -3%
        evaluated_at TEXT NOT NULL,
        PRIMARY KEY (pick_date, sid, strategy)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pick_outcomes_date "
    "ON pick_outcomes(pick_date DESC)",
    # pick_shap_explanations:SHAP ML 解釋性 cache(2026-05-14 加)。
    # 每天 daily-notify 推播前對當日 picks 算一次 SHAP 寫表,後續 Telegram / Streamlit
    # 從表撈 cache 避免重算(shap.TreeExplainer 對 RF 雖然快,N×D 仍可能慢)。
    # top_features 存 JSON array of {feature, value, contribution, contribution_pct, direction}
    # 同 (pick_date, sid, strategy) UPSERT(重跑覆蓋)。
    """
    CREATE TABLE IF NOT EXISTS pick_shap_explanations (
        pick_date     TEXT NOT NULL,    -- 推薦日 YYYY-MM-DD
        sid           TEXT NOT NULL,
        strategy      TEXT NOT NULL,    -- routing strategy 名(general / per-strategy)
        top_features  TEXT NOT NULL,    -- JSON: [{feature, value, contribution, contribution_pct, direction}]
        generated_at  TEXT NOT NULL,
        PRIMARY KEY (pick_date, sid, strategy)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pick_shap_explanations_date "
    "ON pick_shap_explanations(pick_date DESC)",
    # vbt_grid_results:vectorbt 策略 grid search 結果(2026-05-14 加)。
    # 每組 (strategy, params_hash) 唯一,UPSERT 允許重跑覆蓋。params_json 存原始
    # 參數 dict(讓 UI 顯示「最佳組合」),metrics 來自 vbt.Portfolio.stats():
    # total_return / sharpe / max_drawdown / win_rate。n_trades 給樣本量參考。
    # 不自動推進為 production default — 只是「建議」讓主公手動採用。
    """
    CREATE TABLE IF NOT EXISTS vbt_grid_results (
        strategy       TEXT NOT NULL,
        params_hash    TEXT NOT NULL,    -- sha1(json.dumps(params, sort_keys=True))[:12]
        params_json    TEXT NOT NULL,    -- JSON dict 原始參數
        period_start   TEXT NOT NULL,    -- 'YYYY-MM-DD'
        period_end     TEXT NOT NULL,
        n_trades       INTEGER NOT NULL,
        total_return   REAL,             -- 百分比 e.g. 12.34 = 12.34%
        sharpe         REAL,
        max_drawdown   REAL,             -- 百分比(正值 e.g. 8.12 = 8.12%)
        win_rate       REAL,             -- 百分比
        generated_at   TEXT NOT NULL,
        PRIMARY KEY (strategy, params_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_vbt_grid_strategy "
    "ON vbt_grid_results(strategy, sharpe DESC)",
    # ml_walkforward_results:walk-forward CV 評估結果(M2 後續,2026-05-15 加)。
    # 取代 random 80/20 split 評估,對時間序列做 expanding-window CV,輸出每
    # split 的 train/test ROC AUC + PR AUC + log loss 給時序 OOS 觀察。
    # 同 (model_name, split_idx, evaluated_at) UPSERT,scripts/eval_walkforward.py
    # 重跑會覆蓋。供週重訓 A/B gate 用(walk-forward ROC < 舊 - 0.02 → rollback)。
    """
    CREATE TABLE IF NOT EXISTS ml_walkforward_results (
        model_name    TEXT NOT NULL,    -- 'short_pick' / per_strategy 名(e.g. 'gap_up')
        split_idx     INTEGER NOT NULL, -- 0-based,expanding window 第幾 split
        train_start   TEXT,             -- 'YYYY-MM-DD'
        train_end     TEXT,
        test_start    TEXT,
        test_end      TEXT,
        train_n       INTEGER,
        test_n        INTEGER,
        roc_auc       REAL,             -- test 端 ROC AUC
        pr_auc        REAL,             -- test 端 PR AUC
        log_loss      REAL,             -- test 端 log loss
        train_roc_auc REAL,             -- train 端 ROC AUC(overfit gap 參考)
        evaluated_at  TEXT NOT NULL,
        split_method  TEXT NOT NULL DEFAULT 'row',  -- 'row'(舊)/ 'date'(消 cross-sectional 虛高)
        PRIMARY KEY (model_name, split_idx, evaluated_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ml_walkforward_model "
    "ON ml_walkforward_results(model_name, evaluated_at DESC)",
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
        _migrate_paper_trades_add_trailing(conn)
        _migrate_analyst_targets_add_previous(conn)
        _migrate_ml_walkforward_add_split_method(conn)


def _migrate_ml_walkforward_add_split_method(conn) -> None:
    """主公拍板「by-date split」(2026-05-15):加 split_method TEXT 欄到既有
    ml_walkforward_results,標記 'row'(舊行為)或 'date'(新行為,消除
    cross-sectional 虛高)。舊資料 default 'row' 維持向下相容。

    SQLite 沒 ALTER TABLE ADD COLUMN IF NOT EXISTS。冪等。
    """
    cols = {
        r["name"] for r in conn.execute(
            "PRAGMA table_info(ml_walkforward_results)"
        ).fetchall()
    }
    if "split_method" not in cols:
        conn.execute(
            "ALTER TABLE ml_walkforward_results "
            "ADD COLUMN split_method TEXT NOT NULL DEFAULT 'row'"
        )


def _migrate_analyst_targets_add_previous(conn) -> None:
    """主公拍板「法人異動推播」(2026-05-08):加 previous_target_mean /
    previous_fetched_at 欄到既有 analyst_targets,讓 upsert 時保留前一次值
    供推播 / picks Δ 比對(避另開 history table 浪費空間)。冪等。
    """
    cols = {
        r["name"] for r in conn.execute(
            "PRAGMA table_info(analyst_targets)"
        ).fetchall()
    }
    if "previous_target_mean" not in cols:
        conn.execute(
            "ALTER TABLE analyst_targets ADD COLUMN previous_target_mean REAL"
        )
    if "previous_fetched_at" not in cols:
        conn.execute(
            "ALTER TABLE analyst_targets ADD COLUMN previous_fetched_at TEXT"
        )


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


def _migrate_paper_trades_add_trailing(conn) -> None:
    """主公拍板「動態停損 / 移動停利」(2026-05-06):加兩欄到既有 paper_trades。

      current_stop    REAL    當前停損價(初始 = stop_price,trailing 啟動上移)
      trailing_level  INTEGER 0=未觸發 / 1=保本 / 2=鎖2% / 3=鎖5%

    SQLite 沒 ALTER TABLE ADD COLUMN IF NOT EXISTS。冪等。
    """
    cols = {
        r["name"] for r in conn.execute(
            "PRAGMA table_info(paper_trades)"
        ).fetchall()
    }
    if "current_stop" not in cols:
        conn.execute("ALTER TABLE paper_trades ADD COLUMN current_stop REAL")
        # 既有 row 補 current_stop = stop_price(向下相容)
        conn.execute(
            "UPDATE paper_trades SET current_stop = stop_price "
            "WHERE current_stop IS NULL"
        )
    if "trailing_level" not in cols:
        conn.execute(
            "ALTER TABLE paper_trades ADD COLUMN trailing_level INTEGER DEFAULT 0"
        )


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


def upsert_shareholder_concentration(
    rows: Iterable[dict], db_path: str | Path | None = None,
) -> int:
    """寫入 / 更新「千張大戶」週快照(TDCC 股權分散表)。

    rows 每筆需有:sid, week_end, holders_1000up_count, total_holders。
    holders_pct / holders_delta_w 可選(fetcher 算好寫入,App 端讀)。
    """
    rows_list = list(rows)
    if not rows_list:
        return 0
    now = _now_iso()
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO shareholder_concentration
                (sid, week_end, holders_1000up_count, total_holders,
                 holders_pct, holders_delta_w, fetched_at)
            VALUES
                (:sid, :week_end, :holders_1000up_count, :total_holders,
                 :holders_pct, :holders_delta_w, :fetched_at)
            ON CONFLICT(sid, week_end) DO UPDATE SET
                holders_1000up_count=excluded.holders_1000up_count,
                total_holders=excluded.total_holders,
                holders_pct=excluded.holders_pct,
                holders_delta_w=excluded.holders_delta_w,
                fetched_at=excluded.fetched_at
            """,
            [
                {
                    "sid": str(r["sid"]),
                    "week_end": str(r["week_end"]),
                    "holders_1000up_count": int(r["holders_1000up_count"]),
                    "total_holders": int(r["total_holders"]),
                    "holders_pct": (
                        float(r["holders_pct"])
                        if r.get("holders_pct") is not None else None
                    ),
                    "holders_delta_w": (
                        int(r["holders_delta_w"])
                        if r.get("holders_delta_w") is not None else None
                    ),
                    "fetched_at": r.get("fetched_at") or now,
                }
                for r in rows_list
            ],
        )
    return len(rows_list)


def get_latest_shareholder_concentration(
    sid: str, db_path: str | Path | None = None,
) -> dict | None:
    """撈該 sid 最新一筆千張戶資料。沒資料回 None。

    給 notifier / Streamlit 長線卡 enrich 用 — 沒資料 graceful skip 不顯該欄。
    """
    with get_conn(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT sid, week_end, holders_1000up_count, total_holders, "
                "holders_pct, holders_delta_w, fetched_at "
                "FROM shareholder_concentration "
                "WHERE sid=? ORDER BY week_end DESC LIMIT 1",
                (str(sid),),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    if row is None:
        return None
    return dict(row)


def get_shareholder_concentration_for_sids(
    sids: Iterable[str], db_path: str | Path | None = None,
) -> dict[str, dict]:
    """批量撈多檔最新千張戶資料(回 {sid: row_dict})。

    給 _select_top_picks / render_picks_cards 一次 enrich 一批 picks 用,
    比 N 次 get_latest_shareholder_concentration 省 N 次 connect overhead。
    """
    sids_list = [str(s) for s in sids if s]
    if not sids_list:
        return {}
    placeholders = ",".join("?" * len(sids_list))
    with get_conn(db_path) as conn:
        try:
            rows = conn.execute(
                f"SELECT sc.sid, sc.week_end, sc.holders_1000up_count, "
                f"sc.total_holders, sc.holders_pct, sc.holders_delta_w, "
                f"sc.fetched_at "
                f"FROM shareholder_concentration sc "
                f"JOIN (SELECT sid, MAX(week_end) AS mw "
                f"      FROM shareholder_concentration "
                f"      WHERE sid IN ({placeholders}) GROUP BY sid) latest "
                f"  ON sc.sid=latest.sid AND sc.week_end=latest.mw",
                sids_list,
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
    return {r["sid"]: dict(r) for r in rows}


# ====================================================================
# 千張大戶「跨 sid 排行」helpers — 給 _page_big_buyer 用
# ====================================================================
#
# 3 個 helper 都回 list[dict],schema 統一:
#   sid / name / close / holders_1000up_count / holders_delta_w /
#   holders_pct / ml_prob / week_end
# 沒對應資料的欄位走 NULL(Streamlit 顯 N/A),不 raise。
#
# 設計考量:
# - JOIN stocks 帶名稱(沒對應 → name=None)
# - LEFT JOIN 最新 close(daily_prices 每 sid 取 MAX(date) 那行)
# - LEFT JOIN 最新 ml_prob(daily_picks 每 sid 取 MAX(trade_date) MAX(ml_prob))
# - 連續增加用 window function ROW_NUMBER() — SQLite 3.25+ 支援,Python 3.11
#   ships sqlite3 ≥ 3.39 OK
# ====================================================================

_RANKING_COLUMNS = [
    "sid", "name", "close", "holders_1000up_count",
    "holders_delta_w", "holders_pct", "ml_prob", "week_end",
]

# 抽出來給 3 個 helper 共用 — SELECT 帶 close + name + ml_prob enrich
_RANKING_SELECT = """
    sc.sid AS sid,
    s.name AS name,
    (SELECT close FROM daily_prices
       WHERE stock_id = sc.sid
       ORDER BY date DESC LIMIT 1) AS close,
    sc.holders_1000up_count AS holders_1000up_count,
    sc.holders_delta_w AS holders_delta_w,
    sc.holders_pct AS holders_pct,
    (SELECT MAX(ml_prob) FROM daily_picks
       WHERE sid = sc.sid AND ml_prob IS NOT NULL) AS ml_prob,
    sc.week_end AS week_end
"""


def _empty_ranking_rows() -> list[dict]:
    """空結果統一回 [],caller 自行 build pd.DataFrame(empty df 也保有 columns)。"""
    return []


def get_top_shareholder_movers(
    limit: int = 30,
    week_end: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """跨 sid 排行:本週千張戶人數增加 Top N(filter delta_w > 0)。

    week_end None → shareholder_concentration 最新一週。
    JOIN stocks 帶名稱、LEFT JOIN daily_prices 帶最新收盤、daily_picks 帶 ML 分數。
    沒對應 sid / 沒資料 → 空 list。
    """
    with get_conn(db_path) as conn:
        try:
            if week_end is None:
                row = conn.execute(
                    "SELECT MAX(week_end) AS w FROM shareholder_concentration "
                    "WHERE holders_delta_w IS NOT NULL AND holders_delta_w > 0"
                ).fetchone()
                week_end = row["w"] if row and row["w"] else None
            if not week_end:
                return _empty_ranking_rows()
            rows = conn.execute(
                f"""
                SELECT {_RANKING_SELECT}
                FROM shareholder_concentration sc
                LEFT JOIN stocks s ON s.stock_id = sc.sid
                WHERE sc.week_end = ?
                  AND sc.holders_delta_w IS NOT NULL
                  AND sc.holders_delta_w > 0
                ORDER BY sc.holders_delta_w DESC, sc.sid ASC
                LIMIT ?
                """,
                (week_end, int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return _empty_ranking_rows()
    return [dict(r) for r in rows]


def get_top_shareholder_concentration(
    limit: int = 30,
    week_end: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """跨 sid 排行:千張戶占比 Top N(ORDER BY holders_pct DESC)。

    week_end None → shareholder_concentration 最新一週。同 movers 的 enrich
    JOIN(stocks / daily_prices / daily_picks)。
    """
    with get_conn(db_path) as conn:
        try:
            if week_end is None:
                row = conn.execute(
                    "SELECT MAX(week_end) AS w FROM shareholder_concentration"
                ).fetchone()
                week_end = row["w"] if row and row["w"] else None
            if not week_end:
                return _empty_ranking_rows()
            rows = conn.execute(
                f"""
                SELECT {_RANKING_SELECT}
                FROM shareholder_concentration sc
                LEFT JOIN stocks s ON s.stock_id = sc.sid
                WHERE sc.week_end = ?
                  AND sc.holders_pct IS NOT NULL
                ORDER BY sc.holders_pct DESC, sc.sid ASC
                LIMIT ?
                """,
                (week_end, int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return _empty_ranking_rows()
    return [dict(r) for r in rows]


def get_consecutive_shareholder_increases(
    weeks: int = 2,
    limit: int = 30,
    db_path: str | Path | None = None,
) -> list[dict]:
    """連續 ≥ N 週 holders_delta_w > 0 的個股(以最新 N 週 per sid 判斷)。

    用 window function ROW_NUMBER() OVER (PARTITION BY sid ORDER BY week_end DESC)
    取每 sid 最新 N 週 → GROUP BY HAVING COUNT == N AND MIN(delta_w) > 0 篩選。

    資料不足 N 週 → 不入選(回空 list)。資料只一週時 weeks=2 永遠回空,
    符合「資料累積中」的預期行為。
    """
    if weeks < 1:
        return _empty_ranking_rows()
    with get_conn(db_path) as conn:
        try:
            rows = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT sid, week_end, holders_delta_w,
                           ROW_NUMBER() OVER (
                               PARTITION BY sid ORDER BY week_end DESC
                           ) AS rn
                    FROM shareholder_concentration
                    WHERE holders_delta_w IS NOT NULL
                ),
                qualified AS (
                    SELECT sid
                    FROM ranked
                    WHERE rn <= ?
                    GROUP BY sid
                    HAVING COUNT(*) = ? AND MIN(holders_delta_w) > 0
                ),
                latest_per_sid AS (
                    SELECT sid, MAX(week_end) AS mw
                    FROM shareholder_concentration
                    GROUP BY sid
                )
                SELECT {_RANKING_SELECT}
                FROM shareholder_concentration sc
                INNER JOIN qualified q ON q.sid = sc.sid
                INNER JOIN latest_per_sid lp
                    ON lp.sid = sc.sid AND lp.mw = sc.week_end
                LEFT JOIN stocks s ON s.stock_id = sc.sid
                ORDER BY sc.holders_delta_w DESC, sc.sid ASC
                LIMIT ?
                """,
                (int(weeks), int(weeks), int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return _empty_ranking_rows()
    return [dict(r) for r in rows]


# ====================================================================
# 「強者跟蹤」綜合頁 helpers — 給 _page_strong_follower 用
# ====================================================================
#
# 兩個 helper 走 institutional + shareholder_concentration 既有資料,組合成
# 「籌碼共識」訊號。沒新 fetcher,純 SQL 組合。
#
# get_top_inst_consensus:三大法人(外/投/自)連續 ≥ N 個交易日同時 net > 0
# get_strong_follower_composite:同時滿足法人連買 + 千張大戶週增加(交集)
#
# 設計考量(同 千張大戶 helpers,schema 對齊以便 UI 共用 _render_table):
# - JOIN stocks 帶名稱(沒對應 → name=None,UI 顯「—」)
# - 子查詢拿最新 close / ml_prob(LEFT JOIN 風險:有 sid 沒 close 仍要出列)
# - 排序 stable:第一鍵指標 desc,第二鍵 sid asc
# - 沒資料 / 表還沒建 → 回空 list,不 raise
# ====================================================================


def get_top_inst_consensus(
    min_days: int = 2,
    limit: int = 30,
    db_path: str | Path | None = None,
) -> list[dict]:
    """跨 sid 排行:三大法人(外資 / 投信 / 自營商)同時 net > 0 連續 ≥ N 個交易日。

    從 institutional 表撈每 sid 最新 min_days 日,filter 三家 net_buy_sell
    都 > 0 的天數 == min_days(完全共識)。回 list[dict],schema 對齊
    _page_strong_follower UI 用:
      sid / name / close / consensus_days / inst_net_total / last_date / ml_prob

    inst_net_total = 過去 min_days 日三家 net 加總,作為排序鍵(大者優先)。

    Args:
        min_days: 連續共識天數,預設 2(過嚴會空,過鬆失去訊號意義)
        limit: 回傳上限

    Returns:
        list[dict],沒對應資料的欄位 NULL。institutional 表不存在 → []
    """
    if min_days < 1:
        return _empty_ranking_rows()
    with get_conn(db_path) as conn:
        try:
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT stock_id AS sid, date,
                           foreign_buy_sell AS fb,
                           trust_buy_sell AS tb,
                           dealer_buy_sell AS dn,
                           (COALESCE(foreign_buy_sell, 0)
                            + COALESCE(trust_buy_sell, 0)
                            + COALESCE(dealer_buy_sell, 0)) AS net_3i,
                           ROW_NUMBER() OVER (
                               PARTITION BY stock_id ORDER BY date DESC
                           ) AS rn
                    FROM institutional
                ),
                qualified AS (
                    SELECT sid,
                           MAX(date) AS last_date,
                           SUM(net_3i) AS inst_net_total
                    FROM ranked
                    WHERE rn <= ?
                    GROUP BY sid
                    HAVING COUNT(*) = ?
                       AND MIN(fb) > 0
                       AND MIN(tb) > 0
                       AND MIN(dn) > 0
                )
                SELECT q.sid AS sid,
                       s.name AS name,
                       (SELECT close FROM daily_prices
                           WHERE stock_id = q.sid
                           ORDER BY date DESC LIMIT 1) AS close,
                       ? AS consensus_days,
                       q.inst_net_total AS inst_net_total,
                       q.last_date AS last_date,
                       (SELECT MAX(ml_prob) FROM daily_picks
                           WHERE sid = q.sid AND ml_prob IS NOT NULL) AS ml_prob
                FROM qualified q
                LEFT JOIN stocks s ON s.stock_id = q.sid
                ORDER BY q.inst_net_total DESC, q.sid ASC
                LIMIT ?
                """,
                (int(min_days), int(min_days), int(min_days), int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return _empty_ranking_rows()
    return [dict(r) for r in rows]


def get_strong_follower_composite(
    min_inst_days: int = 2,
    limit: int = 30,
    db_path: str | Path | None = None,
) -> list[dict]:
    """『強者跟蹤』綜合排行:同時命中「三大法人連買 ≥ N 日」+「最新一週千張戶增加」。

    交集邏輯:法人共識(get_top_inst_consensus 同條件)的 sid ∩ shareholder_
    concentration 最新一週 holders_delta_w > 0 的 sid。

    分數(composite_score):為了讓兩個量級不同的訊號可比,各自做 rank
    normalization 後加總:
      score = 法人 net_total 在交集內的 rank(0..1) + 千張戶 delta_w 的 rank(0..1)
    在 SQL 裡用 DENSE_RANK + COUNT() 計算,降低 Python 後處理。

    Args:
        min_inst_days: 法人共識門檻,預設 2
        limit: 回傳上限

    Returns:
        list[dict] keys: sid / name / close / consensus_days / inst_net_total /
                        holders_delta_w / composite_score / ml_prob
        資料不足 → []
    """
    if min_inst_days < 1:
        return _empty_ranking_rows()
    with get_conn(db_path) as conn:
        try:
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT stock_id AS sid, date,
                           foreign_buy_sell AS fb,
                           trust_buy_sell AS tb,
                           dealer_buy_sell AS dn,
                           (COALESCE(foreign_buy_sell, 0)
                            + COALESCE(trust_buy_sell, 0)
                            + COALESCE(dealer_buy_sell, 0)) AS net_3i,
                           ROW_NUMBER() OVER (
                               PARTITION BY stock_id ORDER BY date DESC
                           ) AS rn
                    FROM institutional
                ),
                inst_qualified AS (
                    SELECT sid,
                           MAX(date) AS last_date,
                           SUM(net_3i) AS inst_net_total
                    FROM ranked
                    WHERE rn <= ?
                    GROUP BY sid
                    HAVING COUNT(*) = ?
                       AND MIN(fb) > 0
                       AND MIN(tb) > 0
                       AND MIN(dn) > 0
                ),
                sh_latest AS (
                    SELECT sid, MAX(week_end) AS mw
                    FROM shareholder_concentration
                    GROUP BY sid
                ),
                joined AS (
                    SELECT iq.sid AS sid,
                           iq.inst_net_total AS inst_net_total,
                           iq.last_date AS last_date,
                           sc.holders_delta_w AS holders_delta_w
                    FROM inst_qualified iq
                    INNER JOIN sh_latest sl ON sl.sid = iq.sid
                    INNER JOIN shareholder_concentration sc
                        ON sc.sid = iq.sid AND sc.week_end = sl.mw
                    WHERE sc.holders_delta_w IS NOT NULL
                      AND sc.holders_delta_w > 0
                )
                SELECT j.sid AS sid,
                       s.name AS name,
                       (SELECT close FROM daily_prices
                           WHERE stock_id = j.sid
                           ORDER BY date DESC LIMIT 1) AS close,
                       ? AS consensus_days,
                       j.inst_net_total AS inst_net_total,
                       j.holders_delta_w AS holders_delta_w,
                       j.last_date AS last_date,
                       (
                          CAST(
                            (SELECT COUNT(*) FROM joined j2
                                WHERE j2.inst_net_total <= j.inst_net_total)
                            AS REAL
                          ) / NULLIF((SELECT COUNT(*) FROM joined), 0)
                          +
                          CAST(
                            (SELECT COUNT(*) FROM joined j3
                                WHERE j3.holders_delta_w <= j.holders_delta_w)
                            AS REAL
                          ) / NULLIF((SELECT COUNT(*) FROM joined), 0)
                       ) AS composite_score,
                       (SELECT MAX(ml_prob) FROM daily_picks
                           WHERE sid = j.sid AND ml_prob IS NOT NULL) AS ml_prob
                FROM joined j
                LEFT JOIN stocks s ON s.stock_id = j.sid
                ORDER BY composite_score DESC, j.inst_net_total DESC,
                         j.sid ASC
                LIMIT ?
                """,
                (
                    int(min_inst_days), int(min_inst_days),
                    int(min_inst_days), int(limit),
                ),
            ).fetchall()
        except sqlite3.OperationalError:
            return _empty_ranking_rows()
    return [dict(r) for r in rows]


def get_strong_follower_premium(
    min_inst_days: int = 3,
    min_delta_w: int = 1,
    top_n: int = 10,
    db_path: str | Path | None = None,
) -> list[dict]:
    """『高信心精選』三維交集:法人連買 ∩ 千張戶週增 ∩ ML 高信心。

    給 _page_strong_follower Tab 4「✨ 高信心精選」用。
    三維度比 get_strong_follower_composite 嚴一階:
      1. 法人共識:三大法人(外/投/自)連續 ≥ min_inst_days 日全 net > 0
      2. 大戶進場:最新一週 holders_delta_w >= min_delta_w
      3. ML 高信心:daily_picks.ml_prob 過 STRATEGY_ML_THRESHOLDS per-strategy 門檻
         若 DB 完全沒 ML cache → 自動 fallback 用前 2 維(reason_text 省略 ML)

    composite_score(rank-normalize 後加權):
      - 3D:法人 net rank * 0.4 + delta_w rank * 0.4 + ml_prob rank * 0.2
      - 2D fallback:法人 net rank * 0.5 + delta_w rank * 0.5

    回傳每 row 附 reason_text 推薦理由字串(UI st.caption 直接用)。

    Args:
        min_inst_days: 法人連買最小天數,預設 3(比 composite 嚴一階)
        min_delta_w: 千張戶週增最小值,預設 1
        top_n: 回傳上限,預設 10(精選頁設計 = top picks)

    Returns:
        list[dict] keys:sid / name / close / consensus_days / inst_net_total /
                        holders_delta_w / holders_1000up_count / ml_prob /
                        composite_score / last_date / reason_text
        資料不足 / 表不存在 → []
    """
    from src.strategies import STRATEGY_ML_THRESHOLDS  # late import 避循環

    if min_inst_days < 1 or top_n < 1:
        return _empty_ranking_rows()
    with get_conn(db_path) as conn:
        # 1. 偵測 ML cache 是否有資料(table 層級判斷,非 per-sid)
        try:
            ml_count_row = conn.execute(
                "SELECT COUNT(*) AS c FROM daily_picks "
                "WHERE ml_prob IS NOT NULL"
            ).fetchone()
            has_ml_cache = bool(
                ml_count_row and (ml_count_row["c"] or 0) > 0
            )
        except sqlite3.OperationalError:
            has_ml_cache = False

        # 2. 共用 CTE base(法人共識 + 千張戶最新週)
        base_cte = """
            WITH ranked AS (
                SELECT stock_id AS sid, date,
                       foreign_buy_sell AS fb,
                       trust_buy_sell AS tb,
                       dealer_buy_sell AS dn,
                       (COALESCE(foreign_buy_sell, 0)
                        + COALESCE(trust_buy_sell, 0)
                        + COALESCE(dealer_buy_sell, 0)) AS net_3i,
                       ROW_NUMBER() OVER (
                           PARTITION BY stock_id ORDER BY date DESC
                       ) AS rn
                FROM institutional
            ),
            inst_qualified AS (
                SELECT sid,
                       MAX(date) AS last_date,
                       SUM(net_3i) AS inst_net_total
                FROM ranked
                WHERE rn <= ?
                GROUP BY sid
                HAVING COUNT(*) = ?
                   AND MIN(fb) > 0
                   AND MIN(tb) > 0
                   AND MIN(dn) > 0
            ),
            sh_latest AS (
                SELECT sid, MAX(week_end) AS mw
                FROM shareholder_concentration
                GROUP BY sid
            )
        """

        if has_ml_cache and STRATEGY_ML_THRESHOLDS:
            # 3D 模式:加 ml_eligible CTE,per-strategy threshold filter
            ml_clauses = " OR ".join(
                "(strategy = ? AND ml_prob >= ?)"
                for _ in STRATEGY_ML_THRESHOLDS
            )
            ml_params: list = []
            for s, t in STRATEGY_ML_THRESHOLDS.items():
                ml_params.extend([s, float(t)])

            sql = f"""
                {base_cte},
                ml_eligible AS (
                    SELECT sid, MAX(ml_prob) AS ml_prob_max
                    FROM daily_picks
                    WHERE ml_prob IS NOT NULL
                      AND ({ml_clauses})
                    GROUP BY sid
                ),
                joined AS (
                    SELECT iq.sid AS sid,
                           iq.inst_net_total AS inst_net_total,
                           iq.last_date AS last_date,
                           sc.holders_delta_w AS holders_delta_w,
                           sc.holders_1000up_count AS holders_1000up_count,
                           me.ml_prob_max AS ml_prob
                    FROM inst_qualified iq
                    INNER JOIN sh_latest sl ON sl.sid = iq.sid
                    INNER JOIN shareholder_concentration sc
                        ON sc.sid = iq.sid AND sc.week_end = sl.mw
                    INNER JOIN ml_eligible me ON me.sid = iq.sid
                    WHERE sc.holders_delta_w IS NOT NULL
                      AND sc.holders_delta_w >= ?
                )
                SELECT j.sid AS sid,
                       s.name AS name,
                       (SELECT close FROM daily_prices
                           WHERE stock_id = j.sid
                           ORDER BY date DESC LIMIT 1) AS close,
                       ? AS consensus_days,
                       j.inst_net_total AS inst_net_total,
                       j.holders_delta_w AS holders_delta_w,
                       j.holders_1000up_count AS holders_1000up_count,
                       j.ml_prob AS ml_prob,
                       j.last_date AS last_date,
                       (
                           0.4 * CAST(
                             (SELECT COUNT(*) FROM joined j2
                                 WHERE j2.inst_net_total <= j.inst_net_total)
                             AS REAL
                           ) / NULLIF((SELECT COUNT(*) FROM joined), 0)
                           + 0.4 * CAST(
                             (SELECT COUNT(*) FROM joined j3
                                 WHERE j3.holders_delta_w <= j.holders_delta_w)
                             AS REAL
                           ) / NULLIF((SELECT COUNT(*) FROM joined), 0)
                           + 0.2 * CAST(
                             (SELECT COUNT(*) FROM joined j4
                                 WHERE j4.ml_prob <= j.ml_prob)
                             AS REAL
                           ) / NULLIF((SELECT COUNT(*) FROM joined), 0)
                       ) AS composite_score
                FROM joined j
                LEFT JOIN stocks s ON s.stock_id = j.sid
                ORDER BY composite_score DESC, j.inst_net_total DESC,
                         j.sid ASC
                LIMIT ?
            """
            params: tuple = (
                int(min_inst_days), int(min_inst_days),
                *ml_params,
                int(min_delta_w),
                int(min_inst_days),
                int(top_n),
            )
        else:
            # 2D fallback:沒 ML cache,前 2 維交集 + 各權重 0.5
            sql = f"""
                {base_cte},
                joined AS (
                    SELECT iq.sid AS sid,
                           iq.inst_net_total AS inst_net_total,
                           iq.last_date AS last_date,
                           sc.holders_delta_w AS holders_delta_w,
                           sc.holders_1000up_count AS holders_1000up_count
                    FROM inst_qualified iq
                    INNER JOIN sh_latest sl ON sl.sid = iq.sid
                    INNER JOIN shareholder_concentration sc
                        ON sc.sid = iq.sid AND sc.week_end = sl.mw
                    WHERE sc.holders_delta_w IS NOT NULL
                      AND sc.holders_delta_w >= ?
                )
                SELECT j.sid AS sid,
                       s.name AS name,
                       (SELECT close FROM daily_prices
                           WHERE stock_id = j.sid
                           ORDER BY date DESC LIMIT 1) AS close,
                       ? AS consensus_days,
                       j.inst_net_total AS inst_net_total,
                       j.holders_delta_w AS holders_delta_w,
                       j.holders_1000up_count AS holders_1000up_count,
                       NULL AS ml_prob,
                       j.last_date AS last_date,
                       (
                           0.5 * CAST(
                             (SELECT COUNT(*) FROM joined j2
                                 WHERE j2.inst_net_total <= j.inst_net_total)
                             AS REAL
                           ) / NULLIF((SELECT COUNT(*) FROM joined), 0)
                           + 0.5 * CAST(
                             (SELECT COUNT(*) FROM joined j3
                                 WHERE j3.holders_delta_w <= j.holders_delta_w)
                             AS REAL
                           ) / NULLIF((SELECT COUNT(*) FROM joined), 0)
                       ) AS composite_score
                FROM joined j
                LEFT JOIN stocks s ON s.stock_id = j.sid
                ORDER BY composite_score DESC, j.inst_net_total DESC,
                         j.sid ASC
                LIMIT ?
            """
            params = (
                int(min_inst_days), int(min_inst_days),
                int(min_delta_w),
                int(min_inst_days),
                int(top_n),
            )

        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return _empty_ranking_rows()

    # 3. Build reason_text(每 row 推薦理由字串)
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        cd = d.get("consensus_days") or 0
        dw = d.get("holders_delta_w") or 0
        ml = d.get("ml_prob")
        if ml is not None:
            d["reason_text"] = (
                f"🏛️ 三大法人連買 {cd} 天 | "
                f"🐋 千張戶週增 +{dw} | "
                f"🎯 ML {ml:.2f}"
            )
        else:
            d["reason_text"] = (
                f"🏛️ 三大法人連買 {cd} 天 | "
                f"🐋 千張戶週增 +{dw}"
            )
        out.append(d)
    return out


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


def _dump_paper_trades_snapshot(db_path: str | Path | None) -> None:
    """在 paper_trades 變動後 dump CSV + 自動 push GitHub(雲端永久化)。

    跟 _dump_watchlist_snapshot / _dump_trades_snapshot 同 pattern:
    - 一律 dump 進本地 paper_trades.csv(讓本機 streamlit 也能 reload 還原)
    - 有 GITHUB_PAT → fire-and-forget thread push 到 watchlist-sync 分支
    - 沒 PAT → 不開 thread,本機行為不變

    修補:2026-05-07 主公發現「昨天加的實測追蹤不見了」— 雲端 SQLite ephemeral
    重啟清光,paper_trades 從未被 dump 進 git snapshot。
    """
    try:
        from src import paper_trades_snapshot
        n = paper_trades_snapshot.dump_to_csv(db_path=db_path)
    except Exception as ex:  # noqa: BLE001
        logger.error("[GH_SYNC] dump paper_trades.csv 失敗:%s", ex)
        return
    if n < 0:
        return
    if not os.environ.get("GITHUB_PAT"):
        return
    try:
        csv_content = paper_trades_snapshot.dump_to_string(db_path=db_path)
    except Exception as ex:  # noqa: BLE001
        logger.error("[GH_SYNC] 取 paper_trades csv 字串失敗:%s", ex)
        return
    try:
        from src.github_sync import push_paper_trades_to_github
    except ImportError as ex:
        logger.warning(
            "[GH_SYNC] push_paper_trades_to_github 不存在 (%s),skip 推送", ex,
        )
        return
    _spawn_github_push_thread(
        push_paper_trades_to_github, csv_content, "paper_trades",
    )


def _dump_analyst_targets_snapshot(db_path: str | Path | None) -> None:
    """analyst_targets 表變動後 dump CSV + 自動 push GitHub(雲端永久化)。

    跟 _dump_paper_trades_snapshot 同 pattern:
    - dump 進本地 analyst_targets.csv
    - 有 GITHUB_PAT → fire-and-forget thread push 到 watchlist-sync 分支
    - 沒 PAT → 不開 thread,本機行為不變
    """
    try:
        from src import analyst_targets_snapshot
        n = analyst_targets_snapshot.dump_to_csv(db_path=db_path)
    except Exception as ex:  # noqa: BLE001
        logger.error("[GH_SYNC] dump analyst_targets.csv 失敗:%s", ex)
        return
    if n < 0:
        return
    if not os.environ.get("GITHUB_PAT"):
        return
    try:
        csv_content = analyst_targets_snapshot.dump_to_string(db_path=db_path)
    except Exception as ex:  # noqa: BLE001
        logger.error("[GH_SYNC] 取 analyst_targets csv 字串失敗:%s", ex)
        return
    try:
        from src.github_sync import push_analyst_targets_to_github
    except ImportError as ex:
        logger.warning(
            "[GH_SYNC] push_analyst_targets_to_github 不存在 (%s),skip 推送", ex,
        )
        return
    _spawn_github_push_thread(
        push_analyst_targets_to_github, csv_content, "analyst_targets",
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
      daily_prices.csv / institutional.csv / taiex.csv /
      watchlist.csv(2026-05-07 加,讓 actions runner 看到主公的關注股
      → fetch_analyst_targets.py --scope=watchlist 才不會回 0 檔)/
      pick_outcomes.csv(weekly backtest_picks.py dump,讓 daily-notify
      的「昨日複盤」section 直接吃 SQLite 不用每天重算)

    回 {csv stem: rows loaded} 給 caller 用 log。任何 csv 不存在 → skip。
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

    # 3b. financials.monthly_revenue(from backfill-revenue.yml weekly 8-shard)
    rev_csv = snapshot_dir / "monthly_revenue.csv"
    if rev_csv.exists():
        df = pd.read_csv(rev_csv, dtype={"stock_id": str})
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "stock_id": str(r["stock_id"]),
                "period_type": "monthly_revenue",
                "period": str(r["period"]),
                "revenue": (
                    float(r["revenue"]) if pd.notna(r.get("revenue")) else None
                ),
                "revenue_yoy": (
                    float(r["revenue_yoy"])
                    if pd.notna(r.get("revenue_yoy")) else None
                ),
                "eps": None,
                "roe": None,
            })
        if rows:
            upsert_financials(rows, db_path=db_path)
            counts["monthly_revenue"] = len(rows)

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

    # 5b. shareholder_concentration(TDCC 千張大戶週快照)— 從 weekly-shareholder-fetch
    # workflow dump 進 repo 的 CSV reload。沒檔(第一次部署 / fetch fail)→ skip。
    sc_csv = snapshot_dir / "shareholder_concentration.csv"
    if sc_csv.exists():
        try:
            df = pd.read_csv(sc_csv, dtype={"sid": str, "week_end": str})
        except pd.errors.EmptyDataError:
            df = pd.DataFrame()
        if not df.empty:
            rows = []
            for _, r in df.iterrows():
                sid_v = r.get("sid")
                week_v = r.get("week_end")
                cnt_v = r.get("holders_1000up_count")
                tot_v = r.get("total_holders")
                if (
                    not sid_v or pd.isna(sid_v)
                    or not week_v or pd.isna(week_v)
                    or pd.isna(cnt_v) or pd.isna(tot_v)
                ):
                    continue
                rows.append({
                    "sid": str(sid_v),
                    "week_end": str(week_v),
                    "holders_1000up_count": int(cnt_v),
                    "total_holders": int(tot_v),
                    "holders_pct": (
                        float(r["holders_pct"])
                        if pd.notna(r.get("holders_pct")) else None
                    ),
                    "holders_delta_w": (
                        int(r["holders_delta_w"])
                        if pd.notna(r.get("holders_delta_w")) else None
                    ),
                    "fetched_at": (
                        str(r["fetched_at"])
                        if pd.notna(r.get("fetched_at")) else None
                    ),
                })
            if rows:
                upsert_shareholder_concentration(rows, db_path=db_path)
                counts["shareholder_concentration"] = len(rows)

    # 6. TAIEX(獨立 csv,from daily_market_update)
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

    # 7. dividend(年度配息,from backfill-dividend.yml weekly 8-shard)
    div_csv = snapshot_dir / "dividend.csv"
    if div_csv.exists():
        df = pd.read_csv(div_csv, dtype={"stock_id": str})
        rows = []
        for _, r in df.iterrows():
            year_v = r.get("year")
            try:
                year_int = int(year_v) if pd.notna(year_v) else None
            except (TypeError, ValueError):
                year_int = None
            if year_int is None:
                continue
            rows.append({
                "stock_id": str(r["stock_id"]),
                "year": year_int,
                "cash_dividend": (
                    float(r["cash_dividend"])
                    if pd.notna(r.get("cash_dividend")) else 0
                ),
                "stock_dividend": (
                    float(r["stock_dividend"])
                    if pd.notna(r.get("stock_dividend")) else 0
                ),
                "ex_dividend_date": (
                    str(r["ex_dividend_date"])
                    if pd.notna(r.get("ex_dividend_date")) else None
                ),
            })
        if rows:
            upsert_dividend(rows, db_path=db_path)
            counts["dividend"] = len(rows)

    # 7b. news(TWSE 重大訊息,from news-notify.yml hourly)
    # 雲端容器重啟時讀進 SQLite,讓 sent_telegram / sent_discord 跨容器保留
    # 避免推過的重訊在新容器重啟時又再推一次(news_notify 走 sent flag dedup)。
    news_csv = snapshot_dir / "news.csv"
    if news_csv.exists():
        try:
            df = pd.read_csv(news_csv, dtype={"sid": str})
        except pd.errors.EmptyDataError:
            df = pd.DataFrame()
        if not df.empty:
            n_loaded = 0
            with get_conn(db_path) as conn:
                for _, r in df.iterrows():
                    if not r.get("url_hash") or pd.isna(r.get("url_hash")):
                        continue
                    try:
                        cur = conn.execute(
                            """
                            INSERT OR REPLACE INTO news
                                (sid, company_name, publish_date, publish_time,
                                 subject, article_no, description, fact_date,
                                 url_hash, sent_telegram, sent_discord, fetched_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(r["sid"]),
                                str(r.get("company_name") or ""),
                                str(r.get("publish_date") or ""),
                                str(r.get("publish_time") or ""),
                                str(r.get("subject") or ""),
                                str(r.get("article_no") or ""),
                                str(r.get("description") or ""),
                                str(r.get("fact_date") or "") or None,
                                str(r["url_hash"]),
                                int(r.get("sent_telegram") or 0),
                                int(r.get("sent_discord") or 0),
                                str(r.get("fetched_at") or _now_iso()),
                            ),
                        )
                        if cur.rowcount > 0:
                            n_loaded += 1
                    except Exception as e:  # noqa: BLE001
                        logger.debug("[PRELOAD] news row skip: %s", e)
            if n_loaded > 0:
                counts["news"] = n_loaded

    # 8. trades(P&L 紀錄)— delegate 給 portfolio_snapshot,只在表空時灌
    # 避免覆蓋本機使用者新加的交易
    try:
        from src.portfolio_snapshot import load_from_csv as _load_trades
        n_trades = _load_trades(snapshot_dir=snapshot_dir, db_path=db_path)
        if n_trades > 0:
            counts["trades"] = n_trades
    except Exception as e:  # noqa: BLE001
        logger.warning("[PRELOAD] trades load 失敗:%s", e)

    # 8b. paper_trades(實測追蹤)— delegate 給 paper_trades_snapshot,只在表空時灌
    # 修補:2026-05-07 主公發現「昨天加的實測追蹤不見了」— 雲端容器重啟清光 SQLite,
    # 沒這個 preload 紀錄就消失。同 trades 邏輯只在表為空時灌避免覆蓋新進的紀錄。
    try:
        from src.paper_trades_snapshot import (
            load_from_csv as _load_paper_trades,
        )
        n_paper = _load_paper_trades(
            snapshot_dir=snapshot_dir, db_path=db_path,
        )
        if n_paper > 0:
            counts["paper_trades"] = n_paper
    except Exception as e:  # noqa: BLE001
        logger.warning("[PRELOAD] paper_trades load 失敗:%s", e)

    # 8b2. watchlist(關注股)— 讓 actions runner 看到主公的 ☆ 名單
    # 修補:2026-05-07 主公發現 fetch_analyst_targets.py --scope=watchlist 在
    # GitHub Actions 跑時回「共 0 檔」,因為 fresh runner SQLite 是空的、
    # watchlist 表沒人灌。watchlist_snapshot.load_from_csv 走自己的
    # WATCHLIST_CSV(項目根 data/twse_snapshot/watchlist.csv)+ silent skip
    # 在 tmp_path 測試環境(不打到 repo)。
    try:
        from src.watchlist_snapshot import load_from_csv as _load_watchlist
        n_wl = _load_watchlist(db_path=db_path)
        if n_wl > 0:
            counts["watchlist"] = n_wl
    except Exception as e:  # noqa: BLE001
        logger.warning("[PRELOAD] watchlist load 失敗:%s", e)

    # 8c. analyst_targets(法人目標價)— 平日抓 watchlist+picks / 週日抓全市場
    # 雲端容器重啟還原。表已有資料時 INSERT OR REPLACE 不會覆蓋同 (sid, source) PK,
    # 但會把 CSV snapshot 內較新的 fetched_at 寫進去 → 這裡走「永遠 reload」,
    # 因目標價是覆蓋式更新而非歷史紀錄(跟 daily_picks 同邏輯,跟 paper_trades 不同)。
    try:
        from src.analyst_targets_snapshot import (
            load_from_csv as _load_analyst_targets,
        )
        n_at = _load_analyst_targets(
            snapshot_dir=snapshot_dir, db_path=db_path,
        )
        if n_at > 0:
            counts["analyst_targets"] = n_at
    except Exception as e:  # noqa: BLE001
        logger.warning("[PRELOAD] analyst_targets load 失敗:%s", e)

    # 9. daily_picks(precompute 預跑結果)— nightly workflow dump 進 repo,
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

    # 10. pick_outcomes(weekly backtest_picks.py 跑後 dump CSV)— daily-notify
    # 的「昨日複盤」section 從這撈昨天的 picks 實際報酬。
    po_csv = snapshot_dir / "pick_outcomes.csv"
    if po_csv.exists():
        df = pd.read_csv(po_csv, dtype={"sid": str, "pick_date": str, "strategy": str})
        records = df.to_dict("records")
        for r in records:
            for k, v in list(r.items()):
                if pd.isna(v):
                    r[k] = None
        if records:
            with get_conn(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO pick_outcomes (
                        pick_date, sid, strategy, entry_close,
                        return_d1, return_d3, return_d5, return_d10,
                        hit_target, stopped_out, evaluated_at
                    ) VALUES (
                        :pick_date, :sid, :strategy, :entry_close,
                        :return_d1, :return_d3, :return_d5, :return_d10,
                        :hit_target, :stopped_out, :evaluated_at
                    )
                    ON CONFLICT(pick_date, sid, strategy) DO UPDATE SET
                        entry_close=excluded.entry_close,
                        return_d1=excluded.return_d1,
                        return_d3=excluded.return_d3,
                        return_d5=excluded.return_d5,
                        return_d10=excluded.return_d10,
                        hit_target=excluded.hit_target,
                        stopped_out=excluded.stopped_out,
                        evaluated_at=excluded.evaluated_at
                    """,
                    records,
                )
            counts["pick_outcomes"] = len(records)

    return counts


def get_latest_trading_date(
    db_path: str | Path | None = None,
) -> str | None:
    """SQLite daily_prices 內最新一筆 date(個股,排除 TAIEX 大盤指數)。

    給 daily_notify / streamlit 用,週末 / 假日 today() 沒當日 close 時改用
    這個當篩選日期(避免「今日無入選」誤判)。

    **排除 TAIEX**:TAIEX 走 fetch_taiex 跟個股 STOCK_DAY_ALL 不同 endpoint,
    publication 時程也不同步。2026-05-04 事件後發現:fetch 早跑時個股還沒新
    資料但 TAIEX 已有 → MAX(date) 回 5/4 但個股 max=4/30 → 9 個策略
    `df.date.iloc[-1] == period_end` 全 fail。改用個股 MAX 避免不一致。

    daily_prices 空 → 回 None,caller 自己 fallback today。
    """
    with get_conn(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM daily_prices "
                "WHERE stock_id != 'TAIEX'"
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


# === pick_outcomes:把 daily_picks 撈出來算實際報酬(weekly backtest) ===

def dump_pick_outcomes(
    rows: list[dict],
    db_path: str | Path | None = None,
) -> int:
    """bulk UPSERT pick_outcomes rows。

    rows 每筆需含:pick_date, sid, strategy, entry_close, return_d1/d3/d5/d10,
    hit_target, stopped_out, evaluated_at。同 (pick_date, sid, strategy) 重跑
    覆蓋(報酬窗口拉長後重算的場景)。

    回 written row count(empty rows → 0)。
    """
    if not rows:
        return 0
    init_db(db_path)
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO pick_outcomes (
                pick_date, sid, strategy, entry_close,
                return_d1, return_d3, return_d5, return_d10,
                hit_target, stopped_out, evaluated_at
            ) VALUES (
                :pick_date, :sid, :strategy, :entry_close,
                :return_d1, :return_d3, :return_d5, :return_d10,
                :hit_target, :stopped_out, :evaluated_at
            )
            ON CONFLICT(pick_date, sid, strategy) DO UPDATE SET
                entry_close=excluded.entry_close,
                return_d1=excluded.return_d1, return_d3=excluded.return_d3,
                return_d5=excluded.return_d5, return_d10=excluded.return_d10,
                hit_target=excluded.hit_target,
                stopped_out=excluded.stopped_out,
                evaluated_at=excluded.evaluated_at
            """,
            rows,
        )
    return len(rows)


def get_pick_outcomes_for_date(
    pick_date: str,
    db_path: str | Path | None = None,
) -> list[dict]:
    """撈某日所有 pick_outcomes(per-strategy fire,跟 daily_picks 同 granularity)。

    給 notifier.format_yesterday_recap 用 — caller 自己 dedupe by sid /
    aggregate by strategy。empty → 空 list(該日還沒 evaluate 過或 daily_picks
    空)。
    """
    init_db(db_path)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT pick_date, sid, strategy, entry_close,
                   return_d1, return_d3, return_d5, return_d10,
                   hit_target, stopped_out, evaluated_at
            FROM pick_outcomes
            WHERE pick_date=?
            ORDER BY sid, strategy
            """,
            (pick_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_strategy_history_stats(
    db_path: str | Path | None = None,
    since: str | None = None,
) -> list[dict]:
    """聚合 pick_outcomes by strategy:N, D1/D3/D5/D10 平均報酬,命中率,停損率。

    給 app.py 「📊 策略歷史」頁面 by-strategy tab 用。each row =
    {strategy, n, avg_d1, avg_d3, avg_d5, avg_d10, hit_rate, stop_rate}
    依 avg_d5 desc 排序(讓主公一眼看最賺的策略)。

    since='YYYY-MM-DD' → 只算 pick_date >= since 的。AVG / SUM 用 SQL 一次算
    避免 Python loop 處理大量 rows。空表 → 空 list。
    """
    init_db(db_path)
    sql = """
        SELECT strategy,
               COUNT(*) AS n,
               AVG(return_d1)  AS avg_d1,
               AVG(return_d3)  AS avg_d3,
               AVG(return_d5)  AS avg_d5,
               AVG(return_d10) AS avg_d10,
               AVG(hit_target)  AS hit_rate,
               AVG(stopped_out) AS stop_rate
        FROM pick_outcomes
        WHERE return_d1 IS NOT NULL
    """
    args: list = []
    if since:
        sql += " AND pick_date >= ?"
        args.append(since)
    sql += " GROUP BY strategy ORDER BY avg_d5 DESC, n DESC"
    with get_conn(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_pick_outcomes_by_date(
    db_path: str | Path | None = None,
    days: int = 30,
) -> list[dict]:
    """每個 pick_date 的整體 outcome 摘要(跨策略一日合併)。

    給 app.py 「📊 策略歷史」by-date tab 用。each row =
    {pick_date, n, avg_d1, avg_d5, hit_rate, stop_rate}。
    最多回 `days` 天(by pick_date desc),空表 → []。
    """
    init_db(db_path)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT pick_date,
                   COUNT(*) AS n,
                   AVG(return_d1) AS avg_d1,
                   AVG(return_d5) AS avg_d5,
                   AVG(hit_target)  AS hit_rate,
                   AVG(stopped_out) AS stop_rate
            FROM pick_outcomes
            WHERE return_d1 IS NOT NULL
            GROUP BY pick_date
            ORDER BY pick_date DESC
            LIMIT ?
            """,
            (days,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pick_outcomes_raw(
    db_path: str | Path | None = None,
    since: str | None = None,
    strategy: str | None = None,
    limit: int = 5000,
) -> list[dict]:
    """撈 pick_outcomes 原始 rows(已 evaluate 的),給「明細」tab 顯示。

    pick_date desc, sid, strategy 排序。limit 守住手機渲染上限。
    """
    init_db(db_path)
    sql = (
        "SELECT pick_date, sid, strategy, entry_close, "
        "return_d1, return_d3, return_d5, return_d10, "
        "hit_target, stopped_out, evaluated_at "
        "FROM pick_outcomes WHERE return_d1 IS NOT NULL "
    )
    args: list = []
    if since:
        sql += "AND pick_date >= ? "
        args.append(since)
    if strategy:
        sql += "AND strategy = ? "
        args.append(strategy)
    sql += "ORDER BY pick_date DESC, sid ASC, strategy ASC LIMIT ?"
    args.append(limit)
    with get_conn(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_last_evaluated_pick_date(
    db_path: str | Path | None = None,
) -> str | None:
    """回 pick_outcomes 內最新一筆 pick_date(讓 notifier 自動找最近可用複盤日)。

    pick_outcomes 空 → None。
    """
    init_db(db_path)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(pick_date) AS d FROM pick_outcomes "
            "WHERE return_d1 IS NOT NULL"
        ).fetchone()
    return row["d"] if row and row["d"] else None


# === pick_shap_explanations:SHAP ML 解釋性 cache ===

def save_shap_explanation(
    pick_date: str,
    sid: str,
    strategy: str,
    top_features: list[dict],
    db_path: str | Path | None = None,
) -> None:
    """UPSERT 一筆 SHAP 解釋到 pick_shap_explanations。

    top_features: list of dict,每筆含 feature/value/contribution/contribution_pct/direction。
    JSON-serialize 後存 TEXT 欄。同 (pick_date, sid, strategy) 重跑覆蓋。
    """
    import json

    init_db(db_path)
    payload = json.dumps(top_features, ensure_ascii=False)
    now = _now_iso()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO pick_shap_explanations
                (pick_date, sid, strategy, top_features, generated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pick_date, sid, strategy) DO UPDATE SET
                top_features=excluded.top_features,
                generated_at=excluded.generated_at
            """,
            (pick_date, sid, strategy, payload, now),
        )


def get_shap_explanation(
    pick_date: str,
    sid: str,
    strategy: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict] | None:
    """撈 (pick_date, sid, strategy) 的 SHAP top features list。

    strategy=None → 撈該 (pick_date, sid) 第一筆(讓 Streamlit 不需知道 routing 就能查)。
    沒資料 → None。
    """
    import json

    init_db(db_path)
    with get_conn(db_path) as conn:
        if strategy is None:
            row = conn.execute(
                "SELECT top_features FROM pick_shap_explanations "
                "WHERE pick_date=? AND sid=? "
                "ORDER BY generated_at DESC LIMIT 1",
                (pick_date, sid),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT top_features FROM pick_shap_explanations "
                "WHERE pick_date=? AND sid=? AND strategy=?",
                (pick_date, sid, strategy),
            ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["top_features"])
    except (json.JSONDecodeError, TypeError):
        return None


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
    min_fires: int = 10,
) -> dict[str, float]:
    """回 {strategy_name: win_rate},每個 strategy 取最新 period_end 的 win_rate。

    給 App 端 _enrich_with_win_rate 用 — 不論 backtest 跑過多少次,只看最新。
    無資料 → 空 dict。

    min_fires:過濾低樣本 strategies(<10 fires 視為不可信噪音)。Phase 1
    新加策略 (eps_acceleration / high_yield_stable / inst_oversold_reversal /
    revenue_acceleration) 樣本不足會被 backtest 寫進 0-fire rows;若不過濾,
    win_rate=0% 會被 _enrich_with_win_rate 算進平均 → 卡片勝率被拉低。同樣
    防 1-fire 100% 之類的極端噪音(例 ma_squeeze_breakout 1 fire)。
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
            AND n_fires >= ?
            """,
            (min_fires,),
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


# === vbt_grid_results:vectorbt grid search 結果(2026-05-14 加) ===

def upsert_vbt_grid_results(
    rows: list[dict],
    db_path: str | Path | None = None,
) -> int:
    """bulk UPSERT vbt_grid_results。

    rows 每筆需含:strategy, params_hash, params_json, period_start, period_end,
    n_trades, total_return, sharpe, max_drawdown, win_rate, generated_at。
    回 row count。
    """
    if not rows:
        return 0
    init_db(db_path)
    with get_conn(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO vbt_grid_results (
                strategy, params_hash, params_json, period_start, period_end,
                n_trades, total_return, sharpe, max_drawdown, win_rate, generated_at
            ) VALUES (
                :strategy, :params_hash, :params_json, :period_start, :period_end,
                :n_trades, :total_return, :sharpe, :max_drawdown, :win_rate, :generated_at
            )
            ON CONFLICT(strategy, params_hash) DO UPDATE SET
                params_json=excluded.params_json,
                period_start=excluded.period_start,
                period_end=excluded.period_end,
                n_trades=excluded.n_trades,
                total_return=excluded.total_return,
                sharpe=excluded.sharpe,
                max_drawdown=excluded.max_drawdown,
                win_rate=excluded.win_rate,
                generated_at=excluded.generated_at
            """,
            rows,
        )
    return len(rows)


def load_vbt_grid_results(
    strategy: str | None = None,
    top_n: int | None = None,
    db_path: str | Path | None = None,
):
    """撈 vbt_grid_results,按 sharpe DESC 排。

    Args:
        strategy: None = 全策略;否則只撈該 strategy
        top_n: None = 全部;否則只回前 N

    回 pd.DataFrame(空 → 空 DataFrame)。
    """
    import pandas as _pd

    init_db(db_path)
    with get_conn(db_path) as conn:
        sql = "SELECT * FROM vbt_grid_results"
        args: list = []
        if strategy:
            sql += " WHERE strategy=?"
            args.append(strategy)
        sql += " ORDER BY sharpe DESC"
        if top_n is not None and top_n > 0:
            sql += " LIMIT ?"
            args.append(int(top_n))
        rows = conn.execute(sql, tuple(args)).fetchall()
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


def dump_shareholder_concentration_csv(
    snapshot_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> int:
    """SQLite shareholder_concentration → CSV(覆寫)。回行數(skip 時回 -1)。

    給 fetcher 跑完後寫 data/twse_snapshot/shareholder_concentration.csv,
    workflow commit 進 repo 後雲端 / 本機 boot 都能 reload。

    Silent skip(回 -1):
    - 預設 snapshot_dir + DB 不在 PROJECT_ROOT 底下(pytest tmp_path)→ 不寫真實 CSV
    """
    import pandas as pd

    if snapshot_dir is None:
        # 模仿 analyst_targets_snapshot._db_inside_project 的 silent-skip 邏輯
        raw = str(db_path) if db_path is not None else str(config.DATABASE_PATH)
        p = Path(raw)
        if not p.is_absolute():
            p = config.PROJECT_ROOT / p
        try:
            p.resolve().relative_to(config.PROJECT_ROOT.resolve())
        except ValueError:
            return -1
        snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"

    snapshot_dir = Path(snapshot_dir)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT sid, week_end, holders_1000up_count, total_holders, "
            "holders_pct, holders_delta_w, fetched_at "
            "FROM shareholder_concentration "
            "ORDER BY sid ASC, week_end DESC"
        ).fetchall()
    df = pd.DataFrame(
        [dict(r) for r in rows],
        columns=[
            "sid", "week_end", "holders_1000up_count", "total_holders",
            "holders_pct", "holders_delta_w", "fetched_at",
        ],
    )
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / "shareholder_concentration.csv"
    df.to_csv(path, index=False)
    return len(df)


# === 個股深度頁 helpers(2026-05-15 加) ===
#
# 「📊 個股深度」聚合頁(C 計畫最後一件)— 從任何 page 點 sid → 跳到該頁
# 看 K 線 + 籌碼 + ML 解釋 + 新聞。下列 6 個 helper 全部 sid-scoped,
# 給 _page_stock_detail() 在 app.py 內 4 個 tab 使用。
#
# 設計取捨:
# - K 線 helper 連同 MA20/60 + BB(20,2) 一起算 — 用 local import indicators
#   避免 database 模組永久依賴 indicators(只在這個函式內生效)。
# - 各 helper 都 init_db()(跟 file 內其他 read helper 一致)— 雲端首次 boot
#   tmp DB 也能呼叫不炸。
# - 找不到資料一律回空 list / 空 DataFrame,讓 page 端 fallback render
#   「無資料」訊息,而不是 raise(個股深度頁本質就是「能看多少看多少」)。


def get_stock_kline_with_indicators(
    sid: str,
    days: int = 60,
    db_path: str | Path | None = None,
):
    """近 N 天 OHLCV + MA20/MA60 + BB(20,2)。回 pandas DataFrame。

    SQL 多撈 80 天歷史讓 MA60 / BB(20) 在最後 N 天都有滿值(rolling 邊界補滿),
    然後 tail(days) 切出對齊。沒資料回空 DataFrame(0 row, columns 不保證)。

    Returns columns: date, open, high, low, close, volume, ma20, ma60,
                     bb_upper, bb_mid, bb_lower
    """
    import pandas as pd
    from src import indicators as ind

    init_db(db_path)
    fetch_days = days + 80
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume "
            "FROM daily_prices WHERE stock_id=? "
            "ORDER BY date DESC LIMIT ?",
            (sid, fetch_days),
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=[
            "date", "open", "high", "low", "close", "volume",
            "ma20", "ma60", "bb_upper", "bb_mid", "bb_lower",
        ])
    df = pd.DataFrame([dict(r) for r in rows])
    df = df.sort_values("date").reset_index(drop=True)
    df["ma20"] = ind.sma(df, 20)
    df["ma60"] = ind.sma(df, 60)
    bb = ind.bollinger(df, period=20, num_std=2.0)
    df["bb_upper"] = bb["upper"]
    df["bb_mid"] = bb["mid"]
    df["bb_lower"] = bb["lower"]
    # tail(days) — MA60/BB20 在首段是 NaN(歷史不足),保留讓 plotly 自動斷線
    return df.tail(days).reset_index(drop=True)


def get_inst_history(
    sid: str,
    days: int = 7,
    db_path: str | Path | None = None,
) -> list[dict]:
    """近 N 日三大法人買賣超(外資/投信/自營商,單位:股,UI 自行 / 1000 轉張)。

    Sort DESC by date,空 list 表示該 sid 在覆蓋範圍外(法人覆蓋率有限,
    主要是高市值 / 關注清單個股)。
    """
    init_db(db_path)
    with get_conn(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT date, foreign_buy_sell, trust_buy_sell, "
                "dealer_buy_sell FROM institutional "
                "WHERE stock_id=? ORDER BY date DESC LIMIT ?",
                (sid, days),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    return [dict(r) for r in rows]


def get_shareholder_history(
    sid: str,
    weeks: int = 12,
    db_path: str | Path | None = None,
) -> list[dict]:
    """近 N 週千張戶人數歷史。TDCC 週快照,週六凌晨抓上週五公布資料。

    Sort ASC by week_end(讓 UI 直接畫 bar 不用再 reverse),空 list 表示
    該 sid 不在 TDCC 覆蓋(通常是上櫃小型股或新上市)。
    """
    init_db(db_path)
    with get_conn(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT week_end, holders_1000up_count, total_holders, "
                "holders_pct, holders_delta_w FROM shareholder_concentration "
                "WHERE sid=? ORDER BY week_end DESC LIMIT ?",
                (sid, weeks),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    # DESC 撈完反轉成 ASC,UI 畫圖時間軸由左到右
    return [dict(r) for r in reversed(rows)]


def get_news_for_sid(
    sid: str,
    days: int = 7,
    db_path: str | Path | None = None,
) -> list[dict]:
    """近 N 日該 sid 重大訊息(TWSE t187ap04_L)。

    Sort DESC(最新先),只回 UI 渲染需要的欄位。沒資料回空 list。
    """
    from datetime import date, timedelta

    init_db(db_path)
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with get_conn(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT publish_date, publish_time, subject, article_no, "
                "description, fact_date FROM news "
                "WHERE sid=? AND publish_date >= ? "
                "ORDER BY publish_date DESC, publish_time DESC",
                (sid, cutoff),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    return [dict(r) for r in rows]


def get_pick_history_for_sid(
    sid: str,
    limit: int = 30,
    db_path: str | Path | None = None,
) -> list[dict]:
    """該 sid 過去命中過的策略 + 後續 d5 報酬。daily_picks LEFT JOIN pick_outcomes。

    LEFT JOIN — 還沒 evaluate 到的最新 pick(d5 未到)會以 None 出現,
    UI 可顯示「待結算」。Sort DESC by pick_date,限 limit 筆避免列表爆炸。
    """
    init_db(db_path)
    with get_conn(db_path) as conn:
        try:
            rows = conn.execute(
                """
                SELECT p.trade_date AS pick_date, p.strategy, p.score,
                       p.ml_prob,
                       o.entry_close, o.return_d1, o.return_d5, o.return_d10,
                       o.hit_target, o.stopped_out
                FROM daily_picks p
                LEFT JOIN pick_outcomes o
                  ON p.trade_date = o.pick_date
                 AND p.sid = o.sid
                 AND p.strategy = o.strategy
                WHERE p.sid = ?
                ORDER BY p.trade_date DESC, p.strategy ASC
                LIMIT ?
                """,
                (sid, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    return [dict(r) for r in rows]


def get_shap_for_sid_latest(
    sid: str,
    db_path: str | Path | None = None,
) -> dict | None:
    """該 sid 最新一筆 SHAP 解釋(任何 pick_date / strategy 取最新)。

    回 dict {pick_date, strategy, top_features: list[dict]} 或 None
    (該 sid 從未進過 daily_picks → 沒 SHAP)。
    top_features 已 json.loads 解開。
    """
    import json

    init_db(db_path)
    with get_conn(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT pick_date, strategy, top_features "
                "FROM pick_shap_explanations "
                "WHERE sid=? "
                "ORDER BY pick_date DESC, generated_at DESC LIMIT 1",
                (sid,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    if not row:
        return None
    try:
        top = json.loads(row["top_features"])
    except (json.JSONDecodeError, TypeError):
        top = []
    return {
        "pick_date": row["pick_date"],
        "strategy": row["strategy"],
        "top_features": top,
    }


__all__ = [
    "get_conn",
    "init_db",
    "upsert_stocks",
    "upsert_daily_prices",
    "upsert_institutional",
    "upsert_financials",
    "upsert_dividend",
    "upsert_daily_metrics",
    "upsert_shareholder_concentration",
    "get_latest_shareholder_concentration",
    "get_shareholder_concentration_for_sids",
    "get_top_shareholder_movers",
    "get_top_shareholder_concentration",
    "get_consecutive_shareholder_increases",
    "get_top_inst_consensus",
    "get_strong_follower_composite",
    "get_strong_follower_premium",
    "dump_shareholder_concentration_csv",
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
    "save_shap_explanation",
    "get_shap_explanation",
    "get_stock_kline_with_indicators",
    "get_inst_history",
    "get_shareholder_history",
    "get_news_for_sid",
    "get_pick_history_for_sid",
    "get_shap_for_sid_latest",
    "SCHEMA",
]
