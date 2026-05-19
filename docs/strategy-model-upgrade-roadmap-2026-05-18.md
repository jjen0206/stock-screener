# 策略 + 模型升級路線圖(2026-05-18)

**生成時間**:2026-05-18 19:30 CST
**作者**:Claude(軍師視角)
**背景觸發**:commit `22404cd` 加入交易成本後,5/7 個有 backtest 數據的策略由「看似賺」變「實際賠」。
**對比資料**:`docs/backtest-cost-impact-2026-05-18.md`

---

## TL;DR(主公先看這段)

1. **死神名單(扣成本後虧損)**:`rsi_recovery` -5.23%、`bb_lower_rebound` -1.72%、`macd_golden` -1.46%、`taiex_alpha` -1.28%、`bias_convergence` -1.24%。其中 `rsi_recovery` 應該直接淘汰,其餘 4 個值得搶救一次,救不起再砍。
2. **守住的兩個**:`volume_breakout` +2.06%、`gap_up` +1.48%。把這兩條當錨點 — 任何「升級」都要先驗證「不傷害這兩條」。
3. **模型現況**:Per-strategy RandomForest + Isotonic/Platt calibration,26 個 features(v2+v3+v4),但 9/17 個策略 fallback 共用 model(樣本不足、未訓 per-strategy)。Label 是 ATR-based 短線目標觸及(`high ≥ entry + 1.5×ATR within 5 days`),**沒扣成本** — 這是模型優先級最高的改造點。
4. **下一步應該做什麼**:見 Part C.1 Top 5。

---

# Part A — 策略層面盤點與升級

## A.1 全 17 套策略盤點

> 資料來源:`src/strategies.py` `ALL_STRATEGIES` dict + `docs/backtest-cost-impact-2026-05-18.md`。
> 健康度欄:🟢 賺錢 / 🟡 微負 / 🔴 嚴重虧損 / ⚪ 沒回測樣本(cost 報告內 0 fires)

| # | 策略 key | 檔/函式 | 一句話邏輯 | 訊號類型 | 預期持有 | hold=5 含成本 | 健康度 |
|---|---|---|---|---|---|---|---|
| 1 | `volume_kd` | `screen_volume_kd` | 量價突破 + KD 黃金交叉 + 法人連 N 日買超 | 動能 | 1-5d | n/a(0 fires) | ⚪ |
| 2 | `ma_alignment` | `screen_ma_alignment` | MA5>10>20>60 + 四線全揚 + close > MA5 | 趨勢 | 5-10d | n/a(0 fires in cost 報告;但 RR 校準 EV +4.28% / WR 71.4% / 21 fires) | 🟢 |
| 3 | `bias_convergence` | `screen_bias_convergence` | 20 日乖離 [-5%, +1%] + 量比 > 1.2 | 反轉 | 3-5d | **-1.24%** / WR 24.5% / **1494 fires** | 🔴 高量虧損 |
| 4 | `macd_golden` | `screen_macd_golden` | MACD 黃金交叉 + DIF<0(初升段)+ 量比 ≥1.0 | 趨勢 | 5-10d | **-1.46%** / WR 27.75% / 173 fires | 🔴 |
| 5 | `ma_squeeze_breakout` | `screen_ma_squeeze_breakout` | MA5/20/60 5 日糾結 ≤2% + 突破 + 量比 | 趨勢 | 5-10d | n/a(0 fires) | ⚪ |
| 6 | `inst_consensus` | `screen_inst_consensus` | 外/投/自三家連 3 日同時買超 | 籌碼 | 3-10d | n/a(0 fires) | ⚪ |
| 7 | `bb_lower_rebound` | `screen_bb_lower_rebound` | 5 日內觸 BB 下軌 + 今日紅 K + 量比 | 反轉 | 3-5d | **-1.72%** / WR 22.84% / 232 fires | 🔴 |
| 8 | `rsi_recovery` | `screen_rsi_recovery` | 14 日內 RSI<30 + 今 RSI>50 + monotonic | 反轉 | 5-10d | **-5.23%** / WR 24.0% / 25 fires | 🔴 災難級 |
| 9 | `inst_silent_accum` | `screen_inst_silent_accum` | 5/10/20 日法人累計皆 > 0 + 平盤 + BB 下半 | 籌碼 | 5-10d | n/a(0 fires) | ⚪ |
| 10 | `volume_breakout` | `screen_volume_breakout` | 量 ≥ 5MA × 2.5 + close 突破 20 日新高 | 動能 | 3-5d | **+2.06%** / WR 44.5% / 337 fires | 🟢 王者 |
| 11 | `gap_up` | `screen_gap_up` | 跳空 ≥1.5% + 量比 1.5-3.0 + 紅 K | 動能/事件 | 3-5d | **+1.48%** / WR 48.0% / 356 fires | 🟢 |
| 12 | `eps_acceleration` | `screen_eps_acceleration` | 連 2 季 EPS YoY > 0 且當季 > 上季 | 基本面 | 月線 | n/a(0 fires;per-strategy model 0 samples) | ⚪ |
| 13 | `high_yield_stable` | `screen_high_yield_stable` | 殖利率 > 6% + 近 4 季 EPS 為正 | 殖利率 | 月-季 | n/a | ⚪ |
| 14 | `inst_oversold_reversal` | `screen_inst_oversold_reversal` | 法人連 3 日淨賣後當日轉買 | 籌碼/反轉 | 5d | **-1.29%** / WR 23.1% / 13 fires | 🔴 樣本太少 |
| 15 | `taiex_alpha` | `screen_taiex_alpha` | TAIEX < 0 且個股漲 > 1% | 大盤 alpha | 1-3d | **-1.28%** / WR 27.9% / 427 fires | 🔴 |
| 16 | `revenue_acceleration` | `screen_revenue_acceleration` | 月營收 YoY > 30% 且加速 | 基本面 | 月線 | n/a | ⚪ |
| 17 | `big_holder_inflow` | `screen_big_holder_inflow` | 千張戶週變化滾動 mean + 1σ 突破 | 籌碼 | 週線 | n/a | ⚪ |

