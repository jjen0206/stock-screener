# 林恩如選股法 — 功能 Spec 探索報告(v2:完全隔離架構)

> **狀態**:純探索 / 等主公拍板,**尚未實作**。
> **撰寫日期**:2026-05-19(v1)/ 2026-05-19(v2 改架構)
> **架構決定**(主公拍板 2026-05-19):
>   - 「我想把它拉出來 做一個獨立的功能 不要跟其他共用 比較不會混亂」
>   - **採完全隔離架構**:獨立 feature 模組、獨立 strategy、獨立 paper trading table、獨立 UI、獨立 cron、SQLite table prefix 隔離
>   - 放棄 v1 報告裡「跟波段共用 `eps_positive_years` + `strategy_profile` schema」的設計
> **跟波段任務的關係**:**完全切割**,兩邊各自實作所需 feature / schema,不互相 import / refactor / 共用 helper

---

## TL;DR — v2 軍師建議

**先做「方案 B(lite,隔離版)」 — 純 backtest 驗證,不上 UI / 不開新表。**

關鍵跟 v1 不變:
1. **林恩如「不停損 + 達停利」哲學跟現有 paper_trading 衝突** — 既然要完全隔離,不再走「`paper_trades` 加 `strategy_profile` 欄位」的路徑,改開**新表 `lin_enru_trades`** 從一開始就 schema-level 設計「沒 stop_price」「沒 trailing」
2. **現有 `screen_long` 跟 `screen_high_yield_stable` 沒有 52 週新高 timing** — 林恩如方法的精髓就在這個 timing,值得驗證有沒有 alpha
3. **GATE 設計不變**:Phase B backtest 沒過 → 整包不上線

跟 v1 不同(隔離後):
1. **3 個 features helper 各自獨立寫,不抽 `screen_long._evaluate_long` 內邏輯** — 估工時 **+1.5h**
2. **新開 `lin_enru_trades` 表**,不動 `paper_trades` schema、不加 `strategy_profile` 欄位 — schema 部分**反而 -2h**(不用走 SQLite status CHECK 重建表)
3. **UI 不 reuse `render_picks_cards`** — 新寫 `src/lin_enru_ui_cards.py`,估 **+2h**
4. **獨立 GHA workflow** — `lin-enru-daily.yml`(若 Phase D 上),估 **+1.5h**
5. 全部 Phase A-D 完整版淨工時 **30-35h**(v1 估 30-40h,隔離反而沒爆增,因為 schema 簡化抵消 UI 重複)
6. **若林恩如方法 backtest 證明沒 alpha → 整包 5 分鐘砍掉**(對比 v1 要 unwind `strategy_profile` 跨表 migration)

---

## Part 0:隔離原則(主公拍板)

| 原則 | 具體實作 | 不能做的事 |
|---|---|---|
| 獨立 feature 模組 | `src/features/lin_enru/eps_streak.py` 等 | ❌ 不能 import `src/screener_long.py` 任何 helper |
| 獨立 strategy 模組 | `src/strategies/lin_enru/screener.py` | ❌ 不能加進 `STRATEGY_LABELS` dict、不能進 `run_all_strategies` 聚合 |
| 獨立 paper trading 表 | 新表 `lin_enru_trades`(schema 直接設計「沒 stop」) | ❌ 不能在 `paper_trades` 加 `strategy_profile` 欄位 |
| 獨立 UI | `_page_lin_enru()` in `app.py` + 獨立 `src/lin_enru_ui_cards.py` | ❌ 不能塞進 `_page_long` / `_page_short` sub-tab |
| 獨立 cron | `.github/workflows/lin-enru-daily.yml` | ❌ 不能掛 `daily-notify.yml` 既有 step |
| SQLite namespace 隔離 | table prefix `lin_enru_*` 在同一個 `cache.db`(不另開檔) | ❌ 不能把 raw data 也複製一份(daily_prices / financials 仍共讀) |

