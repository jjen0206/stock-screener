# Swing 模組實作主計劃(Phase 0c-B → A → B → C → D)

> **狀態**:**主 spec — 取代** `docs/swing_trading_proposal.md`(ecstatic-pike branch)
> 與 `docs/lin_enru_strategy_proposal.md` v3(clever-panini branch)
> **日期**:2026-05-19
> **撰寫者**:亮(諸葛亮 task)
> **拍板者**:主公(本檔開頭三項決策)
> **本檔角色**:從「兩份各自獨立 spec」整合為「**一套 swing 模組 + 基本面 toggle**」,
> Phase 0c-B 開工的 single source of truth。後續 phases 都依本檔執行,**舊 spec 只當參考**。

---

## 0. 主公拍板的最終形態

| 維度          | 拍板選擇                                                    | 來源 |
|---------------|------------------------------------------------------------|------|
| Q1 模組架構   | **C — 一套 module + 基本面 toggle**                        | 主公 |
| Q2 資料範圍   | **B — 完整集 backfill(daily_prices + institutional + financials + dividend + revenue + company_profiles)** | 主公 |
| Q3 範圍       | **D — 完整(features + backtest + UI + paper trading)**     | 主公 |
| 模組命名      | **`swing`**(波段)— 取代 `lin_enru`、取代 ecstatic-pike 的 「swing」 各自獨立做的舊概念 | 主公 |
| 加速          | **Phase A features 跟 backfill 平行**(Phase 0c-B 跑時 Phase A code 可同時動工)| 主公 |
| 方法論基礎    | **真實林恩如方法**(技術派,週 K 20MA + W 底 + 趨勢線 + 週爆量 + 嚴格停損)+ **可選基本面 toggle**(ROE/毛利率/月營收 YoY)| 兩份 spec 整合 |
| 停損規則      | **林恩如嚴格版**(跌破 20wMA 立即砍 / 跌破趨勢線 / M 頭 / 連 3 週新低)→ 真實林恩如「停損是交易的精髓」 | clever-panini v3 |
| 持有期        | 由 20wMA 與停損訊號決定,典型數週至 1-6 個月                | 兩份 spec 共識 |
| Paper trading 表 | **新表 `swing_trades`** — 完全隔離,不動既有 `paper_trades`(短線用)| 主公「不要跟其他共用」 |
| UI            | 方案 X — 首頁加 「🎯 波段推薦 Top 3」 區塊 + 頂部新 tab 「🎯 波段」 | ecstatic-pike |
| 觀察期        | 4-8 週 paper trading 累積 ≥ 30 筆樣本 → 才上 ML / elite default | 兩份 spec 共識 |

---

## 1. 兩份舊 spec 的整合決策

### 1.1 兩份 spec 互補(都納入)

| 元素                      | 來源                  | 採用方式 |
|---------------------------|-----------------------|----------|
| 20 週均線 helper          | 兩份都點名             | A2 必做  |
| W 底 / M 頭型態識別        | clever-panini v3 A4    | A4(複雜,獨立小檔) |
| 趨勢線(swing point + 線性回歸)| clever-panini v3 A3 | A5 — 但**只在「基本面 toggle」關閉的純技術派模式做** |
| OBV 量價分類              | ecstatic-pike 2.3      | A6 必做  |
| 週爆量                    | clever-panini v3 A5    | A2 整合一起做 |
| 毛利率欄位 + fetcher       | ecstatic-pike 2.4      | A7 — 只在「基本面 toggle 開啟」時用 |
| 個股級 regime 分類器       | ecstatic-pike 2.1      | 暫緩(Phase B 可選) |
| 觀察期 paper trading       | 兩份共識               | D phase  |
| AUC gate(`bias_convergence` rescue pattern) | ecstatic-pike 5 B5 | Phase B  |

### 1.2 兩份 spec 衝突(用「真實林恩如」版)

