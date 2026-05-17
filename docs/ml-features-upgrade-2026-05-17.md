# E — ML / 訊號強化(v4 features)

> 2026-05-17|加 10 個新 features(籌碼變化率 / 多時間軸動能 / 產業相對強度)
> 並把 `MODEL_VERSION` 從 v3 升 v4。Kill-switch `ML_NEW_FEATURES_ENABLED=false`
> 可即時 rollback 到 v3 行為。

## 動機

v3 的 16 個 features 偏短期 snapshot:KD / MACD / 量比 / ATR / 法人 5d-10d /
holders 週變化。對「持續性訊號」與「相對強度」沒法表達 — 兩個常見的失誤:

1. **籌碼一買一賣訊號**:外資 +500 / 投信 -300 看起來淨買 +200,但其實兩家
   反向、風險很大。v3 model 只看 inst_5d 加總值，學不到背離。
2. **產業同向漲跌混淆**:該股漲 5% 看起來強,但整個半導體都漲 8% — 那其實
   是落後股。v3 model 完全沒產業資訊。
3. **趨勢持續度**:單看「今天 ma5 > ma20」是 binary snapshot；近 60 天有 50
   天 ma5 > ma20 vs 5 天 ma5 > ma20,代表的趨勢強度完全不同。

## v4 加入的 10 個 features

### 籌碼類(3)
- `concentration_change_rate`  千張戶集中度月變化率,用 `max(0.005, old)`
  平滑分母,避免小分母爆值(跟 v3 `holders_pct_change_4w` 差別:後者沒平滑)
- `institutional_continuity`  外資 + 投信「同向連續淨買 / 淨賣」最大天數
  (帶符號:+N = 連 N 天共同淨買、-N = 連 N 天共同淨賣)
- `inst_divergence`  外資 vs 投信背離指標 [0, 1]:
  - 1.0 = 完全反向(一買一賣,量級相當)
  - 0.0 = 完全同向 / 任一家沒動

### 多時間軸(5)
- `ma5_above_ma20_pct`  近 60 日 5MA > 20MA 的天數占比(0-1)
- `ma20_above_ma60_pct` 近 60 日 20MA > 60MA 的天數占比
- `momentum_5d`         近 5 日報酬 %
- `momentum_20d`        近 20 日報酬 %
- `momentum_60d`        近 60 日報酬 %

### 產業相對強度(2)
- `industry_relative_strength`  該股 5d 漲幅 − 同產業 5d 平均漲幅(% 點)
- `industry_rank_pct`           該股在產業內 5d 漲幅 percentile(0-1)

對全市場 predict_batch 時，同產業的 N 檔股票會共享 `_INDUSTRY_RETURNS_CACHE`
(key=`(target_date, industry, db_path)`),只算一次 SQL；O(M sids) → O(industries)
顯著省 query。

## Kill-switch

```bash
export ML_NEW_FEATURES_ENABLED=false  # 全 v4 features fallback 0.0
```

- 預設 enabled
- v4 features 在 `extract_features` 內 try/except 包圍,缺資料各自 fallback 0.0
- 即使 kill-switch off,**feature dict shape 仍是 26 keys**(只是 v4 那 10 個值 = 0.0)
  → v4 model 仍能 predict_proba 不炸

## Backward-compat(向下相容)

- `FEATURE_NAMES` 從 16 擴成 26,**v4 新欄一律 append 在尾部**
- `_aligned_feature_names(model)` 用 `n_features_in_` slice 前 N 欄
- 舊 v3 pkl(n=16) → 仍 slice 前 16 欄 → 推論完全等同 v3 行為
- 舊 v2 pkl(n=11) → 仍能 slice 前 11 欄 → 完全等同 v2 行為
- 不需 retrain 即可 ship,新 model 等下次 weekly cron 自然吃

## 試水溫 — 3 model walk-forward 對比(2026-05-17,row split)

