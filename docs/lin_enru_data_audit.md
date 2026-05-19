# 林恩如方法 — 歷史資料完整性 Audit

> **撰寫日期**:2026-05-19
> **DB 位置**:`data/cache.db`(SQLite 主公本機 + GitHub snapshot 同步)
> **Audit 目的**:在 Phase A 開工前確認 5 年 backtest 所需資料是否齊全;**結論:嚴重缺漏,Phase A 之前必須先 backfill**

---

## 🚨 TL;DR

**現有資料嚴重不足以跑林恩如方法 backtest** — 不管做技術派(真實林恩如)還是基本面派(亮 v2 假版),都過不去:

| 資料 | 林恩如方法需求 | 現有狀態 | 缺口程度 |
|---|---|---|---|
| `daily_prices` 5 年(技術派核心) | ≥ 1260 天 / 檔 | **0 檔**有 1260+ 天 | **災難級** |
| `daily_prices` 100 天(20 週 MA 起跑) | ≥ 100 天 / 檔 | **6 檔** | **災難級** |
| `financials.quarterly` 5 年 | ≥ 20 季 | 最舊 2025-Q4(**只 2 季**) | **災難級** |
| `dividend` 5 年 | ~2400 檔 × 5 年 | 8 檔 / 大部分只 1 年 | **災難級** |
| `monthly_revenue` | 全市場 5 年 | **0 筆** | **災難級** |
| `daily_metrics`(殖利率) | 5 年歷史 | 850 檔 × 2.5 週(2026-04-28 起) | **災難級** |
| `institutional`(法人,選用) | TOP_50 5 年 | 322 檔 × 6 個月(2025-11 起) | 中度缺 |

**主公的 Phase B backtest 要跑,前面必須先做 Phase 0c**:**daily_prices 5 年歷史 backfill**(只技術派需要)or **daily_prices + financials + dividend 5 年全 backfill**(基本面派需要)。

**Phase 0c 工時估**:
- **僅技術派(真實林恩如)**:**0.5h 寫 PR 改 backfill_history.py 接受 `--days 1260` 參數** + **5-15h GHA cron 跑完**(背景,主公不用盯)
- **基本面派(亮假版)**:上述 + **financials backfill 5-10h cron** + **dividend backfill 5-10h cron**(主公要按 alert 觸發)

---

## Part 1:逐表 audit 結果

### 1.1 `daily_prices`(林恩如方法最核心)

**現況**:
```
總筆數: 152,733
唯一股票數: 2,408(全市場覆蓋 OK)
最舊日期: 2024-01-02(只 1 檔)
最新日期: 2026-05-15
```

**每年覆蓋(個股數 / 筆數)**:

| 年 | 個股數 | 筆數 |
|---|---|---|
| 2024 | 1 | 242 |
| 2025 | 12 | 641 |
| 2026 | 2,404 | 151,850 |

**每月覆蓋(關鍵發現 — backfill 時間線)**:

| 月份 | 個股數 |
|---|---|
| 2024-01 ~ 2025-09 | **全部只 1 檔**(test stock?) |
| 2025-10 | 2 檔 |
| 2025-11 | 12 檔 |
| 2025-12 | 11 檔 |
| **2026-01** | **1,735 檔**(系統性 backfill 啟動點) |
| 2026-02 | 2,384 檔 |
| 2026-03 | 2,388 檔 |
| 2026-04 | 2,395 檔 |
| 2026-05 | 2,368 檔 |

**每股累積天數分佈**:

| 累積天數 | 個股數 |
|---|---|
| < 100 天(<5 月,**不能算 20 週 MA**) | **2,402 檔(99.7%)** |
| 100-251 天(**剛能算 20 週 MA,但不能 backtest 1 年**) | 5 檔 |
| 252-503 天(1-2 年 backtest 邊緣) | 0 檔 |
| 504-1259 天(2-5 年 backtest) | **1 檔** |
| ≥ 1260 天(5 年 backtest) | **0 檔** |

**TW_TOP_50 內** ≥ 100 天的個股:**只 2 檔**

**結論**:
- **林恩如方法 20 週 MA 算式需要至少 100 個交易日**,現在全市場只 **6 檔**過門檻
- **5 年 backtest 需要每檔 ≥ 1260 天**,**現在 0 檔過門檻**
- 系統性的 daily_prices backfill 是 **2026-01** 才啟動 — 主公 4-5 個月前才開始建這個資料庫,這跟 stock-screener 是新專案吻合

