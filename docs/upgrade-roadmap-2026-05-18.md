# 系統升級路線圖 — 2026-05-18

> **撰寫日期**:2026-05-18(週一)
> **盤點範圍**:21 個 GitHub Actions workflows、~60 個 src/ module、18 個 Streamlit pages、17 個策略、17 個 per-strategy ML model + 1 通用、136 個 test files。
> **時間單位**:工時用「天」= 主公在電腦前實際工作 6-8 小時(含實作 / 單元測試 / 推 commit / cron 驗證)。
> **cron 時間格式**:`UTC X (TW Y)`,所有時間台北。

---

## 🎯 軍師主推 Top 5

按「主公感受到的價值 ÷ 工時」CP 值排序。明天起床看這份,先做這 5 個。

| # | 項目 | 工時 | 為什麼比其他高 CP |
|---|---|---|---|
| 1 | **Telegram bot serve(雙向問答)** | 1.5 天 | code 早就埋好(`notifier.py:2482` 註解指向 `scripts/telegram_bot_serve.py` 但檔案根本不存在,callback_query / InlineKeyboardMarkup 兩個 sister API 已上線)。主公每天用 Telegram,缺一個 daemon 就少了「在路上隨手問 2330」的能力。投入時數最少、使用頻率最高。 |
| 2 | **推播 / cron heartbeat + fail alert** | 0.8 天 | silent fail 主公中過至少三次(`fix(backfill-dividend): silent fail root-cause` / `救 cancelled cron`/`watchlist push 兩層保護`)。一個 `sync_log_heartbeat` table + 一個 `cron_health_alert.py` daily 看「上一次成功是哪天」,直接根治「不知道掛了」這個盲點。 |
| 3 | **首頁 + 系統結論頁加 verdict banner** | 0.5 天 | `individual_stock_verdict.compute_verdict` 已寫好,只剩 wire 進 `🏠 首頁` 與 `📋 系統結論`。主公開 app 第一秒就看到「今天系統覺得能不能進場 / 倉位狀態 / 昨日命中」,不用點四五層才看到判讀。 |
| 4 | **個人化推播 — watchlist 命中置頂** | 0.5 天 | 目前所有 picks 一視同仁。主公自選股(`watchlist` 表)其實是「最關注且最熟」的籃子,命中應排最前+加 ⭐ 標。一行 sort key 改寫 + format 重排。 |
| 5 | **回測加交易成本 + 滑價** | 0.8 天 | `backtester.py` 註解寫「**簡易版,無交易成本/滑價/資金管理**」。台股實際 0.1425%(手續費,buy+sell 雙邊)+ 0.3%(賣方證交稅)= **每筆來回吃 0.585% 純成本**。短線策略 5 日報酬目標通常 5%,扣完成本剩 4.4%,夏普比直接差一截。主公看的所有歷史績效都是**虛胖版本** — 修正一次,所有歷史決策的「真實底氣」才會校準。 |

下面是完整 25 項按七維度展開。

---

## 1. 資料維度

### 1.1 個股級融資融券訊號 🟡
**現況**:`market_sentiment.py:85` 只抓**全市場總額**近 30 日,個股級沒進 DB(沒有 `margin_short` 表)。融資餘額是「散戶熱度」的代理指標,個股維度的融資減少 + 股價上漲 = 主力洗盤,是經典籌碼訊號。
**升級成什麼**:加 FinMind `TaiwanStockMarginPurchaseShortSale`(免費)→ `daily_fetch.py` 補抓 top-50 + watchlist + picks(~120 檔)→ 新 `margin_short` 表 (sid, date, margin_balance, short_balance, margin_change, short_change) → `ml_features.NEW_FEATURE_NAMES` 加 `margin_change_5d_zscore` / `short_to_margin_ratio` 兩個 feature(v5)。
**主公看得到的價值**:「散戶在出 + 主力在進」這種訊號目前完全沒抓,加進去後 ML 多一個維度,籌碼類策略 ROC 預期 +0.02~0.04。
**工時**:1.5 天(0.5 fetcher + DB + 0.5 wire 進 features + 0.5 重訓 + A/B gate 驗證)
**依賴 / 阻擋**:FinMind 1500 calls/hr 配額;個股版單支 ~3 個月一抓即可,壓力低。
**風險**:FinMind 該 endpoint 某些下櫃股回 404,fetcher 需吞例外不 silent skip(借鑑 `backfill_dividend.py:strict=True` 經驗)。

