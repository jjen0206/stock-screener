# 波段交易功能 — 可行性報告

> **狀態**:純評估報告(不含 code)
> **日期**:2026-05-19
> **緣由**:主公拍板「持有 1-6 月、基本面+技術混合、看趨勢/20週MA/量價」,要先看評估才動手
> **結論先放**:**有條件做 — 但只做最小可行(MVP)版,不要當全新策略線。** 詳見第 6 節「誠實檢核」

---

## 0. 主公拍板的需求摘要

| 維度          | 主公選定                                            |
|---------------|------------------------------------------------------|
| 持有期        | C — 長波段 1-6 個月(季度級)                       |
| 差異於短線    | C — 不一樣的 risk profile(更穩、適合大部位)        |
| Alpha source  | D — 基本面 + 技術混合                                |
| 輸出形式      | A — 報告先,看完才動手                                |
| 關鍵訊號      | 趨勢走向 / **20週均線** / 量價關係                   |

---

## 1. 現有策略覆蓋審計(`src/strategies.py` + `src/screener_long.py`)

### 1.1 17 個現役策略全表

| #  | strategy 名稱             | 類別     | hold_days(STRATEGY_RR_PARAMS) | 訊號本質                         | 適合短線/波段/長線? |
|----|---------------------------|----------|---------------------------------|----------------------------------|-----------------------|
| 1  | `volume_kd`               | 動能     | DEFAULT 5                       | 量+KD 黃金叉+法人連 N 日買超     | 純短線(< 1 週)       |
| 2  | `ma_alignment`            | 趨勢     | 10                              | MA5>MA10>MA20>MA60+全上揚        | 短線偏波段初段        |
| 3  | `bias_convergence`        | 反轉     | 5                               | 20D 乖離率 [-5%,+1%]+量比>1.2    | 純短線(均值回歸)     |
| 4  | `macd_golden`             | 趨勢     | 10                              | MACD 黃金叉+DIF<0+量比>=1.0      | 短線偏波段初段        |
| 5  | `ma_squeeze_breakout`     | 趨勢     | DEFAULT 5                       | MA5/MA20/MA60 糾結突破+量比      | 純短線                |
| 6  | `inst_consensus`          | 籌碼     | 10                              | 外+投+自三家同時買超 N 日連續    | 短線偏波段             |
| 7  | `bb_lower_rebound`        | 反轉     | 3                               | BB 下軌 5 日內觸+紅 K            | 純短線                |
| 8  | `rsi_recovery`            | 反轉     | 10                              | 14D RSI<30 後>50                 | 短線(EV ≈ 0 警示)    |
| 9  | `inst_silent_accum`       | 籌碼     | 10                              | 5/10/20D 法人累計>0+平盤+BB<50%  | 偏波段(籌碼累積)     |
| 10 | `volume_breakout`         | 動能     | 3                               | 今日量 ≥ 5D 均量 × 2.5 + 新高    | 純短線                |
| 11 | `gap_up`                  | 動能     | 5                               | 跳空 1.5%+量比+紅 K              | 純短線(事件)         |
| 12 | `eps_acceleration`        | 基本面   | DEFAULT 5                       | 連 2 季 EPS YoY 正+當季 > 前季   | **季度級已是波段料**  |
| 13 | `high_yield_stable`       | 殖利率   | DEFAULT 5                       | 殖利率>6%+近 4 季 EPS 為正       | **長線取向**          |
| 14 | `inst_oversold_reversal`  | 籌碼     | DEFAULT 5                       | 連 N 日法人賣超後今日轉買        | 短線轉折              |
| 15 | `taiex_alpha`             | 大盤     | DEFAULT 5                       | 個股 >1% AND TAIEX <0%           | 純短線(逆勢)         |
| 16 | `revenue_acceleration`    | 基本面   | DEFAULT 5                       | 月營收 YoY > 30% 且加速          | **月度級已是波段料**  |
| 17 | `big_holder_inflow`       | 籌碼     | DEFAULT 5                       | 千張戶滾動 4 週 μ+1σ 突破        | **週度級已是波段料**  |