| 元素        | clever-panini v3 真實林恩如版                                         | ecstatic-pike 版                                              | 採用方向 |
|-------------|----------------------------------------------------------------------|----------------------------------------------------------------|----------|
| 停損        | **嚴格停損**(跌破 20wMA / 跌破趨勢線 / M 頭 / 連 3 週新低 / trailing) | 「持有滿 6 個月未達 +20% 強制 review」(寬鬆)                | **嚴格版**(主公明確要「林恩如」風格) |
| 出場 reason | `break_w20` / `break_uptrend` / `m_top_pattern` / `new_low_3w` / `trail_stop` | `fundamental_break` / `target_hit` / `max_hold_timeout` | **clever-panini 五個技術 reason**(若加基本面 toggle,新增 `fundamental_break` 第六種,但**仍以技術為主**) |
| 持有期上限  | 無上限(由 20MA 走勢決定)                                            | 1-6 月(報告自設)                                            | **無上限,以 20wMA / 停損為主**;UI 顯示「典型 1-6 月」當參考 |
| 進場條件    | 5 條件 AND(漲破 20wMA + 20DMA 上穿 20wMA + 上升趨勢線 + W 底 + 週爆量)| 6 條件 AND(ROE + 毛利率 + 月營收 + 20wMA + 量增價漲 + 無 lower low)| **預設技術派 5 條件(林恩如)**;基本面 toggle 開啟 → 加 3 條 ecstatic-pike 的基本面 AND filter |

### 1.3 命名衝突(全部解掉)

| 舊命名                          | 衝突點                                              | 新命名(本 spec 採用)     |
|---------------------------------|----------------------------------------------------|---------------------------|
| `lin_enru_trades`(clever-panini)| 跟主公「swing」命名不一致                          | **`swing_trades`**         |
| `ex_dividend_swing`(實際 repo) | 已存在於 `src/data_fetcher.py` / `snapshot_health.py` / `price_alerts.py` / `database.py`(除權息事件相關 ETL/notifier)— **不要跟波段混淆** | 保留原名 — 短線歸短線,不動 |
| `paper_trades`(既有表)         | 既有短線 paper trading 用                          | 不動 — 波段走新表 `swing_trades` |
| `_PHASE2_LAUNCH_MARKERS`        | ecstatic-pike 1.4 已澄清不存在                     | 忽略                       |
| `swing` 模組(ecstatic-pike 草案)/ `lin_enru` 模組(clever-panini 草案)| 兩個草案各自獨立                       | **合併為單一 `src/swing/`**(本檔 1.4 目錄結構) |

### 1.4 統一目錄結構

```
src/swing/                       # ← 主模組(新增)
├── __init__.py
├── features/
│   ├── __init__.py
│   ├── weekly_resample.py       # daily → weekly OHLCV helper(A2)
│   ├── ma_signals.py            # 20wMA / 20DMA 上穿 / 斜率(A2)
│   ├── trendline.py             # swing point + 線性回歸求上升趨勢線(A5)
│   ├── pattern.py               # W 底 / M 頭型態識別(A4)
│   ├── volume_signal.py         # 週爆量 + OBV + 量價分類 categorical(A6)
│   ├── fundamentals.py          # ROE / 毛利率 / 月營收 YoY toggle filter(A7,可選)
│   └── universe.py              # 市值 20 億+ + 流動性過濾(A8)
├── strategy.py                  # screen_swing(params, fundamental_toggle=False)(B1)
├── backtest.py                  # 週級別 backtest + 4 停損 + 移動停利(B2)
├── paper_trading.py             # add / evaluate / 移動停利更新(D2)
├── notifier_swing.py            # 週推播(D5,跟既有 daily-notify 隔離)
└── ui_cards.py                  # 卡片元件(C2,獨立卡片不用既有 cards)
```

