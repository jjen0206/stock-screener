# 大戶買入偵測 — 功能 Scope 探索報告

> **狀態**：探索 / 選方向，**尚未實作**。
> **撰寫日期**：2026-05-12
> **目的**：盤點「大戶買入」在台股的常見定義、評估各方案與 stock-screener 現有架構整合難度，給出 MVP 建議。

---

## TL;DR — 軍師建議

**先做「千張大戶（TDCC 集保股權分散表）」MVP**：
- 資料源**完全免費 + 官方原始**（不依賴 FinMind 付費 sponsor）
- 與既有 `institutional` 表正交（不重複法人訊號）
- 整合成本估 **4–6 小時**（含 fetcher + DB schema + 1 隻策略 + UI card 一個欄位）
- 缺點：週更（每週六），不適合做日內 / 隔日訊號 — 但**短線轉折前 1–2 週的籌碼集中**正是它擅長的

**不建議首版做**：分點主力（FinMind 要付費或被擋）、逐筆大單（資料量爆 + 多半付費）。

---

## Part 1：「大戶買入」在台股的 5 種主流定義

| # | 定義 | 資料源 | 頻率 | 免費 | 可信度 | 易得性 |
|---|---|---|---|---|---|---|
| 1 | 三大法人買賣超 | FinMind `...InstitutionalInvestorsBuySell` | 日 | ✅ | 5 | 5 |
| 2 | 主力分點集中度 | FinMind `TaiwanStockTradingDailyReport` | 日 | ❌ Sponsor | 4 | 2 |
| 3 | 千張大戶（股權分散） | TDCC opendata CSV | **週**（六上午） | ✅ | 5 | 5 |
| 4 | 逐筆大單成交 | FinMind `TaiwanStockPriceTick` / 5 秒檔 | 日（T+1） | 部分 | 5 | 2 |
| 5 | 融資融券大戶 | FinMind `...MarginPurchaseShortSale` | 日 | ✅ | 5 | 5 |

### 1. 三大法人買賣超（外資 / 投信 / 自營商）— **已實作**

- **定義**：每日外資、投信、自營商的淨買賣超張數
- **資料源**：FinMind `TaiwanStockInstitutionalInvestorsBuySell`（免 token 可用）
- **更新**：日（T+1，盤後 15:00 後）
- **可靠度**：5/5（FinMind 後端對齊 TWSE 官方）
- **易得性**：5/5（已串好，`fetch_institutional()`）
- **現況**：`institutional` 表已有 `foreign_buy_sell` / `trust_buy_sell` / `dealer_buy_sell` / `total_buy_sell` 四欄；只對 TW_TOP_50 + watchlist（~70 檔）lazy load 以避免燒爆 FinMind 1500/hr 額度。
- **限制**：法人 ≠ 全部大戶。本土主力、中實戶不在這裡。

> 來源：`src/data_fetcher.py:282`、`src/database.py:145-153`、`scripts/daily_fetch.py:159`

---

### 2. 主力分點進出（券商分點集中度）

- **定義**：個股當日各家券商分行買賣量排名，前 N 名買超占比 = 主力集中度。經典「神秘金字塔」「凱基台北買超 5000 張」這種訊號。
- **例子**：2330 今日凱基-台北買超 2000 張、富邦-敦化買超 1500 張 → 集中度高 = 有人在吃貨
- **資料源**：
  - TWSE 官方 `https://bsr.twse.com.tw/bshtm/`：**僅當日**、需驗證碼、無 API、不可歷史回溯
  - TWSE 資訊商店 `eshop.twse.com.tw`：歷史檔**付費購買**
  - FinMind `TaiwanStockTradingDailyReport`：覆蓋 2021-06-30 起，**Sponsor 付費方案**
  - FinMind `TaiwanStockTradingDailyReportSecIdAgg`：彙總版，同樣 **Sponsor 付費**
  - Goodinfo / 神秘金字塔 / wantgoo：免費版**不開放分點明細**，付費 Pro 才有
- **更新**：日（盤後 21:00 後）
- **可靠度**：4/5（FinMind 來源 = TWSE，需驗證 sponsor 穩定性）
- **易得性**：2/5（**付費 + 資料量大**：2360 檔 × 70+ 家券商 × 250 交易日）
- **備註**：這是「短線神功」最有名的訊號，但對個人專案而言，付費 + 爬蟲反制是硬障礙。

---

### 3. 千張大戶 / 股權分散表

- **定義**：集保結算所每週公告「持股 X 張以上的股東人數變化」。常用級距：
  - 級 15：>1000 張（**千張大戶**，本土最強信號）
  - 級 16：>5000 張
  - 級 17：>10000 張（>1 萬張 = 控制性持股）