**主公接受的代價**:
- 開發工時 +10-15%(部分 code 重複)
- 主公要切到專屬 tab 才能看林恩如(這正是「獨立」本意)
- 殖利率算法若日後改進,要同步改 2 處(`screen_long` 跟 `lin_enru` 各一)

---

## Part 1：林恩如方法 → 具體 spec(維持 v1)

### 1.1 進場三條件(全 AND)

| # | 哲學 | 條件 | 資料源 |
|---|---|---|---|
| A | 連續多年 EPS 正 | `financials.yearly_eps` 連續 ≥ 5 年 > 0 | **缺 feature**(從 `financials.quarterly` 自加總,**林恩如自己 derive,不 reuse**) |
| B | 高殖利率穩定 | `daily_metrics.dividend_yield >= 5%` AND `consecutive_dividend_years >= 5` | raw data 共用、helper **林恩如自己寫一份** |
| C | 創 52 週新高 | `close >= MAX(close, 過去 252 個交易日) × 0.99` | `daily_prices` 共用、helper **林恩如自己寫**(不 refactor `screen_volume_breakout`) |

### 1.2 出場規則(維持 v1)

| 觸發 | 條件 | 出場 status / reason |
|---|---|---|
| 達停利 | `current_close >= entry × (1 + target_pct)`,target_pct=30%(可調) | `closed` / `target_hit` |
| 基本面崩 | 最近 2 季 `financials.quarterly.eps` 都 ≤ 0 | `closed` / `fundamental_break` |
| 配息中斷 | 最近 1 個會計年度 `cash_dividend == 0` | `closed` / `dividend_stop` |
| 時間 cap | 進場後 750 交易日(約 3 年) | `closed` / `max_hold_timeout` |
| 停損 | **完全沒有** | — |

### 1.3 部位 / 輪轉
- 首版**不做** portfolio rotation(賣 A 自動買 B);主公手動操作
- 單檔倉位 5-10%,但首版**不做 portfolio sizing**;主公手動

---

## Part 2：隔離後的檔案結構(file tree)

```
src/
├── features/
│   └── lin_enru/                       # 🆕 林恩如專屬 feature module
│       ├── __init__.py
│       ├── eps_streak.py               # 連 N 年 EPS 正(自己讀 financials)
│       ├── price_high.py               # 52 週新高判定
│       └── dividend_stability.py       # 殖利率穩定度 + 連續配息(自己寫,不 reuse)
│
├── strategies/
│   └── lin_enru/                       # 🆕 林恩如專屬 strategy module
│       ├── __init__.py
│       ├── screener.py                 # screen_lin_enru()  主邏輯
│       └── params.py                   # DEFAULT_LIN_ENRU_PARAMS
│
├── lin_enru_paper_trading.py           # 🆕 lin_enru_trades 表的 CRUD + evaluate
├── lin_enru_backtest.py                # 🆕 simulate_outcome_lin_enru + sweep runner
├── lin_enru_ui_cards.py                # 🆕 卡片 helper(無 stop_loss 欄位)
│
├── strategies.py                       # 🚫 不動,17 套維持原樣
├── screener_long.py                    # 🚫 不動
├── paper_trading.py                    # 🚫 不動,paper_trades 表維持原 schema
├── ui_cards.py                         # 🚫 不動
└── database.py                         # ✏️ 加 CREATE TABLE lin_enru_trades 一段,其他不動

app.py                                  # ✏️ 加 _page_lin_enru() + PAGES list 加 "🌱 存股輪轉"

.github/workflows/
└── lin-enru-daily.yml                  # 🆕(Phase D 才加)獨立 cron,不掛 daily-notify

docs/
├── lin_enru_strategy_proposal.md       # 本檔案
└── lin_enru_backtest_results.md        # 🆕 Phase B3 跑完 sweep 後寫
```

**重點**:`strategies.py`、`screener_long.py`、`paper_trading.py`、`ui_cards.py` **完全不動一行**。

---

