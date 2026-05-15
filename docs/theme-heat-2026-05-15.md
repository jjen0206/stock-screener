# 題材熱度動態權重 — 2026-05-15 拍板

## 起因

主公今天查近 5 日 9 大題材表現:
- 🔥 熱:HBM (+8.86%) / 矽光子 (+4.93%) / CoWoS (+4.64%) — 都在噴
- 🧊 冷:重電 (-2.32%) / 國防 (-10.35%) / 低軌衛星 (-0.56%) — 在修正

主公要這 3 個冷題材暫時降權。但 hardcode 不對 — 該做成動態:
**每天根據題材表現自動調權重,熱題材自動加分、冷題材自動降權,
未來輪動到時不用主公手動改。**

## 設計

### 公式

對 `data/themes/*.yaml` 每個題材撈成分股近 N 日(預設 5 日交易日):

```
avg_return = mean((latest_close - oldest_close) / oldest_close × 100)
win_rate   = # sids with positive return / # sids with valid window
heat_score = avg_return × 0.6 + win_rate × 0.4
```

> avg_return 為 percent(例 8.86),win_rate 為 fraction(例 0.7),
> 結果約等於 avg_return 數量級,可拿來跟 % 閥值比。

### Multiplier 規則(主公拍板)

| 條件                                          | Multiplier | Badge |
| --------------------------------------------- | ---------- | ----- |
| `heat_score > 3.0` AND `win_rate > 0.5`       | **×1.3**   | 🔥    |
| `heat_score < -2.0` OR `win_rate < 0.3`       | **×0.7**   | 🧊    |
| 其他                                          | **×1.0**   | ➖    |

### 跨題材 SID 規則

`get_pick_theme_multiplier(conn, sid)` — sid 屬多題材時取 **最高**
multiplier(避免熱題材被冷題材稀釋掉應有的加分)。

例如 2330 同屬 CoWoS(熱)+ tsmc_supply(中性)→ 取 max(1.3, 1.0) = 1.3。

### 推播 / UI 顯示

1. **每張 pick** 加題材 badge:`🔥題材×1.3` / `🧊題材×0.7`(中性 1.0 不顯)
2. **訊息頂部 caption** 列熱 / 冷題材名單:
   ```
   📡 題材熱度(近 5 日)
   🔥 熱題材: HBM / 矽光子 / CoWoS — 自動加分
   🧊 冷題材: 國防 / 重電 / 低軌衛星 — 自動降權
   ```
3. **app.py 兩個地方**重複顯示「📡 題材熱度排行」section:
   - `📋 系統結論` 頁(動態權重明細下方)
   - `📊 強者跟蹤` 頁的「✨ 高信心精選」tab 上方

### Kill switch

env `THEME_HEAT_ENABLED=false` → multiplier 全 1.0,等同關掉(預設 on)。
不像 `STRATEGY_DYNAMIC_WEIGHT_ENABLED` 是 module 常數,題材熱度走 env
讓 ops 不需要改 code 就能切。

## 排序如何受影響

```python
# notifier._compute_pick_score 新增 theme_multiplier 參數
weighted_ml = ml_prob × strategy_weight × theme_multiplier
```

排序 tuple `(-100 if analyst, -weighted_ml, -len(matched), sid)`
ascending → 越小越前。

例:
- A (HBM) ml=0.6 strat_w=1.0 theme=1.3 → weighted=0.78 → score[1]=-0.78
- B (重電) ml=0.7 strat_w=1.0 theme=0.7 → weighted=0.49 → score[1]=-0.49

排序後 A < B(因 -0.78 < -0.49)→ A 排前,即便 A 的純 ml 比 B 低。
**這就是熱題材自動加分的效果。**

## 今日 9 題材熱度數字(2026-05-15)

> 註:本 worktree env 內 `data/cache.db` 為空(未 sync FinMind),以下為
> 主公昨天手動查的 5 日漲幅 + 推算 multiplier(實際 win_rate 由
> production cron 跑 `daily-notify.yml` 後實算)。

| 題材           | 5日均漲   | 預估 win_rate | 預估 heat_score | Multiplier | 判定 |
| -------------- | --------- | ------------- | --------------- | ---------- | ---- |
| HBM            | +8.86%    | ~75%          | ~+5.6           | ×1.3       | 🔥   |
| 矽光子         | +4.93%    | ~65%          | ~+3.2           | ×1.3       | 🔥   |
| CoWoS          | +4.64%    | ~60%          | ~+3.0           | ×1.3 / 邊界 | 🔥   |
| AI 概念        | (待算)    | (待算)        | (待算)          | (待算)     | ?    |
| 台積電供應鏈   | (待算)    | (待算)        | (待算)          | (待算)     | ?    |
| 人形機器人     | (待算)    | (待算)        | (待算)          | (待算)     | ?    |
| 低軌衛星       | -0.56%    | ~30%          | ~-0.2           | ×1.0 / 邊界 | ➖   |
| 重電           | -2.32%    | ~25%          | ~-1.3           | ×0.7       | 🧊   |
| 國防           | -10.35%   | ~10%          | ~-6.2           | ×0.7       | 🧊   |

明天 daily-notify cron 跑完後會在訊息 caption 看到實際分組。

## 影響範圍

### 改了什麼(此 worktree 內)

- `src/theme_heat.py`(新):`compute_theme_heat` / `get_pick_theme_multiplier`
  / `format_theme_heat_caption` + module-level cache + kill switch
- `src/notifier.py`:
  - `_compute_pick_score` 新增 `theme_multiplier=1.0` 參數
  - `_select_top_picks` 撈 theme heat → enrich each pick → 套排序
    → 寫 `_LAST_THEME_HEAT` 模組級 cache
  - `format_yesterday_recap` 同步撈 theme heat(recap 順序與實際推播一致)
  - `format_top_picks_message` 串 `theme_block` caption section
  - `format_pick_block` 加 🔥/🧊 題材 badge 在 #rank 行末
- `app.py`:
  - 新 `_render_theme_heat_section` helper(mobile-first 單欄 dataframe)
  - `_page_strong_follower`「✨ 高信心精選」tab 上方 wire
  - `_page_system_brief`「動態權重明細」expander 下方 wire

### Tests(共 36 個新 test case)

- `tests/test_theme_heat.py`(18):公式驗算 / multiplier 規則邊界 /
  跨題材取最高 / kill-switch / cache / 沒 daily_prices 退化 /
  caption 組合
- `tests/test_notifier_theme_heat_wire.py`(11):signature wire /
  source token / 排序行為(熱排前) / 模組級 cache 存在
- `tests/test_page_theme_heat_wire.py`(7):helper 存在 / 兩頁有 wire /
  mobile-first 不用 st.columns

### 不動的地方

- `_compute_pick_score` 預設 `theme_multiplier=1.0`(完全 backwards compat,
  legacy caller 不傳就跟以前一樣)
- ML calibration / consensus / regime gating / gap_up / vbt sharpe 都沒動
  (其他 task 在改)

## 後續

- `_compute_pick_score` 多人改,主公會手動 review merge
- production daily-notify 跑完後比對「實際排序變動」是否符合熱題材排前預期
- 觀察 1 週後再決定是否調 multiplier 倍率(1.3 / 0.7 → 1.5 / 0.5?)
