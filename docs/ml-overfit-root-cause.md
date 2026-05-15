# ML Overfit Root Cause — W1 Walk-forward 分析報告

**日期**:2026-05-15  **分析時限**:45 分鐘  **產出**:純分析,未動 src/

W1 walk-forward 暴露多個 v3 模型 OOS 比 random 還差(short_pick 0.4452 / gap_up 0.3592)。本文逐項追根。

## TL;DR

**Root cause 不在 feature leakage,而在 walk-forward 設定本身的兩個結構性 bug**:
1. `n_splits=5` cap 讓 WF 只用了資料集**前 200 rows**;`bias_convergence` 92.6% / `short_pick` 82.5% / `taiex_alpha` 91.1% 的樣本完全沒進評估。
2. WF 按 row index 切而非按 date 切,加上 panel data 同日多 sids 同時排序,test_size=20 rows ≈ 半天 ~ 一天,**ROC AUC 統計雜訊極大**(20 樣本算 ROC 標準差 0.1+ 正常)。

並且 `short_pick` 的 train 期 (2024-03~2024-08) 與 test 期 (2024-08~2025-01) 中間正好跨 2024-08-05 全球股災 → train 全 bull / test 後段 bear-recovery,**regime mismatch 直接讓 split 0 ROC=0.0000**(完全反向預測)。

至於 feature leakage:`holders_*` 在週五當天 anchor 可能輕微 leak、`regime_dummy` 對 2025-10-15 之前的 short_pick 樣本全部 fallback 0.0 變成廢 feature — 都是次要因素。

---

## 1. Feature Leakage Audit

| Feature | 來源 SQL / 算法 | t 時點是否可見 | Leak? | 影響度 |
|---|---|---|---|---|
| `kd_k`, `kd_d` | `_load_history WHERE date <= t` | t close EOD 可知 | ✗ | — |
| `macd_dif`, `macd_osc` | 同上 | 同 | ✗ | — |
| `ma_alignment` | t MA5/MA20 | 同 | ✗ | — |
| `bb_position` | t BB(20, 2σ) | 同 | ✗ | — |
| `vol_ratio` | t volume / MA5(vol) | 同 | ✗ | — |
| `bias_pct` | t close vs MA20 | 同 | ✗ | — |
| `atr_normalized` | t ATR(14) | 同 | ✗ | — |
| `inst_5d`, `inst_10d` | `institutional WHERE date <= t` | 三大法人 t 日約 17:30 公布,t 收盤後可知 → 若 entry 在 t close 就壓線 | △ 邊界 | 低 |
| **`holders_delta_w_zscore`** | `shareholder_concentration WHERE week_end <= t` | TDCC 週六公布 week_end=週五 的資料;若 **t 是 Friday 且 week_end=t**,t 時點未公布 | ✓ 約 1/5 樣本 | **低-中** |
| **`inst_5d_zscore`** | rolling 20 天 5d-sum z-score,resource 同 `inst_5d` | 同 inst | △ 邊界 | 低 |
| **`regime_dummy`** | `compute_regime(t)` 讀 TAIEX `WHERE date <= t` | TAIEX t close EOD 可知 | ✗ | **但 short_pick 2024 樣本全部 fallback=0.0**(cache 2025-10-15 才起算),變成廢 feature |
| `holders_pct_change_4w` | 同 `holders_delta_w_zscore` | 同 | ✓ 同上 | 低 |
| `is_theme_member` | static yaml | 靜態 | ✗ | — |

驗證:`cache.db` 顯示 `TAIEX` 表只有 `2025-10-15 ~ 2026-05-14` (n=138),`shareholder_concentration` 最新 `week_end=2026-05-08(Fri)` 在 `fetched_at=2026-05-13` 才寫進來。後者公布 lag 比預期長,但不影響歷史訓練資料(SQL 統一撈 NOW 內容)。

**結論**:沒有任何 v3 feature 是「未來資訊嚴重 leak」的程度;唯一接近的是 holders 在 Friday anchor 的 1/5 邊界 leak。**leakage 解釋不了 ROC < 0.5 的崩潰**。

---

## 2. Train / Test 樣本分布分析

### 2.1 Walk-forward 資料用量浪費(關鍵發現)

`ml_walkforward.walkforward_train_test` 的 line 168:`actual_splits = min(n_splits, max_possible)`。
搭配 `eval_walkforward.py` 預設 `n_splits=5, test_size=20, min_train=100` → 每個 model **永遠只用前 200 rows**。

| Model | dataset N | max splits 可跑 | 實際只跑 | unused % |
|---|---:|---:|---:|---:|
| ma_alignment | 137 | 1 | 1 | 12% |
| macd_golden | 260 | 8 | 5 | 23% |
| bb_lower_rebound | 298 | 9 | 5 | 33% |
| big_holder_inflow | 388 | 14 | 5 | **49%** |
| gap_up | 467 | 18 | 5 | **57%** |
| volume_breakout | 791 | 34 | 5 | **75%** |
| **short_pick** | 1142 | 52 | 5 | **83%** |
| **taiex_alpha** | 2248 | 107 | 5 | **91%** |
| **bias_convergence** | 2685 | 129 | 5 | **93%** |