## Part 3：現有資料 / 策略對照(維持 v1,只簡述)

詳細見 v1 Part 2 — 結論不變:
- **林恩如不是改包裝** — 17 套策略 + `screen_long` 都沒「連 5+ 年 EPS 正 + 殖利率穩定 + 52 週新高」AND
- `screen_long` 跟 `screen_high_yield_stable` 缺 timing — 林恩如方法的「創新高才進」是關鍵差異化
- 缺 2 個基礎 feature(`yearly_eps_streak` / `52_week_high`)— **隔離版各自獨立寫**,不抽 helper

---

## Part 4：`lin_enru_trades` 新表設計

不動 `paper_trades`,改開新表 — schema-level 直接表達林恩如哲學:

```sql
-- 加進 src/database.py 的 schema migration block

CREATE TABLE IF NOT EXISTS lin_enru_trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sid                 TEXT NOT NULL,
    name                TEXT,
    entry_date          TEXT NOT NULL,
    entry_price         REAL NOT NULL CHECK(entry_price > 0),
    target_price        REAL NOT NULL,                  -- 停利價(= entry × (1+target_pct))
    target_pct          REAL NOT NULL,                  -- 設定的停利 %(typically 0.30)
    max_hold_days       INTEGER NOT NULL DEFAULT 750,   -- 時間 cap,約 3 年

    -- 進場時三條件 snapshot(對照 / debug 用)
    eps_streak_years    INTEGER,                        -- 連續年數
    dividend_yield_pct  REAL,                           -- 當下殖利率
    consec_div_years    INTEGER,                        -- 連續配息年數
    high_52w_price      REAL,                           -- 進場時的 52 週高

    -- 出場(active 時為 NULL)
    exit_date           TEXT,
    exit_price          REAL,
    exit_reason         TEXT CHECK(exit_reason IN (
                            'target_hit',           -- 達停利 +target_pct
                            'fundamental_break',    -- 連 2 季 EPS ≤ 0
                            'dividend_stop',        -- 配息中斷
                            'max_hold_timeout'      -- 750 日 cap
                        )),
    return_pct          REAL,                           -- 實際報酬 %

    status              TEXT NOT NULL CHECK(status IN ('active', 'closed')),
    notes               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT,
    UNIQUE(sid, entry_date)
);

CREATE INDEX IF NOT EXISTS idx_lin_enru_trades_status
    ON lin_enru_trades(status);
CREATE INDEX IF NOT EXISTS idx_lin_enru_trades_entry_date
    ON lin_enru_trades(entry_date DESC);
```

**跟 `paper_trades` 對比**(刻意設計):

| 欄位 | `paper_trades` | `lin_enru_trades` | 為何不同 |
|---|---|---|---|
| `stop_price` | NOT NULL ✓ | **無此欄位** | 林恩如不停損 |
| `current_stop` | 有 | **無** | 沒 stop 就沒 current_stop |
| `trailing_level` | 0/1/2/3 | **無** | 不跑 trailing |
| `hold_days` | DEFAULT 5(短) | `max_hold_days` DEFAULT 750(長) | 哲學完全不同 |
| `status` CHECK | 5 種 (`win`/`lose`/`timeout_*`) | 2 種 (`active`/`closed`) | 林恩如出場原因放 `exit_reason` 不放 status |
| `exit_reason` | 無 | 4 種 enum | 林恩如出場原因分類有意義(用於分析:多少 % 走停利 vs 基本面崩) |
| 進場 snapshot 欄位 | 無 | `eps_streak_years` 等 4 個 | 林恩如要記錄「為什麼選這檔」,事後 debug 用 |

**migration 步驟**:
1. 在 `src/database.py` 的 `_SCHEMA_STATEMENTS` list(現有 schema CREATE 集中地)末尾加上述 SQL
2. 第一次 boot 時 `init_db()` 自動 CREATE,**沒 ALTER、沒 migrate** — 全新表
3. 不需要動現有 `paper_trades` 既有 row
4. **回滾**:`DROP TABLE lin_enru_trades` 一行,raw data 不受影響