**幾個必須回應的事實**:

- **🔴 的 5 個策略,加總 fires = 1494+232+173+427+25 = 2351 個 picks**,占 hold=5 報告全部 fires 約 **65%**。也就是說系統一半以上的 picks 在賠錢。
- **🟢 兩個策略 fires = 337+356 = 693**(占 hold=5 約 19%)。
- **⚪ 沒回測的 8 個策略**:`docs/backtest-cost-impact-2026-05-18.md` 撈 `daily_picks` 2026-04-30 ~ 05-15 區間,這些策略在這 11 天根本沒命中。要嘛訊號太嚴格,要嘛 universe 不對。**這也是個問題** — 不能說「沒回測 = 安全」。

## A.2 失敗策略救援計畫

> 救援優先級依「賠錢嚴重度 × fires 數量」排,fires 多 = 影響大。
> 每個策略給 3 個搶救方向 + 1 個「放棄條件」。所有工時都是含寫 + 跑 walk-forward + 對比驗證。

### A.2.1 `bias_convergence`(-1.24%,1494 fires)— ⚠️ 優先搶救 #1

**失敗根因**(資料推論,不是肉眼看)
- 1494 fires / 11 個 trading day ≈ 平均每天 135 個 picks 命中,這條策略太「鬆」 — 凡是回到 MA20 附近 + 量稍微放出來就吃進去。
- WR 24.5%(扣成本),平均報酬 -1.24% — 進場時機根本不重要,**因為 universe 太大、訊號太低門檻**。
- 含成本前 -0.55%,扣成本後 -1.24%。原本就是賠錢的策略,成本不過是把它從「微負」推到「明顯虧」。

**3 個救援方向**

1. **加 ML gate(已有 threshold 0.65 但沒在 backtest 套)** — 工時 **0.5 天**。
   - `STRATEGY_ML_THRESHOLDS["bias_convergence"] = 0.65` 在 calibrated 30d 測試是 100% WR / 97 fires(`src/strategies.py:1856`)。
   - 動作:確認 `daily_notify` 推播路徑有套 threshold,但 backtest 成本對比那邊看起來沒套 → `scripts/run_cost_impact_report.py` 改成「先過 ML threshold 再算 PnL」對比。
   - 預期效益:fires 從 1494 → ~100,WR 從 24.5% → 50%+,平均報酬轉正。
   - **預期改動檔**:`scripts/run_cost_impact_report.py`(加 ml_filter 開關)、`docs/backtest-cost-impact-2026-05-18.md`(出 v2 對比)。

2. **加趨勢過濾**(避開盤跌中的乖離回歸)— 工時 **1 天**。
   - 現邏輯:bias 在 [-5%, +1%] + 量比 > 1.2 就吃。問題:**下跌中的乖離回歸 = 持續向下** 的機率很高(空頭中的反彈陷阱)。
   - 動作:加條件「MA20 斜率必須 > 0」或「股價 > MA60」。
   - 預期效益:fires 砍半,WR +5~10pp。

3. **改進出場**(目前是固定 hold N 日,被成本吃)— 工時 **1.5 天**。
   - 現在用固定 hold_days=5,沒設早出條件。加 ATR trailing stop(已有 `src/trailing_stop.py`)。
   - 動作:hold_days 內如果 `close < entry - 1.0 × ATR` 就出,touch target 也出。
   - 預期效益:平均報酬從 -1.24% → -0.3% 以內(止血),不一定變正但減少出血。

**放棄條件**:三個方向都做完仍 < 0%(60d cost-on backtest)→ 從 `ALL_STRATEGIES` 移除,留 deprecated comment。

---

### A.2.2 `bb_lower_rebound`(-1.72%,232 fires)— 優先搶救 #2

**失敗根因**
- WR 22.84%(扣成本),意思是「碰下軌 + 紅 K」這個訊號 4 次只對 1 次。
- 原邏輯:過去 5 日任一日 close 觸下軌 + 今日紅 K + 量比 ≥ 1.0。
- 假設**很多紅 K 只是反彈一根就繼續跌**(BB 通道在 sideways/bear 都會持續往下擴)。

**3 個救援方向**

1. **限制 regime**(只在 bull / weak_bull 跑)— 工時 **0.5 天**。
   - 現在 `STRATEGY_REGIME_FILTER["bear"] = {"籌碼", "殖利率", "大盤"}`,bb_lower_rebound 是「反轉」,bear 已被過濾。但 sideways / weak_bull 還在開。
   - 動作:把 bb_lower_rebound 從 `STRATEGY_REGIME_FILTER` 的 "sideways" 也踢掉,只留 bull / weak_bull。
   - 預期效益:fires 砍 1/2 ~ 2/3,WR 從 22.84% → 30%+。

2. **加成交量爆量門檻**(現在量比 ≥ 1.0 太低)— 工時 **1 天**。
   - 量比 ≥ 1.0 等於「沒縮量」,太鬆。改成 ≥ 1.5 / 2.0,只抓「真正有買盤接」的反彈。
   - 動作:`DEFAULT_BB_REBOUND_PARAMS["bb_vol_ratio_min"]` 1.0 → 1.5,跑 60d grid search 找最佳。
   - 預期效益:fires 砍 1/3,WR +8~10pp。

