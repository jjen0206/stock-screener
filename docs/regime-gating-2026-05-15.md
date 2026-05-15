# 大盤 Regime Gating 設計說明 (2026-05-15)

## 動機

歷史 backtest 顯示大盤空頭時所有策略 hit rate 一起掉 — 不分種類(量價/反轉/籌碼/動能都拉垮)。
舊系統有 `taiex_regime` 資料(已 wire 進 v3 ML features `regime_dummy`),但**推薦數量 + threshold 沒根據 regime 動態調整**:空頭時還照常推 10 檔短線 = 等於拿石頭砸自己腳。

## 設計

### 三層 regime(本模組獨立判斷,跟 `src/market_regime.py` 並存)

| Regime | 條件 | 短線上限 | 長線上限 | ML threshold uplift | Caption |
|--------|------|----------|----------|---------------------|---------|
| `bull`  (多頭) | 5MA > 20MA > 60MA + 60MA 斜率 > +0.5% | 10 | 10 | 0.00 | 📈 大盤多頭 |
| `range` (盤整) | 兩條 MA 交錯 / 60MA 斜率平 / correction | 5 | 7 | +0.05 | 📊 大盤盤整 |
| `bear`  (空頭) | 5MA < 20MA < 60MA + 60MA 斜率 < -0.5% | 2 | 5 | +0.15 | 📉 大盤空頭 + ⚠️ 警語 |

**correction(修正)**:多頭中短期跌破 5MA/20MA 但 60MA 仍向上 — spec 要求暫歸 `range`。`_classify_regime` 自動處理:`5MA < 20MA` 條件不滿足嚴格 bear 單調,自然落到 range。

**60MA 斜率閾值**:用「今天 60MA vs 20 天前 60MA」變化率;> +0.5% 算向上,< -0.5% 算向下,中間平。

### 跟既有 `src/market_regime.py` 的關係

`market_regime` 的 4-tier(bull / weak_bull / sideways / bear)判斷邏輯是「close vs MA20/MA60」,用途是**策略類別篩選**(STRATEGY_REGIME_FILTER)— 弱多頭時偏反轉、空頭時偏籌碼。

本模組 `regime_gating` 的 3-tier 用「5MA + 20MA + 60MA + 斜率」,用途是**推薦數量 + threshold 動態調整**。
兩個模組獨立,因為:

1. 篩策略類別不需要看斜率(只看價跟均線位置);動態縮量需要看趨勢方向才能判嚴重程度
2. 4-tier 對策略選擇夠細;3-tier 對 gating 三組門檻夠用,過細只增加迷惑

### Kill-switch

- `env REGIME_GATING_ENABLED` 預設 `true`,設 `false / 0 / no / off` 關掉
- 關掉時 `get_regime_gating_params` 永遠回 `bull` params(等同不縮、不拉門檻)
- 也支援 `src.notifier.REGIME_GATING_ENABLED` module-level 旗標(出事時改 source 立即關;原子化跟 `STRATEGY_DYNAMIC_WEIGHT_ENABLED` 對齊)

### Fallback

| 狀況 | 行為 |
|------|------|
| TAIEX 不足 80 天 | 走 fallback regime = `range`(中性,不該因 DB 缺值就盲目壓 bear / 放 bull) |
| TAIEX 完全沒資料 | 走 fallback regime = `range` |
| 整個 gating 拋例外 | notifier silent skip(退回原 `top_n` + base threshold),app badge silent skip(不擋頁面) |

## 整合點

### 1. `src/notifier.py::_select_top_picks`

- 算 picks 前先撈 `get_regime_gating_params` 取 `threshold_uplift`
- 每張候選 pick 過 `base_threshold + uplift` 才入選(bear 加嚴 0.15)
- 排序後用 `min(top_n, short_pick_max_count)` 截斷
- 把 gating metadata 寫進 `pick["regime_gating"]`,供 `format_top_picks_message` 取 caption

### 2. `src/notifier.py::format_top_picks_message`

- header 後緊接 `_regime_gating_caption(picks)` 注入 caption
- picks 空時也會主動撈一次(讓「全市場 0 picks」的死寂日子也顯 regime)
- 任何例外 silent skip,不擋主推播

### 3. `app.py::_render_regime_gating_badge`

- 短線 / 長線 / 系統結論三頁標題下方共用 helper
- mobile-first 大色塊 badge:bull 綠 (`#2ecc71`) / range 黃 (`#f1c40f`) / bear 紅 (`#e74c3c`)
- bear 時 caption 第二行警語用 `st.warning` 凸顯
- 內含 expander「ℹ️ 大盤 gating 說明」展開 max counts / uplift / kill-switch 教學

## 測試

| 檔案 | 涵蓋 |
|------|------|
| `tests/test_regime_gating.py` | 22 個:`_classify_regime` 純邏輯 / `get_regime_gating_params` 從 SQLite 算 bull/range/bear/correction / edge cases(資料不足、TAIEX 缺值)/ env kill-switch / params 合理性 |
| `tests/test_notifier_regime_gating_wire.py` | 11 個:flag 存在 / source 含 wire token / bear 截斷至 2 / caption 注入 header / picks 空時 fallback 撈 caption / kill-switch off 時 helper 回空 |
| `tests/test_page_regime_badge_wire.py` | 8 個:badge helper 存在 / 三色 mapping / expander 顯示 max count + uplift / silent skip 結構 / 三頁(短/長/系統結論)都 wire |

合計 41 個新測試,全綠;不影響既有 1308 個 test(總計 1349 passed)。

## 歷史 backtest hit rate 對比

> ⚠️ 目前 `pick_outcomes` 表未存 regime tag,跨 regime hit rate 比較需要 join `daily_prices` 算當天 TAIEX 5MA/20MA/60MA。等下一輪 nightly job 把 `regime` enrich 進 `pick_outcomes` 後回填,本節暫留方法論。

**方法論**:

```sql
-- 待 pick_outcomes 加 regime 欄位後可跑
WITH regime_per_pick AS (
  SELECT
    po.pick_date,
    po.sid,
    po.d5_ret,
    -- bull / range / bear 從當日 TAIEX 5MA/20MA/60MA + 斜率算
    (SELECT regime FROM regime_gating_snapshot WHERE date = po.pick_date) AS regime
  FROM pick_outcomes po
  WHERE po.d5_ret IS NOT NULL
)
SELECT
  regime,
  COUNT(*) AS n,
  AVG(CASE WHEN d5_ret > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate,
  AVG(d5_ret) AS avg_d5_ret
FROM regime_per_pick
GROUP BY regime;
```

**預期**(基於本任務描述,實證待 enrich 後)：bear hit rate < 40% vs bull > 55%,因此 bear 時:
- 縮 top 10 → top 2 = 砍掉 8 張「會被市場拖死」的 picks
- ML threshold +0.15 = 過濾掉勉強過 base threshold 的弱 picks
- 兩者疊加:bear 時只放 ~20% 的 picks(且只放最有信心)

## 不在本任務範圍

- ML calibration / gap_up / vbt sharpe / consensus(其他 task 在改)
- regime tag 寫進 `pick_outcomes` 表(下一個 task,需要 nightly job 改動)
- 動態策略類別篩選(已由 `src/market_regime.py` + `STRATEGY_REGIME_FILTER` 處理)

## 維運注意

- 改 `_REGIME_PARAMS` 數值前先跑 backtest 對比
- bear caption 文字改動需同步改 `tests/test_regime_gating.py::test_gating_bear_params_with_warning`(『保守操作』token assert)
- kill-switch 不該長期關 — 留作 incident response 用,有問題回滾 commit 後 PR 重新驗證