### 1.2 `screener_long.py`(獨立、不在 17 套內)

- 條件:近 3 年平均季 ROE > 15 + PE < 20 或 < 產業平均 + 近 5 年連續配息 + 殖利率 > 4%
- 純價值型長線(N 年量級),沒有任何「進場時機」訊號
- **沒有 hold_days 概念** — 是「應該擁有」的篩選池,不是「現在進場」的訊號
- UI 在 `_page_long`,獨立卡片,跟短線不混

### 1.3 結論 — 覆蓋現況

| 風格                              | 現有覆蓋                                                          |
|-----------------------------------|--------------------------------------------------------------------|
| 純短線(< 1 週)                  | 策略 1, 3, 5, 7, 10, 11, 15 — **覆蓋密**                          |
| 短線偏波段(1-3 週)              | 策略 2, 4, 6, 8, 14 — 設成 hold=10 但訊號仍是日線級               |
| **波段(1-6 月)**                | **策略 9, 12, 16, 17 部分沾邊,但都沒一個是「為 1-6 月設計」**     |
| 長線(>1 年)                     | `screener_long` 條件式池,**無進場 timing**                       |

**真正的「波段」缺口**:現有沒有一個策略以「**持有 1-6 月、看月線/週線級訊號**」為設計目標。策略 12/16/17 是「訊號 source 是月/週級」但被當日級訊號用、hold_days 配 DEFAULT 5 天 — 這是 mis-aligned 的(訊號時間軸 vs 持有時間軸不匹配)。

### 1.4 釐清誤會 — orchestrator 提到的不存在實體

- **`ex_dividend_swing`** — 不存在於 `src/strategies.py`。grep 全 repo 無此策略
- **`_PHASE2_LAUNCH_MARKERS`** — 此常數不存在。code 內有的 "Phase 1" / "Phase 2" 是:
  - Phase 1 = 策略 12-16(基本面/殖利率/籌碼反轉/大盤 alpha/月營收)5 套
  - Phase 2 = 策略 17(`big_holder_inflow`)的 rolling μ+σ 邏輯
  - 跟「波段交易 Phase 2」**無關**,只是該 5+1 套策略的開發版本標記
- 跟 launch markers 沾邊的 commit 是 `9c93a34 feat(observe): monthly report + 60d/90d windows + multiplier attribution + strategy launch markers` — 是「觀察期評估」用,不是波段相關
- **這份報告寫的是「從 0 開始做波段」,不是「擴增 Phase 3」**

---

## 2. 缺口分析

針對主公 3 個關鍵訊號(趨勢/20週MA/量價)+ 基本面,審 DB / feature pipeline。

### 2.1 趨勢分類器

**現有**:
- `src/market_regime.py` — 算 TAIEX 整體 regime(bull / weak_bull / sideways / bear),依 close vs MA20/MA60 4 象限
- `src/strategies.py` 的 `screen_ma_alignment` — 個股 MA5>MA10>MA20>MA60 + 全上揚 binary 條件
- `src/ml_features.py`:`momentum_5d / 20d / 60d`、`ma5_above_ma20_pct`、`ma20_above_ma60_pct`(連續特徵)

**缺**:
- **個股級 regime 分類** — 沒有「該個股目前是 uptrend / downtrend / sideway」的 categorical label。`ma_alignment` 只回是/否,不分強度
- **MA 斜率** — 沒有「20週MA 斜率為正且 > X%」的條件;只有「今日 > 昨日」binary
- **ADX** — `src/indicators.py` 沒有 ADX、DI+/DI-,趨勢強度量化目前只能靠 ma_alignment 二元
- **高低點結構**(higher highs / lower lows) — 完全沒有

**評**:可重用 `momentum_60d` + `ma20_above_ma60_pct` 當趨勢 proxy,但要做完整 1-6 月波段該補 ADX(14) 或 ATR%-normalized momentum。

### 2.2 **20週均線(關鍵)**