### 1.2 `financials.quarterly`(EPS / ROE)

**現況**:
```
總筆數: 1,829
唯一股票數: 1,073
最舊 period: 2025-Q4
最新 period: 2026-Q1
```

**每年覆蓋**:

| 年 | 個股數 |
|---|---|
| 2025 | 768(只有 Q4) |
| 2026 | 1,061(只有 Q1) |

**結論**:
- 完整資料只 **2 季**(2025-Q4 + 2026-Q1)
- **無法算「連續 N 年 EPS 正」**(連 1 年都湊不齊)
- **無法算 yearly EPS**(年 EPS = 4 季加總,需要連續 4 季資料)
- 即使是真實林恩如方法(不看 EPS),「基本面崩出場條件」(連 2 季 EPS ≤ 0)也要這個資料源 — **不夠**

### 1.3 `dividend`(連續配息年數)

**現況**:
```
總筆數: 44
唯一股票數: 8(全市場 2400 檔的 0.3%)
最舊年份: 2015
最新年份: 2026
```

**每年覆蓋**:

| 年 | 個股數 |
|---|---|
| 2015 | 1 |
| 2016 | 1 |
| 2017 | 1 |
| 2018 | 1 |
| 2019 | 1 |
| 2020 | 1 |
| 2021 | 7 |
| 2022 | 7 |
| 2023 | 7 |
| 2024 | 8 |
| 2025 | 8 |
| 2026 | 1 |

**結論**:
- 全市場只 **8 檔**有 dividend 資料(其中 1 檔從 2015 起,其他多從 2021)
- **無法做殖利率 / 配息 篩選**
- 主公 5/19 拍板的 alert-based backfill(`[[project-backfill-quota-alert-pivot-2026-05-19]]`)應該還沒按過

### 1.4 `monthly_revenue`(月營收 YoY)

**現況**:
```
總筆數: 0
```

**結論**:**完全沒資料**。亮 v2 假版的「月營收 YoY > 某門檻」進場條件 **無法實作**。

(真實林恩如方法不需要月營收,所以這條對技術派不影響。)

### 1.5 `daily_metrics`(殖利率 / PE / PB)

**現況**:
```
總筆數: 7,606
唯一股票數: 850
日期區間: 2026-04-28 ~ 2026-05-15(2.5 週)
```

**結論**:
- **只 2.5 週**歷史
- 雖然 850 檔覆蓋還行,但無法做歷史殖利率分析
- 即使「現在殖利率 ≥ 5%」可用,但「過去 5 年殖利率穩定 ≥ 5%」**完全不行**

### 1.6 `institutional`(三大法人,選用)

**現況**:
```
總筆數: 5,114
唯一股票數: 322(TOP_50 + watchlist 範圍)
日期區間: 2025-11-04 ~ 2026-05-15(6 個月)
```

**結論**:
- 半年資料,夠跑 6 個月的法人共識策略
- 林恩如方法**不看法人**,這對林恩如不關鍵 — 但如果要加 enhanced filter(例如「進場時三大法人也淨買」)就要更多歷史

### 1.7 `stocks`(全市場清單)

**現況**:
```
總個股: 2,715
TW: 2,715(全 TWSE/TPEx)
```

**欄位**:
```
stock_id (TEXT)
name (TEXT)
market (TEXT)
industry (TEXT)
type (TEXT)
updated_at (TEXT)
```

**結論**:
- 全市場清單**齊全**(2,715 檔覆蓋 TWSE + TPEx + 興櫃)
- **缺 `market_cap` 欄位** — 林恩如選股池「市值 20 億+」需要這個欄位
- **建議加** `market_cap REAL` 欄位 + 每月 FinMind backfill 一次

---

## Part 2:結論 — 林恩如方法各情境的資料缺口

### 情境 A:做**真實林恩如方法**(技術派 / 20 週均線)

**核心資料需求**:`daily_prices` 5 年(只這一個表)

**缺口**:
- **0 檔**有 5 年(1260 天)
- **6 檔**有 100 天(20 週 MA 起跑門檻)
- **需要 backfill ~2400 檔 × 5 年 daily_prices**