- **例子**：2330 在某週千張大戶人數從 1200 人 → 1230 人（+30 人），持股比例 71.5% → 71.8%（+0.3%）→ 籌碼集中
- **資料源**：
  - **TDCC 官方 opendata CSV（首選）**：`https://opendata.tdcc.com.tw/getOD.ashx?id=1-5`
  - 查詢介面：`https://www.tdcc.com.tw/portal/zh/smWeb/qryStock`
  - 政府資料開放平臺鏡像：`https://data.gov.tw/dataset/11452`
  - FinMind `TaiwanStockHoldingSharesPer`：同樣資料，但**需 Sponsor 付費**（沒必要走這個）
- **欄位**：`資料日期, 證券代號, 持股分級(1–17), 人數, 股數, 占集保庫存比例%`
- **更新**：**週**（每週六上午，資料日期為前一週五）
- **可靠度**：5/5（集保官方原始）
- **易得性**：5/5（CSV 直接 `pd.read_csv`、無 token、無 rate limit）
- **缺點**：**週更**不適合日內訊號；只能做「中期持倉變化」訊號（適合短線 2–5 日波段切入）

---

### 4. 大單成交 / 逐筆百張以上買賣分佈

- **定義**：盤中單筆 ≥ 100 張的大單為買盤 / 賣盤的比例。即時主力動向最敏感的訊號。
- **例子**：2330 今日 ≥100 張大單買賣比 60:40 → 主力偏買
- **資料源**：
  - FinMind `TaiwanStockPriceTick`：歷史逐筆，**Sponsor 付費**，2019-01 起，**單次請求只能要一檔×一日**
  - FinMind `TaiwanStockStatisticsOfOrderBookAndTrade`：**5 秒級** order book + trade 統計，**免費**，2005 起 — 可從 5 秒成交量峰值近似「大單」
  - TWSE 官方逐筆：付費購買、非 API
- **更新**：T+1（盤後檔），即時逐筆要看券商 API
- **可靠度**：5/5（資料源 = TWSE）
- **易得性**：**逐筆 2/5**（付費 + 單檔限制 + 容量大：單檔單日 10–100 萬列）／**5 秒檔 4/5**（免費但仍需自行設計「大單近似」邏輯）
- **資料量風險**：SQLite 吃逐筆會崩，要轉 Parquet。對個人專案而言，**這是 over-engineering**。

---

### 5. 融資融券（信用交易餘額）

- **定義**：融資餘額 = 散戶看多槓桿 / 融券餘額 = 看空。**嚴格說不算「大戶」**（融資戶常是散戶），但**融券急降 + 股價上漲 = 軋空、融資急降 + 股價跌 = 解套賣壓**是常用輔助訊號。
- **資料源**：FinMind `TaiwanStockMarginPurchaseShortSale`（**免費**）
- **欄位**：`date, stock_id, TodayBalance, YesBalance, buy, sell, Return`
- **更新**：日（21:00）
- **可靠度** 5/5、**易得性** 5/5
- **建議**：可當作 ML 特徵或 Pick Card 附加欄位，但**不要當主訊號**。

---

### 其他補充

| 定義 | 資料源 | 費用 | 用途 |
|---|---|---|---|
| 借券賣出明細 | FinMind `TaiwanStockSecuritiesLending` | 免費 | 機構看空訊號 |
| 八大行庫進出 | FinMind `TaiwanStockGovernmentBankBuySell` | 付費 | 國安基金 / 公股動向 |
| 鉅額交易 | FinMind `TaiwanStockBlockTrade` | 付費 | 機構間大宗轉讓 |
| 暫停融券 | FinMind `TaiwanStockMarginShortSaleSuspension` | 免費 | 除權息 / 鎖籌碼 |

---

## Part 2：與 stock-screener 現有架構整合

### 現有 chip 相關 Pipeline 摸底

**資料抓取**（`src/data_fetcher.py`）
- 只有 `fetch_institutional()`（line 282–305），抓 FinMind 三大法人
- 走 `sync_log` 增量快取邏輯，重複日期不重打 API

**DB schema**（`src/database.py:145-153`）— 只有 `institutional` 一張籌碼表：
```sql
CREATE TABLE institutional (
  stock_id TEXT, date TEXT,
  foreign_buy_sell INTEGER, trust_buy_sell INTEGER,
  dealer_buy_sell INTEGER, total_buy_sell INTEGER,
  PRIMARY KEY (stock_id, date)
)
```
**沒有** holders / shareholders / broker / dispersion 任何表。

**Daily pipeline**（`scripts/daily_fetch.py`）
- 全市場 ~2360 檔走 TWSE bulk endpoint 抓 OHLCV
- 法人只對 **TW_TOP_50 + watchlist（~70 檔）lazy load** 近 7 天
- 原因：FinMind 1500/hr 不夠燒 2360 檔

