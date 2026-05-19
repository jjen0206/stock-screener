# 林恩如選股法 — 功能 Spec(v3:基於真實林恩如方法 + 資料 audit)

> **狀態**:**主公決策卡關** — 等主公先選真實林恩如方法 / v2 假版 / 別人的方法,**Phase A 不要開工**
> **撰寫日期**:2026-05-19(v1 初版 → v2 隔離架構 → v3 對齊真實方法 + 資料 audit 結果)
> **本檔角色**:**3 份文件的總指揮** — research / audit / 本 spec 並列,看完 research 先 + audit 再讀本檔
> **配套文件**:
>   - `docs/lin_enru_methodology_research.md` — 真實林恩如方法深度研究(450 行)
>   - `docs/lin_enru_data_audit.md` — 歷史資料完整性 audit(370 行)
>   - 本檔 — spec / 工時 / decision options

---

## 🚨 TL;DR — v3 軍師建議

**主公,Phase A **不能**直接開工**。我深度研究 + 資料 audit 後發現兩個阻塞:

### 阻塞 1:**「林恩如方法」misalignment**

亮 v1/v2 spec 描述的「林恩如=基本面派 + 不停損 + 連 5 年 EPS 正 + 52 週新高」**完全不符實際**。

真實林恩如(4 個獨立來源 100% 確認):
- **純技術派**(20 週均線 + W 底 + 趨勢線 + 週爆量)
- **嚴格停損**(跌破 20 週 MA 立即砍 — 「停損是交易的精髓」)
- **完全不看**:EPS、殖利率、配息、月營收、財報

詳見 `docs/lin_enru_methodology_research.md` Part 0 警告。

**主公必須選**:
- **A. 真實林恩如方法**(技術派)→ v2 spec 全部作廢,以 research doc 為基礎重寫
- **B. 主公本意是其他人**(可能是陳重銘 / 雪球股温國信 / 棒喬飛 等存股派)→ 請主公指明
- **C. v2 假版**(亮 / 主公 hybrid 創作的「基本面 + 不停損」)→ 改名「存股輪轉策略」,別叫林恩如

### 阻塞 2:**歷史資料嚴重缺漏**

不管選哪個方法,**現有資料都不夠**:

| 資料 | 需求 | 現有 |
|---|---|---|
| daily_prices ≥ 1260 天 / 檔(5 年 backtest) | 全市場 | **0 檔過門檻** |
| daily_prices ≥ 100 天 / 檔(20 週 MA 起跑) | 全市場 | **6 檔** |
| financials.quarterly | 5 年 | 只 1.5 季 |
| dividend | 5 年 | 8 檔 |
| monthly_revenue | 5 年 | **0 筆** |
| daily_metrics(殖利率) | 5 年 | 850 檔 × 2.5 週 |

**Phase A 之前必須先做 Phase 0c(backfill)**。詳見 `docs/lin_enru_data_audit.md`。

### 推薦路徑

**B-A 組合**(2 個阻塞都先解):
1. 主公選 **A**(真實林恩如方法,技術派)
2. 先做 **Phase 0c-A**(daily_prices 5 年 backfill,主公 3.5h + 機器 5-15h 背景)
3. 再做 **Phase A + B**(技術派版工時 **16.5h**,比 v2 假版 13.5h 多 3h 因為技術線型 W 底 / 趨勢線比較難寫)
4. GATE 通過 → 再開 C/D

**總工時(主公人力)**:Phase 0c-A 3.5h + Phase A+B 16.5h = **20h**(機器跑時間另計)

---

## Part 1:重大修正 — v2 spec 全部作廢

v2 spec 的所有「林恩如方法 spec」內容**全部基於錯的方法假設**:

| v2 章節 | v2 內容(錯的) | v3 修正(對的) |
|---|---|---|
| Part 1.1 進場三條件 | 連 5 年 EPS + 殖利率 ≥ 5% + 52 週新高 | **5 條件:漲破 20 週 MA + 20 日 MA 上穿 + 上升趨勢線 + W 底 + 週爆量** |
| Part 1.2 出場規則 | 達停利 +30% + 基本面崩 + 不停損 | **4 停損訊號(跌破 20MA / 跌破趨勢線 / M 頭 / 連 3 週新低)+ 移動停利(跌破上週低)** |
| Part 1.3 持有期 | 1-3 年(無上限) | **由 20 MA 決定,典型數週至數月** |
| Part 4 paper trading | `lin_enru_trades` 表(無 stop_price / 有 target_pct) | **`lin_enru_trades` 表設計大改**:有 5 個 `cond_*` snapshot 欄位、有 `last_week_low` 移動停利欄位、`exit_reason` enum 改 5 種技術訊號(沒有 fundamental_break)|
| Part 5/6 features 補齊 | `eps_streak.py` / `price_high.py` / `dividend_stability.py` | **4 個技術 features**:`weekly_kline.py` / `ma_signals.py` / `trendline.py` / `pattern_w_m.py` / `volume_signal.py` |
| Part 8 工時 | 13.5h B / 33.5h D | **16.5h B / 35-38h D**(技術派比假基本面版多 3-4h 因為 W 底 + 趨勢線難寫) |

**v3 對應的新 spec 細節全在 `docs/lin_enru_methodology_research.md` Part 2-6**,本檔不重複,只標差異。

---

## Part 2:Phase 0c — backfill 步驟(Phase A 開工前必做)

詳見 `docs/lin_enru_data_audit.md` Part 3-4。摘要:

### 0c-A:技術派最小集(若主公選真實林恩如)

| 步驟 | 內容 | 主公工時 | 機器時間 |
|---|---|---|---|
| 0c-A1 | PR 改 `scripts/backfill_history.py` 加 `--days N` 參數 | 0.5h | 0h |
| 0c-A2 | PR 改 `.github/workflows/backfill-history.yml` 加 workflow_dispatch input | 0.5h | 0h |
| 0c-A3 | 觸發 GHA,跑 2700 sid × 5 年 daily_prices backfill | 0.5h(觸發 + 偶爾看) | **5-15h 背景** |
| 0c-A4 | PR 加 `stocks.market_cap` 欄位 + 寫 `backfill_market_cap.py` | 2h | 0h |
| 0c-A5 | 觸發 market_cap backfill GHA | 0.5h | **1-2h 背景** |
| **0c-A 主公小計** | | **4h** | 6-17h |

### 0c-B:基本面完整集(若主公選 v2 假版)

| 步驟 | 內容 | 主公工時 | 機器時間 |
|---|---|---|---|
| 0c-A1-A5 | 同上 | 4h | 6-17h |
| 0c-B1 | 觸發 financials backfill(改 `--years 5`) | 1h | **5-10h** |
| 0c-B2 | 觸發 dividend backfill(改 `--years 5`) | 1h | **5-10h** |
| 0c-B3 | 寫 `backfill_daily_metrics.py`(完全新寫) | 2-3h | **5-10h** |
| **0c-B 主公小計** | | **8-9h** | 21-47h |

### 0c-C:Skip backfill 跑 pilot(不推薦)

| 內容 | 主公工時 | 結果 |
|---|---|---|
| 用現有 2026-01 ~ 05 約 4 個月資料 + 那 6 檔有 100+ 天的個股 | 0h | **只能驗證 code 正確性,不能驗證 alpha** — GATE 沒實質意義 |

**亮建議**:0c-A(若選真實林恩如)是合理路徑,0c-B 工時翻倍但只在選 v2 假版時才需要。

---

## Part 3:Phase A + B + C + D 工時(v3 真實林恩如版)

完整工時細節在 `docs/lin_enru_methodology_research.md` Part 3.1。摘要:

### Phase A:獨立 features(技術派)

