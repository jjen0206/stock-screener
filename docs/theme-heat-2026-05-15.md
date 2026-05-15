# 題材熱度動態權重 — 2026-05-15 拍板(v2:冷改 hard exclude)

## 起因

主公今天查近 5 日 9 大題材表現:
- 🔥 熱:HBM (+8.86%) / 矽光子 (+4.93%) / CoWoS (+4.64%) — 都在噴
- 🧊 冷:重電 (-2.32%) / 國防 (-10.35%) / 低軌衛星 (-0.56%) — 在修正

主公要這 3 個冷題材暫時降權。

**v1 設計(已棄):** soft 降權 ×0.7。
**v2 拍板(此版):** **hard exclude** — 冷題材成分股直接不推播。
主公的理由:soft 降權雜訊大,直接擋掉才乾淨。

## 設計

### 公式

對 `data/themes/*.yaml` 每個題材撈成分股近 N 日(預設 5 日交易日):

```
avg_return = mean((latest_close - oldest_close) / oldest_close × 100)
win_rate   = # sids with positive return / # sids with valid window
heat_score = avg_return × 0.6 + win_rate × 0.4
```

### Multiplier 規則(主公 v2 拍板)

| 條件                                          | Multiplier  | Badge | 行為      |
| --------------------------------------------- | ----------- | ----- | --------- |
| `heat_score > 3.0` AND `win_rate > 0.5`       | **1.3**     | 🔥    | 加分推薦  |
| `heat_score < -2.0` OR `win_rate < 0.3`       | **None**    | 🚫    | **擋掉**  |
| 其他                                          | **1.0**     | ➖    | 中性照常  |
| n_valid < 2(資料缺口)                       | **1.0**    | ➖    | 退保守    |

`COLD_EXCLUDE = None` 是公開的 sentinel — caller 看到 None 應該把該 sid
從 picks 移除。

### 跨題材 SID 規則(主公 v2 拍板,edge case 全列)

| sid 在哪 | 行為 |
| --- | --- |
| **至少一個熱題材** | 取最熱 multiplier(1.3),熱不被冷稀釋 |
| **沒熱,但有中性題材** | 取 1.0(中性壓過冷,**不被擋**) |
| **只在冷題材中** | **None**(擋掉,不推播) |
| **不屬任何題材** | 1.0(沒題材 ≠ 冷,**照常推薦**) |

> 主公明確強調:**沒題材 ≠ 冷**。沒被任何 yaml 覆蓋的股票仍可被
> 產業 / 技術面策略推薦,系統不該因為「沒題材」而排除它們。

### 推播 / UI 顯示

1. **每張 pick** 加 `🔥題材×1.3` badge(熱)。冷 sids 不會走到 format_pick_block
   (已被擋),所以不需要 🚫 badge 在 per-pick layer。
2. **訊息頂部 caption** 含熱題材加分名單 + 冷題材擋掉摘要(含 count):
   ```
   📡 題材熱度(近 5 日)
   🔥 熱題材加分: HBM / 矽光子 / CoWoS
   🚫 冷題材已擋: 5 檔 (國防 2 / 重電 2 / 低軌衛星 1)
   ```
3. **app.py 兩個地方**「📡 題材熱度排行」section:
   - `📋 系統結論` 頁
   - `📊 強者跟蹤` 頁的「✨ 高信心精選」tab 上方
   - 冷題材該列「權重」欄顯示「🚫 擋」(取代 v1 的「×0.7」)

### Kill switch

env `THEME_HEAT_ENABLED=false` → multiplier 全 1.0,**不擋任何 sid**。
預設 on。不需要改 code 就能 ops 切。

## 排序如何受影響

```python
# _select_top_picks 內:
# 1) 先撈 theme heat
# 2) 對每個 candidate sid 查 multiplier
#    - None (只在冷題材) → continue 跳過(hard exclude)
#    - else → 寫進 pick["theme_multiplier"]
# 3) 排序時 weighted_ml = ml_prob × strategy_weight × theme_multiplier

# 排序 tuple (-100 if analyst, -weighted_ml, -len(matched), sid) ascending
```