### 1.2 期貨 / 選擇權 PCR + 外資台指期未平倉 🟡
**現況**:沒抓。沒有 `futures` / `options` 表,大盤情緒只看現股 TAIEX 漲跌 + 法人現股買賣。
**升級成什麼**:`scripts/fetch_taifex.py` 新增,抓 TAIFEX 公開資料(免費 CSV):
  - `OpenPosition_Daily.csv`(三大法人台指期未平倉、台指選擇權 PCR)
  - 兩個欄位加進 `market_regime.py` 判斷 — 外資台指期淨多空翻轉 = 大盤 regime 切換領先 1-2 日;PCR 極端值 = 反轉訊號。
**主公看得到的價值**:「📊 大盤」頁多兩個關鍵卡(外資期貨多空、PCR),regime 切換更早一天。
**工時**:1 天(0.4 抓 + 解析 + 0.3 wire 進 regime + 0.3 UI + test)
**依賴 / 阻擋**:TAIFEX 公開資料 CSV 結構,需要寫 column parser。
**風險**:TAIFEX 假日 / 休市表跟現股有差,日期對齊要小心。

### 1.3 美股 ADR 同步(2330ADR / UMC / AVGO / TSM) 🟡
**現況**:PRD F1.4 標台股 phase 1,TASKS 標美股 P2。`yfinance` 已在依賴內,完全沒用來抓 ADR。台積電 ADR(TSM)收盤跟台股 2330 開盤經常有 +/-2% gap up/down 預示作用,目前完全沒納入。
**升級成什麼**:新 `scripts/fetch_adr_overnight.py` — 每天 TW 早上 7:30 cron 抓 TSM / UMC / ASX / AVGO 4 檔 ADR 隔夜漲跌 → 寫 `adr_overnight` 表 → wire 進 `morning_brief.py` 推播「ADR 隔夜 / 台積夜盤」一行。
**主公看得到的價值**:8:00 收 morning brief 多一行「TSM 隔夜 +1.5% → 2330 可能高開」,進場前先有預期。不需要做完整美股,先做 ADR overnight signal。
**工時**:1 天
**依賴 / 阻擋**:yfinance 對 TSM / UMC 穩定;美股交易日曆(夏令時間切換)。
**風險**:yfinance 假日 / 颱風假對齊;台股缺市但美股有市的日子處理。

### 1.4 monthly_revenue 歷史擴張 22 月 → 36 月 🟢
**現況**:`backfill_revenue.py` 抓 22 個月(`fix(backfill-revenue): quota fail-fast`)。`revenue_acceleration` 策略需要 3 個月 vs 12 個月 YoY 比較,22 個月只夠看 1 個 YoY 週期。
**升級成什麼**:backfill 拉到 36 個月(3 年完整月營收) → revenue_acceleration ML 訓練樣本量翻倍 → `eps_acceleration` 也能加「營收先行 vs EPS 後驗」cross-feature。
**主公看得到的價值**:基本面策略樣本量倍增,ML ROC 比較穩(目前 per-strategy 100-500 樣本偏小)。
**工時**:0.5 天(只是擴大 backfill window + 一次性 GH Actions workflow_dispatch 跑)
**依賴 / 阻擋**:FinMind 配額(已有 fail-fast,所以撞到會自動 abort 不污染)。
**風險**:同 `backfill-revenue.yml` 已踩過的雷,但 fail-fast 已修。

---

## 2. ML / 訊號品質

