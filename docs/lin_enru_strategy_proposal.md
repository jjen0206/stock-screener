# 林恩如選股法 — 功能 Spec 探索報告

> **狀態**:純探索 / 等主公拍板，**尚未實作**。
> **撰寫日期**:2026-05-19
> **目的**:把林恩如「波段存股」哲學翻成可寫 code 的 spec、盤點現有資料 / 策略缺口、給 UI 位置與 paper trading 改造建議、誠實評估「跟現有功能差別 / 該不該做」。
> **跟波段任務的關係**:另有一份 `swing_trading_proposal.md`(20週MA + 量價 + 1-6 月持有)由另一 session 同步寫。兩份**獨立功能、不 merge**，但基本面門檻可共用 feature(本文有對齊註記)。

---

## TL;DR — 軍師建議

**先做「方案 B(lite)」 — 純 backtest 驗證，不上 UI / 不動 paper trading。**

理由:
1. 林恩如哲學的 **「不停損」跟主公現有 paper_trading 的 `stop_price` + trailing stop (`_update_trailing_stop`) 邏輯衝突**，要動 schema(加 `disable_stop` flag)+ 改 `_evaluate_one`。動之前**先驗證林恩如方法在台股 2020-2026 真有 edge**，避免改一輪 paper trading 結果發現 alpha 是負的。
2. 現有 `screen_long`(ROE+PE+殖利率+連續配息)跟林恩如有 ~50% overlap，但**林恩如多了「創 52 週新高」這個 timing 條件**，現在 codebase 完全沒有 52 週新高 feature(`volume_breakout` 只看 20 日)。先補這一個 feature + 寫 `screen_lin_enru` + 跑 backtester 比較,**1.5 天工作量**。
3. 驗證有 alpha → 再做方案 A(full):新 tab + paper trading `disable_stop` 模式 + 完整出場規則。沒 alpha → 把 backtest 結果寫進報告當研究結論,**不上 UI 不加 strategy**(`feedback_no_pointless_work`)。

**不建議首版做**:
- 直接新增 tab(現有 PAGES 已 21 個，主公手機 sidebar 已經擠)
- 改 paper_trading schema 加 `disable_stop`(在驗證 alpha 前不值得)
- 「停利 +30%」當固定參數(林恩如真實做法是「達基本面評估的合理價」，每檔不同 — 簡化成 `target_pct` 固定值會失真，先用 backtester sweep 看 sweet spot 在哪)

---

## Part 1：林恩如方法 → 可執行 spec 轉譯

### 1.1 進場三條件(全 AND)

| # | 林恩如哲學 | 可寫 code 的條件 | 資料源(現有 / 缺) |
|---|---|---|---|
| A | 連續多年 EPS 正 | `financials.yearly` 連續 ≥ 5 年 `eps > 0` | **缺 feature**(現有只看季) |
| B | 高殖利率穩定 | `consecutive_dividend_years ≥ 5` AND `dividend_yield ≥ 5%` | 兩個都**已有**(`screen_long` 內) |
| C | 創 52 週新高 | `close ≥ MAX(close, 252 trading days)` 且 `close ≥ MAX(close, 252) × 0.99` | **缺 feature**(現有只 20 日) |

**亮的初始假設 vs 林恩如書中描述**:
- A 條件:亮給 N=5。林恩如書中常說「5-10 年」，5 年算入門門檻(2330 / 1216 / 2412 都過)。**可調參數** `eps_positive_years` 預設 5。
- B 條件:殖利率 5% 比 `screen_high_yield_stable` 的 6% 寬鬆 — 林恩如的「高殖利率」是 vs 大盤(~3.5%)，不是絕對 6%。**可調參數** `min_dividend_yield` 預設 5.0。
- C 條件:創新高定義有 3 種寫法
  - C1:`close == MAX(close, 252)` 嚴格今日新高
  - C2:`close ≥ MAX(close, 252) × 0.99` 近 1% 內(亮推薦,避免錯過盤中震盪)
  - C3:`close ≥ MAX(high, 252)` 用日 high 不用 close(更嚴)
  - **採 C2** — 跟現有 `screen_volume_breakout` 的「`highest_lookback=20` 突破 + 量比 ≥ 2.5」邏輯一致(只是把窗口從 20 → 252)

### 1.2 出場條件