**現有**:
- `src/indicators.py` 提供 `sma(df, period)` — 但 `df` 是 daily bars
- daily 100 期 ≈ 20 週,但「100日MA」≠「20週MA」(週收盤平均 vs 100 日收盤平均略不同,週末/假日效應使日級噪音更大)
- 整個 codebase **沒有 weekly resampling**(`grep -rE "resample" src/` 無命中)
- 唯一週級資料是 `shareholder_concentration` 表(TDCC 集保週公布)+ `weekly-shareholder-fetch.yml` cron(週六 18:00 UTC),但這走 `week_end` 欄位 + 籌碼資料,不是 OHLC 週線

**缺**:
- 把 daily_prices 重採樣成 weekly OHLCV 的 helper(`pd.resample('W-FRI').agg(...)` 一行,但目前無)
- 20週MA、20週MA 斜率、close vs 20週MA bias、週量比 等 weekly indicator
- 雙層條件:**週線多頭**(close > 20週MA + 20週MA 上揚)**且**日線進場時機 — 整個架構未支援

**評**:此為主公點名的關鍵訊號,**必須補**。技術上不難(pandas resample),但要決定:
- (a)即時算(每次 picks 重算 weekly)— 慢但簡單,純函數 / cache 友善
- (b)寫入 DB 表 `weekly_prices`(類比 daily_prices) — 快但要新 cron / preload
- 建議 **(a)** 先做,觀察 picks 計算總時長,真的卡再升 (b)

### 2.3 量價關係

**現有**:
- `vol_ratio`(今日量 / 5D 均量) — 多策略共用
- `volume_breakout`(策略 10) — 今日量 ≥ 5D 均量 × 2.5
- `bb_lower_rebound` / `gap_up` 也帶量比條件
- ML features 有 `vol_ratio`、但不分量價方向

**缺**:
- **OBV(On-Balance Volume)** — `src/indicators.py` 完全沒有(grep 確認)
- **量價背離偵測** — 沒有「價創新高但量未創新高」或反向組合的 helper
- **量縮回踩 vs 量增續攻 的 categorical 分類**(主公點名要的「量增價漲/量縮回踩/量價背離」)
- 5D 均量太短,週線級該配 **20D 或週均量**

**評**:量價分類是「**訊號質感**」核心,不能光靠 vol_ratio 一個數。最少要補 OBV + 一個 categorical helper `classify_volume_price(df, lookback) -> {"rising_with_volume", "pullback_low_volume", "divergence", "other"}`。

### 2.4 基本面欄位

**現有 `financials` 表**(`src/database.py:156-166`):
- `stock_id, period_type, period, revenue, revenue_yoy, eps, roe` — 月營收 + 季 EPS + 季 ROE 都在,以 `period_type` 區分
- `dividend` 表:`year, cash_dividend, stock_dividend, ex_dividend_date`
- `daily_metrics` 表:`close, pe, pb, dividend_yield`(TWSE 官方,每日)

**缺**:
- **毛利率 / 營業利益率 / 淨利率** — `financials` 表沒有此欄位(grep 全 repo 無 `gross_margin / 毛利率`)
- 季 EPS YoY 加速度只在策略 12 計算,不存表
- 月營收 YoY 加速度同樣不存,每次重算

**評**:**毛利率是波段選股的核心 quality 指標**(高毛利 = 護城河),建議補。其他指標可重用既有。

**資料新鮮度 vs cron** :

| 資料                  | 公告週期         | 現有 cron                                                |
|-----------------------|------------------|----------------------------------------------------------|
| 月營收                | 每月 10 日       | `backfill-revenue.yml` 週一 15:13 UTC(8-shard 分散)     |
| 季 EPS / ROE          | Q1/Q3 5月 8月 11月 / Q4 隔年 3 月底 | `backfill-financials-once.yml` 手動觸發(已 quota-alert pivot,memory `project_backfill_quota_alert_pivot_2026_05_19.md`) |
| 配息                  | 股東會後 7-9 月  | `backfill-dividend.yml` 每天 ~14:00 UTC                  |
| 千張戶集保            | 每週公布         | `weekly-shareholder-fetch.yml` 週六 18:00 UTC            |
| Daily prices          | 每收盤後         | `daily-notify.yml` 22:13 台北 + `morning-refetch.yml`    |
| daily_metrics(PE)     | 每日 TWSE 官方   | 整合在 daily 鏈                                          |

