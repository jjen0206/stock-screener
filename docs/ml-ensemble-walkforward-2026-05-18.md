# Stacking Ensemble vs RandomForest Walk-forward 比較

**生成時間**:2026-05-18T15:03:01+00:00
**Task**:Phase 2 #P2-5(LightGBM + Multi-task + Stacking ensemble)
**Universe**:TW_TOP_50(50 檔)
**Dataset**:1290 samples,base win rate 44.0%
**Walk-forward**:expanding window,min_train_days=120,test_days=30
**Splits**:13(7 stacking / 6 RF fallback)

## TL;DR

❌ **FAIL** — ensemble AUC < RF baseline,**這個 dataset 不建議 merge 為 default**(production 維持 RF)。需要 debug LightGBM hyperparameters / feature 前處理 / 或對更大的 per-strategy dataset 重跑 eval。

## 聚合指標(test 段)

| Metric | RF baseline | Stacking Ensemble | Δ |
|---|---:|---:|---:|
| ROC AUC mean (std) | 0.5766 (0.1265) | 0.5501 (0.1370) | -0.0265 |
| Brier raw mean | 0.2789 | 0.3210 | +0.0421 |
| Brier calibrated mean | 0.2899 | 0.3298 | +0.0400 |

(Δ 為 ensemble − RF;AUC 越大越好,Brier 越小越好)

## Per-split 明細

| Split | Train range | Test range | n_train | n_test | pos_rate_test | RF AUC | Ens AUC | ΔAUC | RF Brier | Ens Brier | Backend |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| 0 | 2024-03-14→2024-09-05 | 2024-09-06→2024-10-23 | 120 | 30 | 0.73 | 0.614 | 0.614 | +0.000 | 0.302 | 0.302 | rf |
| 1 | 2024-03-14→2024-10-23 | 2024-10-24→2024-12-05 | 150 | 30 | 0.37 | 0.598 | 0.598 | +0.000 | 0.250 | 0.250 | rf |
| 2 | 2024-03-14→2024-12-05 | 2024-12-06→2025-01-17 | 180 | 30 | 0.43 | 0.525 | 0.525 | +0.000 | 0.268 | 0.268 | rf |
| 3 | 2024-03-14→2025-01-17 | 2025-01-20→2025-03-12 | 210 | 30 | 0.07 | 0.536 | 0.536 | +0.000 | 0.326 | 0.326 | rf |
| 4 | 2024-03-14→2025-03-12 | 2025-03-13→2025-04-25 | 240 | 30 | 0.33 | 0.480 | 0.480 | +0.000 | 0.303 | 0.303 | rf |
| 5 | 2024-03-14→2025-04-25 | 2025-04-28→2025-06-10 | 270 | 30 | 0.53 | 0.629 | 0.629 | +0.000 | 0.241 | 0.241 | rf |
| 6 | 2024-03-14→2025-06-10 | 2025-06-11→2025-07-22 | 300 | 30 | 0.33 | 0.590 | 0.405 | -0.185 | 0.248 | 0.242 | stacking |
| 7 | 2024-03-14→2025-07-22 | 2025-07-23→2025-09-02 | 330 | 30 | 0.30 | 0.831 | 0.730 | -0.101 | 0.266 | 0.210 | stacking |
| 8 | 2024-03-14→2025-09-02 | 2025-09-03→2025-10-17 | 360 | 30 | 0.80 | 0.764 | 0.701 | -0.062 | 0.300 | 0.472 | stacking |
| 9 | 2024-03-14→2025-10-17 | 2025-10-20→2025-12-01 | 390 | 30 | 0.27 | 0.466 | 0.324 | -0.142 | 0.274 | 0.458 | stacking |
| 10 | 2024-03-14→2025-12-01 | 2025-12-02→2026-01-14 | 420 | 36 | 0.69 | 0.567 | 0.687 | +0.120 | 0.252 | 0.321 | stacking |
| 11 | 2024-03-14→2026-01-14 | 2026-01-15→2026-03-09 | 456 | 60 | 0.60 | 0.597 | 0.639 | +0.042 | 0.248 | 0.289 | stacking |
| 12 | 2024-03-14→2026-03-09 | 2026-03-10→2026-04-22 | 516 | 283 | 0.37 | 0.299 | 0.283 | -0.016 | 0.348 | 0.490 | stacking |

## 解讀說明

- **AUC delta**:單個 split 上下浮動 ±0.05 是常態(時序 OOS 本身雜訊大),看 mean / std 比看單 split 重要。
- **Brier raw vs calibrated**:calibrator 的 holdout 是 train 段後 20% (時序內),不 leak test。calibrated < raw 代表機率校準在生效。
- **Backend column**:'stacking' = 該 split 樣本 ≥ MIN_STACKING_SAMPLES=300,'rf' = 樣本不足 fallback。

## 評判 gate

> Spec 拍板門檻:
>  - **新 model AUC ≥ 舊 model AUC + 2pp** → merge
>  - **新 model AUC < 舊 model AUC**(任何 -delta) → 不 merge,debug
>  - **中間區間(0 ≤ Δ < 2pp)** → marginal,留 `--backend` toggle 觀察

本次 Δ AUC = **-0.0265** → ❌ **FAIL**

## 範圍限制(IMPORTANT — 讀完再下結論)

這次 eval 用的是 **TW_TOP_50 sliding window + 5d ATR target label**(對應
`scripts/train_ml_model.py` 訓的 short_pick fallback model)。**不能**直接
推論到 per-strategy models — 它們的 dataset 不一樣:

| 維度 | 本 eval(short_pick fallback) | per-strategy models |
|---|---|---|
| Universe | TW_TOP_50(50 檔大型股) | pure_stock_universe(~2400 檔) |
| 樣本量 | ~1290 | bias_convergence 2358 / 其餘 1000-3600 |
| Label | 5d ATR(1.5×)target hit | cost-aware +5%/-3%/5d hold(扣 0.585% round-trip) |
| 訊號來源 | 每天每檔都算 | 只看該 strategy fire 的 picks |

結論建議:
- `scripts/train_ml_model.py` default 維持 **rf**(對齊本次 eval 結果)
- `scripts/train_per_strategy_ml.py` default 維持 **ensemble**(per-strategy 樣本
  更大 + cost-aware label 更複雜,spec 假設 stacking 有 edge — 需要 dedicated
  per-strategy walk-forward eval 才能定論;下次 nightly cron 跑出來再看 OOB / Brier 對比)
- production 既有 `models/short_pick.pkl` / per-strategy `.pkl` **不重訓**
  (避免在 eval 還沒驗證的情況下動 production model)

## 後續可以做的事

1. **更大的 dataset 上重跑**:擴 universe 到 Top 200 或全市場,觀察樣本量
   ≥ 3000 時 stacking AUC 是否反轉。
2. **LightGBM hyperparameter sweep**:300-500 sample folds 上,`num_leaves=31` 可能
   太大;試 `num_leaves=7-15` + `reg_alpha=0.1` + `min_child_samples=20` 抑制 overfit。
3. **Per-strategy walk-forward**:寫 sister script `scripts/eval_ml_ensemble_walkforward_per_strategy.py`
   對 bias_convergence / volume_breakout / gap_up 各跑一次,看哪些 strategy benefit。
4. **Multi-task heads OOS eval**:目前只 store 不評,加 `predict_multitask` 對應的
   1d/3d/10d label OOS AUC,看共享 representation 是否真的提升 5d 主 head。