林恩如真實做法是「達合理價」，但「合理價」每檔不同(K-line 自由心證 / DDM / Graham number)，**code 化要簡化**:

| # | 條件 | spec 建議 | 備註 |
|---|---|---|---|
| 1 | 達停利 | `unrealized_return ≥ target_pct` | 預設 30%，亮建議**走 backtester sweep**(15% / 20% / 30% / 50%)看哪個 EV 最高 |
| 2 | 基本面崩 | `eps_quarterly` 連 **2 季** ≤ 0 | 強制出場，覆蓋「不停損」鐵則 |
| 3 | 配息停發 | `cash_dividend_year` = 0 連 **1 年** | 強制出場(林恩如的「存股前提」失效) |
| 4 | 不停損(刻意) | **無** `stop_pct` | 跟現有 `screen_volume_kd` / paper trading 完全相反 |

亮的 caveat:**條件 2/3 等於「軟停損」** — 林恩如書中其實也允許這種「基本面破壞就跑」，不是宗教式硬抱。文字寫「不停損」但實際操作對「EPS 轉負」一定會跑。spec 要寫進去，不然就是失真版的林恩如。

### 1.3 持有期 / 部位

| 項目 | spec | 備註 |
|---|---|---|
| `hold_days` | **無上限**(`hold_days = None`) | 現有 paper_trading `hold_days int` 必填 — 需 schema 改造，或塞個 9999 hack |
| 單檔倉位 % | 5-10% | 跟現有波段一致;portfolio level 不在 strategy 內處理 |
| 同類股上限 | 30%(產業集中度) | 進階,首版不做 |
| 同時持有檔數 | 5-10 檔輪轉 | 進階,首版不做 |

---

## Part 2：現有資料 / 策略對照盤點

### 2.1 三個進場條件 → 現有資料 / feature 覆蓋

| 林恩如條件 | 現有 feature / table | 缺口 | 補齊難度 |
|---|---|---|---|
| **A. 連續 N 年 EPS 正** | `financials.period_type='quarterly'`(只有季 EPS) | **沒有「年 EPS」聚合 feature**;現有 `screen_eps_acceleration` 只看 **5 季**(1.25 年) | 小:1 SQL 聚合 view 或一個 helper function — 用 `SUM(eps) GROUP BY year` 把季加成年 |
| **B. 殖利率 ≥ 5% + 連續配息 ≥ 5 年** | `daily_metrics.dividend_yield`(TWSE 官方,優先);`dividend.cash_dividend` 年表 | **完全沒缺** — `screen_long._evaluate_long` 已有 `consecutive_dividend_years` 算法,直接 copy | **無** |
| **C. 創 52 週新高** | `daily_prices.close`(252 trading days 滾動 max) | **完全沒這個 feature**;`screen_volume_breakout` 用 `highest_lookback=20`(20 日) | 小:1 SQL `MAX(close) OVER` 或 pandas rolling — 跟 ATR 計算同一個 `bulk_load_prices` 路徑 |

### 2.2 現有策略對照 — 跟林恩如有何差別

| 策略 | 跟林恩如重疊 | 主要差別 | 結論 |
|---|---|---|---|
| `screen_long`(長線) | 高(殖利率 + 連續配息) | 多 ROE + PE 限制;**完全沒 timing 條件**(創新高) | **林恩如可視為 `screen_long` 的「去 PE/ROE + 加 52 週新高」變種** |
| `screen_high_yield_stable`(策略 13) | 中(殖利率 + EPS 為正) | 只看 **4 季** EPS、要求殖利率 ≥ 6%、無 timing | 範圍更窄,不適合直接擴 |
| `screen_eps_acceleration`(策略 12) | 低(EPS 但要加速) | 林恩如**要穩定 ≠ 加速**;條件相反 | 互斥 — 不能複用 |
| `screen_revenue_acceleration`(策略 16) | 低(營收成長) | 林恩如**不必要 ≥ 30% YoY**,要穩定不要爆發 | 互斥 |
| `screen_volume_breakout`(策略 10) | 中(突破新高) | 林恩如不要量比,窗口要 **252 日 ≠ 20 日** | 邏輯類似,需參數化 `highest_lookback` 才能 reuse |

**亮的結論**:林恩如**不是現有策略的改包裝** — 創 52 週新高 + 連續 5 年 EPS 正 + 殖利率穩定的 AND 組合，在現有 17 套裡**沒有任何一套覆蓋**。值得新增。