### 2.5 LightGBM / XGBoost 取代或對拍 RandomForest 🟡
**現況**:`ml_predictor.py` 全用 `RandomForestClassifier`,17 個 per-strategy + 1 通用(`models/short_pick.pkl`)。RF 在 ~1290 樣本下已展現過擬合(`docs/ml-overfit-root-cause.md` 提到 train ROC 0.95 vs test 0.6),且 RF 對 numeric feature 互動學習弱於 GBDT。
**升級成什麼**:新 `models/short_pick_gbm.pkl` 用 LightGBM,A/B 對拍同樣 walk-forward 評分;若 ROC AUC + 0.03 以上則切主軸,RF 保留為 fallback。LightGBM 對小樣本 + 類別不平衡(prec_recall 偏移)友善,且原生支援 categorical feature(`regime_dummy` 可不用 one-hot)。
**主公看得到的價值**:ML 勝率提升 3-5pp,推播命中率有感。
**工時**:2 天(0.5 加依賴 + 訓練 script 平行版 + 1 A/B gate 邏輯改寫支援多 model artifact + 0.5 整合測試)
**依賴 / 阻擋**:`requirements.txt` 加 `lightgbm`(< 5MB,輕);沿用現有 walk-forward gate。
**風險**:小樣本 GBM 也可能 overfit,需要 strict 的 `min_data_in_leaf`;依賴升級可能跟 numpy 衝突(目前 numpy 1.26-3.0)。

### 2.6 跨策略 stacking / meta-model 🟡
**現況**:17 個 per-strategy model 獨立預測 + 通用 `short_pick.pkl` 做最終分數融合,但融合邏輯是固定 multiplier(`STRATEGY_CONSENSUS_ENABLED` 1.05/1.10/1.15)+ ML threshold 過濾,**沒學習層**。
**升級成什麼**:新 `src/ml_stacking.py` — 把 17 個 per-strategy `predict_proba` 當作 17 個 meta-feature,再加 5 個 context feature(regime_dummy / theme_heat / consensus_count / watchlist_member / institutional_5d),訓 LightGBM meta-model 預測 d5 win → 取代或加權現有 `_select_top_picks` 的固定公式。
**主公看得到的價值**:「為什麼這檔被選」從「3 個策略亮燈」進化到「策略 A 0.8 + 策略 B 0.6 + 大盤 bull + 主公 watchlist」這種真正 weighted 判讀,SHAP 可同層解釋。
**工時**:3 天(1 設計 + 1 訓練 + 1 wire + walk-forward 驗證)
**依賴 / 阻擋**:需要先做 #2.5(LightGBM)更划算;需要 `daily_picks` × `pick_outcomes` join 至少 6 個月歷史。
**風險**:meta-model layer 容易 over-fit 在 base model 已 leak 的部分,要嚴格 time-split。

### 2.7 walk-forward gate 加 Brier Score 條件 🟢
**現況**:`models/ab_summary.json` 顯示 KEEP/ROLLBACK 只看 `roc_auc` 比較,但 `short_pick.meta.json` 已記錄 `raw_brier` / `calibrated_brier`(機率校準品質)。ROC 排序能力與 Brier 機率準度是不同維度 — ROC 升但 Brier 變糟代表「排序準但機率灌水」,UI 顯「AI 勝率 70%」會更不準。
**升級成什麼**:`eval_walkforward.py` 同時計算 Brier;A/B gate 改成 `(ROC ≥ old - 0.02) AND (Brier ≤ old + 0.01)` 才 KEEP。
**主公看得到的價值**:「AI 勝率」數字長期校準,主公對機率不會慢慢失去信任。
**工時**:0.4 天(改 metric 計算 + gate 條件 + 1 個 test)
**依賴 / 阻擋**:無。
**風險**:小機率新 model ROC 漲 0.05 但 Brier 漲 0.02 被擋下,需要主公手動 override flag 才上線。

### 2.8 ML training set 全市場 backfill 深度 +6 個月 🟢
**現況**:`per_strategy` 樣本量 100-500(`macd_golden.meta.json: samples=368`),`short_pick` 1290。小樣本是 ROC 不穩主因。
**升級成什麼**:`backfill_pick_shap.py` 反向使用:用歷史 OHLC + institutional 重跑 17 策略 → 取得歷史 picks → 加進 `daily_picks` cold archive → 訓練集擴張。目前 backfill 已有 `--days 126`,可加到 252(1 年)甚至 504。
**主公看得到的價值**:per-strategy model 樣本量 2-3x,walk-forward 結果穩定性大幅提升。
**工時**:1 天(0.3 改 backfill window + 0.7 重訓 + A/B gate)
**依賴 / 阻擋**:institutional / shareholder_concentration 歷史深度(目前 institutional 22 月、TDCC 半年);TDCC 對 1 年前可能拿不到,fetcher 要 graceful skip。
**風險**:歷史資料越久,survivorship bias 越大(已下市股不在 universe);regime 切換點(例 2024 Q3 全球股災)train/test split 要小心。