**對比 v1 方案(改 `paper_trades` schema)的 migration 複雜度**:
- v1 要走 `ALTER TABLE paper_trades ADD COLUMN strategy_profile`
- v1 要走 SQLite status CHECK 重建表(RENAME → CREATE → INSERT SELECT → DROP)
- v1 改完後 `paper_trades_snapshot.py` 的 CSV dump/load 要適配新欄位
- v2 隔離方案完全跳過這些 — **2h → 0.5h**

---

## Part 5：跟現有 paper trading 衝突細節(更新版)

v1 詳列「`paper_trades.stop_price NOT NULL` 等衝突欄位」,**v2 隔離後這些衝突不存在** — 林恩如完全不寫 `paper_trades`,所以不衝突。

**新增風險**:
- `evaluate_active_trades`(現有,跑 swing 結算)跟 `evaluate_active_lin_enru_trades`(新)是**兩個獨立函式**,sidebar / cron 要兩邊都 call
- 主公到時候要決定:「實測追蹤」頁顯 `paper_trades`,「存股追蹤」頁顯 `lin_enru_trades`,**兩頁分開 not 一頁兩 tab**(隔離原則)
- `performance_analysis.py` 的策略表現統計目前只看 `paper_trades` — **林恩如有自己的統計頁**,不混進現有績效分析(否則就違反隔離)

---

## Part 6：2 個缺 feature 的補法(隔離版)

### 6.1 `eps_streak_years`(連 N 年 EPS 正)

**檔案**:`src/features/lin_enru/eps_streak.py`(新檔,**不放 `src/features_fundamental.py` 共用模組**)

**SQL**:從 `financials.period_type='quarterly'` 自己加總(資料源跟 `screen_eps_acceleration` 同,但 helper 各寫各的):
```python
def compute_yearly_eps_streak(stock_id: str) -> int:
    """回連續年數(從最近年往回,4 季都齊且 sum > 0 才算)。
    
    林恩如專用 helper。不 reuse screen_eps_acceleration 內邏輯(那邊看季 YoY)。
    """
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT SUBSTR(period, 1, 4) AS year,
                   SUM(eps) AS yearly_eps,
                   COUNT(*) AS q_count
            FROM financials
            WHERE stock_id = ? AND period_type='quarterly'
                  AND eps IS NOT NULL
            GROUP BY year
            ORDER BY year DESC
        """, (stock_id,)).fetchall()
    
    streak = 0
    for r in rows:
        if r["q_count"] == 4 and r["yearly_eps"] > 0:
            streak += 1
        else:
            break
    return streak
```

**工時**:1.5h(寫 + tests:全齊 / 缺季 / EPS 為 0 / 完全沒財報 4 case)

### 6.2 `is_at_52_week_high`(close ≥ 252 日 max × 0.99)

**檔案**:`src/features/lin_enru/price_high.py`(新檔,**不 refactor `_evaluate_volume_breakout`**)

**邏輯**(獨立寫,跟 `screen_volume_breakout` 的 20 日邏輯**沒共用 helper**):
```python
def is_at_52_week_high(
    price_df: pd.DataFrame,  # 至少含 daily close 過去 252 日
    tolerance: float = 0.99,
) -> tuple[bool, float]:
    """回 (是否近 52 週新高, 當下 52 週高價)。
    
    林恩如專用 helper。即使 _evaluate_volume_breakout 的 20 日邏輯結構類似,
    為了隔離,不 refactor / 不抽 _is_n_day_high()。
    """
    if len(price_df) < 252:
        return False, 0.0  # 資料不足,不命中
    
    window = price_df["close"].iloc[-253:-1]  # 過去 252 日(不含今日)
    high_252 = float(window.max())
    today_close = float(price_df["close"].iloc[-1])
    
    is_at_high = today_close >= high_252 * tolerance
    return is_at_high, high_252
```