### 2.3 月營收 / 財報 backfill 現況(主公 2026-05 拍板)

跟林恩如進場條件**直接相關**:
- **dividend backfill** PR #29 改 alert-based(每月 1 號 09:00 推 Telegram + Discord,主公手動觸發),`[[project-backfill-quota-alert-pivot-2026-05-19]]`
- **financials backfill** 同樣 alert-based(每月 1 號),林恩如 A 條件用的 **`yearly` EPS** 也走這條路徑 — backfill 沒跑就沒資料
- **monthly revenue backfill** 主公 2026-05 暫停 FinMind backfill,6/1 恢復 — **林恩如本身不需要月營收**(只看年 EPS),這條 unblock 不影響

**啟動風險**:如果主公的 dividend / financials 沒 backfill 滿 5 年(從 2020-2026)，A + B 條件會 **filter 掉大部分股票**(`fin_count = 0` → 回空 DataFrame，跟 `screen_long._data_availability` 同樣 fallback 邏輯)。**先寫策略前要 sanity check** `SELECT COUNT(*) FROM financials WHERE period_type='yearly' GROUP BY stock_id HAVING COUNT(*) >= 5` 看實際覆蓋率。

---

## Part 3：UI 位置提案

主公拍板「**單獨的功能**」+「不要塞進波段或長線」。三方案對照:

| 方案 | 優點 | 缺點 | 適合度 |
|---|---|---|---|
| **X. 新增 tab `🌱 存股輪轉`** | 真正「單獨」,符合主公拍板 | PAGES 從 21 → 22 個,手機 sidebar 更擠 | ⭐⭐⭐⭐ |
| Y. 長線頁加 sub-tab「📌 林恩如模式」 | 不增 tab 數;同類聚合 | 違反主公「不塞進長線」明文要求 | ⭐ (排除) |
| Z. 純當 strategy 17b,塞進短線 | 重用既有 tabs 顏色分類 | 林恩如是長期持有 ≠ 短線本質;放短線會誤導 | ⭐ (排除) |
| **W. 不上 UI,純 backtester / system_brief 報告**(方案 B lite) | 0 UI 變動成本;先驗證 alpha | 主公看不到日常推薦 | ⭐⭐⭐⭐⭐(首版推薦) |

**亮推薦**:
- **首版做 W**(不上 UI,純 backtester) — 驗證有 alpha 再上 X
- **第二版做 X** — 加新 tab,顏色用綠色系跟「💎 長線」區隔(長線藍 / 存股綠)
- **X tab 內結構**:
  ```
  🌱 存股輪轉(林恩如)
  ├─ 主公須知 caption(不停損 / 達停利才賣 / 持有期可達 1-3 年)
  ├─ 「執行掃描」按鈕(跟 _page_long 同模式 lazy run)
  ├─ 卡片清單(同 `render_picks_cards`,但**不顯示 stop_loss 欄位**)
  ├─ 「加入存股追蹤」按鈕 → 寫 paper_trades 但用 `target_only` 模式
  └─ 已追蹤清單(浮動報酬 + 距離停利 % + 基本面狀態紅綠燈)
  ```

---

## Part 4：「不停損設停利」整合 Paper Trading

### 4.1 現有 paper_trading 規則 vs 林恩如

| 項目 | 現有 `paper_trades` schema | 林恩如需求 | 衝突? |
|---|---|---|---|
| `stop_price` | `REAL NOT NULL` | **不要停損** | **衝突** — schema 強制要 |
| `hold_days` | `INTEGER NOT NULL DEFAULT 5` | **無上限** | **衝突** — 短期固定 vs 長期不定 |
| `target_price` | `REAL NOT NULL`(預設 +5%) | **+30%(可調)** | 可調,不衝突 |
| trailing stop | `_update_trailing_stop` 在 +3% / +5% / +8% 鎖更高停損 | **整套關掉** | **衝突** — 鎖停損 = 變相停損 |
| `_evaluate_one` 結算邏輯 | `low ≤ stop_price` → `lose`;超 `hold_days` → `timeout_*` | 兩條都不適用 | **衝突** |
| 基本面崩出場 | **完全沒有** | EPS 連 2 季 ≤ 0 強制出場 | **缺功能** |

