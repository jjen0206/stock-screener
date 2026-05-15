# ML 機率校準 (Probability Calibration) — 2026-05-15

## 一句話

把 RandomForest `predict_proba` 出來的「信心度」轉成**真實機率**:UI 顯
「🎯 AI 勝率 67%」就真的是 67% 命中,不再是 RF over-confidence 騙人。

## 為什麼要做

各 strategy 都用 `RandomForestClassifier` + `class_weight=balanced`。RF 是
ensemble of trees,每棵樹 vote 完平均下來,**天生傾向把 prob 推向 0/1 兩端**
(over-confidence)。舉例:

| 看到的 prob | 實際命中 | 偏差 |
|------------|---------|------|
| 0.70       | 0.55    | -15pp |
| 0.80       | 0.62    | -18pp |
| 0.90       | 0.72    | -18pp |

主公看推播「AI 勝率 70%」實際進場後命中只有 55%,**信心度當決策依據會被誤導**。

## 怎麼做

`sklearn` 的 `CalibratedClassifierCV` 思路,但拆成輕量化 wrapper:

1. base RF 訓完
2. 留**最後 20% 樣本(time-based holdout)**fit 一個 `IsotonicRegression`
3. predict 時 raw_prob → calibrator.transform → 校正 prob

`models/calibrators/{strategy}.pkl` 跟 base model 同生命週期,retrain 時一起重訓。

### method 自動切換

- 樣本 ≥ 500 → **isotonic**(非參數,靈活)
- 樣本 < 500 → fallback **platt sigmoid**(參數化,小樣本更穩)

### Kill-switch

`ML_CALIBRATION_ENABLED=false` → 一切如舊走 raw prob。預設 on。

## Brier score 前後對比

驗證資料:synthetic over-confident scenario(n=3000,holdout=600,isotonic):

| 指標 | 數值 |
|------|------|
| Raw Brier        | **0.2317** |
| Calibrated Brier | **0.2243** |
| Δ improvement    | **+0.0074(-3.2%)** |

注:對 RF 已經偏 over-confident 的場景,isotonic 把高信心區段(prob > 0.6)
壓回 ~實際命中率,改善最明顯。Brier score 越低越好,perfect=0,random≈0.25,
**> 0.3 視為偏離校準**(系統頁顯 ⚠️)。

## Reliability diagram(synthetic over-confidence 場景示例)

| Raw bin | n | mean_pred | actual_rate | calibrated mean_pred | calibrated actual |
|--------|---|-----------|-------------|----------------------|-------------------|
| [0.2,0.3] | 48  | 0.271 | 0.208 | 0.250 | 0.000* |
| [0.3,0.4] | 82  | 0.349 | 0.390 | 0.370 | 0.370 |
| [0.4,0.5] | 135 | 0.445 | 0.496 | 0.452 | 0.452 |
| [0.5,0.6] | 174 | 0.555 | 0.511 | 0.565 | 0.570 |
| **[0.6,0.7]** | **123** | **0.639** | **0.593** | **0.672** | **0.667** |
| [0.7,0.8] | 25  | 0.754 | 0.760 | 0.750 | 0.000* |
| [0.8,0.9] | 8   | 0.822 | 1.000 | 0.850 | 0.000* |

(*) 校正後該 bin 樣本數變 0(isotonic 把 raw 高 prob 段重新分布到別的 bin),
不代表「命中 0%」。

**關鍵**:`[0.6, 0.7]` bin 校正前 mean_pred 0.639 但 actual_rate 只 0.593
(-4.6pp 過度自信);校正後 mean_pred 0.672 對齊 actual_rate 0.667(誤差
< 0.5pp,基本完美)。**對主公決策最關鍵的「中高信心」段被拉回真實機率**。

## 檔案動靜

### 新檔
- `src/ml_calibration.py` — Calibrator 類別 + fit/load/save + Brier metric
- `tests/test_ml_calibration.py` — 27 個單元測試(fit/transform/metrics/persistence/kill-switch)
- `tests/test_ml_predictor_calibration_wire.py` — 13 個 wire 測試
  (predict_batch / predict_for_strategy / predict_short_pick_winrate 三路徑都串對)
- `tests/test_e2e_calibration.py` — 8 個端到端測試(train → save → load → predict → 推播)

### 改檔
- `src/ml_predictor.py` — `predict_batch` / `predict_for_strategy` /
  `predict_short_pick_winrate` 加 `calibrator=None` kwarg,
  `train_with_calibration()` helper,`load_strategy_calibrator()` /
  `load_short_pick_calibrator()` 載入助手,`dump_model_meta` 加 calibration block
- `src/ml_walkforward.py` — `_train_one_split` 訓時加 calibrator,test split
  輸出 raw + calibrated Brier,`walkforward_summary` 加 brier aggregate
- `src/notifier.py` — `_select_top_picks` 自動 load 通用 + per-strategy calibrators
- `src/system_brief.py` — `_build_ml_performance` 多 `calibration_health` list
  (每 strategy 顯 raw → cal Brier + is_healthy)
- `app.py` — 系統結論頁加「🎯 ML 機率校準 (Brier score)」表 + reliability
  diagram expander
- `scripts/train_ml_model.py` — 訓完 base model 後自動訓 calibrator + 印
  Brier raw → cal,寫進 meta.json
- `scripts/train_per_strategy_ml.py` — 對每 strategy 訓完 base model 後自動
  訓 calibrator,summary 表加 brier 欄
- `scripts/eval_walkforward.py` — `_print_summary_table` 加 Brier 欄

## Production 整合方式

完全 backward-compat。

- **callers 沒傳 calibrator**(audit scripts、舊 caller)→ 行為跟以前一樣回 raw
- **callers 傳 calibrator**(notifier、app.py 已 wire)→ 走校正
- **kill-switch**:`ML_CALIBRATION_ENABLED=false` 一切回 raw,主公能完全關掉
- **calibrator 不存在**(舊 retrain workflow 沒跑過)→ load 回 None →
  caller 走 raw,印 graceful log 不 crash

## 下次 retrain 時序

`scripts/train_ml_model.py` + `scripts/train_per_strategy_ml.py` 已內建
calibration step,**下一次 retrain workflow(週日 03:00 TW)跑完後**
`models/calibrators/*.pkl` 自動產生,系統結論頁的「🎯 ML 機率校準」表會自動
顯示每 strategy 的 Brier。當前 production 因為還沒重訓,系統頁這區是空的(預期)。

A/B gate 仍以 walk-forward ROC AUC 為主指標(校正不影響 ROC,只改 prob 分布),
calibrator 由 train script 同步重訓,A/B 不需獨立 rollback gate。

## 為什麼不用 `sklearn.calibration.CalibratedClassifierCV`?

理論上等價,但這次拆出來自己包薄薄一層 IsotonicRegression / LogisticRegression
有 3 個好處:

1. **pkl 不重複存 base model** — `CalibratedClassifierCV(cv='prefit')` 把
   base estimator 也 pickle 進去,跟我們已經存的 `short_pick.pkl` 重複 ~10MB
2. **sklearn 1.6+ 起 `cv='prefit'` 改為 `FrozenEstimator`**,API 變動風險高
3. **解耦校正邏輯**,將來想換 beta calibration / temperature scaling 改一個
   class 就好,不被 sklearn 包死