**關鍵原則**(主公拍板「不跟其他共用」):
- swing 模組所有 features helper(weekly_resample / ma_signals / trendline / pattern / volume_signal)**獨立寫**,**不 reuse** `src/indicators.py` 既有 helper
- 例外:`src/indicators.py` 的 `sma()` / `ema()` / `rsi()` 仍可 import 使用(純數學函式,風險低)
- 但**新加的** weekly resample / W 底識別 / 趨勢線等**只放在 `src/swing/features/`**,不污染現有 `src/indicators.py`

### 1.5 舊 spec 處理(archive)

執行本 phase 時:

| 舊檔                               | 動作                                                            | 理由 |
|------------------------------------|----------------------------------------------------------------|------|
| `docs/swing_trading_proposal.md`(ecstatic-pike branch)| **歸檔當參考** — 在檔頭加 `> Status: ARCHIVED, see docs/swing_implementation_plan.md` warning | 寫得不錯但已被本檔取代 |
| `docs/lin_enru_strategy_proposal.md`(clever-panini branch)| **歸檔當參考** — 同上                                                | v3 方法論研究價值高,但 spec 框架已併入本檔 |
| `docs/lin_enru_methodology_research.md`(clever-panini branch)| **保留當原始研究** — 不歸檔,本檔多處引用 | 4 個獨立來源研究真實林恩如方法,還很值得讀 |
| `docs/lin_enru_data_audit.md`(clever-panini branch)| **保留** — 本 phase 0c-B 的 audit 根據                    | Audit 數據(0 檔過 1260 天門檻 / financials 1.5 季 / dividend 8 檔 / monthly_revenue 0 筆)Phase 0c-B 必須對齊 |

歸檔做法:branch merge 前在兩份 spec 檔頭加 `> ⚠️ ARCHIVED 2026-05-19 — superseded by [docs/swing_implementation_plan.md](swing_implementation_plan.md)` 一行,不刪檔(避免 git history 斷)。

---

## 2. Phase 0c-B(本 task 範圍)— 資料 backfill 改造

**目標**:把 6 個 backfill scripts + 6 個 GHA workflows 確認都支援 5 年(1260 交易日)backfill,主公手動觸發即可開跑。**不重啟自動 cron**(memory `[[project-backfill-quota-alert-pivot-2026-05-19]]` 已暫停 financials/company_profiles cron;dividend/revenue cron 仍跑但 quota-friendly,本 phase 不動)。

### 2.1 現況 audit 結果

| Script                                 | 5-year 支援狀態 | 備註 |
|----------------------------------------|----------------|------|
| `scripts/backfill_history.py`          | ✅ `--days N`(default 90)→ 傳 1825 即 5 年 | `--min-existing 1260` 才會強制 backfill 已有 stocks |
| `scripts/backfill_financials.py`       | ✅ `--years 5`(default 5) | 已支援,quota fail-fast 已加 |
| `scripts/backfill_dividend.py`         | ✅ `--years 5`(default 5) | 已支援 |
| `scripts/backfill_revenue.py`          | ✅ `--years 5`(default 5) | 已支援 |
| `scripts/backfill_institutional.py`    | ✅ `--start --end` 自由區間 | end default 2025-11-03(寫死),需改成「LATEST 或 today」 |
| `scripts/backfill_company_profiles.py` | ✅ universe-based,無 years 概念 | 跑 `--universe pure_stock` 全市場即可;LLM 模式吃 Gemini quota |

| Workflow                                              | workflow_dispatch | 5-year input | 備註 |
|-------------------------------------------------------|-------------------|--------------|------|
| `.github/workflows/backfill-history.yml`              | ✅                | `days` input default 90 → 傳 1825 | input 描述要加 swing 5y 用法說明 |
| `.github/workflows/backfill-financials-once.yml`      | ✅                | `years` input default 5 | 已 OK |
| `.github/workflows/backfill-dividend.yml`             | ✅ + 週日 cron     | `years` input default 5 | 已 OK |
| `.github/workflows/backfill-revenue.yml`              | ✅ + 週一 cron     | `years` input default 5 | 已 OK |
| `.github/workflows/backfill-institutional-once.yml`   | ✅                | `start`/`end` input | end default 2025-11-03 改 `LATEST`(或不動,SOP 教主公自己填) |
| `.github/workflows/backfill-company-profiles-llm-once.yml` | ✅           | universe + batch | 已 OK |