**策略層**（`src/strategies.py`）— 2 隻策略已用 institutional：
- `screen_inst_consensus()`（line 562）— 外/投/自連續 N 天同買，分類「籌碼」
- `screen_inst_oversold_reversal()`（line 1247）— 連續賣後反轉買，分類「籌碼」

**ML 特徵**（`src/ml_predictor.py:39-42`）— 11 個特徵裡有 2 個籌碼：
```python
FEATURE_NAMES = [
    "kd_k", "kd_d", "macd_dif", "macd_osc", "ma_alignment",
    "bb_position", "vol_ratio", "bias_pct", "atr_normalized",
    "inst_5d", "inst_10d"  # 5/10 日法人合計（張）
]
```
- 用 `(foreign + trust + dealer) / 1000` 直接加總、**沒做 rolling / z-score / ratio**
- RandomForest 直吃 raw 張數

**UI 層**
- `src/individual_sections.py:443` `_compute_main_force_signal()` — 主力訊號區塊（紅燈出貨初期 / 綠燈吸貨初期）+ 0–5 強度長條
- `app.py:2390` `_render_institutional_table()` — 近 10 日法人表
- `src/notifier.py:698` — Telegram 推播 1 行「法人 3 日 ±N K張」
- `src/ui_cards.py` — Pick Card 摘要

### 各方案整合難度評估

#### 方案 A：千張大戶（TDCC 股權分散表）

| 整合點 | 難度 | 估時 |
|---|---|---|
| 新 fetcher `fetch_holders_distribution()` | 簡單（CSV download） | 1h |
| 新表 `holders_distribution`（schema 見下） | 簡單 | 0.5h |
| `scripts/daily_fetch.py` 加排程（每週六） | 簡單，獨立 cron | 0.5h |
| 新策略 `screen_holders_concentration()` | 中（要設計訊號邏輯） | 1.5h |
| Pick Card UI 加「千張大戶人數變化」欄位 | 簡單 | 1h |
| ML 加 1 個特徵 `holders_1k_delta_4w` | 中（要重訓 + 驗證） | 1.5h |
| **小計** | | **~6h** |

建議 schema：
```sql
CREATE TABLE holders_distribution (
  stock_id TEXT, date TEXT,       -- 週五日期
  level INTEGER,                  -- 1-17（>1000 張 = 15）
  people INTEGER, shares INTEGER, pct REAL,
  PRIMARY KEY (stock_id, date, level)
)
```

訊號邏輯例：
- `千張人數_本週 - 千張人數_4週前 ≥ +5` → 大戶進場
- `千張持股比例_週變化 ≥ +0.3%` → 籌碼集中

#### 方案 B：主力分點集中度

| 整合點 | 難度 | 估時 |
|---|---|---|
| 升級 FinMind Sponsor 付費（$$） | 阻擋 | — |
| 或：研究 Goodinfo / wantgoo 爬蟲（高風險） | 高 | 8h+ |
| 新表 `broker_daily`（stock × broker × date） | 中（資料量大） | 1h |
| 集中度計算（top-N 買超比例） | 中 | 2h |
| 整合策略 + UI + ML | 中 | 4h |
| **小計（含付費）** | | **~10h + 月費** |

不推薦：付費或爬蟲都不划算。

#### 方案 C：大單成交（5 秒檔近似）

| 整合點 | 難度 | 估時 |
|---|---|---|
| 新 fetcher（5 秒檔免費但量大） | 中 | 2h |
| **資料量處理**：SQLite vs Parquet 改造 | 高（拉動 architecture） | 6h+ |
| 大單偵測邏輯（從 5 秒峰值反推） | 中 | 3h |
| 整合 | 中 | 3h |
| **小計** | | **~14h** + 既有架構動盪 |

不推薦：與「個人零成本、SQLite」原則違背。

#### 方案 D：融資融券（補充訊號）

| 整合點 | 難度 | 估時 |
|---|---|---|
| 新 fetcher（FinMind 免費） | 簡單 | 0.5h |
| 新表 `margin_trading` | 簡單 | 0.5h |
| daily_fetch 加排程（同 institutional batch） | 簡單 | 0.5h |
| Pick Card 加 1 行融資/融券餘額 | 簡單 | 0.5h |
| ML 加 1 個特徵 `margin_balance_5d_change` | 中 | 1h |
| **小計** | | **~3h** |

可當「方案 A 完成後的下一步」。

---

## Part 3：軍師推薦

主公偏好（已知）：短線交易、台股、Streamlit、ML 增強、零成本、效率優先。

### 🎯 MVP（先做這個）

**方案 A：千張大戶（TDCC 集保股權分散）**，估 **4–6 小時**