**選用資料需求**(若加進 enhanced 條件):
- `stocks.market_cap`(篩中大型股池)
- `institutional`(進場確認法人也淨買)
- `daily_metrics.dividend_yield`(選股時排除超低殖利率股,雖然林恩如本人沒這麼做)

### 情境 B:做**亮 v2 假基本面版**(連 5 年 EPS / 殖利率 / 52 週新高)

**核心資料需求**:
- `daily_prices` 5 年(52 週新高 + backtest)
- `financials.quarterly` 5 年(連 5 年 EPS 正)
- `dividend` 5 年(連續配息)
- `daily_metrics` 5 年(殖利率歷史)

**缺口**:**4 個表全部都嚴重不足**(見 Part 1 各小節)

**Phase 0c 工時**:**+10-20h**(daily_prices + financials + dividend + daily_metrics 4 軸都要 backfill)

### 情境 C:**先做小範圍 pilot**(避開 backfill)

如果主公**不想等 backfill 5 年資料**,可以:
- **Pilot universe**:只跑 **TW_TOP_50 中那 2 檔**有 100+ 天歷史的股
- **Pilot backtest 期間**:2026-01-15 ~ 2026-05-15(只 4 個月,~16 週,**勉強夠 20 週 MA 起跑**)
- **Pilot signal 數**:估算只 5-15 個訊號(統計顯著性非常弱)
- **用途**:**驗證 code 正確性**,不驗證 alpha
- **後續**:backfill 完再正式跑 5 年 backtest

**Pilot 工時**:**+0h**(就用現有資料)
**Pilot 價值**:60% — 能驗證程式邏輯但無法判斷 alpha

---

## Part 3:Backfill 工時與作法

### 3.1 `daily_prices` 5 年 backfill(技術派最緊急)

**現有工具**:`scripts/backfill_history.py` + `.github/workflows/backfill-history.yml`(8 shard 並發)

**現況**:預設只回補 **90 天**(`backfill_history.py` line 1 doc 寫「90 天」)。

**改造步驟**:
1. **0.5h** — PR 修改 `backfill_history.py`,接受 `--days N` 參數(default 90,但可傳 1260)
2. **0.5h** — `.github/workflows/backfill-history.yml` 加 workflow_dispatch input `days`,主公手動觸發
3. **0h** — 觸發 8 shard 並發跑(背景,主公不用看)

**機器跑時間**:
- 2700 檔 × 1260 天 ≈ 3.4M rows
- FinMind `TaiwanStockPrice` 支援 from_date / to_date,**1 個 sid 1 個 call 拉 5 年**
- 限額:免費 token 600/hr、老 token 1500/hr
- **理想**:2700 calls / 1500/hr = 1.8 小時
- **實測(8 shard 並發,但 quota per-token shared)**:**5-15 hr** 視 backfill 過程有沒有被 cancel(`[[project-backfill-revenue-cancellation]]` 教訓 — 但 daily_prices 不該有 strict=True 路徑)

**主公工時**:**1 小時**(寫 PR + 觸發 + 偶爾看一下進度)

### 3.2 `financials.quarterly` 5 年 backfill(僅基本面版需要)

**現有工具**:`scripts/backfill_financials.py` + `.github/workflows/backfill-financials-once.yml`

**現況**(`[[project-backfill-quota-alert-pivot-2026-05-19]]`):**alert-based** — 每月 1 號 09:00 Telegram + Discord 推主公,主公手動觸發。

**主公要做**:**等下次 alert 來** or **手動 dispatch workflow** + 設 `--years 5` 參數(需確認 script 支援)

**機器時間**:**5-10 hr**(FinMind 限額大,FinancialStatements dataset 更慢)

### 3.3 `dividend` 5 年 backfill(僅基本面版需要)

**現有工具**:`scripts/backfill_dividend.py` + `.github/workflows/backfill-dividend.yml`

**現況**:8 shard 並發,主公拍板 alert-based(每月 1 號推)

**機器時間**:**5-10 hr**

### 3.4 `daily_metrics` 5 年 backfill(僅基本面版需要)

**現有工具**:**沒有專屬 backfill script**(grep 不到 `backfill_metrics`)。`scripts/daily_fetch.py` 應該每天抓當日的。

**結論**:歷史 `daily_metrics` 要**現寫 script**(預估 2-3h 寫 + 5-10h 跑),或者**直接放棄**(用 `financials` + `dividend` 配合 `daily_prices` 自算殖利率)。