### 2.2 本 phase 改動

**Code 改動**:
1. `scripts/backfill_history.py` — 加 docstring 註解標明「swing 5y 模式 = `--days 1825 --min-existing 1260`」,加 `--days > 1000` warning(避免主公誤觸大 backfill)。
2. `scripts/backfill_institutional.py` — 改 `--end` 接受 `LATEST` sentinel(自動代換成今天),docstring 同上加 swing 5y SOP 提示。
3. **無新檔**:不寫 `backfill_daily_metrics.py`(swing 不需要 5 年 PE/PB/yield;daily_metrics 既有 TWSE OpenAPI 只回最新日,5 年歷史對 swing 技術派非必要)。

**Workflow 改動**:
1. `.github/workflows/backfill-history.yml` — input `days` 描述加 「5 年 swing backfill 填 1825」,`min_existing` 同步加 「5 年模式建議 1260」。
2. `.github/workflows/backfill-institutional-once.yml` — input `end` 改 default `LATEST`,description 加 「5 年模式 start=2021-05-19 / end=LATEST」。
3. **無新 workflow**:6 個都已 workflow_dispatch 化。

**Docs 新增**:
1. `docs/swing_backfill_runbook.md` — 主公觸發 SOP,包含順序、預估時間、配額處理、失敗 retry、SQL 驗證範例。

**舊 spec 歸檔標記**(留待 PR merge 時手動加,本 PR 不動 branch):
1. ecstatic-pike branch 上 `docs/swing_trading_proposal.md` 檔頭加 ARCHIVED warning(下次 merge 那 branch 時動)
2. clever-panini branch 上 `docs/lin_enru_strategy_proposal.md` 檔頭加 ARCHIVED warning(同上)

### 2.3 配額管理(主公拍板觸發前必看)

memory `reference_cronjob_org_morning_brief.md` + 本 task 註記:

| 配額源       | 重置時間              | 5-year backfill 影響                                          |
|--------------|----------------------|---------------------------------------------------------------|
| FinMind      | 6/1 09:00 台北(每月 1 號)| daily_prices / financials / dividend / revenue / institutional fallback 都吃此 quota。**主公拍 5y backfill 前確認 quota 重置時間**;最壞情境 8-shard × 多日跑分批 |
| Gemini       | 5/20 00:00 台北(每天) | company_profiles LLM 模式吃此 quota(15 RPM / 1500 RPD free)。 5y backfill 不需要 LLM,跑 warm-up 模式即可避開 |
| GH Actions   | 每月免費 2000 min      | 5y backfill 全套(6 種 × 8 shard × 30-60 min)可能 800-1500 min,單月吃滿一半以上 — **建議分週跑** |

### 2.4 Phase 0c-B 驗收條件

主公拍 PR merge 後,**手動觸發各 backfill** 跑完(這 phase 內不跑真實 backfill,只是 ship code + workflow + SOP):