---

## 3. 策略維度

### 3.9 新策略「突破回踩確認」(breakout_pullback) 🟡
**現況**:`ma_squeeze_breakout` + `volume_breakout` 兩個破繭出量策略,但都是「破繭當日進」— 假突破容易被洗。沒有「突破後 2-3 日量縮回測 + 收紅守支撐」這種 confirmation 策略,經典且勝率高(統計上 ~55-60% WR）。
**升級成什麼**:`src/strategies.py` 新 `screen_breakout_pullback`:條件 = ① N 日前破 20MA + ② 隔日量縮 < 50% + ③ 今日收紅 + 守住 20MA + ④ pattern「黑三鴉之後紅鎚」white plain 化。
**主公看得到的價值**:多一條「假突破過濾後再進」的策略,勝率比 naive breakout 高、信號質量乾淨。
**工時**:1.5 天(0.5 規格 + 0.5 寫 + 0.5 backtest_picks 對拍歷史 WR + ML 訓練 + 加進 STRATEGY_LABELS / CATEGORY / RR_PARAMS)
**依賴 / 阻擋**:無。
**風險**:歷史樣本不足會被 ROC 0.5 卡住;先 rule-based 灰度上線、再加 ML threshold。

### 3.10 配對 / 同產業 head-tail spread 🔴
**現況**:沒做。`industry_filter.py` 已有 canonical industry map,但 industry 內 relative strength(industry_relative_strength feature 已有 v4)還沒衍生成策略。
**升級成什麼**:新 `screen_pair_meanrevert` — 同產業內 5 日 best vs worst 收盤 % 差 > 8% → 看好 worst 反彈、看淡 best 拉回(配對交易單腳版)。
**主公看得到的價值**:多一個非趨勢類訊號,跟現有 17 策略低相關,組合分散度提升。
**工時**:2 天
**依賴 / 阻擋**:industry_relative_strength v4 features 等下次 weekly retrain 才落地;策略本身與 ML 解耦,可先 rule-based。
**風險**:配對策略單腳版本質是「均值回歸 bet」,bear market 下會連續失敗,需要 `STRATEGY_REGIME_FILTER` 排除 bear。

### 3.11 backtest 加交易成本 + 滑價 🟢
**現況**:`src/backtester.py` docstring 寫「**簡易版,無交易成本/滑價/資金管理**」。台股實際單向手續費 0.1425%、賣方證交稅 0.3%,**買賣來回 0.585%**,加上跳空滑價(`gap_up` 開盤價接 +1% 是常態)。所有歷史績效都是虛胖版。
**升級成什麼**:`backtester._simulate_trade` 加 `commission_rate=0.001425` / `tax_rate=0.003` / `slippage_pct=0.005` 三個 kwarg,預設打開;`vbt_backtest.py` 同步;**保留** `--no-cost` flag 對拍歷史。
**主公看得到的價值**:所有「過去一年策略勝率 + 報酬」校準到真實值,主公第一時間看到「實際扣完成本還剩多少」。
**工時**:0.8 天
**依賴 / 阻擋**:`pick_outcomes` 表的 d1/d3/d5/d10 是 raw return,改顯示 net return 要分欄保留兩種。
**風險**:歷史顯示績效會「變差」,主公心理衝擊;先做雙欄(gross / net)漸進過渡。

---

## 4. UI / UX

### 4.12 首頁 + 系統結論加 verdict banner 🟢
**現況**:`individual_stock_verdict.compute_verdict` 已 ship(2026-05-18),目前**只 wire 進個股深度頁 banner + 推播卡片短標**。`🏠 首頁` 與 `📋 系統結論` 兩頁還沒掛 verdict。
**升級成什麼**:首頁第一行 = 「📊 系統今日結論」by aggregating ① 大盤 regime ② 持倉中 verdict 分布(綠/黃/紅 count)③ 昨日命中率 ④ 警示中 sid 數;系統結論頁加「持倉 verdict 全表」帶顏色 chip。
**主公看得到的價值**:打開 app 第一秒就看到「今天能不能 / 該不該進場」,不用點四層才看到。
**工時**:0.5 天
**依賴 / 阻擋**:verdict 已上線,純 UI 整合。
**風險**:無。

### 4.13 個人化推播 — watchlist 命中置頂 🟢
**現況**:`notifier._select_top_picks` 全 universe ranking 後取 top-N,主公自選股 picks 沒特殊處理,排在哪看 ml_prob × consensus × theme heat。
**升級成什麼**:format 階段把 watchlist 命中的 sid 抽出來放最前面 + 標 ⭐;非 watchlist 命中正常排;若 watchlist 命中數量 ≥ 5,加 section header「⭐ 你關注的籃子今天 5 檔出訊號」。
**主公看得到的價值**:看推播第一眼先看到自己最熟的標的,不用滑長串找。
**工時**:0.5 天
**依賴 / 阻擋**:`watchlist` 表已存在。
**風險**:無。

### 4.14 策略歷史命中熱圖(時序視覺化) 🟡
**現況**:`📊 策略歷史` 頁是表格(`tab_strat, tab_date, tab_raw, tab_vbt`),看數字累。
**升級成什麼**:加 `tab_heatmap` — y 軸 17 策略、x 軸近 30 個交易日、cell 顏色 = 當日命中數 × 平均 d5 報酬(綠 → 紅)。一眼看出「哪個策略最近熱、哪個策略走冷」。
**主公看得到的價值**:策略選股權的直覺判斷,知道現在該偏哪一類策略。
**工時**:1 天(plotly heatmap + aggregation query)
**依賴 / 阻擋**:`pick_outcomes` 表已有 d5 資料。
**風險**:策略樣本量小的(eps_acceleration 一週 1-2 次)cell 會很稀,需要灰色 fallback。

### 4.15 手機版底部導覽(BottomNav) 🔴
**現況**:18 pages 全靠 sidebar segmented_control,iOS 加到主畫面後切頁要點側欄抽屜,手感中等。
**升級成什麼**:用 `st.bottom_container` + CSS sticky 固定底部,放 4 個最常用 icon(首頁 / 短線 / 關注 / 系統),mobile-first 體驗近 native App。
**主公看得到的價值**:手機切頁從「拉抽屜選」變「點底部 icon」,日常使用順手 30%。
**工時**:1.5 天(CSS hack + page route hook + 5 個 e2e AppTest)
**依賴 / 阻擋**:Streamlit 1.34+ 已有 bottom_container;但全頁切換 reload 行為要小心 session_state。
**風險**:Streamlit cloud 視窗 width 偵測 不穩(已知);desktop 視窗縮小會誤觸發 mobile mode。

---

## 5. 自動化 / 觀測性

### 5.16 推播 / cron heartbeat + fail alert 🟢
**現況**:21 個 workflows,失敗只依賴 GitHub email 通知;過去 3 次 silent fail(`backfill-revenue cancelled cron`、`backfill-dividend strict=True` 沒加、`watchlist push 兩層保護`)都不是 GHA 顯示綠燈但實際資料沒進的 case。
**升級成什麼**:
  1. 新 `sync_log_heartbeat` table (workflow_name, last_success_at, last_run_at, last_status, message)。
  2. 每個關鍵 cron script(`daily_notify.py` / `news_notify.py` / `weekly_shareholder_fetch.py` / `ml_weekly_retrain` 訓練後 / `daily_market_update.py`)結尾 UPSERT 一筆。
  3. 新 `scripts/cron_health_alert.py` 每天 UTC 02:00 (TW 10:00) 跑,若任一 workflow `last_success_at` > 預期間隔 × 1.5,推 Telegram + Discord「⚠️ daily-notify 上次成功是 2026-05-15,已逾期 72 hr」。
**主公看得到的價值**:silent fail 從「看 commit 才發現」到「主動推播提醒」,zero blind spot。
**工時**:0.8 天
**依賴 / 阻擋**:新增 schema migration 走 `_migrate_*` pattern。
**風險**:cron_health_alert 自己掛了沒人提醒;workaround = 加進 `data-health-alert.yml`,workflow 本身已每天 9:00 跑。

### 5.17 統一 workflow fail webhook(取代 email) 🟡
**現況**:依賴 GitHub email 通知 workflow failure,email 容易漏看。
**升級成什麼**:每個 workflow 最後加 `if: failure()` step 推 Discord webhook「❌ workflow X failed at YYYY-MM-DD HH:MM, see {url}」;統一用 `.github/workflows/_alert.yml` reusable workflow 給其他 21 個 call。
**主公看得到的價值**:workflow 失敗在 Discord 立刻看到,不用翻郵件。
**工時**:0.6 天(寫 reusable workflow + 21 個 call site 改寫 = 模板化)
**依賴 / 阻擋**:`DISCORD_WEBHOOK_FAIL` env var 加進 GitHub secrets。
**風險**:每個 workflow 都加 webhook 會多 21 個 secret reference,管理麻煩;先做 5 個關鍵 cron(daily-notify / news-notify / weekly-* / ml-weekly-retrain)。

### 5.18 daily-notify step 拆分 + 平行化 🟡
**現況**:`daily-notify.yml` 串接 fetch → precompute → backtest → notify → market_update,timeout 120 min。如果 market_update 卡 timeout,通常已過推播時間,但若 fetch 卡了則整鏈斷。step 間沒平行。
**升級成什麼**:拆 `daily-fetch.yml`(只跑 fetch + precompute + backtest + commit)+ `daily-notify-only.yml`(已存在,做 notify)+ `daily-market-update.yml` 變成 trigger by daily-notify 完成。三個 workflow `workflow_run` 串接,可平行的部分平行。
**主公看得到的價值**:daily-notify 從 60-90 min 壓到 ~30 min,推播時間更穩;market_update 卡了不影響推播。
**工時**:1.2 天(workflow_run 觸發鏈、artifact 傳遞、commit 衝突處理)
**依賴 / 阻擋**:GHA workflow_run trigger 在 PR branch 上不會跑,需要 main 上手動測一次。
**風險**:workflow_run chained 的 commit race condition(三個 workflow 都要 push 到 main);需要先做 #5.16 heartbeat 才能放心。

### 5.19 daily_market_update 改增量 + checkpoint resume 🟡
**現況**:全市場 ~15 min,跑到一半 timeout cancel 全部白費。
**升級成什麼**:加 `--resume-from-sid` flag,失敗時 checkpoint 寫 `data/twse_snapshot/.market_update_checkpoint.json`,下次接續跑。
**主公看得到的價值**:cron quota / 網路問題不會讓整批白費。
**工時**:0.7 天
**依賴 / 阻擋**:`daily_market_update.py` 內部 loop 結構改寫。
**風險**:checkpoint 跨多次 run 的「邏輯失效日期」處理(隔週 resume 等於跳了一天)。

---

## 6. 推播 / 整合 / AI assistant

### 6.20 Telegram bot serve(雙向問答 daemon) 🟢
**現況**:`notifier.py:2482` 註解寫「對應 handler 由 scripts/telegram_bot_serve.py 在 Bot API getUpdates / webhook」 — **但 `scripts/telegram_bot_serve.py` 根本不存在**。已有的:`InlineKeyboardMarkup` 按鈕 (`send_telegram_message_with_keyboard`)、`callback_query` answer (`answer_callback_query`)、`/ask` 邏輯雛形(`ai_assistant.py` 已有 `_call_gemini`)。缺的只是 serve loop。
**升級成什麼**:新 `scripts/telegram_bot_serve.py`(~150 行):long-polling getUpdates → 路由 `/ask 2330` / `/picks` / `/verdict 2330` / `/watch 2330` / 數字-only 訊息(直接判讀 sid)→ 呼叫 `ai_assistant.ask_question` / `compute_verdict`。可選跑在自己電腦背景 / GHA `*/10 * * * *` cron(已知 GHA 不適合 long-running)。
**主公看得到的價值**:在 Telegram 隨手敲「2330」就回 verdict + ML 勝率 + 最近 picks,出門路上、開會空檔都能用,真正貼身軍師。
**工時**:1.5 天(0.5 serve loop + 0.5 路由 + 0.3 接 ai_assistant + 0.2 deploy doc)
**依賴 / 阻擋**:Telegram Bot Token 已設定;serve 機器 = 主公本機 systemd / Windows Task Scheduler,或租 0 元 fly.io 小機。
**風險**:long-polling 死循環需要 supervisor;雲端 24/7 跑會耗主公 free tier;個人專案先用本機跑就好。

### 6.21 weekly-brief 改 LLM 撰寫敘事(取代純 stats) 🟡
**現況**:`send_weekly_brief.py` 推 `system_brief` stats(資料覆蓋 / picks 統計 / 真實績效),數字直貼。
**升級成什麼**:把同樣的 stats 餵 Gemini 2.5 Flash Lite,prompt 「以軍師口吻、繁體中文、白話寫一段 400 字本週系統 review,提及 3 件值得注意的事」,LLM 輸出當週報內容;原 stats 收進 `<details>` 折疊。
**主公看得到的價值**:週報從「資料快照」進化到「軍師敘事」,讀完一篇有判斷感而非看完一表還要自己想。
**工時**:0.6 天(prompt 設計 + few-shot example + fallback「LLM 失敗回到 stats 純文字」)
**依賴 / 阻擋**:`google-generativeai` 已在依賴;Gemini 月配額。
**風險**:LLM 偶發胡謅(已知 risk),用 few-shot constrain + 系統指令禁推測。

### 6.22 Discord slash command serve(已 register 但無 serve) 🟡
**現況**:`src/discord_bot.py:get_slash_command_definitions` + `register_commands` 都已寫好,`scripts/discord_bot_register.py` 用來把指令一次性 PUT 到 Discord;**但 serve loop(`discord_bot_serve.py` 只有 83 行,看起來是 scaffold)還沒接 interaction 處理**。
**升級成什麼**:把 `discord_bot_serve.py` 寫完整,用 discord.py / interactions.py 任一接 interaction → 路由到 `compute_verdict` / `daily_picks` query。可選用 Cloudflare Worker 純 HTTP 處理 interaction(免 24/7 daemon)。
**主公看得到的價值**:Discord channel 內 `/verdict 2330` 直接拿判讀;適合多人 channel(以後若分享給朋友群)。
**工時**:1.5 天
**依賴 / 阻擋**:Discord App 已建立,interaction endpoint URL 要設;Cloudflare Worker 0 元 plan 夠用。
**風險**:Discord 要求 interaction 3 秒內回應(slow query 要走 follow-up message);技術門檻比 Telegram 高。

---

## 7. 維運 / 技術債

### 7.23 google-generativeai → google-genai 遷移 🟢
**現況**:`requirements.txt:32` 註明「Google 已將 google-generativeai 標 deprecated,等之後再 migrate google-genai」。同 import 在 `ai_assistant.py` / `company_profile.py` / `analyst_targets_snapshot.py` 至少 3 處使用。
**升級成什麼**:新 SDK API surface 不同(`genai.Client(api_key=)` + `client.models.generate_content`),要包一個 `src/llm_client.py` thin wrapper 隔離,改完所有 caller 走 wrapper。
**主公看得到的價值**:依賴不被 deprecated 鎖住,Gemini 2.5+ 新功能(structured output / function calling)有 path 可用。
**工時**:0.8 天(0.5 寫 wrapper + 0.3 改 3 個 caller + test)
**依賴 / 阻擋**:`google-genai` 加進 requirements;舊 SDK 並存一段時間(灰度切換)。
**風險**:`gemini-2.5-flash-lite` 在新 SDK 的 model name 對齊;old SDK 行為差異(safety category)。

### 7.24 notifier.py 拆 pick_pipeline + format 🟡
**現況**:`notifier.py` 2673 行,做 picks 篩選邏輯 + 文字格式化 + Telegram/Discord 發送 + InlineKeyboard。ARCHITECTURE.md 第 8 節已列為 Round 2 短期目標(`pick_pipeline.py` + `format.py`)。
**升級成什麼**:拆 `src/notifier_pipeline.py`(_select_top_picks orchestration) + `src/notifier_format.py`(format_top_picks_message / Telegram HTML / Discord MD) + 保留 `src/notifier.py` 為 transport layer(send_telegram / send_discord)。
**主公看得到的價值**:之後想加新推播 channel(LINE Bot via 廠商 / Email)只動 transport,pipeline 與 format 不動。
**工時**:1.5 天(拆 + 改 import + 跑 2177 test 確認沒回歸)
**依賴 / 阻擋**:無。
**風險**:`notifier.py` 是熱檔(每天 cron 進入),拆完任何 import 漏改即 silent fail;需要先在 PR 跑完整 e2e。

### 7.25 DuckDB 替代部分 SQLite 重查詢 🔴
**現況**:SQLite WAL + 38 helper 已涵蓋所有 read/write。`daily_prices` 全市場 2 年 ~1.8M rows,backtest 全策略 d1-d10 join 全 picks 表(weekly cron)在 GitHub Actions 跑 ~2-3 min。
**升級成什麼**:read-heavy 全表 scan 改用 DuckDB(`duckdb.connect('data/cache.db', read_only=True)`,SQLite 檔可直接 attach),不改 schema 也不改 write path;只 `backtest_picks.py` / `vbt_backtest.py` / `performance_analysis.py` 三個熱 query 走 DuckDB。
**主公看得到的價值**:weekly backtest 縮短 5-10x,週日 cron 跑完更快,主公看到結果更新時間提前 ~1 hr。
**工時**:2 天(DuckDB pin 依賴 + 三個 caller 改 connection layer + e2e 驗證 numeric 對齊 SQLite)
**依賴 / 阻擋**:`duckdb` 加進 requirements(~50MB wheel,接受);DuckDB 對 SQLite 某些 PRAGMA / type 行為有差異。
**風險**:numeric 邊角(date string parse、NULL 處理)兩個引擎結果不一致;先在非關鍵路徑(performance_analysis.py)pilot 再擴。

---

## 工時總計 + 優先順序建議

| Tier | 項目 | 累計工時 |
|---|---|---|
| **Tier 1 — 兩週內做完** | #1.4 / #2.7 / #3.11 / #4.12 / #4.13 / #5.16 / #6.20 | ~5.0 天 |
| **Tier 2 — 一個月內做完** | #1.1 / #1.2 / #2.5 / #2.8 / #5.17 / #5.18 / #6.21 / #7.23 | ~7.5 天 |
| **Tier 3 — 一季內** | #1.3 / #2.6 / #3.9 / #4.14 / #5.19 / #6.22 / #7.24 | ~9.6 天 |
| **Tier 4 — 看心情** | #3.10 / #4.15 / #7.25 | ~5.5 天 |

🎯 **建議從 Top 5 起跳:#6.20 → #5.16 → #4.12 → #4.13 → #3.11**(總計 ~4.1 天可在週末 + 兩個工作日午後做完)。做完後主公的「資訊雷達 + 真實底氣 + 軍師可隨身」三層補齊,接著再開 Tier 2 / 3 自然分批。

---

## 不在此份報告的項目(明確排除)

| 項目 | 為什麼不列 |
|---|---|
| 分點主力 dage 功能 | `docs/dage-feature-scope.md` 已研判免費路線都死巷、付費違反零成本原則 |
| 美股完整 phase | PRD F1.4 + TASKS P2 已標,目前優先做 ADR 概覽(#1.3)即可 |
| DB schema 大重構(SQLite → Postgres) | 個人專案 + Streamlit Cloud 限制,SQLite WAL 已夠用 |
| OAuth / 會員 / 訂閱 | CLAUDE.md 明確禁止 |
| Docker / k8s / CI 工程 | CLAUDE.md 明確禁止 |
| 加更多 unit test | 已 2177 passed,核心邏輯覆蓋足夠,不再無腦加 |

---

**報告完。**