### 4.2 改造方案 — 三選一

**方案 P1(最小改動,推薦首版做)**:
- 加 schema 欄位 `strategy_profile TEXT NOT NULL DEFAULT 'swing'`(`'swing'` / `'lin_enru'`)
- `_evaluate_one` 判斷 `profile == 'lin_enru'`:
  - **不掃 `low ≤ stop_price`**(關停損)
  - **不跑 `_update_trailing_stop`**(關 trailing)
  - **不算 `timeout_*`**(`hold_days` 改塞 9999 或 NULL)
  - **新增掃描:每結算日查 `financials.quarterly` 最近 2 季 EPS;若連 2 季 ≤ 0 → 出場 `status='lose_fundamental'`**
- `add_paper_trade` 開新 kwarg `profile='swing'`
- 工作量:**中**(4-6 小時 + tests)

**方案 P2(獨立表,完全隔離)**:
- 新建 `paper_trades_lin_enru` table(無 `stop_price` / `hold_days` / `trailing_level`)
- 新建 `lin_enru_paper_trading.py` module
- 優點:不污染現有 paper_trading 邏輯;缺點:**performance 頁要改聚合兩表**(統計 by_strategy 跨表)
- 工作量:**大**(2-3 天)

**方案 P3(完全不上 paper trading)**:
- 林恩如**只進 backtester**(`src/backtester.py` 加 `_lin_enru_outcome` 函式),不做 paper tracking
- 優點:0 schema 變更;缺點:主公手動操盤時沒地方追進度
- 工作量:**小**(2-3 小時)

**亮推薦**:**方案 P3 首版** → 證明 alpha 後上 **P1**。先別碰 schema。

### 4.3 backtester 補丁 spec(P3 / 驗證 alpha 用)

`src/backtest.py` 現有 `simulate_outcome` 假設「`hold_days` 內掃 stop/target」。林恩如版要:
```python
def simulate_outcome_lin_enru(
    sid, entry_date, entry_price,
    target_pct=0.30,
    max_hold_trading_days=750,  # 約 3 年,實際 cap 避免無限
    fundamental_exit=True,      # 連 2 季 EPS ≤ 0 → 出場
):
    """掃 entry_date 到 +max_hold 天:
    - high ≥ entry × (1+target_pct) → win
    - 每季財報新公佈日,檢查最近 2 季 eps 是否都 ≤ 0 → lose_fundamental
    - hit max_hold → timeout(剩餘報酬照原樣計)
    完全不跑 stop_price / trailing。
    """
```

跑 backtester sweep:`target_pct ∈ {0.15, 0.20, 0.30, 0.50}`、`eps_positive_years ∈ {3, 5, 7}`、`min_dividend_yield ∈ {4.0, 5.0, 6.0}`,過去 5 年回測,看哪個組合 EV / Sharpe 最高。**先有數據再決定首版 default params**。

---

## Part 5：工作量分塊

| Phase | 任務 | 大小 | 依賴 | 備註 |
|---|---|---|---|---|
| **A1** | `eps_positive_years` feature(年 EPS 連 N 年正) | 小(1h) | `financials.yearly` 須有資料 | SQL `GROUP BY stock_id, year` 加 helper |
| **A2** | `price_at_52w_high` feature(close 達 252 日 max × 0.99) | 小(1h) | `daily_prices` 一年內覆蓋 | pandas rolling 或 SQL window function |
| **A3** | 殖利率穩定度 helper(reuse `_evaluate_long` 內邏輯) | XS(30min) | 無 | refactor 抽出來變 module-level |
| **B1** | `screen_lin_enru` strategy 函式(A+B+C AND) | 中(2-3h) | A1 / A2 / A3 | 跟 `screen_high_yield_stable` 同 pattern |
| **B2** | `simulate_outcome_lin_enru` backtester(無 stop / 季報 check) | 中(3-4h) | B1 + financials.quarterly | 加 financials 公佈日查表邏輯 |
| **B3** | 跑 backtester sweep(target_pct × 篩選參數) | 中(2-3h + 機器時間) | B1 / B2 + 5 年回測資料 | 結果寫 `docs/lin_enru_backtest_results.md` |
| **🛑 GATE** | **主公看 B3 結果決定是否往下** | — | — | **alpha < 0 → 停;alpha 顯著正 → 往下** |
| **C1** | 新 tab `🌱 存股輪轉` + 卡片(無 stop 欄位) | 中(3-4h) | B1 | 仿 `_page_long` 結構;render 隱藏 stop 列 |
| **D1** | paper_trades schema 加 `strategy_profile` 欄位 | 小(1-2h) | 無 | sqlite migration + snapshot dump 同步 |
| **D2** | `_evaluate_one` 加 `profile=='lin_enru'` 分支 | 中(3-4h) | D1 + B2(共用邏輯) | tests:既有 swing tests 一個都不能破 |
| **D3** | 基本面崩出場(查最近 2 季 EPS) | 中(2-3h) | D2 + financials.quarterly | 新狀態 `lose_fundamental` 加進 CHECK constraint |
| **D4** | UI「加入存股追蹤」按鈕 + 已追蹤清單 | 中(3-4h) | C1 + D2 | 重用 `auto_seed_from_picks` 加 `profile` kwarg |