理由：
1. **零成本**：TDCC opendata 完全免費、官方原始
2. **與既有訊號正交**：法人是「即時資金面」，千張大戶是「中期籌碼面」— 兩者搭配 = 短線轉折更可信
3. **整合摩擦最低**：CSV download → SQLite → 抄 `_compute_main_force_signal()` pattern 即可
4. **週更其實是優點**：避免高頻 noise、和短線日線策略的 2–5 日波段尺度匹配
5. **ML 加 1 個特徵 = 邊際成本小**：`holders_1k_delta_4w` 加到 FEATURE_NAMES 重訓一次

### 🚀 理想完整版（MVP 後迭代）

**A（千張大戶）+ D（融資融券）+ 既有法人** = 三層籌碼框架：
- 法人 = 機構即時資金
- 千張大戶 = 中期本土主力
- 融資融券 = 散戶情緒反指標

估 **9–10 小時**（A 6h + D 3h），ML 重訓 1 次。

### ❌ 不建議做

| 方案 | 不做原因 |
|---|---|
| B 分點主力 | 付費或爬蟲，違反零成本原則；爬 Goodinfo / wantgoo 法律風險 + 反爬蟲 cat-and-mouse |
| C 大單逐筆 | 資料量壓垮 SQLite；要動 architecture；CP 值低 |
| 八大行庫 / 鉅額交易 | FinMind 付費 + 散戶用途有限 |

---

## Part 4：資料合規與成本

### 合規性

| 來源 | ToS / robots.txt | 結論 |
|---|---|---|
| TDCC opendata | 政府開放資料 OGL（可商用） | ✅ 可放心爬 |
| FinMind 免費 | 個人非商用 OK，rate limit 1500/hr | ✅ 既有用法已合規 |
| TWSE OpenAPI | 政府開放資料 OGL | ✅ |
| Goodinfo | robots.txt 限制嚴格、反爬 | ❌ 不爬 |
| wantgoo / 神秘金字塔 / CMoney | 註冊條款禁止自動化 | ❌ 不爬 |

### 反爬蟲對策（如果未來真的要碰 Goodinfo）
- User-Agent 輪換、proxy、低頻率（< 1 req/秒）
- 但本質上**違反 ToS**，不建議
- 替代：付費官方資料源（成本估 $300–$1500 / 月，個人專案不划算）

### 付費考量

| 服務 | 月費（估） | 拿到什麼 |
|---|---|---|
| FinMind Sponsor | NT$ 不公開、需問 | 分點 + 逐筆 + 千張完整版 |
| Goodinfo VIP | NT$ 200 / 月 | 分點明細、券商買賣排名 |
| CMoney VIP | NT$ 300+ / 月 | 同上 + 法人完整版 |

**結論**：MVP 階段全部用免費（FinMind 免費 + TDCC opendata + TWSE OpenAPI）就夠。需要付費才能拿到的訊號（分點、真逐筆），暫不納入。

---

## 附錄 A：實作 Checklist（如果決定走 MVP）

- [ ] `src/data_fetcher.py` 加 `fetch_holders_distribution(stock_id, date)` — 走 TDCC opendata CSV
- [ ] `src/database.py` 加 `holders_distribution` 表 + index on `(stock_id, date)`
- [ ] `scripts/daily_fetch.py` 或新 `scripts/weekly_fetch.py` 加每週六排程（GitHub Actions cron）
- [ ] `src/strategies.py` 加 `screen_holders_concentration()` — 訊號：4 週千張人數變化 ≥ +5 OR 持股比例 ≥ +0.3%
- [ ] `src/individual_sections.py` 加 `_compute_holders_signal()` — Pick Card 顯示「千張大戶 近 4 週 +N 人 / +N.N%」
- [ ] `src/ml_predictor.py` `FEATURE_NAMES` 加 `holders_1k_delta_4w`，重訓 + 評估 ROC-AUC 變化
- [ ] `src/notifier.py` 推播 1 行附加籌碼欄位
- [ ] 測試：pytest 對 TDCC CSV parse + signal 計算
- [ ] 更新 `docs/DATA_NOTES.md` 記錄 TDCC 來源、週更時點、欄位定義

## 附錄 B：未驗證 / 待釐清項目

1. **FinMind sponsor 方案實際定價**：報告中標「付費」但月費需直接問 FinMind 維護者
2. **TDCC CSV 實際欄位編碼**：建議實際 download 一次 `id=1-5` 驗證欄位名（可能是 Big5 / CSV / fixed-width）
3. **歷史回溯能拿到多久**：TDCC opendata 通常只給最近一年，超過要去查歷史檔
4. **ML 重訓影響評估**：加 `holders_1k_delta_4w` 後對 11→12 特徵的 RandomForest 是否顯著提升 — 需先做 ablation

---

**報告完。** 下一步等主公裁示是否進入 MVP 實作。