```sql
-- daily_prices:5y / 2700 sid → ~3.4M rows
SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(date), MAX(date) FROM daily_prices;
-- 期望: > 3,000,000 rows / > 2,000 stock_id / MIN 2021-05-19~ / MAX 今天

-- 過 1260 天門檻檔數
SELECT COUNT(DISTINCT stock_id) FROM (
  SELECT stock_id FROM daily_prices GROUP BY stock_id HAVING COUNT(*) >= 1260
);
-- 期望: > 1500 檔(從 audit 0 檔提升)

-- financials.quarterly:5y × 20 季 / 2000 sid → ~40k rows
SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(period), MAX(period)
FROM financials WHERE period_type='quarterly';
-- 期望: > 30,000 rows / > 1,800 stock_id / 範圍 2020Q1~ ~ 2026Q1

-- dividend:5y / 2000 sid → ~10k rows
SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(year), MAX(year) FROM dividend;
-- 期望: > 8,000 rows / > 1,500 stock_id

-- monthly_revenue:5y × 60 月 / 2000 sid → ~120k rows
SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(period), MAX(period)
FROM financials WHERE period_type='monthly_revenue';
-- 期望: > 100,000 rows / > 1,800 stock_id

-- institutional:5y × 250 工作日 / 2000 sid → ~2.5M rows
SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(date), MAX(date) FROM institutional;
-- 期望: > 2,000,000 rows / > 2,000 stock_id / 範圍 2021-05~ 今天

-- company_profiles:全 universe 至少 facts(industry / market)
SELECT COUNT(*) FROM company_profiles WHERE industry IS NOT NULL;
-- 期望: > 2,500 檔
```

主公確認所有 SQL 都過門檻後,**Phase 0c-B 完成**,Phase A 才開工(features 寫起來)。

---

## 3. Phase A — Features(7 個 helper,主公人力 ~15h)

**前置依賴**:Phase 0c-B daily_prices ≥ 5 年完成(才能 weekly resample 算 20wMA + 趨勢線等)。

**可平行做**:Phase 0c-B 跑 backfill 同時,Phase A code 可動工(用既有 50 大型股 cache 開發 + 跑 unit test,等 backfill 完再跑 full universe smoke test)。

| 步驟 | 內容                                                                     | 工時 | 平行依賴 |
|------|--------------------------------------------------------------------------|------|----------|
| A1   | `src/swing/__init__.py` + module skeleton + `tests/test_swing_*.py` setup | 0.5h | 無       |
| A2   | `src/swing/features/weekly_resample.py` + `ma_signals.py`(週 K resample / 20wMA / 20DMA 上穿)| 2.5h | 無 |
| A3   | `src/swing/features/volume_signal.py`(週爆量 + OBV + 量價分類 categorical:量增價漲 / 量縮回踩 / 量價背離 / 其他)| 2h | A2 |
| A4   | `src/swing/features/pattern.py`(W 底 / M 頭型態識別 — peak/trough 偵測 + 比較)| 3-4h | A2 |
| A5   | `src/swing/features/trendline.py`(swing point + linear regression 求上升趨勢線)| 4-5h | A2 |
| A6   | `src/swing/features/universe.py`(市值 ≥ 20 億 + ADV ≥ 1000 張 過濾)| 1.5h | 需先確認 `stocks.market_cap` 欄位是否存在,缺則加 migration |
| A7   | `src/swing/features/fundamentals.py`(ROE 近 4 季均 ≥ 12% + 毛利率 ≥ 25% + 月營收 YoY > 0% 近 3 月至少 2 月)| 2h | financials + dividend + revenue backfill 完成 |
| A8   | `tests/test_swing/test_*.py` unit tests 覆蓋率 ≥ 80% | 3h | A2-A7 |
| **A 小計** | | **15h** | |

**A 完成驗收**:
- `pytest tests/test_swing/` 全綠
- `from src.swing.features.ma_signals import is_above_20wma; is_above_20wma("2330")` 回 True/False(實打 2330 5 年資料)
- `from src.swing.features.pattern import detect_w_bottom; detect_w_bottom("2330", lookback_weeks=24)` 回 dict

---

## 4. Phase B — Strategy + Backtest(主公人力 ~10h)

**前置依賴**:Phase A 完成。

