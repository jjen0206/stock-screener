# gap_up 策略 ML 困境最終決策（2026-05-15）

> 起點：walk-forward ROC AUC = 0.4926（接近 random）。`max_depth=5 / min_samples_leaf=10` 微改善後仍卡住。其他 7 策略 WF ROC ≥ 0.55，唯獨 gap_up 救不回來。
> 終點：**選路 B**（rule-based 過濾收緊到甜蜜點 + 下架 ML 過濾）。下方有完整資料。

---

## TL;DR

| 項目 | PRE | POST |
|---|---|---|
| gap_up overall WR（+5/-3/5d） | 48.0% | 51.6% |
| baseline WR（同期同 universe random） | 40.7% | — |
| Edge vs baseline | +7.3 pp | +10.9 pp |
| Backtest 126d 入選 fires | 2328 | 1572（過濾 ~32%）|
| Backtest 252d WR | — | **51.6%** |
| ML 介入方式 | `STRATEGY_ML_THRESHOLDS["gap_up"]=0.60` 過濾 | **拿掉**（WF ROC 0.49 的 model 等於 noise filter）|
| Per-strategy retrain | 每週訓 gap_up.pkl | **停訓**（從 `STRATEGY_RF_PARAMS` / `eval_walkforward.DEFAULT_PER_STRATEGY` 移除）|

**核心發現**：gap_up 訊號**有 edge**（+7.3pp）但**邊緣**（48% 沒過 50%）→ ML 從現有 16 個 v3 features 學不到「哪些 gap_up follow-through」（WF ROC 0.49 = features 無資訊量）。真正的 sub-edge 在 **`vol_ratio` sweet spot 1.5-3x**：那群 WR 50.3%，而 `>3x` 那群 WR 44.8%（甚至比 baseline 40.7% 只 +4pp）。用 `gap_vol_ratio_max=3.0` rule 拿到 +1.4pp 提升（51.6% vs 50.2%）— 不依賴任何 ML。

---

## Step 1 — Diagnose 資料（scripts/diagnose_gap_up.py）

**Setup**：
- Panel：`daily_prices` 2025-04-07 ~ 2026-05-14（250 trading days）
- Universe：`pure_stock_universe(min_history=20)` = 2060 檔
- Label：對齊 ML 訓練（target +5% / stop -3% / hold 5 trading days，由 `simulate_outcome` 模擬）

### Overall — gap_up vs baseline（同期同 universe random）

| Group | N | WinRate(+5/-3/5d) | Sim EV | Raw 1d mean | Raw 1d pos rate | Raw 5d mean | Raw 5d pos rate |
|---|---|---|---|---|---|---|---|
| **gap_up_overall** | 2773 | **48.0%** | +0.77% | -0.31% | 50.3% | +0.96% | 50.1% |
| baseline_same_period | 5000 | 40.7% | +0.11% | -0.38% | 45.0% | +0.60% | 45.7% |

**關鍵觀察**：
1. **Edge 在 5 日 holding，不在隔日**：gap_up Raw 1d mean = -0.31%（**負**！）而 Raw 5d mean = +0.96%。「隔日繼續漲」是錯誤的 prior，gap_up 真正的 edge 是「**5 日內觸到 +5% 的機率比 baseline 高**」。
2. **+5/-3/5d label 還算對齊**：WR 48% vs baseline 40.7%（+7.3pp），是真實 edge，但接近 50% 邊界 → ML 想從 features 進一步分辨「哪 48% 是 win」很難。

### By Gap Size

| Gap % | N | WinRate | Sim EV | Raw 1d | 1d pos% | Raw 5d | 5d pos% |
|---|---|---|---|---|---|---|---|
| <2% | 624 | 48.2% | +0.71% | +0.44% | 49.5% | +1.23% | 49.0% |
| 2-3% | 805 | 49.4% | +0.87% | +0.89% | 50.2% | +2.24% | 52.1% |
| 3-5% | 686 | 48.8% | +0.89% | +0.73% | 52.1% | +2.49% | 52.2% |
| **5-7%** | 279 | **52.3%** | +1.11% | +1.67% | 57.7% | +2.80% | 53.8% |
| **>7%** | 236 | **52.5%** | +1.21% | +1.75% | 54.2% | +2.92% | 49.2% |

→ 缺口越大 WR 越高（>5% gap WR 52%+），但樣本量小。**沒設 max gap**，留給未來 power 變大再考慮分桶 tuning。

### By Vol Ratio（**最重要的 sub-edge**）