**評**:資料新鮮度對 1-6 月波段已足夠;月營收 monthly cron 對齊月 10 日公告。**唯一要補的是毛利率**(需要從 income statement 抓,FinMind 免費版 `TaiwanStockFinancialStatements` 有,但目前 fetcher 沒拉)。

### 2.5 缺口總結

| 訊號類             | 現有可用                          | 真正要補                                             | 工作量   |
|--------------------|-----------------------------------|------------------------------------------------------|----------|
| 趨勢分類器         | regime / ma_alignment / momentum  | ADX 或斜率連續值(可選);**個股級 categorical label** | 中       |
| **20週均線**       | **無**                            | **weekly resample helper + 20wMA 系列指標**         | **中**   |
| 量價分類           | vol_ratio                         | **OBV + 量價組合 categorical**                       | 中       |
| 基本面             | revenue/eps/roe/dividend          | **毛利率欄位 + fetcher**                              | 小-中    |
| 資料新鮮度         | 已對齊                            | 無                                                   | 0        |

---

## 3. 策略 spec 草稿(2 個範例)

### 3.1 範例 A — **價值動能波段(value_momentum_swing)**

**設計動機**:基本面有護城河(高 ROE + 高毛利)+ 技術趨勢明確(站上 20wMA)+ 量價良性(量增價漲),持 1-6 月。

**進場條件**(全部 AND):

| # | 條件                                                                              | 資料來源                |
|---|-----------------------------------------------------------------------------------|-------------------------|
| 1 | 近 4 季平均 ROE ≥ 12%                                                             | financials.quarterly    |
| 2 | 近 1 季毛利率 ≥ 該產業 P50(或絕對 ≥ 25%)                                        | **待新增欄位**          |
| 3 | 月營收 YoY > 0%(近 3 個月至少 2 個月)                                             | financials.monthly      |
| 4 | close > 20週MA AND 20週MA 上揚(本週均線 > 4 週前均線)                            | **待新增 weekly helper**|
| 5 | 過去 4 週至少 2 週收紅且週量 > 13週均量 × 1.2(量增價漲確認)                       | **待新增 helper**       |
| 6 | 近 4 週周 K 不破前低(無 lower low) — higher lows 結構                            | weekly resample         |

**出場條件**(任一觸發):

| # | 條件                                                              | 動作                  |
|---|-------------------------------------------------------------------|-----------------------|
| 1 | close < 20週MA 且連 2 週收下                                     | 全部出場              |
| 2 | 周 K 量價背離(週價新高 + 週 OBV 未創新高)連 2 週                | 砍半倉                |
| 3 | 月營收 YoY 由正轉負                                              | 全部出場              |
| 4 | 持有滿 6 個月仍未達 +20% — 強制檢視                              | 人工 review,不自動賣 |
| 5 | 從進場最高點回撤 > 1.5 × 20週ATR%                                | 全部出場(trailing)  |

**部位**:單檔 5-10%(由 ATR 反推 — 1R 風險定 1% 總部位,跟既有 `position_sizing.py` Kelly 對齊)

**初估勝率**:**TBD**(沒回測前不講)。同類論文 / 美股研究 1-6 月波段勝率落在 45-55% 但 R:R 通常 2-3:1,EV 為正。

### 3.2 範例 B — **法人籌碼週級接力(inst_weekly_handoff)**

**設計動機**:沿用既有 `big_holder_inflow`(千張戶滾動週均突破)的成功 pattern,加強籌碼層 + 拉長到月級。

**進場條件**:

| # | 條件                                                                                      |
|---|-------------------------------------------------------------------------------------------|
| 1 | 近 4 週千張戶 holders_delta_w 都 > 0(連續累積,不是單週脈衝)                            |
| 2 | 近 4 週三大法人累計買超 / 流通股 > 0.5%                                                   |
| 3 | close > 20週MA                                                                            |
| 4 | 季 EPS YoY > 0%(基本面確認非雷)                                                         |
| 5 | 週量比 > 1.0(無萎縮)                                                                    |

**出場**:類似 A,但更看籌碼 — 法人連 3 週賣超就走。

**部位**:單檔 3-7%(籌碼信號 noise 略高,部位略小)

**初估勝率**:**TBD**

### 3.3 為何要兩個

- A 偏基本面(慢、穩、適合 IF)
- B 偏籌碼(快、跟主力、技術參數較多)
- 主公可以挑一個 MVP 先做(我建議 A — 基本面缺口最容易補、語義最清楚、跟主公敘事「看趨勢/20週MA/量價」最對齊)。B 留 Phase B 再評估。

---

## 4. UI/UX 提案

### 4.1 現狀重新審查

orchestrator 說「現有 3 張卡片:短線/長線/關注」,**但實際 grep 確認**:
- `_page_dashboard` 結構是 4 區塊(`app.py:1133` docstring):**大盤 / 短線推薦 Top 3 / 關注 Top 3 / 系統狀態**
- 長線推薦**沒在首頁**,只在 `💎 長線` 獨立 tab(`_page_long`)
- 整個 app 走 `segmented_control` 上方水平 tabs,21 頁(`app.py:110-118`)

### 4.2 三個方案比較

| 方案 | 內容                                                                                          | 優點                                                  | 缺點                                                                          |
|------|-----------------------------------------------------------------------------------------------|-------------------------------------------------------|-------------------------------------------------------------------------------|
| X    | 首頁加第 4 區塊「波段推薦 Top 3」、頂部加 tab「🎯 波段」                                       | 視覺一致、跟現有 segmented_control 同 pattern         | 首頁從 4 區塊變 5 區塊,手機 scroll 變長;tab 已 21 個,擠                    |
| Y    | 在 `💎 長線` 頁加 horizon toggle(基本面池 / 波段進場時機),重用既有 page                    | 不增加 tab、語義(長線 / 波段都跟基本面有關)接近     | 跟既有 `screener_long`(篩選池,無時機)語義不一樣,可能混淆;手機 toggle 容易誤觸 |
| Z    | 短線 picks 加一欄 `horizon`(短/中/長),靠 STRATEGY_CATEGORY 標籤過濾                          | 0 新頁 / 卡片 schema 統一                             | **混淆風險最高** — 短線/波段 risk profile 不同,放同卡使用者可能拿短線 sizing 進場波段;主公明確說「不一樣的 risk profile」就是要分開 |

### 4.3 建議 — **方案 X**

**理由**:
1. 主公的明確需求:「**不一樣的 risk profile,更穩、適合大部位**」 → UI 必須讓波段跟短線**視覺上分得開**,提示使用者用不同 sizing
2. `_page_dashboard` 從 4 變 5 區塊雖然 scroll 變長,但每塊都是 Top 3 限制(`render_picks_cards` 已支援 `head 3`),手機 scroll 一螢幕 1 塊也只是再多滑一下 — 主公的 iPhone 主畫面用法可接受
3. 頂部 tab 雖已 21 個,但 segmented_control 是水平 scroll 不擠版面;反而長線/短線/波段一字排開,語義清楚
4. 方案 Y 跟 `screener_long`(篩選池)語義衝突,使用者會把「應該擁有」跟「現在進場」混淆 — 出 bug 機率高
5. 方案 Z 跟主公明確需求矛盾,先排除