| 步驟 | 內容 | 工時 |
|---|---|---|
| A1 | `src/features/lin_enru/weekly_kline.py`(日 K → 週 K resample) | 1.5h |
| A2 | `src/features/lin_enru/ma_signals.py`(20 週 MA + 20 日 MA + 上穿判定) | 1h |
| A3 | `src/features/lin_enru/trendline.py`(swing point + 線性回歸求趨勢線) | **4-6h** |
| A4 | `src/features/lin_enru/pattern_w_m.py`(W 底 / M 頭型態識別) | **3-4h** |
| A5 | `src/features/lin_enru/volume_signal.py`(週爆量) | 1h |
| A6 | `src/features/lin_enru/risk_calc.py`(均差距 / 乖離率) | 0.5h |
| A7 | `src/features/lin_enru/universe.py`(市值 20 億+) | 1.5h |
| A8 | `src/features/lin_enru/__init__.py` + module setup | 0.5h |
| **A 小計** | | **13-15h** |

### Phase B:strategy + backtest

| 步驟 | 內容 | 工時 |
|---|---|---|
| B1 | `src/strategies/lin_enru/screener.py`(5 條件 AND) | 2.5h |
| B2 | `src/lin_enru_backtest.py`(週級別 backtest + 4 停損 + 移動停利) | 4.5h |
| B3a | Backtest sweep(12 組合 × 5 年 / 週級別) | 2h |
| B3b | 寫 `docs/lin_enru_backtest_results.md` | 1.5h |
| **B 小計** | | **10.5h** |

### Phase C:獨立 UI

| 步驟 | 內容 | 工時 |
|---|---|---|
| C1 | `PAGES` 加 "📈 林恩如" tab + `_page_lin_enru()` | 1h |
| C2 | `src/lin_enru_ui_cards.py`(獨立卡片 + 顯示 5 條件 + 乖離率 + 預估虧損) | 3.5h |
| C3 | 「執行掃描」按鈕 + cache + 串 `screen_lin_enru` | 2h |
| **C 小計** | | **6.5h** |

### Phase D:獨立 paper trading

| 步驟 | 內容 | 工時 |
|---|---|---|
| D1 | `CREATE TABLE lin_enru_trades` schema | 1h |
| D2 | `src/lin_enru_paper_trading.py`(add + evaluate + 移動停利) | 5h |
| D3 | UI「加入存股追蹤」按鈕 + 已追蹤頁(顯持有週數 + 4 停損訊號狀態) | 4h |
| D4 | `lin_enru_trades.csv` snapshot dump/load | 2h |
| D5 | `.github/workflows/lin-enru-weekly.yml`(每週五觸發掃描 + 推播) | 2h |
| **D 小計** | | **14h** |

### 總計(v3 真實林恩如版)

| Option | v2 假版工時 | v3 真實版工時 | Δ |
|---|---|---|---|
| **B = A + Phase B(backtest only)** | 13.5h | **23.5-25.5h** | **+10-12h** |
| C = A + B + Phase C | 19.5h | **30-32h** | +10-13h |
| **D = A + B + C + Phase D(完整)** | 33.5h | **44-46h** | **+10-13h** |

**為什麼真實版工時暴增 ~10h?**
- A3 趨勢線偵測(swing point + linear regression):4-6h(假版完全沒這個)
- A4 W 底 / M 頭型態識別:3-4h(假版只看 close 突破)
- A7 市值 universe(需 backfill market_cap):1.5h(假版用 TW_TOP_50)
- B2 backtest 改週級別 + 移動停利邏輯:+1h(假版日級別 + 固定停利簡單)

**加上 Phase 0c-A backfill(主公 4h + 機器 6-17h),總主公工時**:
- Phase 0c-A + B(backtest only)= **4h + 23.5-25.5h = 27.5-29.5h**
- Phase 0c-A + 完整 D = **4h + 44-46h = 48-50h**

---

## Part 4:`lin_enru_trades` schema(v3 — 技術派版)

跟 v2 schema **完全不同**(v2 假版有 EPS / 殖利率 snapshot 欄位、有 target_pct;v3 真實版改 5 個技術訊號 snapshot 欄位):

完整 schema 在 `docs/lin_enru_methodology_research.md` Part 4。摘要關鍵變化:

```sql
-- v2 假版(已作廢)欄位:
target_price, target_pct, max_hold_days,
eps_streak_years, dividend_yield_pct,
consec_div_years, high_52w_price,
exit_reason ∈ {target_hit, fundamental_break, dividend_stop, max_hold_timeout}

-- v3 真實版(新)欄位:
entry_week_ma20,    -- 進場時 20 週 MA
entry_bias_pct,     -- 乖離率(關鍵風險指標)
max_loss_per_lot,   -- 均差距 × 1000(每張預估虧損)
cond_break_w20,     -- 進場條件 1 snapshot
cond_d20_cross_w20, -- 進場條件 2
cond_uptrend,       -- 進場條件 3
cond_w_bottom,      -- 進場條件 4
cond_volume_burst,  -- 進場條件 5
last_week_low,      -- 移動停利狀態(每週更新)
holding_weeks,      -- 持有週數
exit_reason ∈ {break_w20, break_uptrend, m_top_pattern, new_low_3w, trail_stop}
```

---

## Part 5:跟波段 task 的關係(v3:仍切割)

主公拍板隔離後,波段任務也獨立 — v3 不變,**繼續完全切割**:
- 波段如果用「20 週 MA」邏輯,**自己寫一份**(不 reuse 林恩如 `ma_signals.py`)
- `lin_enru_trades` 跟波段如要的 `swing_trades` 表獨立

亮(orchestrator)無須協調兩 task 的 schema / helper / cron。

---

## Part 6:「不做沒意義的事」誠實檢核(v3 更新)

### 6.1 misalignment 浪費 vs 收益

**過去成本**:亮 v1/v2 寫 spec 大概 1-2h(reading + writing),沒實作所以**沒 sunk cost code** — 只浪費了主公看 v2 spec 的時間。

**避免成本**:**現在發現 misalignment 比寫完 Phase D 才發現省 ~40h**。**主公拍 B 之前我深度 research 是值得的**。

### 6.2 backfill 5 年資料是不是過度?

**檢查**:技術派只需要 daily_prices,**不像基本面派要 4 個表都 backfill**。
- 5 年 backfill 對 ~2700 檔 daily_prices = ~3.4M rows ≈ 100MB CSV
- 對個人專案 ok(SQLite ~400MB,Streamlit Cloud 還能放)
- **必要,不是過度**

### 6.3 真實林恩如方法 vs 現有 17 套真的差異?

仍**有差異**(v2 結論不變):
- 現有 `ma_alignment`(日 K 4 條均線排列)≠ 林恩如(週 K 20MA + W 底 + 趨勢線 + 爆量)
- 現有 `volume_breakout`(日 K 20 日新高)≠ 林恩如(週 K 突破 20 週 MA)
- 沒一套真的覆蓋林恩如方法
- **值得做**

### 6.4 backfill 失敗風險

- FinMind quota 風險 + GHA runner contention(`[[project-backfill-revenue-cancellation]]` 教訓)
- 但 daily_prices backfill 不像 dividend 那樣 strict=True 路徑容易踩
- **可接受風險**

### 6.5 結論(v3)

**做**(走推薦路徑 B-A 組合):
1. 主公先選方法 A/B/C(預設選 A 真實林恩如)
2. 跑 Phase 0c-A(backfill 5 年 daily_prices + 加 market_cap)
3. 跑 Phase A + B(features 補齊 + backtest)
4. GATE 通過 → 主公決定要不要 C/D

**不做** Phase C/D 直到 B 通過 GATE,理由跟 v2 同(不浪費 ~28h 賭沒驗證的假設)。

---

## Part 7:5 個 decision options(v3 重列)

| Option | 內容 | 主公工時 | 機器時間 | 風險 | 隔離後變化 |
|---|---|---|---|---|---|
| **A. 全不做** | 報告當研究存檔 | 0h | 0h | 0 | 不變 |
| **B. Phase 0c-A + A + B(backtest only)** | backfill 5 年 + features + screener + sweep + 結果報告 | **27.5-29.5h** | 6-17h(背景) | **低**:全部新檔,不動現有 code | **v3 比 v2 +14-16h**(0c-A 4h backfill + 真實版 features 多 ~10h) |
| **C. B + Phase C(加 UI)** | B 全套 + 新 tab + 獨立卡片 | **34-36h** | 6-17h | **低-中** | v3 比 v2 +14-16h |
| **D. 完整 B + C + Phase D** | 全套 + `lin_enru_trades` 表 + paper trading + cron | **48-50h** | 6-17h | **中** | v3 比 v2 +14-16h |
| **E. 主公另有思路** | 例如:選方法 B(別人的方法)/ 方法 C(v2 假版自創) | — | — | — | — |