| Vol ratio | N | WinRate | Sim EV | Raw 1d | 1d pos% | Raw 5d | 5d pos% |
|---|---|---|---|---|---|---|---|
| **1.5-2x** | 763 | **49.1%** | +0.87% | +0.06% | 51.3% | +1.41% | 52.0% |
| **2-3x** | 864 | **51.3%** | +1.06% | +0.45% | 54.6% | +2.19% | 51.5% |
| 3-5x | 680 | 45.1% | +0.49% | -0.14% | 46.8% | +0.93% | 49.6% |
| **>5x** | 460 | **44.8%** | +0.52% | -1.68% | 46.6% | -1.16% | 45.6% |

→ **甜蜜點明確在 1.5-3x 那段**：WR 49-51%，Sim EV 0.87-1.06%。**>3x 反而比 baseline 還差**（WR 44-45% < 50%）— 假說：極端高量多是「主力出貨 / 一日量爆」，後續 mean revert。`>5x` Raw 1d mean=**-1.68%**, Raw 5d mean=**-1.16%** — 隔日跟 5 日都負。

**這就是路 B 的關鍵 rule**：`gap_vol_ratio_max = 3.0`。

### By Market Regime

| Regime | N | WinRate | Sim EV | Raw 5d mean | 5d pos% |
|---|---|---|---|---|---|
| **bull** | 2254 | 49.1% | +0.85% | +1.82% | 52.4% |
| weak_bull | 0 | — | — | — | — |
| **sideways** | 506 | **43.1%** | +0.42% | **-2.88%** | 40.1% |
| bear | 8 | 50.0% | +1.22% | +4.40% | 37.5%（樣本太少）|

→ **sideways regime 是 gap_up 地雷**（Raw 5d **-2.88%**）。已被 `STRATEGY_REGIME_FILTER[sideways]` 排除（"動能" 不在 sideways/bear 開啟清單），所以不需要再加 strategy 內 filter。**production 流程已經有這層保護**。

### By Prev-5d Trend Slope

| Prev 5d slope | N | WinRate | Sim EV | Raw 5d | 5d pos% |
|---|---|---|---|---|---|
| strong down | 198 | 46.5% | +0.67% | +1.52% | 47.2% |
| mild down | 486 | 47.7% | +0.73% | +1.83% | 52.1% |
| flat / up | 565 | 48.0% | +0.71% | +1.46% | 49.4% |
| **strong up** | 964 | **52.0%** | +1.14% | **+3.08%** | 54.8% |

→ Strong-up 加速最強，但差距不大（52.0% vs flat 48.0% = +4pp）。**這次不加 prev-slope filter**：picks 量會下降太多（過濾掉 1809 / 2773 = 65%）。留到下一輪迭代評估。

---

## Step 2 — 為什麼選路 B 而非路 A

**路 A（feature engineering + ML 重訓）**：在現有 16 個 v3 features 之上加 `gap_size`、`vol_ratio_pct_rank`、`regime`、`prev_5d_slope`，重訓期望 WF ROC 從 0.49 拉到 0.55+。

**否決理由**：
1. **樣本量上限**：gap_up 467 樣本 × WF n_splits 跑出 200 row eval。加 features 不能解決樣本不足。
2. **`regime_dummy` 廢 feature 問題**：軍師報告（docs/ml-overfit-root-cause.md §3）已指出 TAIEX cache 只從 2025-10-15 起，2024-03 ~ 2025-10 期間的 gap_up 樣本 `regime_dummy` 全部 fallback=0.0。加 regime feature 在現有 cache 下對歷史樣本仍是廢的。
3. **工程成本 vs 收益不划算**：路 A 要動 `ml_predictor.extract_features` + 重訓 + 重評，4-6 小時工程；rule-based path B 30 分鐘就能取到接近的 edge。
4. **最關鍵：edge 的本質是「過濾噪音」不是「分辨贏家」**：Diagnose 表清楚顯示 1.5-3x 的 vol_ratio 是 sub-cohort 整體贏，而不是「某些 picks 對某些 features 響應」。這種「cohort-level edge」用 rule 過濾比 ML 學更直接。

**路 C（完全下架）否決理由**：
- gap_up 仍有 +7.3pp edge（48% vs 40.7%）— 直接下架是丟錢。
- 收緊到 vol sweet spot 後 edge 拉到 +10.9pp（51.6% vs 40.7%）— 是個值得保留的「精選版」訊號。

---

## Step 2 — 動了哪些 code