**修改 spec**(若採方案 X):
- `PAGES` 加 `"🎯 波段"`,放在 `"💎 長線"` 之後
- `_page_dashboard` 加區塊「### 🎯 今日波段推薦 (Top 3)」,放在「短線推薦」與「關注」之間
- 新 page `_page_swing` 結構參考 `_page_short`(picks 表 + 卡片 + 立即推播 + 「加入持倉/觀察」按鈕),但:
  - 預設 `horizon="swing"` 標籤,卡片右上加 🎯 圖示
  - 部位建議改 5-10%(`position_sizing` 加 horizon 參數)
  - 出場警報走 weekly check,不是 daily(降低 push 噪音)

---

## 5. 工作量分塊(Phase A/B/C/D)

| Phase | 內容                                                                                       | 大小 | 前置依賴                          |
|-------|---------------------------------------------------------------------------------------------|------|-----------------------------------|
| **A** | **訊號 + spec 落地**:                                                                       | 中   | 無                                |
|       | A1. `src/indicators.py` 加 `obv()`、`adx()` 或斜率版本                                       | 小   |                                   |
|       | A2. 加 `src/weekly_resample.py`(daily → weekly OHLCV helper + cache decorator)             | 小   |                                   |
|       | A3. 加 `volume_price_classify(df) -> str` helper(量增價漲/量縮回踩/背離 categorical)        | 小   | A1                                |
|       | A4. `src/financial_fetcher_free.py` 補抓毛利率(`TaiwanStockFinancialStatements` 解析 GM 列)| 小   | DB schema migration               |
|       | A5. DB schema:`financials` 表加 `gross_margin REAL` 欄位 + migration                          | 小   |                                   |
|       | A6. `src/strategies.py` 加 `screen_value_momentum_swing()` + 註冊 `STRATEGY_CATEGORY`        | 中   | A1-A5                             |
|       | A7. 單元 test(`tests/test_strategy_swing_*.py`)— 至少訊號邊界                              | 小   | A6                                |
| **B** | **回測 + AUC gate**:                                                                        | 大   | A 完成                            |
|       | B1. 把 `backtester.py` 擴展支援 `hold_days` 可變(目前已支援,但驗證 60-180 天區間穩定性)   | 小   |                                   |
|       | B2. 對 N=126 / 252 / 504 日歷史跑波段策略回測,寫進 `strategy_backtest` 表                    | 中   | B1                                |
|       | B3. `vbt_backtest.py` grid sweep 找出最佳 (target_pct, stop_pct, hold_days)                  | 中   | B2                                |
|       | B4. 加 AUC gate — 跟既有 ML 那套 walkforward 同 pattern(`ml_walkforward.py`)               | 中   | B2                                |
|       | B5. 若 AUC < 0.55 或 EV < 2%(扣交易成本) → **不上線**,改回頭調訊號 / 砍策略              | 0    | B4 evidence                       |
| **C** | **UI + 觀察期**:                                                                            | 中   | B 通過(否則不做 C)              |
|       | C1. `PAGES` 加 "🎯 波段" + `_page_swing` 實作(參考 `_page_short`)                          | 中   | B 通過                            |
|       | C2. `_page_dashboard` 加第 4 區塊 Top 3                                                      | 小   | C1                                |
|       | C3. `notifier.py` 加波段推播分類(daily-notify 不混,自走 weekly 推播)                       | 中   | C1                                |
|       | C4. `position_sizing.py` 加 `horizon` 參數,波段 5-10% vs 短線 2-5%                          | 小   |                                   |
|       | C5. 觀察期 4-8 週(`memory/observation` pattern)— 不上 elite default、走 paper trading 流  | 0    | C1-C4 + paper_trades 表已存在     |
| **D** | **Paper trading 驗證 + ML**:                                                                | 大   | C 4-8 週觀察期累積樣本 ≥ 30 筆    |
|       | D1. ML 訓練(`ml_walkforward.py` 加 horizon 參數)                                            | 中   | C5 樣本                           |
|       | D2. ML 過濾 + threshold 校準(類似既有 `STRATEGY_ML_THRESHOLDS`)                            | 中   | D1                                |
|       | D3. 上 elite default + ML gate(對齊 memory `project_notification_elite_mode_2026_05_19`)   | 小   | D2 通過                           |