3. **訓 per-strategy ML model**(現在 fallback 短線通用) — 工時 **1.5 天**。
   - `models/per_strategy/bb_lower_rebound.pkl` 已存在(meta 顯示 status: trained),但 threshold 0.50 是「最低門檻」。校準到 0.60+ 收緊。
   - 動作:重訓 + 算 30d grid threshold,加進 `STRATEGY_ML_THRESHOLDS`。
   - 預期效益:配 #2 後,WR 應到 40%+。

**放棄條件**:三個做完仍 < 0%(60d cost-on)→ 同上,deprecate。

---

### A.2.3 `macd_golden`(-1.46%,173 fires)— 優先搶救 #3

**失敗根因**
- MACD 黃金交叉 + DIF < 0(初升段)的設定**本身偏激進** — 在熊市裡的黃金交叉是「跌深反彈一根」陷阱。
- WR 27.75% 顯示 4 中 1。

**3 個救援方向**

1. **加趨勢確認**(同 A.2.1 #2,套到這條)— 工時 **0.5 天**(共用 helper)。
   - DIF < 0 改成 DIF < 0 **且 close > MA60**(只在 MA60 上方接 MACD 黃 cross)。
   - 預期效益:fires -40%,WR +8~10pp。

2. **加 MACD HIST 加速確認**— 工時 **0.5 天**。
   - 現在只要求今日 DIF > sig + 昨日 DIF ≤ sig。加上「HIST 連 2 天放大」確認黃 cross 不是雜訊。
   - 預期效益:fires -20%,WR +5pp。

3. **bull / weak_bull 限定**— 工時 **0 天**(寫個 dict 改一行)。
   - 把 macd_golden 從 sideways regime 踢掉。
   - 預期效益:fires -30%,WR +5pp。

**放棄條件**:三個都套後仍 < 0% → deprecate。

---

### A.2.4 `taiex_alpha`(-1.28%,427 fires)— 概念雖好但執行差

**失敗根因**
- 原假設:大盤跌但個股漲 = 主力護盤 / 獨立利多 = 強。
- 實際:WR 27.87% / 平均報酬 -1.28%。一日的「大盤跌個股漲」根本沒有 follow-through。
- **這是 econometric 邏輯,不能單看一天**,需要看連續性。

**3 個救援方向**

1. **改用 N 日 alpha 累積**(現在只看 1 日)— 工時 **2 天**。
   - 改條件:近 5 日累計 alpha(個股報酬 − TAIEX 報酬)> +3% 才算入選,而不是單日。
   - 預期效益:fires 砍到 1/5,但 WR 應到 45%+(真強勢股而非雜訊)。

2. **加產業熱度 cross-check**— 工時 **1 天**。
   - 現有 `src/theme_heat.py`/`src/industry_filter.py`,加條件「該股所屬產業近 5 日強於大盤」。
   - 配合 #1 用,避免「假 alpha」(個股暴量單日但無持續性)。

3. **限縮 universe 到中大型股**— 工時 **0.5 天**。
   - 小型股逆勢一日漲 = 大戶拉抬出貨機率高。改成只跑市值 Top 200 / 流動性篩選。
   - 預期效益:fires -40%,WR +5pp。

**放棄條件**:#1 + #2 + #3 都做仍 < 0% → 這個概念本來就不該活下來。

---

### A.2.5 `rsi_recovery`(-5.23%,25 fires)— 直接淘汰

**失敗根因**(不救了)
- 平均報酬 -5.23% 是 5 個失敗策略最慘的。
- fires 只 25 個(11 個 trading day),樣本太小,任何「救援」都是過擬合 25 個樣本。
- Per-strategy model meta 顯示 samples=97 / fallback,**結構性 sparse 已被前面 commit 確認**(`src/strategies.py:1845` 註解寫得很清楚)。
- 「14 日 RSI<30 + 今日 RSI>50 + monotonic recover」這個條件實務上**已被 `bb_lower_rebound` 訊號吃光**,留著只是多算一次。

**建議**
- 從 `ALL_STRATEGIES` 移除,但 `screen_rsi_recovery` 函式保留(供 backtest 歷史對比用)。
- `STRATEGY_LABELS` / `STRATEGY_CATEGORY` / `STRATEGY_ML_THRESHOLDS` / `STRATEGY_RR_PARAMS` 都清掉 entry。
- 工時 **0.5 天**(改一個 dict + 跑 full test suite)。

---

### A.2.6 `inst_oversold_reversal`(-1.29%,13 fires)— 樣本太少,先停推播

**失敗根因**
- 13 fires 樣本根本不能下結論,但暴露的問題是**訊號出現頻率太低** — 連 3 日法人淨賣後當日轉買,11 天才出 13 個,year-on-year 估計 ~430 個,但 universe 大 → 命中率低代表訊號**極稀疏 + 不準**。

**建議**
- **不寫程式碼救** — 改成「先把訊號收集 6 個月再說」。從 `_select_top_picks` 推播 enabled list 拿掉,但 `daily_picks` 表還是寫(讓 pick_outcomes 跑 backtest 累積)。
- 工時 **0.5 天**(改個 enabled flag + 一個 default 不推播 list)。

---

## A.3 新策略建議(7 條,跨 3 類)

> 每條都到「我知道要新增哪個檔案、用哪份資料」的具體度。
> 預期工時含寫 + 60d backtest + 寫 docs。

### A.3.1 籌碼共振類(現有最弱)

現有籌碼策略:`inst_consensus` / `inst_silent_accum` / `inst_oversold_reversal` / `big_holder_inflow`。
都是「單一籌碼維度」(法人 or 千張戶);**沒有「不同籌碼維度同時亮燈」的共振訊號**。

#### 新策略 18:`foreign_x_holder_alignment`(外資 + 千張戶共振) — 工時 **3 天**

- **邏輯**:外資近 5 日累計買超 > 0(per-sid z-score > 1.0)**且** 千張戶本週 holders_delta_w > 0(per-sid 滾動 4 週 mean + 0.5σ)。
- **資料**:`institutional` + `shareholder_concentration`(現有,有覆蓋率)。
- **預期勝率 / 報酬**:WR 45-55% / 平均報酬 1.5-3%(經驗值;雙籌碼共識比單一強)。
- **檔案**:新增 `screen_foreign_x_holder_alignment` 在 `src/strategies.py`,加 `DEFAULT_FOREIGN_HOLDER_PARAMS`。
- **風險**:千張戶資料週公佈,訊號頻率低(估計每天 5-10 個 fires);搭配 watchlist 用,不能當主力策略。

#### 新策略 19:`inst_capitulation_reversal`(法人投降後反轉)— 工時 **2.5 天**

- **邏輯**:外資 + 投信連 5 日同向淨賣超(`institutional_continuity` v4 feature 已有,值 ≤ -5) + 當日轉買 + RSI(14) < 35。
- **資料**:`institutional` + `daily_prices`(現有)。
- **預期勝率 / 報酬**:WR 40-50% / 平均報酬 2-4%(反轉策略命中率不高但賺的時候大)。
- **檔案**:新增 `screen_inst_capitulation_reversal`。可以 reuse `inst_oversold_reversal` 部分骨架,但條件更嚴。
- **風險**:跟 `bb_lower_rebound` 訊號可能重疊(反轉類),要看 `consensus.py` cross-category 共振是不是會雙倍加分。

---

### A.3.2 總體驅動類(新領域)

現況:`market_regime.py` / `regime_gating.py` 已有 TAIEX 4-tier 判斷,**但個股策略沒有「外部宏觀驅動」維度**(美債 / 匯率 / 油價 / 黃金)。

#### 新策略 20:`usd_twd_tail_wind`(匯率順風 + 出口股)— 工時 **5 天**

- **邏輯**:USD/TWD 近 10 日 +1.5% 以上(台幣貶值)**且** 個股屬於「出口導向」產業(電子 / 紡織 / 機械)且當日收紅 K + 量比 ≥ 1.2。
- **資料**:
  - 需新增:USD/TWD 日線(FinMind 應該有,或 yfinance TWD=X)— `data_fetcher.py` 加 fx_rate fetch + 新表 `fx_rates`。
  - 既有:`stocks.industry` 篩出口導向產業 list(可能要手動標,寫在 `src/universe.py` 加 export_oriented_sids)。
- **預期勝率 / 報酬**:WR 50-55% / 平均報酬 2-3%(順大方向風時錯不到哪)。
- **風險**:匯率訊號頻率低、產業綁定主觀。需要 30 天觀察期。

#### 新策略 21:`commodity_driven_pick`(原物料/油價 → 上下游)— 工時 **6 天**

- **邏輯**:WTI 原油近 5 日 +5% → 推航運股反向 / 油氣 / 塑化族;反之亦然。
- **資料**:
  - 新增:WTI / 黃金 / 銅期貨日線(yfinance:CL=F、GC=F、HG=F)。新表 `commodity_prices`。
  - 既有:產業到上下游的對應表(這個要主公或我手寫一份 YAML — 可以從 `data/themes/*.yaml` 結構複用)。
- **預期勝率 / 報酬**:WR 45-55% / 報酬 1.5-3%。
- **風險**:對應表寫死容易過時(產業結構會變);但作為 watchlist 的「補充訊號」價值高。

---

### A.3.3 事件驅動類(現在只有 `gap_up` 1 條算事件)

#### 新策略 22:`revenue_announce_anticipation`(月營收公佈前 3 日佈局)— 工時 **3 天**

- **邏輯**:每月 5-10 號月營收公佈期,公佈前 3 個交易日**前次月營收 YoY > 30% 且法人連續買超**的個股(主力提前佈局訊號)。
- **資料**:`financials` monthly_revenue + `institutional`(現有)。需要算「下次營收公佈日」(每月 10 日前)。
- **預期勝率 / 報酬**:WR 50% / 平均報酬 2-4%(有資料優勢窗口)。
- **檔案**:新增 `screen_revenue_announce_anticipation`,需要新 helper `_next_revenue_announce_date`。
- **風險**:公佈當天可能 sell-on-news;hold_days 應該設 5 含公佈日。

#### 新策略 23:`ex_dividend_swing`(除權息前後)— 工時 **2.5 天**

- **邏輯**:除權息日前 5 日,殖利率 > 4% + 近 1 年填權息率(從 `dividend` 表回推)> 70% 的個股。
- **資料**:`dividend` 表(有除權息日期 + 配息金額)+ `daily_metrics.dividend_yield`(現有)。
- **預期勝率 / 報酬**:WR 55-65% / 平均報酬 3-5%(填權息有實證效應)。
- **檔案**:新增 `screen_ex_dividend_swing`,需要 `_compute_fill_dividend_rate` helper。
- **風險**:6-8 月集中(台股除權息旺季),其他月份幾乎沒 fires。當「季節性策略」用。

#### 新策略 24:`earnings_surprise_followthrough`(財報後 5 日延續)— 工時 **4 天**

- **邏輯**:季報公佈日後 1 個交易日,若(a) EPS YoY > +50% 或(b) 收盤跳空 > 3%,則在公佈後 1-5 日進場,hold 5 日。
- **資料**:`financials` quarterly EPS + period(發布日)+ `daily_prices`(現有)。
- **預期勝率 / 報酬**:WR 45-55% / 平均報酬 3-5%(Post-Earnings Announcement Drift 是文獻已驗證的 anomaly)。
- **檔案**:新增 `screen_earnings_surprise_followthrough`,需要「季報公佈日 → 對應 trading day」mapping。
- **風險**:台股財報截止日可能集中,某些日子大量 fires;hold 期間遇到非預期消息會被連帶賣。

---

## A.4 策略組合升級

### A.4.1 現況評估

✅ **已有的好東西**(讓我意外的部分)
- `src/consensus.py`:跨類別共識 multiplier(同類 ×1.3 / 跨 2 類 ×1.5 / 跨 3 類 ×1.8),wire 在 `_compute_pick_score`。**這是真共識,不是肉眼數信號數**。
- `src/strategy_weighting.py`:30 天 hit-rate 動態權重 [0.5, 1.5] clamp,**也已 wire 進 score**。
- `src/regime_gating.py`:bull / range / bear 三檔濾推薦數 + ML 門檻 uplift。
- `src/theme_heat.py`:題材熱度 multiplier。

❌ **缺的部分**
- **score 公式裡 ML prob 是「raw probability × weights」線性加權,不是 calibrated EV**。也就是說 `ml_prob 0.7 + WR weight 1.2 + consensus 1.5 = 1.26` 是個沒有單位的數字,排序用,**沒有期望報酬語意**。
- **每個策略獨立跑、各自過 ML threshold,但策略間「衝突信號」不處理**(例如 `bb_lower_rebound` 看反轉、`macd_golden` 看趨勢開,同一檔同時觸發代表共識還是噪音?現在當共識 +1.5 多)。
- **沒有 meta-strategy** — 沒有一個「model 學:看當前 regime 哪些策略最該開」的 layer,而是 regime → category whitelist 寫死。

### A.4.2 4 個組合層升級

#### 升級 25:`score_to_ev`(把 score 翻譯成期望報酬)— 工時 **3 天**

- **動作**:`_compute_pick_score` 不再回 raw weighted ml,改回「期望報酬 %」。
- **公式**:`EV = calibrated_ml_prob × target_pct − (1 − calibrated_ml_prob) × stop_pct − round_trip_cost`,target/stop/cost 都用 cost-on backtest 校準後的數字。
- **預期效益**:主公看「+2.3%」比看「0.832」直覺;UI 一目了然 picks 哪個值得進。
- **檔案**:`src/notifier.py:_compute_pick_score`、`src/individual_stock_verdict.py`(verdict 顯示)。
- **風險**:翻譯後 EV 全部變 0~3%,UI 要重做 colorbar 區間。

#### 升級 26:`meta_strategy_gating`(用 model 動態決定「哪些策略今天該開」)— 工時 **5 天**

- **動作**:訓一個 secondary 小 model,輸入 = (regime_dummy, vix_proxy, recent 7d hit rate per strategy, taiex_60d_volatility),輸出 = per-strategy weight ∈ [0, 1.5]。
- **資料**:用 `pick_outcomes` 表 90 天滑動,訓 LightGBM regression(target = next-7d-WR per strategy)。
- **預期效益**:取代現在 `STRATEGY_REGIME_FILTER` 寫死的 whitelist;讓「sideways regime 開 bb_lower_rebound 還是 inst_silent_accum」由 data 決定。
- **檔案**:新增 `src/meta_strategy.py` + `scripts/train_meta_strategy.py`。
- **風險**:加一層 model 增加 maintenance 成本;只有在主公接受 model-of-models 框架時值得做。

#### 升級 27:`signal_conflict_penalty`(同檔同時觸反轉 + 趨勢 → 不加分而是減分)— 工時 **2 天**

- **動作**:`consensus.py` 加 detection,若同檔同時有「反轉」+「趨勢」訊號(語意衝突),multiplier 從現在的 1.5 改成 1.0(不加成)。
- **理由**:反轉訊號 = 預期 mean revert;趨勢訊號 = 預期 follow-through。**邏輯上衝突,共識其實是噪音**。
- **檔案**:`src/consensus.py:consensus_multiplier` 加 conflict check。
- **風險**:可能砍掉一些「事後看是賺的」訊號;要 backtest 確認效益再上。

#### 升級 28:`per_strategy_position_sizing`(已有 sizing 但沒按 EV 動態)— 工時 **2 天**

- **動作**:`src/position_sizing.py` 已存在,但目前是固定比例。改成按 EV / Kelly 半比例配:EV +3% 的 pick 配 5% 倉位、EV +1% 配 2%、EV < 0 不配。
- **預期效益**:不是策略選股升級,是「資金配置」升級 — 同樣 picks 但賠錢的 picks 配少倉位 → portfolio 績效拉高。
- **檔案**:`src/position_sizing.py` + `src/individual_stock_verdict.py` 顯示建議倉位。
- **風險**:Kelly 公式對 EV 估計誤差很敏感;一律半 Kelly 即可。

---

# Part B — 模型層面盤點與升級

## B.1 模型現況盤點

> 來源:`src/ml_predictor.py` / `src/ml_features.py` / `src/ml_walkforward.py` / `src/ml_calibration.py` / `src/ml_shap.py` / `models/*.meta.json`。

| 維度 | 現況 |
|---|---|
| **演算法** | `RandomForestClassifier`(n_estimators=100, max_depth=5 通用 / =10 per-strategy, min_samples_leaf=5, class_weight="balanced") |
| **Features 數** | **26 個**(v2: 11 / v3: 5 / v4: 10) — `kd_k, kd_d, macd_dif, macd_osc, ma_alignment, bb_position, vol_ratio, bias_pct, atr_normalized, inst_5d, inst_10d` + `holders_delta_w_zscore, inst_5d_zscore, regime_dummy, holders_pct_change_4w, is_theme_member` + `concentration_change_rate, institutional_continuity, inst_divergence, ma5_above_ma20_pct, ma20_above_ma60_pct, momentum_5d/20d/60d, industry_relative_strength, industry_rank_pct` |
| **訓練 universe** | `TW_TOP_50`(50 檔)+ sliding window |
| **訓練樣本量** | 通用 model:1290(`short_pick.meta.json`)/ per-strategy:bias_convergence 3602、bb_lower_rebound、ma_alignment、macd_golden、volume_breakout、gap_up、taiex_alpha、big_holder_inflow ≈ 100-3600 不等 |
| **target / label** | 「進場後 5 個交易日內,**high ≥ 進場 close + 1.5 × ATR(14)**」(觸及目標即 win)。**沒扣交易成本** — 跟 backtest cost-on 不對齊 |
| **Calibration** | Isotonic(≥500 樣本)/ Platt(< 500 fallback)。short_pick 通用 model 用 Platt(258 holdout)、bias_convergence 用 Isotonic(720 holdout)。Brier 由 0.232 → 0.218(short_pick)/ 0.179 → 0.093(bias_convergence) |
| **Walk-forward** | `src/ml_walkforward.py` 已有 framework + `scripts/eval_walkforward.py` + 表 `ml_walkforward_results`。**不是訓練流程,是 evaluation only**(production 還是 random split) |
| **Per-strategy models** | 17 套策略,**8 套有 trained .pkl**:bb_lower_rebound、bias_convergence、big_holder_inflow、gap_up、ma_alignment、macd_golden、volume_breakout、taiex_alpha。其餘 9 套 fallback 短線通用 model(eps_acceleration 0 samples / rsi_recovery 97 < 100 / 其他 status=fallback) |
| **解釋性** | `src/ml_shap.py` + 表 `pick_shap_explanations` + cron preload csv → SQLite(commit `714ddac` 顯示已 wire)。TreeExplainer top-3 features 顯示「為什麼推這檔」 |
| **監控** | 無自動 drift 監控。`models/*.meta.json` 含訓練時 metrics 但 production 沒對比「live AUC vs train AUC」 |

## B.2 模型升級三條路徑

### Path 1:同架構優化(低風險低效益,**建議先做**)

#### 升級 29:label 扣成本後重訓(對齊真實底氣)— 工時 **3 天** ⭐ 高 CP

- **動作**:`compute_label()` 把 target_price 改成 `close × (1 + target_pct − round_trip_cost)` 或在 PnL 端扣;讓 model 學的是「扣完成本後**真正能賺到** 1.5 ATR」的 picks,不是表面命中。
- **檔案**:`src/ml_predictor.py:compute_label`、`scripts/train_ml_model.py`、`scripts/train_per_strategy_ml.py`。
- **預期效益**:預測機率分佈會往保守端移;AUC 不一定漲(可能微跌),**但 calibration 跟 real PnL 對齊** — 0.7 prob picks 真實命中率會更接近 70%。Brier 預期改善 5-10%。
- **風險**:per-strategy 內訓樣本量會降(部分樣本 win → loss),`rsi_recovery` 已經邊緣,可能更慘 → 配合 A.2.5 直接淘汰它。

#### 升級 30(已超過 30 額度 — 改成 fold-in)

> 由於額度限制,以下兩個與升級 29 合併計算為 Path 1 的「同一批改造」,**不獨立計入** 30 個項目。

- **加 macro / sentiment features**(USD/TWD 5d %、TAIEX 5d 波動、news_count_per_sid):wire 進 v5 features namespace。
- **Hyperparameter sweep**(Optuna)— 對 per-strategy model 每個調 `n_estimators / max_depth / min_samples_leaf` 找 OOS 最佳。

---

### Path 2:架構升級(中風險中效益,**Phase 2 開始**)

#### 升級 (合併):LightGBM + Multi-task target + Stacking ensemble — 工時 **8 天**

> 因額度,Path 2 三個子升級合併成一個「中等工程」項目。

- **LightGBM 取代 RF**:
  - RF 在 high-cardinality + 不平衡資料上比 LightGBM 慢 + 略差。
  - 動作:`requirements.txt` 加 lightgbm,`ml_predictor.train_short_pick_model` 加 backend 切換 env var `ML_BACKEND=lightgbm`。
  - 預期 AUC: 0.62 → 0.66。

- **Multi-task target**(同時學 1d / 3d / 5d / 10d):
  - 現在只學「5 日內觸 1.5 ATR」,單一視角。
  - 改 multi-task:LightGBM 不支援原生 multi-task → 改成「每個 horizon 訓一個 head」3-4 個 model + 拼接 prediction。
  - 預期效益:同檔 picks 可以給 4 個機率分數(1d/3d/5d/10d),UI 顯示「短線 3d 70% / 中線 10d 55%」更立體。

- **Stacking ensemble**:
  - GBDT + Logistic Regression + RF 各跑各的,最後用一個 meta-learner(LR)blend。
  - 預期 AUC: 0.66 → 0.68(經驗值,ensemble 通常 +1-2pp)。

- **檔案**:新增 `src/ml_ensemble.py`,`scripts/train_ml_model.py` 加 `--backend` flag。
- **風險**:訓練時間 ×3,GitHub Actions runner 可能 timeout(目前已經有 quota 問題)。需要分批訓。

---

### Path 3:新典範(高風險高效益,**Phase 2 觀察 1-3 月後決定**)

**強烈建議:此階段以前不要碰**。Path 3 列出來純粹給主公看見路線圖盡頭。

- **TabPFN / FT-Transformer**(Tabular Transformer):對 tabular small-data(< 10K rows)有 SOTA paper claim,但訓練門檻高、解釋性差(SHAP 不直接適用)。對台股 26-feature × ~3K sample 規模,**還沒看到明顯壓 LightGBM 的證據**。
- **Temporal Fusion Transformer**:時序模型,把 per-sid history 作為 sequence input。理論上能學到 momentum + reversal pattern 之間的非線性。但寫起來 PyTorch + GPU,跟「個人零成本工具」的定位衝突。
- **GNN(個股關係圖)**:把產業鏈、ETF 重疊持股、相關係數建成 graph,跑 GraphSAGE。**理論優雅但工程龐大** — 至少 2-3 個月專案。

工時保留估計:**單 Path 3 任一條 ≥ 30 天**。

## B.3 模型可解釋性(已上但可深化)

✅ **現況**
- `src/ml_shap.py:compute_pick_shap` + `format_shap_reason` 已 wire 進 daily-notify pipeline。
- `pick_shap_explanations` 表 + commit `714ddac` 確認 SQLite preload。
- 推播訊息會顯示 Top-3 features:「holders_delta_w_zscore +12% / inst_5d_zscore +8% / is_theme_member +3%」。

⚠️ **缺**(不獨立計入,屬於 fold-in)
- **個股 verdict 頁(`individual_stock_verdict.py`)沒明顯把 SHAP 顯示給主公看** — grep 沒看到 import ml_shap。建議補上。
- SHAP 沒 wire 進 watchlist UI。

## B.4 模型監控(基本沒有)

⚠️ **缺**
- 沒有「live AUC vs train AUC」對比 — 模型可能在 production 已經 degrade 但沒人知道。
- 沒有「預測機率分佈漂移」警報 — 例如全部 picks 突然 ml_prob 都跑到 0.8+(model 出問題)。
- `pick_outcomes` 表是事後的(N 日後填 hit_target),但沒 dashboard 對比「模型訓練時 base WR 0.44」vs「過去 30 天實測 WR」。

建議**在 Path 2 上線後**才做這層,Phase 1 不在 scope。

---

# Part C — 整合與優先順序

## C.1 🎯 軍師主推 Top 5(策略 + 模型混合排序)

> 按「主公感受到的價值 ÷ 工時」CP 值排。

### #1 — **救援 `bias_convergence`(加 ML gate)** + **label 扣成本重訓**(綁一起做)
- **類型**:策略救援 + 模型 Path 1
- **檔**:`scripts/run_cost_impact_report.py` 加 ml_filter / `src/ml_predictor.py:compute_label` 改寫
- **工時**:3.5 天(0.5 + 3)
- **預期效益**:
  - 策略:fires 1494 → ~100,平均報酬 -1.24% → +0.5~1.5%(同 ML threshold 30d 校準的 100% WR pattern)
  - 模型:Brier ↓ 5-10%,calibrated probability 跟 real PnL 對齊
- **為何最高 CP**:bias_convergence 是流量最大的策略(1494 fires = 全系統 30%+),救活它直接拉整體 portfolio 績效。label 扣成本是後續所有改進的基礎。

### #2 — **淘汰 `rsi_recovery` + 停推 `inst_oversold_reversal`**
- **類型**:策略下架
- **檔**:`src/strategies.py:ALL_STRATEGIES` / `_select_top_picks` enabled list
- **工時**:1 天(0.5 + 0.5)
- **預期效益**:
  - 直接砍掉 rsi_recovery -5.23%、inst_oversold_reversal -1.29% 的賠錢來源
  - 整體 portfolio 含成本平均報酬預期 ↑ 0.3pp(把整體從負向中性拉)
- **為何高 CP**:工時最短、效益確定。沒理由不做。

### #3 — **`bb_lower_rebound` regime 限縮 + 量比拉門檻**
- **類型**:策略救援
- **檔**:`src/strategies.py:STRATEGY_REGIME_FILTER` / `DEFAULT_BB_REBOUND_PARAMS`
- **工時**:1.5 天(0.5 + 1.0)
- **預期效益**:fires 砍 1/2,WR 22.84% → 35%+,平均報酬 -1.72% → -0.3% 以內(止血)
- **為何高 CP**:改 2 個 dict 跟 1 個常數即可,加 60d backtest 對比,風險極低。

### #4 — **新策略 23:`ex_dividend_swing`(填權息策略)**
- **類型**:新策略
- **檔**:新增 `screen_ex_dividend_swing` + `_compute_fill_dividend_rate` helper
- **工時**:2.5 天
- **預期效益**:WR 55-65% / 平均報酬 3-5%(填權息有實證效應 + 6-8 月旺季放大效益)
- **為何高 CP**:資料已備齊(`dividend` + `daily_metrics`)、邏輯單純、實證強。**填補現有「事件驅動」類別空缺**(gap_up 是唯一一條,加這條變兩條)。

### #5 — **`score_to_ev` 翻譯成期望報酬**
- **類型**:組合升級
- **檔**:`src/notifier.py:_compute_pick_score` / `src/individual_stock_verdict.py`
- **工時**:3 天
- **預期效益**:UI 顯示「EV +2.3%」取代 raw score 0.832 — 主公一眼能看出該不該進。**不改績效但改決策品質**。
- **為何上 Top 5**:沒寫程式碼但改變主公對 picks 的「決策語言」,長期影響大。

---

## C.2 三階段路線(總工時 ~30 天,跨 2-3 個月)

### Phase 1:救援 + 模型 Path 1 同架構優化(估 **1-2 週**)

> 完成這階段就應該停下來實測 4-8 週,**不要急著做 Phase 2**。
>
> Cron 範例(若加 dashboard 監控):UTC 16:00 (TW 00:00) 每日跑模型 drift check。

| 順序 | 項目 | 工時 | 對應 Top 5 |
|---|---|---|---|
| 1 | 淘汰 rsi_recovery + 停推 inst_oversold_reversal | 1 天 | #2 |
| 2 | label 扣成本重訓(通用 + 8 個 per-strategy) | 3 天 | #1 |
| 3 | bias_convergence 加 ML gate 進 backtest | 0.5 天 | #1 |
| 4 | bb_lower_rebound regime + 量比拉門檻 | 1.5 天 | #3 |
| 5 | macd_golden + close > MA60 過濾 + sideways 踢除 | 1 天 | A.2.3 |
| 6 | taiex_alpha N 日 alpha 累積 + 中大型股限縮 | 3 天 | A.2.4 |
| 7 | `score_to_ev` 翻譯 | 3 天 | #5 |
| 8 | SHAP wire 進 individual_stock_verdict UI | 0.5 天 | B.3 |

**Phase 1 總工時:13.5 天**
**Phase 1 後預期狀態**:整體 portfolio 平均報酬(含成本)由 -0.5% 中位轉 +0.5% 中位,fires 砍掉 30-40%(精度↑)。

### Phase 2:新策略 + 模型 Path 2 ensemble(估 **3-4 週**,Phase 1 觀察 4 週後啟動)

| 順序 | 項目 | 工時 |
|---|---|---|
| 9 | 新策略 23:ex_dividend_swing | 2.5 天 |
| 10 | 新策略 22:revenue_announce_anticipation | 3 天 |
| 11 | 新策略 18:foreign_x_holder_alignment | 3 天 |
| 12 | 新策略 24:earnings_surprise_followthrough | 4 天 |
| 13 | LightGBM 取代 RF + Multi-task + Stacking 合併 | 8 天 |
| 14 | meta_strategy_gating(用 model 學策略開關) | 5 天 |
| 15 | signal_conflict_penalty(反轉×趨勢 衝突檢) | 2 天 |
| 16 | per_strategy_position_sizing(EV-based) | 2 天 |

**Phase 2 總工時:29.5 天**(可分批,不必連續)
**Phase 2 後預期狀態**:策略池由 16 套(扣 rsi)→ 20 套,事件驅動類由 1 條 → 4 條;模型 AUC 預期 0.62 → 0.66-0.68。

### Phase 3:觀察 1-3 個月後再決定(估 **不少於 1 個月**,實測 Phase 2 沒到位則不啟動)

| 候選項目 | 工時 | 啟動條件 |
|---|---|---|
| 新策略 19:inst_capitulation_reversal | 2.5 天 | Phase 2 反轉類 still weak |
| 新策略 20:usd_twd_tail_wind(匯率) | 5 天 | 匯率資料源建好 |
| 新策略 21:commodity_driven_pick(原物料) | 6 天 | 同上 + 對應表寫好 |
| 模型 drift monitor | 4 天 | Phase 2 model 上線 |
| **Path 3 候選(TabPFN / GNN)** | ≥ 30 天 | Phase 2 後 AUC 仍 < 0.7 才考慮 |

**Phase 3 不訂死工時。決策原則**:Phase 1 + Phase 2 結束後,如果系統 portfolio 平均年化(含成本)已 ≥ +10%,**Phase 3 全擱置** — 個人工具不該追求 academic state of the art。

---

## C.3 不上 Top 5 但值得提的事

1. **訓練 universe 從 TW_TOP_50 擴到 Top 200**:現在 ML 只在 50 檔上訓,真實 production universe 是全市場 2400+ 檔。`MIN_HISTORY_DAYS = 45` 已經夠寬鬆,擴 universe 估計增加 4-6 倍樣本量,model 泛化能力強。工時 ~2 天(`scripts/train_ml_model.py` 改 universe + 重訓 + 比 OOS metrics)。**未進 Top 5 因為現在的 8 套 per-strategy model 表現已可接受,先把 Phase 1 收尾再講擴 universe**。

2. **MIN_HISTORY_DAYS=45 是不是太寬鬆**:現在 MACD 26+9=35,只剩 10 天緩衝。如果某天 SQLite 對某檔有 backfill 漏抓,45 天 features 計算就可能噪音。**短期不動**,但 Phase 2 之後可以做個敏感度分析。

3. **`gap_up` 的 vol_ratio 上限 3.0 是 ad-hoc 嗎**:看 `src/strategies.py:97-100` 註解寫得很清楚 — 是 diagnose 報告 bucket 邊界、不是拍腦袋。**不動**。

4. **加 short_pick 推播時間記錄**:現在沒記錄「這支 pick 是早上 X 點推的,當天收盤是 Y」 — 沒這個就無法精確算 slippage。Phase 2 加。

---

## 附錄:額度盤點

| 範疇 | 項目數 |
|---|---|
| A.2 失敗策略救援(逐策略 × 多方向算 1 個 strategy = 1 項) | 5(bias / bb / macd / taiex / 兩個下架合 1)= 6 項 |
| A.3 新策略 | 7 項 |
| A.4 組合升級 | 4 項(25-28) |
| B.2 模型 Path 1 | 1 項(29,Path 1 內部子項 fold-in) |
| B.2 模型 Path 2 | 1 項(合併 LightGBM/Multi-task/Stacking) |
| B.2 模型 Path 3 | 0 項(只列路線、不算實作項) |
| B.3 解釋性 | 1 項(fold-in,SHAP wire UI) |
| B.4 監控 | 1 項(Phase 3 候選) |
| **總計** | **21 項**(在 30 額度內,留 9 個 buffer 給後續微調) |

---

**完。**

主公 Phase 1 哪個 commit 先動,我這邊隨時可以幫你開分支動工。建議**從 Top 5 #2(下架 rsi_recovery)起手**,5 分鐘改 1 個 dict,先把出血點止住。