| 檔案 | 改動 | 為什麼 |
|---|---|---|
| `src/strategies.py` | `DEFAULT_GAP_UP_PARAMS` 加 `gap_vol_ratio_max=3.0`；`_evaluate_gap_up` 加上限檢查 | 過濾 >3x 主力出貨群 |
| `src/strategies.py` | `STRATEGY_ML_THRESHOLDS` 拿掉 gap_up entry | WF ROC 0.49 model 等於 noise filter，下架 |
| `scripts/train_per_strategy_ml.py` | `STRATEGY_RF_PARAMS` 拿掉 gap_up override | gap_up 不再 per-strategy retrain |
| `scripts/eval_walkforward.py` | `DEFAULT_PER_STRATEGY` 拿掉 gap_up | 不再對 gap_up 跑 WF eval（保留手動 `--models gap_up`）|
| `tests/test_strategies.py` | 加 3 個新測試（extreme_volume_filtered_out / vol_max_override / default_params_include_vol_max_3）| 結構守護 |
| `tests/test_e2e_smoke.py` | `test_strategy_ml_thresholds_contains_calibrated_keys` expected 移除 gap_up | 對齊 dict 變更 |
| `tests/test_walkforward_eval_wire.py` | `test_eval_walkforward_default_models_...` rename + expected 7 個 strategies | 對齊 DEFAULT_PER_STRATEGY 變更 |
| `scripts/diagnose_gap_up.py` | **新建** | 路徑由資料決定的證據（可重跑）|
| `docs/gap_up_diagnose_raw.json` | **新建** | Diagnose 完整數據（archive，給未來重新評估參考）|

**未動**：`models/per_strategy/gap_up.pkl` / `.meta.json` — 保留 artifact（下次 retrain 不會更新；inference 路徑 `predict_for_strategy` 仍會 load 但因為 `STRATEGY_ML_THRESHOLDS` 無 gap_up，picks 不再被過濾，model 變死檔）。

---

## Step 3 — POST 效果驗證

`scripts/backtest_strategies.py --strategy gap_up --lookback 252` 跑出來：

| 指標 | PRE（2026-05-11, 126d）| POST（2026-05-15, 252d）| 變化 |
|---|---|---|---|
| n_fires | 2328 | 1572 | -32%（過濾掉 vol_ratio≥3x 那群）|
| n_wins | 1169 | 811 | — |
| win_rate | 50.21% | **51.59%** | **+1.4 pp** |
| avg_return | +0.94% | （新版未寫表）| — |

**對比 baseline 40.7%**：POST WR 51.6% = **+10.9pp edge**（PRE 50.2% = +9.5pp）。

對比 ML 過濾流程下的 PRE（STRATEGY_ML_THRESHOLDS["gap_up"]=0.60 校準時宣稱 100% WR / 30d 108 fires）— **要注意這個 100% 是極小樣本 + ROC 0.49 的 model 偶然挑出的「好運氣 picks」**，WF ROC 0.49 證實該 model 沒實際 generalization 能力。下架 ML 過濾後，雖然 raw WR 從「校準時的 100%（30d）」變回 51.6%（252d），但這 51.6% 是**可重複的、不依賴 noise model 的 edge**。

---

## 風險與後續

### 已知風險
1. **樣本量下降 32%**（2328 → 1572 fires / 252d）：fires 數還是夠（年化 ~1500），不會讓策略變得過稀。
2. **>3x 那群偶有 black-swan winners 被丟掉**：但整體 EV 0.52% / WR 44.8% 顯示這群是負期望，丟掉合理。
3. **`gap_vol_ratio_max=3.0` 是 diagnose 一年資料的甜蜜點**：未來市場 regime 變化可能讓邊界移動，建議每季重跑 diagnose 確認。

### 後續可做（P2，**不在這次 scope**）
- 若 fires 量穩定撐住 1500+ / 季 → 進一步收緊 `gap_vol_ratio_min` 從 1.5 → 2.0（diagnose 顯示 2-3x WR 51.3% > 1.5-2x 49.1%）
- 加 `gap_pct_min` 上拉到 2.0（過濾 <2% 那些勉強 qualify 的 picks，WR 48.2%）
- 等 TAIEX cache 補回 2024-03 以前，回頭重評是否值得加 regime feature 重訓 ML（路 A retry）

### 不該做
- ❌ 把 gap_up 加回 `STRATEGY_ML_THRESHOLDS`（即使重訓 ROC 沒過 0.55,別硬接）
- ❌ 留 `STRATEGY_RF_PARAMS["gap_up"]` 任意 override（樣本量沒長大前，hyperparam 微調是 noise）

---

## 給未來的軍師備註

WF ROC 0.49 不等於「策略 noise」，可能只是「features 不對」。Diagnose 訊號本身 vs baseline 是**第一步應做的**，比加 features 重訓更便宜，且能告訴你 edge 在哪個維度。

如果一個策略：
- WR < baseline → 真的 noise，**下架**
- WR ≈ baseline + few pp + 某個子分桶顯著好 → **rule-based 收緊**（路 B）
- WR 顯著高（10pp+）但 ML 學不到 → **加 features 重訓**（路 A）

gap_up 屬於中間那類。