| 步驟 | 內容                                                                       | 工時 |
|------|----------------------------------------------------------------------------|------|
| B1   | `src/swing/strategy.py`:`screen_swing(date, params, fundamental_toggle=False) -> DataFrame` — 5 個技術 AND(toggle 開啟 → 加 3 個基本面 AND)| 2.5h |
| B2   | `src/swing/backtest.py`:**週級別 backtest** + 4 種停損(跌破 20wMA / 跌破趨勢線 / M 頭 / 連 3 週新低)+ trailing stop(跌破上週低)| 4.5h |
| B3a  | Backtest sweep:12 種參數組合(MA 期數 ×3 / 停損鬆嚴 ×4 ×2 toggle)× 5 年 weekly | 2h   |
| B3b  | 寫 `docs/swing_backtest_results.md`(AUC / WR / EV / MaxDD per 組合)| 1.5h |
| **B 小計** | | **10.5h** |

**B 完成驗收(GATE)**:
- AUC ≥ 0.55 **且** 扣交易成本 EV ≥ 2%(對齊 `[[project-bias-convergence-rescue-2026-05-18]]` rescue 流程)
- 若不過 → **不上 Phase C**,改備案(本檔 7.2)

---

## 5. Phase C — UI(主公人力 ~7h,**B 通過 GATE 才做**)

| 步驟 | 內容                                                              | 工時 |
|------|-------------------------------------------------------------------|------|
| C1   | `app.py` PAGES 加 `"🎯 波段"` tab(在 `"💎 長線"` 之後)+ `_page_swing()` 骨架 | 1h   |
| C2   | `src/swing/ui_cards.py`:獨立卡片(顯示 5 條件 + 乖離率 + 預估虧損 + 🎯 圖示)| 3h   |
| C3   | `_page_dashboard` 加第 4 區塊 「🎯 今日波段推薦 Top 3」(在「短線推薦」與「關注」之間)| 1h |
| C4   | 「執行掃描」按鈕 + Streamlit cache(5 min TTL)+ 串 `screen_swing()` + fundamental_toggle UI(`st.checkbox`)| 1.5h |
| C5   | `tests/test_app/test_swing_page.py` AppTest smoke(2-3 個)| 0.5h |
| **C 小計** | | **7h** |

---

## 6. Phase D — Paper Trading + 觀察期 + ML(主公人力 ~14h)

| 步驟 | 內容                                                                     | 工時 |
|------|--------------------------------------------------------------------------|------|
| D1   | `src/database.py` 加 `swing_trades` 表 schema + migration                | 1h   |
| D2   | `src/swing/paper_trading.py`:add / evaluate(週級別 5 停損 + trailing)/ snapshot dump-load | 5h |
| D3   | UI 「加入波段追蹤」按鈕 + 「已追蹤波段」頁(顯示持有週數 + 5 停損訊號狀態 + 移動停利距離)| 3h |
| D4   | `swing_trades.csv` snapshot dump/load(對齊既有 paper_trades 模式)| 2h |
| D5   | `src/swing/notifier_swing.py` + `.github/workflows/swing-weekly.yml`(每週五 15:30 台北觸發掃描 + 推播)— **獨立 cron,不混 daily-notify** | 2h |
| D6   | 觀察期 4-8 週累積 ≥ 30 筆 → 主公 review → 上 elite default + ML gate(類似 `[[project-notification-elite-mode-2026-05-19]]`)| 1h |
| **D 小計** | | **14h** |

---

## 7. 整合工時 + 風險

### 7.1 總工時(主公人力,不含背景機器時間)

| Phase                          | 工時      | 累計 |
|--------------------------------|-----------|------|
| **0c-B(本 task)** code/docs   | 2-3h(寫 spec + runbook + 微改 script/workflow)| 2-3h |
| 0c-B 主公手動觸發 backfill     | ~4h(分 6 次點按 + 偶爾看 + 重跑失敗)| 6-7h |
| Phase A features               | 15h       | 21-22h |
| Phase B strategy + backtest    | 10.5h     | 31-33h |
| **GATE B 不通過 → 停止 / 走備案**| ±0       | — |
| Phase C UI(若 B 通過)        | 7h        | 38-40h |
| Phase D paper trading + notifier| 14h      | 52-54h |
| **總計(全套 + B 通過)**       |           | **52-54h** |