**保險閥**:**B5 不過就停**,不要硬上 C。寧可不做也別做了一個 EV 為負的策略。

---

## 6. 「不做沒意義的事」誠實檢核

主公的規矩(memory `feedback_no_pointless_work.md`):動工前先問「有意義嗎」。

### 6.1 增量在哪 / 不在哪

**真正的增量**:
1. **20週MA + 週級量價分類**:現有完全沒有,這是新訊號維度,**有增量**
2. **毛利率欄位**:基本面 quality 角度現缺,加了能讓多策略受益(不只波段),**有增量**
3. **波段 risk profile(更大部位、更慢出場)**:主公明確說要的,既有所有策略都是短線部位邏輯,**有增量**

**包裝改寫,不是新增**:
1. `eps_acceleration`(策略 12)/ `revenue_acceleration`(策略 16)— 訊號 source 本來就是月/季級,**只是 hold_days 配錯**。如果只改 hold_days 而沒加 20wMA / 量價 / 毛利率,等於只是給短線策略換個標籤
2. `big_holder_inflow`(策略 17)— rolling 4 週本來就是週級,改 hold_days 即可,**也是包裝改寫**

**沒意義的工作**:
1. 把所有 17 個策略加 horizon toggle(方案 Z) — 純增加複雜度,沒新訊號
2. 為波段重做完整 ML pipeline(D phase)— 在沒有 30+ 樣本前訓不出穩定 model,跟 v4 ML kill-switch 教訓對齊
3. 為波段建獨立 cron 線(weekly-swing-notify) — 既有 daily-notify 22:13 已是台股收盤後固定時段,沒理由再開一線

### 6.2 資料/模型/UI 工作量 vs 預期勝率提升

**樂觀情境**:
- 假設加 20wMA + 量價 + 毛利率三條件後,波段策略 EV 落在 +3-5%(對齊 `bias_convergence` rescue 後表現)
- 配 hold_days=60-120 天,年化交易頻率 2-4 次 / 檔,跟短線(每月翻倉)互補
- 大部位 5-10% × 5 檔 = 25-50% 倉位用波段,適合主公「不每天盯盤」的長期視野
- **有意義**

**悲觀情境**:
- 加完訊號後 AUC < 0.55 / 扣成本 EV < 0 → B5 觸發停損,等於只賺到「補了 20wMA / OBV / 毛利率」這幾個 helper 給其他策略用
- **這個下限也還行** — 因為 20wMA / OBV / 毛利率是「multiplier」,加進 ml_features 後其他策略也能用,不是純沉沒成本
- 但只衝這個會違反「先問有意義嗎」 — 因此**前提是 B5 evidence 過了才做 C**

### 6.3 有沒有更務實的替代

**替代 1**:**只擴 `screener_long`,加「20週MA 上揚」filter**
- 不開新策略、不開新頁、不開新 cron
- 主公的「波段」需求大半可以靠「篩選池 + 1 個技術 timing 條件」滿足
- 工作量 = Phase A2(weekly helper)+ 改 `screener_long` 條件 + UI 加 toggle
- **缺點**:跟現有 `screener_long`(N 年連續配息 / ROE 池子)語義不一樣 — 配息是長線、20wMA 站上是中波段,塞同個 page 會語義不清

**替代 2**:**只改既有策略 12/16/17 的 hold_days,UI 加 horizon 欄**
- 工作量最小,1 天可上
- **缺點**:沒回測 evidence、沒新訊號、純調參數,違反 memory `feedback_no_pointless_work.md`(改包裝不算意義)

**替代 3**(本報告主建議):**做 Phase A1-A6 訊號層、上 B 回測、B5 evidence 過了才做 C UI**
- 最重的路徑,但唯一一條能驗證「波段是否真的有 alpha」的路
- A1-A5(indicators/helpers/毛利率)就算 B5 不過,**也對 ml_features / 既有策略有正向 spillover**
- 是 ROI 最確定的選擇

### 6.4 明確結論

**做。但只走 Phase A + B,B5 evidence 過了才做 C。**