**工時**:1.5h(寫 + tests:剛剛突破 / 在窗口 / 接近但沒到 / 資料不足 252 日 4 case)

### 6.3 `consecutive_dividend_years`(連續配息年數)

**檔案**:`src/features/lin_enru/dividend_stability.py`(新檔)

**邏輯**(自己寫一份,**不 import** `screener_long.py:232-240` 那段邏輯):
```python
def compute_consecutive_dividend_years(stock_id: str) -> int:
    """從最近年往回算連續配息年數。
    
    林恩如專用。screen_long._evaluate_long 內有同樣演算法,
    為了隔離不 import,而是在這裡複寫一份。
    """
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT year, cash_dividend FROM dividend
            WHERE stock_id = ? ORDER BY year DESC
        """, (stock_id,)).fetchall()
    
    streak = 0
    for r in rows:
        if r["cash_dividend"] and float(r["cash_dividend"]) > 0:
            streak += 1
        else:
            break
    return streak
```

**工時**:1h(邏輯簡單,主要寫 tests)

**重複代碼真實成本**:`screener_long._evaluate_long` 內這段 6 行邏輯**重複到 `dividend_stability.py`**。對個人專案 6 行 ok;若主公**未來改殖利率算法**(例如除權息日後 prorate),**要改 2 處**。隔離原則的取捨在此。

---

## Part 7：Phase A + B + C + D 工時分解(隔離版)

### Phase A:獨立基礎建設(feature)

| 步驟 | 內容 | 工時(隔離) | v1 工時 | Δ |
|---|---|---|---|---|
| A1 | `eps_streak.py` + tests(獨立寫) | 1.5h | 1.5h | 0 |
| A2 | `price_high.py` + tests(獨立寫,不 refactor volume_breakout) | 1.5h | 1.5h | 0(原本就是新寫) |
| A3 | `dividend_stability.py` + tests(獨立,不抽 `screen_long`) | 1h | 0.5h(原本只 refactor) | **+0.5h** |
| A4 | Sanity check SQL — universe 大小 | 0.5h | 0.5h | 0 |
| A5 | `src/features/lin_enru/__init__.py` + module 結構 setup | 0.5h | 0 | **+0.5h** |
| **A 小計** | | **5h** | 4.5h | **+0.5h** |

### Phase B:獨立 strategy + backtest

| 步驟 | 內容 | 工時(隔離) | v1 工時 | Δ |
|---|---|---|---|---|
| B1 | `src/strategies/lin_enru/screener.py`(三條件 AND) | 2h | 2h | 0 |
| B2 | `src/lin_enru_backtest.py`(`simulate_outcome_lin_enru`) | 3.5h | 3.5h | 0 |
| B3a | Backtest sweep(36 組合 × 5 年) | 1.5h | 1.5h | 0 |
| B3b | 寫 `docs/lin_enru_backtest_results.md` | 1.5h | 1.5h | 0 |
| **B 小計** | | **8.5h** | 8.5h | **0** |

**🛑 GATE**:B3 結果決定是否往下做 C/D。沒過 → 整包(Phase A code 留作通用 feature 但不上線)。

### Phase C:獨立 UI

| 步驟 | 內容 | 工時(隔離) | v1 工時 | Δ |
|---|---|---|---|---|
| C1 | `PAGES` list 加 "🌱 存股輪轉" + `_page_lin_enru()` 函式骨架 | 1h | 1h | 0 |
| C2 | `src/lin_enru_ui_cards.py`(獨立卡片,無 stop_loss 欄位) | 3h | 1h(reuse `render_picks_cards`) | **+2h** |
| C3 | 「執行掃描」按鈕 + cache + 串 `screen_lin_enru` | 2h | 2h | 0 |
| **C 小計** | | **6h** | 4h | **+2h** |

### Phase D:獨立 paper trading + workflow