加上機器時間:
- daily_prices 5y 8-shard backfill: ~30-90 min × 1 run
- institutional 5y: ~120 min(workflow timeout 上限)
- financials/dividend/revenue 5y: 各 ~30-60 min(8-shard 平行)
- company_profiles full universe warm-up: ~30 min(無 LLM)

### 7.2 GATE B 不通過的備案

`[[project-bias-convergence-rescue-2026-05-18]]` 教訓:WR 33.7% → 92.6% 經 threshold 校準。本 phase 走同 pattern:

**備案 1(輕)**:重調 fundamental toggle 預設 + 停損鬆緊度 + 持有期上限,再跑 B3 sweep
**備案 2(中)**:把 A 層 features(weekly_resample / OBV / pattern)合進既有 `ml_features.py`,讓 v4 ML retrain 多吃 3 個維度 → 不開 swing strategy,只當 ml features spillover
**備案 3(放棄)**:整包砍,A 層 features 留著當 spillover,Phase C/D code 不寫

### 7.3 風險訊號(主公盯著)

| 風險                            | 偵測                                | 應對 |
|---------------------------------|-------------------------------------|------|
| FinMind quota 6/1 重置前撞牆     | backfill 跑到一半 exit 非 0          | 等 6/1 09:00,分批跑 |
| Gemini quota 5/20 重置前撞牆     | company_profiles LLM 模式 fail-fast | 5y backfill 跑 warm-up 模式即可,跳過 LLM |
| GH Actions 月度 2000 min 燒完    | workflow 跑不起來                    | 分週跑,優先 daily_prices |
| AUC < 0.55 / EV < 2%             | B3 結果                              | 走備案 1-3 |
| Paper trading 4-8 週 < 30 樣本   | 訊號太嚴                            | 放寬 1-2 條件,重跑 B3 → 再進 D |

---

## 8. 跟既有功能的隔離

主公拍板「不要跟其他共用」:

| 既有功能              | swing 是否動?                            | 原因 |
|-----------------------|------------------------------------------|------|
| `src/indicators.py`   | **不動**(只可 import,不可加新 helper)| 既有 17 套策略依賴,動了 regression 風險高 |
| `src/strategies.py`   | **不動**                                 | 同上 |
| `src/screener_short.py` / `screener_long.py` | **不動**          | 既有 short/long 邏輯隔離 |
| `paper_trades` 表     | **不動** — 新表 `swing_trades`           | 既有短線 paper trading 用 |
| `daily-notify.yml` cron| **不動** — 新 workflow `swing-weekly.yml`| 既有 daily 推播 elite default 已穩 |
| `morning-brief.yml`   | **不動**                                 | swing 走 weekly 推播,不混 morning |
| ML pipeline           | Phase D 才考慮整合,觀察期 ≥ 30 樣本後 | 對齊 `[[project-notification-elite-mode-2026-05-19]]` |

例外(可動):
- `src/database.py` — 加 `swing_trades` 表 schema + 可選加 `stocks.market_cap` 欄位(若 A6 需要)
- `app.py` — 加 PAGES tab + dashboard 第 4 區塊(C1 / C3)

---

## 9. 「不做沒意義的事」誠實檢核

memory `feedback_no_pointless_work.md` 紅線:

**有意義(做)**:
1. 整合主 spec 取代兩份各自獨立 spec → 避免後續開發踩兩個方向
2. 0c-B 確認 5y backfill 可跑 → audit 結果 0 檔過 1260 天門檻,不 backfill **整個 swing 就跑不起來**
3. Phase A 寫 5 個技術 features → ecstatic-pike + clever-panini audit 都確認沒一個既有 strategy / helper 真的覆蓋 「20wMA + W底 + 趨勢線 + 週爆量 + OBV」 組合
4. Phase B GATE → 對齊 bias_convergence rescue 流程,有 evidence 才上