**理由**:
1. 主公明確需求,需求本身合理(現有架構真的沒有 1-6 月波段這層)
2. 缺口是真的缺口 — 20週MA / OBV / 毛利率沒有就是沒有,不是已有改名
3. A 層工作量中、可平行 sunk cost(spillover 給 ml_features + 其他策略)
4. B 層保險閥可信 — `bias_convergence rescue 2026-05-18`(WR 33.7→92.6%)就是「訊號 + threshold 校準後驗 EV」流程跑通的證據,沿用此 pattern
5. C 層在 B5 evidence 通過前**不要做**,守 `feedback_no_pointless_work.md` 紅線

**不做**:
1. 不做方案 Z(短線卡片加 horizon 欄)— 跟主公「不同 risk profile」需求衝突
2. 不做 Phase D ML pipeline,等 paper trading 4-8 週樣本累積後再評估
3. 不為波段開新 cron 線(daily-notify 22:13 + weekly-shareholder-fetch 已夠用)

**改做別的(備案,若 B5 不過)**:
1. 把 A 層的 helpers(weekly resample / OBV / 毛利率)合進 ml_features,讓既有 v4 model 重訓多 3 個維度 — 不開新策略,但讓既有 17 套吃到新訊號
2. 把 `screener_long` 加 20wMA filter(替代 1)— 不開新 page,只強化既有長線池

---

## 7. 建議的下一步(給主公拍板)

主公拍板以下 3 個決定才開工:

1. **是否認可方案 X UI**(首頁 5 區塊 + 新 tab 🎯 波段)— 或改方案 Y / Z(本報告反對 Z)
2. **B5 不過時要不要降級走「替代 1」(擴 screener_long)** — 還是直接放棄
3. **Phase A 是否先動 A1-A3 + A4-A5 平行**(`indicators` 加 OBV / weekly helper 跟毛利率 fetcher 可平行做,但要主公拍要不要兩條一起)

開工順序建議:**A1 → A2 → A3 → A4 → A5 → A6 → A7 → B1-B5(gate)→ C(若 B5 過)**

工時:A 總計 1-2 個工作天、B 約 2-3 個工作天(含等回測 + grid sweep)、C 約 1-2 個工作天。**C 是否做不確定,所以總時程 = 3-5 天 + 4-8 週觀察期。**

---

## 8. 附錄 — 用到的 evidence 引用

| Reference                                                                | 用途                                          |
|--------------------------------------------------------------------------|-----------------------------------------------|
| `src/strategies.py:35-141, 1051-1696`                                    | 17 策略 default params + Phase 1/2 命名來源 |
| `src/strategies.py:2082-2115`                                            | STRATEGY_CATEGORY + regime filter            |
| `src/strategies.py:1885-1899`                                            | STRATEGY_RR_PARAMS hold_days 表              |
| `src/screener_long.py:39-150`                                            | 長線篩選池現有條件                            |
| `src/market_regime.py:1-128`                                             | TAIEX regime 邏輯                             |
| `src/indicators.py:1-285`                                                | 確認無 OBV / ADX                              |
| `src/ml_features.py:332-346`、`src/ml_predictor.py:62-87`                | 既有 features 列表(無 weekly / OBV)        |
| `src/database.py:120-186`                                                | financials 表 schema(無 gross_margin)       |
| `app.py:110-118, 985-1041, 1132-1316`                                    | PAGES + dashboard 4 區塊現況                  |
| `.github/workflows/{weekly-shareholder-fetch,backfill-revenue,daily-notify}.yml` | 既有 cron 排程                          |
| memory `project_bias_convergence_rescue_2026_05_18.md`                   | B 層回測 evidence 流程的 reference            |
| memory `project_notification_elite_mode_2026_05_19.md`                   | C 層 elite default 推播策略                   |
| memory `project_backfill_quota_alert_pivot_2026_05_19.md`                | 財報 backfill 已 alert-based(對 A4 規劃)    |

---

**報告結束**。主公拍板上述 3 個決定後即可動工。