**首版工作量**(Phase A + B,**不上 UI / 不動 paper trading**):**6-10 小時**。
**完整版**(過 GATE 後加 C + D):**再 15-20 小時**(2-3 天人 / 5-7 天碎時間)。

---

## Part 6：「不做沒意義的事」誠實檢核

依主公 `feedback_no_pointless_work` 規矩,自我審視:

### 6.1 林恩如方法 vs 現有策略 — 真的不同嗎?

**有顯著差異**(Part 2.2 已列表),三個關鍵獨特性:
1. **52 週新高 timing**:現有 17 套策略沒一套用,連 `screen_volume_breakout` 都只 20 日。林恩如方法的精髓「**等市場驗證再進**」現在 codebase **完全沒有**這個概念。
2. **連續 5+ 年 EPS 正**:跟 `screen_eps_acceleration` 的 5 季 / `screen_high_yield_stable` 的 4 季都不同 — 是更嚴格的長期穩定篩選。
3. **不停損 + 達停利哲學**:跟現有所有 strategy 對 paper trading 都跑 `stop_pct` + trailing 的部位管理**根本對立**,**值得驗證另一邊是否真有 alpha**。

→ **結論:不是改包裝,有實質差異**。

### 6.2 「不停損」跟現有風控原則衝突嗎?

**衝突,但可以隔離**:
- 現有 `risk_management.py`、`position_sizing.py`、`trailing_stop.py` 三模組都假設「有停損」
- `paper_trades.stop_price NOT NULL` schema 層強制(Part 4.1 已列)
- **隔離方案**:加 `strategy_profile` 欄位 + `profile=='lin_enru'` 分支(Part 4.2 P1)。
- **不要做的事**:不要把林恩如的「不停損」邏輯擴散到既有 swing strategy — 主公會炸。

### 6.3 主公會真的操盤,還是只想看回測?

**這題只有主公能回答,但亮提供決策框架**:

| 場景 | 推薦做法 |
|---|---|
| 主公**真打算用林恩如方法操作幾檔** | 做 Phase A + B + C + D 全套(有 UI + paper tracking) |
| 主公只想**比較林恩如方法 vs 現有策略表現** | 做 Phase A + B(backtester only),寫 `docs/lin_enru_backtest_results.md` 給主公看;不上 UI |
| 主公**只是覺得「林恩如有名,要不要看一下」** | 做 Phase A1 + A2 兩個 features(都是通用基礎建設),不寫 strategy / 不上 UI;林恩如就成為「已具備 features,主公想跑時 1 小時寫個 ad-hoc query 即可」 |

**亮預設假設**:主公 portfolio 應該已經有真實長期持股(像 2412 / 0050),不會專門換成林恩如方法。**最可能是場景 2(比較表現)**,所以**首版做 backtest-only**最匹配。

### 6.4 明確結論 — 做 / 不做 / 改做別的?

**做**,但**先做 lite 版**(Phase A1 + A2 + B1 + B2 + B3):
- 補 2 個通用 features(52 週新高、連年 EPS 正) — 這兩個**就算林恩如不做,以後其他策略也會用到**
- 寫 strategy + backtester 跑數據
- **B3 結果出爐後,主公看數字再決定要不要 C + D**

