# Cold-load Profiling Results

跑法:`PYTHONIOENCODING=utf-8 python scripts/profile_short_page.py`(本機 AppTest,headless,2026-05-04)。

## Summary

| Phase | Wallclock |
|---|---|
| 1. Cold load(boot + 首頁 dashboard) | **13,188ms** |
| 2. 切到「🔥 短線」頁(尚未執行選股) | 1,384ms |
| 3. 執行選股(50 檔大型股 universe) | 1,854ms |

## Phase 1:Cold load — 13.2 秒(主要痛點)

```
main_total                  12,663ms
  page_🏠 首頁              11,106ms   ← 84% 在這
  boot_setup                 1,514ms
  sidebar                        0ms
```

**`page_🏠 首頁` = 11.1 秒**:首頁 dashboard 內 `run_all_strategies` 跑全市場(~2000 檔)+ FinMind 抓 TAIEX。`_page_dashboard` 預設展示「今日推薦 Top 3」要先選股一輪。

**`boot_setup` = 1.5 秒**:`_load_snapshot_if_needed()` 把 6 個 CSV(stocks / daily_metrics / financials / daily_prices ~131K rows / institutional / taiex)load 進 SQLite。

## Phase 2:切到短線頁 — 1.4 秒

```
main_total                   1,265ms
  boot_setup                 1,246ms   ← 切頁仍重 load snapshot
  page_🔥 短線                  16ms   ← 短線頁本身只 16ms
  sidebar                        0ms
```

**`boot_setup` 在每次 rerun 都重跑** — 應該只第一次。`_load_snapshot_if_needed()` 內部有 guard 嗎?還是 guard 失效?

短線頁本體 16ms — 因為還沒 submit,只 render 初始畫面。

## Phase 3:執行選股(50 檔大型股) — 1.9 秒

```
main_total                   1,732ms
  boot_setup                 1,325ms   ← 又跑一次
  page_🔥 短線                 404ms
    short_run_all_strategies   338ms   ← 主工作
    short_render_picks          45ms   ← lazy expander 生效
    short_aggregated_to_df       2ms
    short_resolve_universe       0ms
  sidebar                        0ms
```

`short_run_all_strategies` 50 檔 = 338ms,合理。
`short_render_picks` 只 45ms — 確認 lazy expander + pagination 生效(原本 cold load 跑 138 cards × 5 helpers 應該 1-2 秒)。

## Top 3 ROI 對策(等 Phase 2 決策)

1. **🥇 dashboard cold load 11s → ?**
   - `_page_dashboard` 內「今日推薦 Top 3」自動跑 `run_all_strategies` 全市場
   - 改 lazy(預設不跑,加按鈕「載入今日推薦」)→ cold load 14s → ~3s
   - 或者 cache 結果 `@st.cache_data(ttl=300)` 包整個 agg

2. **🥈 boot_setup 每 rerun 1.3s × 浪費**
   - `_load_snapshot_if_needed()` 應只跑一次,但目前每輪都跑
   - 加 session_state guard:`if "_snapshot_loaded" in st.session_state: return`
   - 預期省 1.3s × N 個 rerun

3. **🥉 sticky submit 重複跑 run_all_strategies**
   - submit 後每次 button click(展開 / 載入更多)都重跑 338ms
   - 用 session_state cache 整個 agg,key = (date, universe, params hash)
   - 重 rerun 0ms

## 推薦先做順序

1. 🥈 **boot_setup guard**(最簡單,影響面最大,所有頁都受惠)
2. 🥇 **dashboard 改 lazy**(影響 cold load 最直接)
3. 🥉 **agg cache**(短線頁互動體感)

(此次只做 profile,不動任何優化。等使用者決定 Phase 2 內容。)

---

# Phase 2 結果(2026-05-04 完成 A + B + C)

三項一起做完,**整體從 16.4 秒砍到 2.9 秒**(-82%):

| Phase | Before | After | 變化 |
|---|---|---|---|
| 1. Cold load(boot + 首頁) | 13,188ms | **2,200ms** | **-83%** |
| 2. 切短線頁 | 1,384ms | **147ms** | **-89%** |
| 3. 執行選股(50 檔) | 1,854ms | **571ms** | **-69%** |
| **合計** | 16,426ms | **2,918ms** | **-82%** |

## After 細項

### Phase 1(2,200ms,主要剩 boot 不可省)
```
main_total                  1,623ms
  boot_setup                1,536ms   ← 這次第一次 preload,合理
  page_🏠 首頁                 40ms   ← 從 11,106ms 降到這
  sidebar                       0ms
```

### Phase 2(147ms,boot guard 生效)
```
main_total                     21ms
  page_🔥 短線                  18ms
  boot_setup                    0ms   ← 從 1,246ms 降到 0(session_state guard)
  sidebar                       0ms
```

### Phase 3(571ms,主要剩 run_all_strategies 算)
```
main_total                    437ms
  page_🔥 短線                 434ms
    short_run_all_strategies  365ms   ← cache miss(首次執行),第二次 rerun 0ms
    short_render_picks         47ms   ← lazy expander 維持
  boot_setup                    0ms   ← guard 生效
```

## 修法

### A. `_load_snapshot_if_needed` 改 session_state guard
原本 `_snapshot_loaded` 是 module-level global,但 streamlit 每輪 rerun 都重執行 script body → global 被 reset 回 False → preload 每次都跑。改 `st.session_state[_BOOT_DONE_KEY]` 才能跨 rerun persist。

### B. Dashboard lazy
`_page_dashboard` 內「今日推薦 Top 3」原本一進首頁自動跑全市場 `run_all_strategies` → 11 秒。改成「🚀 載入今日推薦」按鈕,user 主動點才跑。session_state flag 控制。

### C. `_run_all_strategies_cached` wrapper
`@st.cache_data(ttl=600)` 包 `run_all_strategies`,key = `(date, universe_tuple, enabled_tuple, params_items_tuple)`。同 (date, universe, params) 跨頁面共享 cache。短線頁 sticky-submit 後 rerun 直接 hit。

## 守門 Test
- `test_boot_setup_runs_only_once`:多輪 rerun preload 只跑 1 次
- `test_dashboard_does_not_auto_run_strategies_on_cold_load`:cold load 0 次 strategies call
- `test_run_all_strategies_cached_hits_on_repeated_args`:同 args 第 2/3 次 cache hit