### 3.5 `stocks.market_cap` 加欄位 + 月度 backfill

**工具**:**沒有**,要新寫:
1. **0.5h** — schema migration `ALTER TABLE stocks ADD COLUMN market_cap REAL`
2. **1.5h** — `scripts/backfill_market_cap.py`(用 FinMind `TaiwanStockShareholding` × `daily_prices.close` 算)
3. **0.5h** — `.github/workflows/backfill-market-cap-monthly.yml`(每月跑一次,因為市值變化慢)

**主公工時**:**2.5h** 寫 PR + 觸發

---

## Part 4:Phase 0c 三種策略(主公拍板)

| 方案 | 內容 | 主公工時 | 機器時間 | 結果 |
|---|---|---|---|---|
| **0c-A. 技術派最小集**(若選真實林恩如) | daily_prices 5 年 + market_cap 月度 | **3.5h** | 5-15h(背景) | 5 年完整 daily 資料,Phase A/B 可跑 5 年 backtest |
| **0c-B. 基本面完整集**(若選 v2 假版) | 0c-A 全部 + financials + dividend + daily_metrics | **+8-12h** | +10-20h | 5 年完整基本面,但工時翻倍 |
| **0c-C. Skip backfill, pilot 跑**(若主公趕時間) | 不 backfill,用現有 2026-01 ~ 05 的 4 個月資料 | **0h** | 0h | **只能驗證 code 正確性,不能驗證 alpha**;對 GATE 沒實質意義 |

**亮的建議**:
- 若主公選真實林恩如(情境 A)→ **走 0c-A**(3.5h,值得)
- 若主公選 v2 假版(情境 B)→ **走 0c-B**(>10h,要等)
- 不要走 0c-C(pilot)— 失敗 / 成功都不算數,白做

---

## Part 5:風險與注意事項

### 5.1 FinMind quota 風險
- 主公 `[[feedback]]` 教訓:backfill 之前要確認 quota 沒爆
- 5 年 daily_prices backfill 是 **2700 calls**,以 1500/hr 老 token 計算 = 約 2 小時 quota
- 同時段不要跑其他 backfill(financials / dividend),會互搶 quota

### 5.2 GHA runner contention
- `[[project-backfill-revenue-cancellation]]`(PR `0b8bfe0`):runner concurrency 太高會被 cancelled
- 8 shard 並發 + 其他 cron 同時跑 → 可能 cancellation
- 建議:**只在週末或主公主動觸發,別跟 daily-notify cron 撞**

### 5.3 Snapshot push 風險
- backfill 完要 dump CSV 到 `data/twse_snapshot/` 然後 git commit + push
- 5 年 daily_prices = 152,733 rows × 1260/152 ≈ **1.27M rows ≈ 100MB CSV**
- **可能超 GitHub 100MB 單檔限制** — 要分 shard CSV(現有 backfill_history.py 應該已支援 shard,但要確認 aggregate 後不超限)
- 萬一超限,要走 **git LFS** 或 **拆更多 shard**

### 5.4 SQLite 體積膨脹
- daily_prices 從 152k → 1.27M rows,SQLite 檔案估從目前 ~50MB → ~400MB
- 對個人專案 ok,Streamlit Cloud 還能放(限 200MB-1GB 視 plan,主公確認過 GitHub commit 帶得動)

---

## Part 6:檢核清單(主公拍板用)

主公在開 Phase A 之前,要先回答:

- [ ] **Q1**:做真實林恩如方法(技術派),還是亮 v2 假基本面版?(關鍵分歧,影響 backfill 範圍)
- [ ] **Q2**:跑 0c-A(技術派 backfill)/ 0c-B(完整 backfill)/ 0c-C(skip backfill pilot)?
- [ ] **Q3**:同意 daily_prices 5 年 backfill 估 5-15h 機器時間嗎?(背景跑,主公不盯)
- [ ] **Q4**:同意加 `stocks.market_cap` 欄位 + 月度 backfill 嗎?(中大型股 universe 篩選需要)
- [ ] **Q5**:同意改 backfill_history.py 接受 `--days 1260` 參數嗎?(主公工時 0.5h PR)

主公答完 → 亮排 Phase 0c 工作 → 跑完才開 Phase A。