**不做** Phase C / D 直到 B3 alpha 驗證:
- 上 UI / 改 paper trading 的成本(schema 改、tests 寫、回 regression 風險)只在 alpha 為正時值得付
- 過去 `bias_convergence` 校準 [[project-bias-convergence-rescue-2026-05-18]] 教訓:**先 cost-aware 驗證,再上線**

---

## Part 7：跟「波段交易」task 的對齊

另一份 `swing_trading_proposal.md` 由另一 session 同步寫(主公會看到)。本報告與其關係:

| 元素 | 林恩如(本報告) | 波段交易(另報告) | 對齊建議 |
|---|---|---|---|
| 持有期 | 1-3 年(無上限) | 1-6 月(有上限) | **不 merge**,差一個量級 |
| Timing 訊號 | **創 52 週新高**(market validation) | 20 週 MA + 量價(趨勢確認) | 互斥(同檔股不太可能同時在 52 週新高且剛突破 20 週 MA) |
| 基本面門檻 | 連 5 年 EPS 正 + 殖利率穩定 | 待波段 spec 確認 | **可共用 `eps_positive_years` feature(Phase A1)** — 寫一次兩邊吃 |
| 停損 | **無**(刻意) | 有(波段慣例) | **不要 merge** — 風控哲學相反 |
| Paper trading | `profile='lin_enru'`(P1 方案) | `profile='swing'`(現有預設) | **共用 `strategy_profile` 欄位**(Phase D1)→ 一次改 schema 兩邊用 |
| UI | 新 tab `🌱 存股輪轉` | 看另一報告 | 兩個各自獨立 tab,**不要互引用**(主公 mobile-first 不喜歡跳 tab) |

**結論**:
- **Phase A1**(`eps_positive_years` feature)同時對波段有用 → 兩邊先別重複寫,先 align 命名 / SQL
- **Phase D1**(`strategy_profile` schema 欄位)如果波段也要新模式 → 兩邊一次改完
- 其他元素**完全獨立**

---

## 附錄:採用建議的 spec 雛形(Phase B1 草稿)

```python
# src/strategies.py 新加(同 screen_high_yield_stable 風格)

DEFAULT_LIN_ENRU_PARAMS: dict[str, Any] = {
    "eps_positive_years": 5,        # 連續 N 年 EPS > 0
    "min_dividend_yield": 5.0,      # 殖利率 ≥ 5%
    "consecutive_div_years": 5,     # 連續配息 ≥ 5 年
    "near_52w_high_pct": 0.99,      # close ≥ 252 日 max × 此倍率
    "high_lookback_days": 252,      # 約 1 年(52 週 × 5 交易日)
}


def screen_lin_enru(
    date: str,
    params: dict | None = None,
    stock_ids: list[str] | None = None,
) -> pd.DataFrame:
    """策略 18:林恩如選股法 — 基本面穩定 + 創 52 週新高 + 高殖利率。

    AND 三條件:
      A. 連 N 年(預設 5)年 EPS 都 > 0 — 獲利穩定
      B. 殖利率 ≥ 5% AND 連續配息 ≥ N 年(預設 5)— 高息 + 穩配
      C. close ≥ MAX(close, 252) × 0.99 — 創/近 52 週新高 = 市場驗證

    出場(本函式只負責進場;出場在 simulate_outcome_lin_enru / paper trading):
      - 達停利 +target_pct(預設 30%)
      - 連 2 季 EPS ≤ 0 → 強制出
      - 不停損

    意義:林恩如波段存股派的鐵三角條件 — 基本面 + 高息 + 市場認同。
    """
    # 跟 screen_high_yield_stable 同 pattern:批量 SQL + 三條件 filter
    ...


STRATEGY_LABELS["lin_enru"] = "林恩如選股"
```

---

## 給主公的決策清單

請主公從以下選一:

- [ ] **A. 全部不做** — 林恩如方法沒興趣,當這份報告是研究
- [ ] **B. 只做 Phase A + B(backtest 驗證)** — **亮推薦**;6-10h,出 backtest 結果 → 再決定
- [ ] **C. 做 A + B + C(加 UI 但不動 paper trading)** — 全套 strategy + UI 卡片,但 paper tracking 走 P3(只 backtest);15-20h
- [ ] **D. 做完整 A + B + C + D(含 paper trading `disable_stop`)** — 全套;30-40h(2 個整天)
- [ ] **E. 主公自己另一種思路** — 寫下來

亮等主公拍板再開工。