| 步驟 | 內容 | 工時(隔離) | v1 工時 | Δ |
|---|---|---|---|---|
| D1 | `CREATE TABLE lin_enru_trades` schema(加進 `database.py`) | 1h | 1.5h(原本要 ALTER + status CHECK 重建) | **-0.5h** |
| D2 | `src/lin_enru_paper_trading.py`(`add_*` / `evaluate_active_*` / `list_*`) | 4h | 3.5h(原本要改 `_evaluate_one` 加分支) | **+0.5h** |
| D3 | 「基本面崩」「配息中斷」出場邏輯(查季 EPS + 年 dividend) | 2.5h | 2.5h | 0 |
| D4 | UI「加入存股追蹤」按鈕 + 已追蹤頁 | 3h | 3h | 0 |
| D5 | `lin_enru_trades.csv` snapshot dump/load(跨容器持久化,**獨立 file**) | 2h | 0(reuse `paper_trades_snapshot.py`) | **+2h** |
| D6 | `.github/workflows/lin-enru-daily.yml`(獨立 cron) | 1.5h | 0(掛現有 workflow) | **+1.5h** |
| **D 小計** | | **14h** | 10.5h | **+3.5h** |

### 總計

| Option | v1 工時 | v2 隔離工時 | Δ |
|---|---|---|---|
| A 只做 features(無 strategy) | — | — | — |
| **B = A + Phase B(backtest only)** | 13h | **13.5h** | +0.5h |
| C = A + B + Phase C(加 UI) | 17h | **19.5h** | +2.5h |
| **D = A + B + C + Phase D(完整)** | 27.5h | **33.5h** | +6h |

主公接受的「+10-20%」工時,**實測下來 B 幾乎沒影響(+4%),D +22%**(主因 D5/D6 兩塊獨立 snapshot + workflow)。

---

## Part 8：5 個 decision options(v2 重列)

| Option | 內容 | 工時 | 風險 | 收益 | 隔離後變化 |
|---|---|---|---|---|---|
| **A. 全不做** | 報告當研究存檔 | 0h | 0 | 0 | 不變 |
| **B. Phase A + B(backtest only)** | features 補齊 + screener + backtest sweep + 結果報告 | **13.5h**(原 13h) | **極低**:全部新檔,完全不動現有 code;最壞情況 ~13h 沒結果 | **中-高**:有數據決策;features 留作後續可能 reuse(但隔離原則下不會) | +0.5h(`__init__.py` setup) |
| **C. A + B + Phase C(加 UI)** | B 全套 + 新 tab + 獨立卡片 helper | **19.5h**(原 17h) | **低-中**:加 tab(PAGES 21→22);獨立卡片要新測試 | **中**:主公能日常看到,但無 paper tracking | +2.5h(獨立 UI 卡片) |
| **D. 完整 A+B+C+D** | 全套 + `lin_enru_trades` 表 + 獨立 evaluate + 獨立 snapshot + 獨立 cron | **33.5h**(原 27.5h) | **中**:新表但完全隔離,**不會破現有 paper_trades 任何 test**;snapshot dump 跨容器邏輯要小心 | **高**(若 alpha 真存在):完整自動化 paper portfolio | +6h(獨立 snapshot/workflow) |
| **E. 主公另有思路** | — | — | — | — | — |

**為什麼仍推薦 B 而不是 C/D(隔離後論點)**:

1. **隔離讓 B 風險更低** — 既然 Phase B 全部寫在新檔(`src/features/lin_enru/` + `src/strategies/lin_enru/` + `src/lin_enru_backtest.py`),**不動現有任何一行 code**。最壞情況「林恩如沒 alpha」→ 整個資料夾 5 分鐘砍掉,連 `git revert` 都不用。Phase B 本質是**無遺憾下注**(no-regret bet)。
2. **隔離讓 D 工時 +22% 真實成本浮現** — v1 估 D 是 27.5h,看起來不誇張;隔離後 D5(獨立 snapshot)+ D6(獨立 cron)各加 ~2h,**33.5h 才是真實成本**。這個成本只在 alpha 確認後才值得付。
3. **C 沒 paper tracking 是「半套」** — 不寫進 `lin_enru_trades` → 主公手動操作無從 track 表現。除非主公**明確說只想看每日推薦不要追蹤**,否則 C 是雞肋(做了 UI 但用不順)。
4. **回滾優勢顯著** — 隔離架構下:
   - Phase B 失敗:`rm -r src/features/lin_enru/ src/strategies/lin_enru/ src/lin_enru_backtest.py`
   - Phase D 失敗:再加 `DROP TABLE lin_enru_trades`、刪 cron YAML、刪 tab from PAGES、刪 `_page_lin_enru()` 一個函式
   - **總共 5 分鐘**,完全不影響現有 17 套策略 / `screen_long` / `paper_trades`

**反過來,直接做 C 的條件**:主公直覺超強林恩如方法可行 + 願賭 33.5h(不,**19.5h** 因為 C 不含 D)賠率。我尊重主公判斷,但會在報告開頭寫「未經 backtest 驗證」warning。

---

## Part 9：跟「波段交易」task 的關係(v2:完全切割)

主公拍板「不要跟其他共用」**也適用於波段 task**。新原則:

| 元素 | 林恩如(本報告) | 波段(另報告) | 共用? |
|---|---|---|---|
| `eps_positive_years` feature | `src/features/lin_enru/eps_streak.py` 自己寫 | 波段如要,**自己寫一份在 `src/features/swing/`** | ❌ **不共用** |
| `52_week_high` | `src/features/lin_enru/price_high.py` 自己寫 | 波段如要,自己寫 | ❌ **不共用** |
| Paper trading 表 | `lin_enru_trades` 新表 | 波段用現有 `paper_trades`(因為波段有 stop) | ✅ **天然分開** |
| `strategy_profile` schema 欄位 | **不加**(因為林恩如有自己的表) | 波段如有新模式需求,自己決定要不要加 | ❌ **不協調** |
| UI | `_page_lin_enru()` 獨立 tab | 波段自己一個 tab | ❌ **天然分開** |
| Cron | `lin-enru-daily.yml` | 波段自己一個 workflow | ❌ **天然分開** |

**結論**:**兩個 task 各做各的,亮(orchestrator)也不用協調 schema 順序** — 因為兩邊根本不碰同個 schema。

**對 orchestrator 的建議**:v1 寫的「先讓波段 task 確認 schema 順序」**已作廢**,兩 task 可完全 parallel 開工。

---

## Part 10：「不做沒意義的事」誠實檢核(v2:含隔離取捨)

### 10.1 隔離架構是否反而違反「不做沒意義的事」?

**重複代碼成本**:
- `compute_consecutive_dividend_years` — 在 `screener_long._evaluate_long` 跟 `dividend_stability.py` 各一份 = **6 行邏輯重複**
- `is_at_n_day_high` 邏輯 — `screen_volume_breakout`(20 日) + `is_at_52_week_high`(252 日) = **約 5 行邏輯重複**
- `yearly_eps_streak` 跟 `screen_eps_acceleration` 都讀 `financials.quarterly`,但聚合方式不同(年 vs 季 YoY),**邏輯本就不同,沒重複**

**總計重複 code**:~11 行 + 隔離的 `__init__.py` / module 結構 ~50 行 boilerplate ≈ **60 行**重複 / boilerplate。

**心智成本**:
- 主公未來改殖利率算法(例如除權息日後 prorate)→ 要改 2 處,容易漏一處
- 但**隔離的本意正是「改一處不影響另一處」** — 主公可能**故意**只想升級 `screen_long` 的算法但保留林恩如的原版做對照
- 重複是 feature 不是 bug

### 10.2 對「探索性策略」隔離有意義嗎?

