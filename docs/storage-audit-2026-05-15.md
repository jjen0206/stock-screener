# Storage Audit — 2026-05-15

> Round 1 即時清理:盤點本機產出物大小、加上自動清理腳本。

## 當前大小(本機 worktree 量測)

| 路徑 | 大小 | 說明 |
|---|---|---|
| `data/` | 21M | 全市場 CSV snapshot(twse_snapshot/ 14 個檔)+ themes |
| `data/cache.db` | **不存在** | gitignored,runtime 才產生(`data/*.db`) |
| `logs/` | **不存在** | repo 沒固定 log 目錄;若有 stdout redirect 才會有 |
| `models/` | 12M | `short_pick.pkl` (313KB) + `per_strategy/*.pkl` × 11(20KB-3.4MB 不等) |
| `models/per_strategy/` | 11M | 11 個 per-strategy ML 模型(部分 fallback 只剩 meta.json) |

**結論**:本機 repo 占用 ~33MB,**完全在合理範圍**,無立即清理需求。

> 註:`data/cache.db` 在實際部署/長期跑後可能膨脹到數百 MB;新加的 `cleanup_artifacts.py --execute` 會 VACUUM 釋放空間。

## 觀察

### `data/twse_snapshot/` 14 個 CSV(commit 進 repo)

| 檔案 | 用途 |
|---|---|
| `daily_prices.csv` | 全市場 90 天 OHLC |
| `daily_metrics.csv` | 全市場每日 PE/PB/EPS |
| `institutional.csv` | 三大法人 |
| `daily_picks.csv` | 每日 picks 結果 |
| `pick_outcomes.csv` | picks T+10 評分 |
| `financials_quarterly.csv` | 季財報 |
| `paper_trades.csv` | 模擬倉位 |
| `analyst_targets.csv` | 法人目標價 |
| `news.csv` | TWSE 重大訊息 dedup 表 |
| `shareholder_concentration.csv` | TDCC 集保 |
| `stocks.csv` | 股票主表 |
| `strategy_backtest.csv` | 策略回測結果 |
| `taiex.csv` | 大盤指數 |
| `watchlist.csv` | 主公自選 |
| `last_backfill.txt` / `last_update.txt` | timestamp 標記 |

這些**都需要 commit 進 repo**(Streamlit Cloud 啟動讀進 SQLite 用),不能清。

### `models/per_strategy/` 結構

11 個 strategy 各一份 .pkl + meta.json。看 file size 分佈:
- **大檔(>500KB)**:`bias_convergence.pkl` (3.5MB)、`taiex_alpha.pkl` (3.2MB)、`volume_breakout.pkl` (1.6MB)、`big_holder_inflow.pkl` (1.1MB)、`macd_golden.pkl` (780KB)、`bb_lower_rebound.pkl` (830KB)、`gap_up.pkl` (540KB)、`ma_alignment.pkl` (490KB)
- **fallback(只有 meta.json)**:`eps_acceleration` / `high_yield_stable` / `inst_consensus` / `inst_oversold_reversal` / `inst_silent_accum` / `ma_squeeze_breakout` / `revenue_acceleration` / `rsi_recovery` / `volume_kd`

fallback 是設計的(MIN_TRAIN_SAMPLES 不夠就不訓 pkl,inference 用通用 model),不是壞掉。

### 沒看到 backup 殘留

掃 `*.bak` / `*.pre_retrain.bak` / `*.v3.candidate`(`.gitignore` 有列)→ **0 檔**。
這表示先前 retrain workflow 都有正確清理 backup,沒堆積。

## 新增工具:`scripts/cleanup_artifacts.py`

### 功能
1. **VACUUM `data/cache.db`** — 釋放 SQLite delete 留下的 page space
2. **刪 `logs/*.log`** > 7 天
3. **刪 `models/**/*.bak`** > 30 天(每組最新一份永遠保留 → safety net)

### 用法
```bash
# 看會刪哪些(dry-run)
python scripts/cleanup_artifacts.py

# 真執行
python scripts/cleanup_artifacts.py --execute

# 只 VACUUM
python scripts/cleanup_artifacts.py --vacuum-only --execute

# 改門檻
python scripts/cleanup_artifacts.py --log-days 14 --model-days 60 --execute
```

### 安全設計
- **預設 dry-run**:不加 `--execute` 不會動任何檔
- **每組 model backup 最新一份永遠保留**:即使超齡,仍留作 fallback
- 不動 `data/twse_snapshot/*.csv`(這些是 source of truth,不該被 cleanup script 碰)
- 不動 `models/*.pkl`(只動 `.bak` / `.candidate`)

### 建議跑頻率
- 主公本機:每月一次手動跑 `--execute`(若 cache.db 變大才需要)
- **不**塞 GitHub Actions:runner 上的 cache 是暫態,沒清理需求

## 行動項

| 行動 | 狀態 |
|---|---|
| 寫 `scripts/cleanup_artifacts.py` | ✅ 完成 |
| dry-run 驗證 | ✅ 完成(無檔案要清,符合預期) |
| 加進 daily-notify workflow | ❌ 不加(runner 不需要) |

## 結論

- repo 本機占用合理(~33MB)
- 無 backup 殘留,先前 retrain workflow 清理正常
- 提供 `cleanup_artifacts.py` 作為**未來 cache.db 膨脹時的工具**,目前沒有立即清的需求