**亮推薦不變(B + 真實林恩如)**:
- Phase 0c-A backfill 是無遺憾投資(daily_prices 5 年資料對任何技術派策略都有用,不是林恩如專用)
- Phase A 寫的 4 個技術 features(weekly_kline / ma_signals / trendline / pattern_w_m / volume_signal)**理論上其他技術派策略可能用到**,但**隔離原則下不會 reuse**(本來就是規矩)
- B3 backtest 結果 GATE 通過 → 主公再決定 C/D
- 不通過 → 整包砍 5 分鐘,只浪費了主公 27.5-29.5h(不含背景機器時間)

---

## Part 8:主公決策清單(v3 整合版)

請主公**依序拍**(前一題沒拍 → 後續沒意義):

### Q1:misalignment 怎麼處理?

- [ ] **A. 走真實林恩如方法**(技術派,本 v3 spec)
- [ ] **B. 主公本意是其他人** — 請主公指明(陳重銘 / 雪球股温國信 / 棒喬飛 / 其他)
- [ ] **C. 走 v2 假版**(亮原本寫的「基本面 + 不停損」,改名「存股輪轉策略」非林恩如)
- [ ] **D. 直接放棄這個 task,不做**

### Q2(若 Q1 選 A 或 C):資料 backfill 策略

- [ ] **0c-A**(技術派最小集,主公 4h + 機器 6-17h)
- [ ] **0c-B**(基本面完整集,主公 8-9h + 機器 21-47h)
- [ ] **0c-C**(skip backfill 跑 pilot,**不推薦**)

### Q3:Phase 範圍

- [ ] **Option B**(backtest only,主公 27.5-29.5h)— **亮推薦**
- [ ] **Option C**(加 UI,主公 34-36h)
- [ ] **Option D**(完整含 paper trading,主公 48-50h)

主公答完 Q1 / Q2 / Q3 → 亮排 cron / Phase 0c 工作 → 跑完才開 Phase A。

---

## Appendix A:v1 → v2 → v3 變更摘要

| 章節 | v1 | v2 | v3 |
|---|---|---|---|
| 林恩如方法 | 基本面派(亮 guess) | 同 v1 | **技術派**(真實研究結果) |
| 進場條件 | 連 5 年 EPS + 殖利率 + 52 週新高 | 同 v1 | **漲破 20 週 MA + W 底 + 趨勢線 + 週爆量 + 20 日 MA 上穿** |
| 出場規則 | 不停損 + 達 30% 停利 + 基本面崩 | 同 v1 | **嚴格停損(4 訊號)+ 移動停利** |
| 持有期 | 1-3 年(無上限) | 同 v1 | **由 20 MA 決定,典型數週至數月** |
| 架構 | 部分共用 | 完全隔離 | **完全隔離(v2 設定不變)** |
| paper trading 表 | 改 paper_trades 加欄位 | 新表 lin_enru_trades(假版欄位) | **新表 lin_enru_trades(技術版欄位)** |
| Phase B 工時 | 13h | 13.5h | **23.5-25.5h** |
| Phase D 工時 | 27.5h | 33.5h | **44-46h** |
| 加上 backfill | 沒講 | 沒講 | **Phase 0c-A 主公 4h + 機器 6-17h** |
| 跟波段共用 | 共用 helper + schema | 完全切割 | **同 v2,切割** |
| Sources | 無研究來源 | 無 | **4 個獨立 web 來源 + repo SQL audit** |

**v3 跟 v2 最大差異**:**方法本身對齊真實林恩如** + **承認資料 audit 結果不能跳過 backfill**。