**有意義**,理由:
1. **林恩如方法是「賭注」不是「核心」** — 跟 17 套既有策略不同(那 17 套已 backtest + 上線 + ML 校準),林恩如還沒驗證
2. **賭輸要能無痛砍掉** — 隔離架構下整包刪除 5 分鐘;耦合架構下要 unwind helpers / schema / snapshot 整整一下午
3. **賭贏可漸進整合** — 若 backtest 結果好到主公想統一架構(把林恩如的 features 升級成全局共用),那是「**未來決策**」,屆時 refactor 路徑清楚:把 `src/features/lin_enru/` 改名移到 `src/features/common/`;這個搬家 1h 工時,比一開始就共用、後來發現要分,容易得多

### 10.3 是否該補 「features common module」的基礎建設?

**現況**:`src/features/` 整個資料夾目前**還不存在**(我前面 `Glob` 確認過,所有 src 檔案平鋪)。
- 林恩如 Phase A 是**第一個**用 `src/features/<name>/` 這個目錄 pattern
- **不要趁機建 `src/features/common/`** — 主公明確說「不要共用」,自己建一個沒人用的 common 違反原則
- **未來如果波段也要 features**,各自開 `src/features/swing/`,兩者並列,**沒有 common**
- 若未來真的要重構成共用,那是另一個專門的 refactor task

### 10.4 明確結論(v2)

**做**,Phase A + B(13.5h),走完全隔離架構:
- A 階段補 3 個獨立 feature helpers
- B 階段跑 backtest sweep + 出 GATE 結果
- 過 GATE → 主公再拍板 C / D
- 沒過 GATE → 5 分鐘整包砍掉,不影響現有 17 套策略 / `screen_long` / `paper_trades` / `swing` task 任何東西

**隔離成本(+0.5h on Phase B,+6h on full)** 換到的**收益**:**清晰邊界 + 無痛回滾 + 跟波段 task 零協調成本**。對個人專案的探索性新策略,**隔離原則划算**。

---

## 給主公的決策清單(v2)

請主公從以下選一:

- [ ] **A. 全部不做** — 林恩如方法沒興趣
- [ ] **B. 做 Phase A + B(13.5h,隔離 backtest 驗證)** — **亮推薦**;有數據再決定下一步
- [ ] **C. A + B + C(19.5h,加獨立 UI 但不開新表)** — 主公想日常看推薦但不追蹤
- [ ] **D. 完整 A + B + C + D(33.5h,獨立 `lin_enru_trades` 全套)** — 主公要完整 paper portfolio
- [ ] **E. 主公自己另一種思路** — 寫下來

---

## Appendix A:v1 → v2 變更摘要

| 章節 | v1 | v2 |
|---|---|---|
| 架構 | 部分共用(eps feature / strategy_profile schema) | **完全隔離**(獨立 features / strategy / 表 / UI / cron) |
| Phase D paper trading | 改 `paper_trades` 加 `strategy_profile` | **新開 `lin_enru_trades` 表**,不動 `paper_trades` |
| Helper 抽取 | refactor `screener_long._evaluate_long` 抽 `consecutive_dividend_years` | **不抽**,林恩如自己寫一份 |
| `52_week_high` | refactor `_evaluate_volume_breakout` 抽 `_is_n_day_high(n, tolerance)` | **不 refactor**,獨立寫 `is_at_52_week_high` |
| Snapshot | reuse `paper_trades_snapshot.py` | **新開** `lin_enru_trades.csv` snapshot |
| Cron | 掛 `daily-notify.yml` | **新開** `lin-enru-daily.yml` |
| 跟波段 task 共用 | `eps_positive_years` + `strategy_profile` 共用 | **完全切割**,兩邊各做各的,並行無協調 |
| Phase B 工時 | 13h | **13.5h**(+0.5h `__init__.py` setup) |
| Phase D 工時 | 27.5h(total) | **33.5h**(+6h,主要 D5/D6 獨立 snapshot/workflow) |
| 回滾成本 | 需 unwind `strategy_profile` migration + status CHECK 縮回 | **5 分鐘整包 rm + DROP TABLE**,完全不影響其他模組 |
