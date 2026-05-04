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