**不要做(本 phase 排除)**:
1. 不重啟既有 backfill 自動 cron(主公已暫停,守 `[[project-backfill-quota-alert-pivot-2026-05-19]]`)
2. 不動既有 17 套策略 / paper_trades / daily-notify(隔離原則)
3. 不寫 `backfill_daily_metrics.py`(swing 5y 不需要 5 年 PE/PB/yield,只需要 today 一筆當 UI 顯示用)
4. 不為 swing 重做 ML pipeline(D 觀察期樣本 ≥ 30 再評估)
5. 不做 Phase D ML 直接訓練 model(觀察期沒過就訓 = `[[feedback-no-pointless-work]]` 教訓)

---

## 10. 參考文件 + 引用

| Reference                                                                | 用途                                          |
|--------------------------------------------------------------------------|-----------------------------------------------|
| `docs/swing_trading_proposal.md`(ecstatic-pike branch,**ARCHIVED**)| 缺口 audit / UI 方案 X / Phase 工作量草案     |
| `docs/lin_enru_strategy_proposal.md` v3(clever-panini branch,**ARCHIVED**)| 真實林恩如方法落地 spec + 隔離原則 |
| `docs/lin_enru_methodology_research.md`(clever-panini branch,保留)| 真實林恩如方法 4 來源研究 |
| `docs/lin_enru_data_audit.md`(clever-panini branch,保留)| 5y backfill 必要性的 audit 根據 |
| `docs/swing_backfill_runbook.md`(本 phase 新增)| 主公觸發 SOP |
| memory `project_bias_convergence_rescue_2026_05_18.md`                    | B GATE 流程 reference |
| memory `project_notification_elite_mode_2026_05_19.md`                    | D phase elite default 推播 |
| memory `project_backfill_quota_alert_pivot_2026_05_19.md`                 | 0c-B 不重啟 cron 的根據 |
| memory `feedback_e2e_test_isolation_for_persistence.md`                   | Phase D paper trading test 隔離原則 |
| memory `feedback_watchlist_persistence_invariants.md`                     | Phase D snapshot dump/load pattern reference |

---

## 11. 主公拍板的後續決策(Phase A 開工前)

本 spec 開頭三項決策已拍(Q1.C / Q2.B / Q3.D)。Phase A 開工前還需主公拍:

1. **Phase 0c-B 觸發時間** — 等 FinMind quota 6/1 09:00 重置後 / 還是先用既有 quota 跑?
2. **fundamental toggle 預設** — 開 / 關?亮建議 **預設關**(純技術派林恩如版),UI checkbox 主公點開才啟用
3. **`stocks.market_cap` 欄位** — 是否同 phase 0c-B 加上?亮建議 **加**(A6 universe filter 需要),用 FinMind `TaiwanStockShareholding` × close 計算
4. **swing-weekly.yml cron 時段** — 每週五 15:30 台北(週收盤後)?還是週六早上?

亮對上述 4 項預設答(若主公不另指):
1. 等 6/1 重置(13 天後)— FinMind 配額幾乎全給 daily_prices 5y 用
2. fundamental toggle 預設關(林恩如純技術派)
3. 加(Phase 0c-B 開 PR 時順便加 migration)
4. 週五 15:30 台北(週收盤 15:00 + 30 min sanity check)

---

**主 spec 結束**。Phase 0c-B 本 task scope 之內的 deliverables 見第 2.2 節;主公 PR merge 後依 `docs/swing_backfill_runbook.md` 觸發即可。

**後續 Phase A/B/C/D 由獨立 task 接力**(亮會在 PR 描述列出 hand-off 清單,主公拍板 Phase A 開工才動)。