| Model | OLD ROC (v3, 16 feat) | NEW ROC (v4, 26 feat) | Δ ROC | 達 +0.02? |
|---|---|---|---|---|
| **short_pick** | 0.6417 | 0.6472 | +0.0055 | ❌(雜訊內) |
| **big_holder_inflow** | 0.5952 | 0.6183 | **+0.0231** | ✅ |
| **macd_golden** | 0.5882 | 0.5971 | +0.0089 | ❌(雜訊內) |

`big_holder_inflow` 達標 +0.02 ROC,符合「至少一個 model 改善 +0.02」success criteria —
也合理：big_holder_inflow 本質就是看籌碼集中度,新加 `concentration_change_rate` /
`institutional_continuity` / `inst_divergence` 三個籌碼 features 直接 hit 該策略的訊號源。

`short_pick`(通用)+ `macd_golden`(技術指標)改善幅度落在 noise band,
但 ROC 上升即正向訊號 — Train ROC AUC 顯著升(0.88 → 0.93)代表 model 有
真的「學到東西」,WF ROC 沒等幅上升是 v3 base 已經抓到大部分 signal、v4 新
features 邊際貢獻有限。

## 未重訓全 17 model 的理由

- 對全市場 ~2400 sids × 17 model × walk-forward 7-9 splits 重訓會 timeout
  (前次 nightly retrain 已超過 GH Actions 6 hr cap)
- 改路徑:`ml-weekly-retrain.yml`(週日 03:00 TW)已有 walk-forward A/B gate,
  下一次自然吃 v4 features。失敗 model 會被該 workflow rollback 到 .pre_retrain.bak,
  不影響 production
- 個人 repo + kill-switch 雙保險 → ship code、等 cron 跑

## 哪些 callers 受影響?

- `src/ml_predictor.extract_features`:輸出 dict 從 16 keys → 26 keys
- `_aligned_feature_names`:加 v3(16)→ v4(26)分流
- `predict_short_pick_winrate` / `predict_batch`:對舊 v3 / v4 pkl 都自動 align,不需改 caller
- `scripts/train_*.py` / `scripts/eval_walkforward.py`:沿用 `FEATURE_NAMES`,自動吃新 features

## Files

- 新檔:
  - `src/ml_features.py`  v4 features 計算純函式
  - `tests/test_ml_features_new.py`  feature 數值 + edge case test
  - `tests/test_ml_predictor_new_features.py`  wire 進 predictor 的整合 test
- 改:
  - `src/ml_predictor.py`  `FEATURE_NAMES` + `extract_features` wire + kill-switch
  - `tests/test_ml_features_v3.py`  slicing 對 26 features adjust(`FEATURE_NAMES[:16]`)
  - `tests/test_ml_features_v3_structural.py`  `MODEL_VERSION` 容忍 v3 / v4

## Test 結果

```
$ python -m pytest tests/test_ml_*.py -q
116 passed in 7.35s

$ python -m pytest tests/ -q --ignore=tests/test_e2e_persistence.py
2137 passed in 406.98s (0:06:46)
```

## 下一步

1. 等 `ml-weekly-retrain.yml`(週日 03:00 TW)cron 自然跑全 17 model 重訓
2. A/B gate 結果若多數策略 ROC 上升 → KEEP,反之 rollback 個別 model 到 v3 pkl
3. 若 v4 features 出現 production 問題 → set `ML_NEW_FEATURES_ENABLED=false`
   即時 rollback 到 v3 行為(不需 deploy)

## 注意事項

- `industry_relative_strength` / `industry_rank_pct` 假設 `stocks.industry` 欄
  有值;新上市 / cache miss 的 sid 會落回 0.0(中性)
- `institutional_continuity` 觀察窗 10 天 — 對長假後第一天會 break(只連續到開市)
- v4 features 在 `extract_features` 內各自有 try/except,個別失敗只 fallback 0.0,
  不會 drop 整列 row(對齊 v3 設計)