→ 對大樣本策略,WF 等於只測了**最早期 200 rows**,完全沒測現代資料(離 production date 還很遠)。

### 2.2 短期 test (20 rows) 統計雜訊

DB 撈出來的 `test_start = test_end` 比比皆是:
- `taiex_alpha` 5 splits **全部 test=[2026-04-17..2026-04-17]** — 5 個 test fold 同一天!
- `bias_convergence` split 0-3 全部 test=[2026-04-15..2026-04-15]
- `gap_up` 5 splits test 跨 2026-04-17 ~ 2026-04-21

原因:strategy fire 集中在少數幾天(panel data 同日多 sids 同時打中),sort by date 後 row 群在同一個 date bin。**test_size=20 rows 多數 = 同一天的 cross-sectional snapshot,根本不是「未來 20 天」**。20 樣本算 ROC AUC 的標準差約 0.1+,「0.45 vs 0.55」根本進不了顯著性閾值。

### 2.3 short_pick 跨 regime change boundary

`short_pick` WF 軌跡:
```
split 0: train=[2024-03-14..2024-08-08] n=100 → test=[2024-08-09..2024-09-05] n=20  ROC=0.0000 ★
split 1: train=[..2024-09-05]            n=120 → test=[..2024-10-08]            ROC=0.5490
split 2: train=[..2024-10-08]            n=140 → test=[..2024-11-07]            ROC=0.6800
split 3: train=[..2024-11-07]            n=160 → test=[..2024-12-05]            ROC=0.6429
split 4: train=[..2024-12-05]            n=180 → test=[..2025-01-03]            ROC=0.3542
```

**split 0 ROC = 0.0** 不是巧合 — train 期間 2024-03 ~ 2024-08-08 完全平靜,test 期 2024-08-09 起跨**2024-08-05 全球股災後修復行情**(TSMC 2024-08-05 close=815 → 2024-08-09 close=934,4 天 +14.6%)。model 訓的「正常 ATR 突破」模式被「劇烈反彈」直接打反。**這是 regime change at split boundary,不是 leakage**。

另注意:short_pick 訓練樣本期 2024-03 ~ 2025-01,**TAIEX cache 只 2025-10-15 起**,所以這段所有 sample 的 `regime_dummy` 全部 fallback=0.0 → 是**廢 feature**,model 無從感知大盤狀態。

---

## 3. Per-strategy 對比 — 為何 bias_convergence/big_holder_inflow 穩,short_pick/gap_up 崩?

| 屬性 | short_pick | gap_up | bias_convergence | big_holder_inflow |
|---|---|---|---|---|
| Label 口徑 | 1.5 × ATR 5 日內觸到 | +5% / -3% / 5 日 | 同 gap_up | 同 gap_up |
| 樣本構造 | TW_TOP_50 sliding,**每檔每天 1 row** | 該 strategy fire 才算 | 同 | 同 |
| Dataset N | 1142 | 467 | 2685 | 388 |
| WF 實際樣本 | 200(82.5% 沒用) | 200(57% 沒用) | 200(93% 沒用) | 200(49% 沒用) |
| WF 時段 | 2024-03 ~ 2025-01(老資料) | 2025-08 ~ 2026-04(近期) | 2025-08 ~ 2026-04 | 2025-07 ~ 2026-04 |
| 跨 regime change? | **Yes**(2024-08 全球股災) | No | No | No |
| WF ROC mean | **0.4452** | **0.3592** | 0.6265 | 0.6377 |
| Train ROC | 0.96 | 0.97 | 0.95 | 0.98 |
| 模型 OOB(per-strategy meta) | n/a(8:2 acc=0.64) | 55.9% | 63.8% | 70.1% |

**觀察**:
1. **`short_pick` 之所以崩,**核心**不是 feature 也不是 leak,是它 dataset 跨 2024-08 股災,但 WF cap=5 splits 全在這段早期,split 0 直接吃到 regime change。** OOB 結果不適用 short_pick(用 8:2 split,不是 OOB),既有 random split ROC 0.7180 是 cross-sectional 混排 → 看不到 regime mismatch。
2. **`gap_up` 之所以崩**:樣本 467 偏少 + label noise (+5%/-3%) + **多 max_depth=10 / n_estimators=200** (train_per_strategy_ml.py:219-228) → overfit 程度更高(meta 顯示 OOB=55.9%,本身就近 random)。WF 在 5 個 splits 跨 4 個交易日,樣本太集中、模型對該段時間的特定模式 memorize → test 在「下一天」失效。
3. **`bias_convergence` 穩** 因為(a) dataset 2685 → 前 200 rows 比較密集 + label 分布平均,(b) 該策略本身穩定(per-strategy OOB=63.8%),(c) test 全部 2026-04-15 一天 = 純 cross-sectional,不算嚴格 WF。
4. **`big_holder_inflow` 穩**:per-strategy OOB 70.1% 本來就高(features 跟 strategy 共鳴),WF 200 rows 還夠看出訊號。