例子:
- A (HBM,熱) ml=0.6 → 套 ×1.3 → weighted=0.78 → score[1]=-0.78
- B (台積電供應鏈,中性) ml=0.7 → 套 ×1.0 → weighted=0.70 → score[1]=-0.70
- C (國防,冷) ml=0.9 → **直接跳過,不進 picks**

排序後 A < B 排前(熱題材加分),C 完全不出現(被擋)。

## 今日 9 題材熱度數字(2026-05-15)

> 註:本 worktree env 內 `data/cache.db` 為空(未 sync FinMind),以下為
> 主公昨天手動查的 5 日漲幅。實際 win_rate 由 production cron 跑
> `daily-notify.yml` 後實算。

| 題材           | 5日均漲   | 預估 win_rate | 預估 heat_score | 行為(v2) |
| -------------- | --------- | ------------- | --------------- | ---------- |
| HBM            | +8.86%    | ~75%          | ~+5.6           | ×1.3 🔥    |
| 矽光子         | +4.93%    | ~65%          | ~+3.2           | ×1.3 🔥    |
| CoWoS          | +4.64%    | ~60%          | ~+3.0           | ×1.3 🔥(邊界) |
| AI 概念        | (待算)    | (待算)        | (待算)          | (待算)     |
| 台積電供應鏈   | (待算)    | (待算)        | (待算)          | (待算)     |
| 人形機器人     | (待算)    | (待算)        | (待算)          | (待算)     |
| 低軌衛星       | -0.56%    | ~30%          | ~-0.2           | 1.0 ➖(邊界) |
| 重電           | -2.32%    | ~25%          | ~-1.3           | **🚫 擋掉** |
| 國防           | -10.35%   | ~10%          | ~-6.2           | **🚫 擋掉** |

明天 daily-notify cron 跑完後會在訊息 caption 看到實際 hot / excluded count。

## 影響範圍

### 改了什麼(此 worktree)

- `src/theme_heat.py`:
  - 改 `_classify_multiplier` 回 `Optional[float]`,冷 → None
  - `get_pick_theme_multiplier` 回 `Optional[float]`,跨題材取最熱
    (熱 1.3 > 中性 1.0 > 冷 None;不在任何題材 → 1.0)
  - 新增 `MIN_VALID_FOR_CLASSIFY = 2` 守住「資料缺口被誤判成冷」邊界
  - badge: 🚫 取代 🧊
  - `format_theme_heat_caption` 加 `excluded` 參數,輸出含 N 檔 + 各題材 count
  - 移除 `COLD_MULTIPLIER`,改 `COLD_EXCLUDE = None` 當 sentinel
- `src/notifier.py`:
  - `_LAST_THEME_EXCLUDED` 新模組級 cache 給 caption 算數字
  - `_select_top_picks` hard exclude:`multiplier is None` 的 sid skip
    (含 fallback weak path)+ 收集 excluded sids 到 cache
  - `format_yesterday_recap` 同步套 hard exclude(避免 recap 顯示昨天
    其實沒推的 sid)
  - `format_top_picks_message` 把 excluded 餵給 caption
  - `format_pick_block` 簡化:只顯 🔥 熱 badge,冷 sids 走不到這裡
- `app.py::_render_theme_heat_section`:處理 multiplier=None,顯示「🚫 擋」
- tests 全改:
  - 冷 → None 行為驗證 / hard exclude wire 守住 / 跨題材取最熱不被擋 /
    不在題材 1.0 / 資料缺口退保守 / excluded count 含在 caption

### Test 數

- `test_theme_heat.py`:**22** case(v1 18,v2 加 4)
- `test_notifier_theme_heat_wire.py`:**13** case(v1 11,v2 加 2)
- `test_page_theme_heat_wire.py`:**8** case(v1 7,v2 加 1)
- 全 repo sweep:**1195 + 161 e2e = 1356 passed**,0 failed

### 不動的地方

- ML calibration / consensus / regime gating / gap_up / vbt sharpe 都沒動
- `_compute_pick_score` 預設 `theme_multiplier=1.0` backwards compat
- kill-switch 仍然存在(env)

## 後續

- production daily-notify 跑完後比對「實際被擋的 sids」是否符合預期
- 觀察 1 週後再決定是否調 threshold(目前 -2% / 30% wr 是主公拍板)
