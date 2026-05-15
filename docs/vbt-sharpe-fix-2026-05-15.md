# vbt grid Sharpe N-膨脹修法（2026-05-15）

## TL;DR

`src/vbt_backtest.py` 的 grid search 原本用 `trade-level Sharpe = mean/std × sqrt(N)`
做評估,N 是 trade 筆數。大樣本（N=6000+）時 sqrt(N) ≈ 77.5× 把 Sharpe 線性放大,
**同一條 daily PnL 切成不同筆數的 trade,trade-level Sharpe 會差到 6×**，
跨策略 / 不同 N 的比較失去意義。

修法:新增 `_compute_daily_sharpe()` helper,把 trade returns 歸到 exit 當天後
reindex 到完整交易日序列(沒交易日 = 0 報酬),用 daily 報酬序列算 annualized
Sharpe(× sqrt(252))。新欄 `sharpe_daily` 並列舊欄 `sharpe`(deprecated),
UI 預設用新欄排序 + 顯示。

## 為什麼修

trade-level Sharpe 不是「每單位 calendar time 的風險調整報酬」,而是「每筆 trade
的 z-score × sqrt(N)」。問題:

1. **N 大就虛高**:同一條策略,只要切碎成更多 trade,Sharpe 就會線性飛漲
2. **跨策略不可比**:selective 策略(N=50)vs spray 策略(N=6000),trade-level
   給 spray 不公平的 sqrt(120) ≈ 11× 加分
3. **獨立性假設破**:同一日多筆 trade、同一波動的多筆 trade 報酬高度相關,
   sqrt(N) 假設它們是獨立 sample,這在實務上不成立

daily-aggregated Sharpe(annualized × sqrt(252))把「策略一日整體 PnL」當 sample
單位,scale 由 calendar time 決定,N 怎麼切都不影響結果。

## 改了什麼

| 檔案 | 變更 |
|---|---|
| `src/vbt_backtest.py` | 新增 `_compute_daily_sharpe(trades_records, trading_index)` helper。`_portfolio_stats` 回 dict 加 `sharpe_daily` 欄。`backtest_strategy_with_params` 排序鍵改用 `sharpe_daily`。`persist_grid_results` 寫 `sharpe_daily` 進 DB。 |
| `src/database.py` | `vbt_grid_results` schema 加 `sharpe_daily REAL` 欄;migrate function `_migrate_vbt_grid_add_sharpe_daily` 對既有 DB 做 ALTER TABLE。`upsert_vbt_grid_results` UPSERT SQL 加 `sharpe_daily`(沒帶 → NULL 向下相容)。 |
| `app.py::_render_vbt_grid_tab` | 多一個顯示欄「Sharpe(日)」,排序鍵切到新欄,選策略預設值也用新欄。舊 `sharpe` 標「Sharpe(trade,舊)」並列對照。新增 helper `_vbt_sort_key_col()`。 |
| `tests/test_vbt_sharpe_daily.py` | 17 個新測試,覆蓋算法正確性、edge cases、N 膨脹 regression、schema migrate、upsert/load。 |

## 守則

- **舊 `sharpe` 欄保留**(deprecated 並行)— 不打亂歷史 grid result row 結構
- 舊資料(`sharpe_daily IS NULL`)在 UI 顯「—」並 fallback 到舊 sharpe 排序
- 新跑出來的 row 自動帶 `sharpe_daily` 值

## 實測前後對比

同一條 daily return 序列(132 個交易日,真實年化 Sharpe ≈ 1.34),把它切成
不同筆數的 trade(同 exit_date,sum 後等於原 daily PnL),觀察兩種 Sharpe 算法:

```
切碎方式                           Sharpe(trade,舊)    Sharpe(日,新)
─────────────────────────────────  ─────────────────  ───────────────
N=  129  (每日 1 筆)               +0.958             +1.339
N=  387  (每日 3 筆)               +1.663             +1.339
N=5934   (每日 46 筆)              +6.521             +1.339
─────────────────────────────────  ─────────────────  ───────────────
比例                               6.8×(虛高)          1.0×(穩定)
```

結論:trade-level 把同一條 PnL 切碎 46 倍 → Sharpe 從 +0.96 飛到 +6.52,
daily 穩在 +1.34。**新指標才是策略品質的真實刻度**,大樣本 spray 策略
不會因 N 大就拿到不勞而獲的 sharpe boost。

## 部署 / 重跑

- 既有 `vbt_grid_results` 表會在 `init_db()` 第一次跑時自動 ALTER TABLE 加欄
- 舊 row 的 `sharpe_daily` 為 NULL → UI 顯示「—」,需要重跑 grid 才會填值
- 重跑指令(per strategy):
  ```bash
  python scripts/vbt_grid_search.py --strategy volume_breakout --months 6
  python scripts/vbt_grid_search.py --strategy bias_convergence --months 6
  python scripts/vbt_grid_search.py --strategy macd_golden --months 6
  python scripts/vbt_grid_search.py --strategy ma_alignment --months 6
  ```

## 後續

- 主公採用新 winner 時,記得用新欄 Sharpe(日)做判讀,別再看舊 Sharpe(trade)
- 等所有歷史 row 都 backfill 完,可以考慮把 `_render_vbt_grid_tab` 預設只顯示
  新欄(舊欄移到 expand panel),縮窄手機畫面