**核心差異是 dataset 時間覆蓋 + regime change boundary,不是 feature 集**(兩者用同一 16 個 v3 features)。

---

## 4. Walk-forward 設定 sanity

| 設定 | 預設 | 問題 | 建議 |
|---|---|---|---|
| `n_splits` | 5 | **硬 cap** 造成大樣本浪費 92% | 改成 `None` / 高值,讓 `max_possible` 主導 |
| `test_size` | 20 rows | panel data → 同日多 sids 全擠進 20-row test | 切換到 **by-date** 而非 by-row;test_size= N 天(例 5 trading days) |
| `min_train_size` | 100 rows | 對 short_pick / taiex_alpha 太小,前 100 row 只覆蓋 1-2 個交易日的 fire | 提高到 500+ rows 或「至少 30 trading days」 |
| split 策略 | row index expanding | panel data 不適合 row-based split | **GroupKFold by date** 或 expanding by trading-day |

未實跑 sanity(時間 + 主公指示不動 code);理論預測:
- 若改 `n_splits=20, test_size=50, min_train=500`,`bias_convergence` WF ROC 應接近 OOB 63.8%(0.60-0.65 區間)而非 0.62 → 印證 WF 設定才是噪音來源
- 若改 by-date split,`taiex_alpha` 不會出現「5 splits test 全在 2026-04-17」這種顯然壞掉的 test fold

---

## 🎖️ 軍師結論

**Root cause(精簡)**:
1. **`ml_walkforward.walkforward_train_test` 的 `n_splits=5` 硬 cap + by-row 切法**,讓 WF 退化成「前 200 rows 五等分」測試,完全沒測現代資料,且 test fold 多半 = 同一天 cross-sectional snapshot;統計 power 低到隨機 fold ROC 可以從 0 跳到 1。
2. **`short_pick` 額外 + 1**:dataset 涵蓋 2024-08 全球股災,WF split 0 train/test 正好跨在這個 regime change 邊界,單一 split 拖垮 mean。

Feature leakage 是次要(holders Friday 邊界、inst t-EOD 邊界)、不致命。

---

## 🎯 修法 prioritized list

### 必修(P0,改完跑一次就知道是否解決)

1. **`ml_walkforward.walkforward_train_test`:把 `actual_splits = min(n_splits, max_possible)` 改為 `actual_splits = max_possible if n_splits is None else min(...)`**,並把 `eval_walkforward.py` 預設 `n_splits=None`。
   - 工程量:**10 行 code** + 1 個 test
   - 預期效果:bias_convergence / taiex_alpha / short_pick WF ROC 統計穩定性大幅提升
2. **`eval_walkforward.py` 預設 `test_size: 50, min_train_size: 300`**(原 20/100 太小)。
   - 工程量:**3 行**
3. **WF 切換成 by-date expanding window**(`feature_cols + date` 分組,test_window = N 個 unique dates,而非 N rows)。
   - 工程量:**30-50 行**(改 `walkforward_train_test` 內部 + tests)
   - 預期效果:消除「test fold 全在同一天」的失效模式

### 可做(P1,改善精度但非急)

4. **檢查 `_load_holders_weeks` 是否該嚴格 `week_end < target_date`**(避開 Friday 邊界 leak)。
   - 工程量:**1 行 SQL 改寫 + 1 個 test case**
5. **`regime_dummy` fallback 不該變成廢 feature**:若 TAIEX 資料對該 target_date 不可得,該整批 sample drop 而非 fallback 0(避免 model 學到「regime=0 → ?」的假相關)。
   - 工程量:**~10 行**(extract_features 加 strict mode)
6. **gap_up overfit 額外應對**:per-strategy `max_depth=10` 對 467 樣本太深 → 改 `max_depth=5` + 增加 `min_samples_leaf=10`。
   - 工程量:**2 行 hyperparam 調整 + 重訓**

### 不必(P2)

7. ❌ 拿掉 v3 新 feature(holders/regime/theme)— 它們不是主因,拿掉反而失去訊號
8. ❌ 整體換掉 RandomForest — train ROC 0.95 是 RF 在小樣本上的正常 memorize,不是模型選擇錯
9. ❌ 重新 fetch TDCC / inst 資料源 — leak 邊界影響太小

---

**下一步建議**:先做 P0 #1 + #2(總工程量 < 15 行),重跑 WF,看 short_pick / gap_up 是否升回 0.55-0.65。若仍崩 → 才動 P0 #3 / P1。**不需要先改 feature 集**。
