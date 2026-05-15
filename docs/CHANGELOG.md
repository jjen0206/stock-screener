# Changelog

按主題 group，最新放上面。日期 = commit 日，非 release tag。
歷史更早的變更不在此列，需要溯源走 `git log`。

---

## 2026-05-15 — 警示股 annotate-only(amendment:不替主公做隱藏決定)

### Changed
- **拿掉 hard exclude**:同日稍早版本 `exclude_warned_stocks` 把違約 / 全額股直接從 picks 剔除,違反「軍師主動提示風險,但不替主公做隱藏決定」原則 — 違約股偶爾反彈很猛,主公有時有特殊資訊想接刀,系統沒資格替他擋
- `src/warnings_filter.py`:
  - `exclude_warned_stocks` → 改為 `annotate_warned_stocks(conn, picks, as_of=None)` 只**標註**,picks 不過濾(in-place 注入 'warnings' 欄位)
  - `apply_soft_warning_penalty` 加重分流:SEVERE (default_settlement / full_cash) → ml_prob × 0.3 沉到推薦末段但仍顯;SOFT (attention / disposition / method_changed) → ml_prob × 0.7 維持
  - `format_excluded_caption` rename `format_warning_caption`,文案改「⚠️ 推薦中含 N 檔警示股 (違約X 全額Y ...) — 風險已標註,進場與否主公自行判斷」
  - `HARD_EXCLUDE_TYPES` rename `SEVERE_PENALTY_TYPES`(語意:嚴重等級降權,不是過濾)
  - kill-switch `WARNING_FILTER_ENABLED` rename `WARNING_ANNOTATE_ENABLED`(精準描述新行為)
- `src/notifier.py`:
  - `_select_top_picks` 拔掉 hard exclude call,只留 annotate + soft penalty
  - 模組級 cache `_LAST_EXCLUDED_WARNINGS` rename `_LAST_ANNOTATED_WARNINGS`
  - `_format_short_picks_section` 改用 `format_warning_caption`,picks 範圍只算 top N(避免 caption 含未顯示的 picks)
- `src/ui_cards.py`:badge 分嚴重等級樣式 — SEVERE 紅底白字加粗 13px(iPhone 窄屏絕對看見)/ SOFT 黃底紅字 11px;同 pick 兩類都中 → render 兩個 badge 全貌

### Tests
- `tests/test_warnings_filter.py` 重寫:18 cases 驗 annotate **不過濾** + SEVERE × 0.3 / SOFT × 0.7 + mixed types 取 SEVERE 不被稀釋 + 新文案不能含「已濾掉」+ regression guard 守住舊 hard-exclude API 已移除
- `tests/test_notifier_warning_wire.py` 重寫:9 cases 驗警示股**仍在 picks 中**(只是排序往後)+ caption 出現新文案 + 舊 `exclude_warned_stocks` / `_LAST_EXCLUDED_WARNINGS` 不能再出現

### Rationale
主公規矩:**主動提示風險,但不替主公做隱藏決定**。隱藏會讓主公失去判斷的機會。改用「強弱軟降權 + UI 顯眼 badge + caption 提示」讓主公自己看到自己決定 — 嚴重的(違約 / 全額)用 ×0.3 沉到末段但仍顯,一般的(注意 / 處置 / 變更方法)用 ×0.7 自然往後。

---

## 2026-05-15 — 警示股過濾(違約交割教訓 root cause)

### Added

**TWSE / TPEx 警示股 filter 三層防護**
- DB schema `stock_warnings`(stock_id + warning_type + announced_date 三 PK,5 類:default_settlement / full_cash / attention / disposition / method_changed)+ 兩個 index(`feat(database): add stock_warnings table for TWSE warning records`)
- 新 fetcher `scripts/fetch_stock_warnings.py`(BeautifulSoup 解析 TWSE punish / notice / disposition / method 公告頁;disposition 主端點失敗 fallback TWTBAU2;User-Agent 必填、retry 3 次)
- 新 workflow `.github/workflows/stock-warnings.yml`(交易日收盤後 17:13 cron + commit cache.db,跟 daily-fetch / weekly-shareholder-fetch 並列)
- 新 helper `src/warnings_filter.py`:`exclude_warned_stocks` 硬擋 default_settlement + full_cash;`apply_soft_warning_penalty` soft 降權 attention / disposition / method_changed(ml_prob × 0.7 排序自然往後沉);`format_excluded_caption` 組「✅ 已濾掉 N 檔警示股 (違約X 全額Y)」
- `notifier._select_top_picks` 接線:跑完 confluence + ML threshold 後接警示濾鏡,`_format_short_picks_section` 顯 caption(`feat(notifier): wire stock-warning filter into top-picks pipeline`)
- Kill-switch:`WARNING_FILTER_ENABLED=false` 退化整個 module 為 no-op,主公出事可立刻關
- UI:`ui_cards._build_card_html` 加 `warnings` 參數,有 active warning → 紅色 ⚠️ badge inline 顯示類別中文(mobile-first 不用 hover-only,iPhone 窄屏看得見)
- UI:`render_picks_cards` 走 `enrich_rows_with_warnings` 批次 SQL bulk-enrich,免每張卡 single query
- UI:📊 個股深度頁加「⚠️ 警示紀錄」section,顯該 sid 過去 90 天 stock_warnings 時間軸(含已解除,綠 / 紅 status badge 區分)
- requirements.txt 新增 `beautifulsoup4>=4.12.0`

### Tests
- `tests/test_fetch_stock_warnings.py` 15 cases:民國 / 西元日期 normalize、4 種 parser fixture、disposition fallback、idempotent re-fetch、User-Agent 必填、schema 對齊 production
- `tests/test_warnings_filter.py` 15 cases:hard exclude / 過期 warning / soft penalty multiplier / kill-switch / caption 字串組成 / db helper 對齊
- `tests/test_notifier_warning_wire.py` 8 cases:結構性 import / call guards、_LAST_EXCLUDED_WARNINGS 模組級 cache、soft penalty 改 ml_prob 後排序自動往後、kill-switch
- `tests/test_page_stock_detail_warning_wire.py` 8 cases:section render guard、`_build_card_html` warnings 參數、AppTest 帶警示 sid 確認 section 出現、乾淨股 graceful skip

### Notes
- 主公昨天買到違約交割股,要實作 ⚠️ 警示股 filter 避免未來再撞 — root cause 是無資料源、無濾鏡、無 UI 提示三層全空
- 設計分硬擋 / soft 降權雙層:違約交割 + 全額交割(picks 真會卡停損 → 直接剔);注意 / 處置 / 變更交易方法(資訊性訊號 → soft 降權留在 picks 但排後面)
- TPEx 對應 endpoint 暫 TODO,後續 follow-up

---

## 2026-05-14 — vectorbt 回測引擎升級

### Added

**vectorbt 策略級 grid search**
- `requirements.txt` 新增 `vectorbt>=1.0.0`（`chore(deps): add vectorbt for grid-search backtesting`）
- 新 module `src/vbt_backtest.py`：`backtest_strategy_with_params(strategy, params_grid, start, end, universe)` → DataFrame[strategy, params_hash, total_return, sharpe, max_drawdown, win_rate, n_trades]，按 sharpe DESC 排（`feat(vbt): add vectorbt wrapper for grid-search on existing strategies`）
- DB schema `vbt_grid_results`（strategy + params_hash 雙 PK，UPSERT 允許重跑覆蓋）
- 新 CLI `scripts/vbt_grid_search.py`：baseline 對 `volume_breakout` 跑 4×4 = 16 組合 × 全 universe × 6 個月（`feat(vbt): run first grid search on volume_breakout strategy`）
- Streamlit「📊 策略歷史」頁加第 4 個 sub-tab「🎲 參數最佳化」顯示 grid 結果 + 最佳組合建議卡（**不自動覆蓋既有 production default**，主公手動採用）（`feat(streamlit): show vectorbt grid-search results in strategy-history page`）

### Tests
- `tests/test_vbt_backtest.py` 13 cases：params hash 穩定 / grid 展開 / exits clamp / portfolio_stats fixture / 完整 backtest / persist UPSERT / load top_n
- `tests/test_vbt_grid_wire.py` 7 cases：tab 4 個標籤、`_render_vbt_grid_tab` 對接 DB helper、schema 必要欄位、安全聲明守住

### Notes
- 既有 `src/backtest.py`（逐 pick simulate_outcome）不取代 — vectorbt 是「策略級多參數最佳化」工具，跟「pick 級停利停損模擬」是兩個維度
- volume_breakout 6 個月 baseline：最佳組合 `vbo_vol_ratio_min=1.5 / highest_lookback=3` Sharpe 1.77 / WR 100%（但僅 3 trades，樣本仍小，需後續其他 16 策略補進 grid 才有比較基準）
- pandas 從 3.0.2 自動 downgrade 到 2.3.3（vectorbt 1.0 兼容性），仍符合 `pandas>=2.2.0`

---

## 2026-05-13 — May 2026 feature wave

### Added

**千張大戶 MVP 上線**
- TDCC 集保「股權分散表」weekly pipeline（`feat(shareholder): add weekly TDCC chip-concentration pipeline`）
- DB schema `shareholder_concentration`（sid / week_end / total_holders / top1000_ratio / delta_w）
- `data/twse_snapshot/shareholder_concentration.csv` 跟著 commit 進 repo
- 長線 pick 卡片 + 個股頁顯示集中度（`feat(streamlit): add shareholder concentration to long-term pick card`）
- Notifier 長線推播帶上集中度欄位（`feat(notifier): show shareholder concentration in long-term picks`）
- Cross-sid ranking helpers，供大戶入場頁查詢（`feat(database): add cross-sid shareholder ranking queries`）
- 獨立 weekly workflow `.github/workflows/weekly-shareholder-fetch.yml`，**週日 02:00 Asia/Taipei** 抓 + commit（避免拖累每日推播鏈）
- TDCC qryStock 12 週 backfill 給 theme stocks 補歷史（`feat(backfill): add TDCC qryStock 12-week backfill for theme stocks`）

**👥 大戶入場頁**
- 新 Streamlit page 含 3 ranking tabs：當週進場排行 / 連續加碼 / 加碼幅度（`feat(streamlit): add big buyer page with 3 ranking tabs`）
- E2E boot smoke 涵蓋（`test(e2e): include big-buyer + tracking pages in boot smoke`）
- 結構性 guards（`test(streamlit): structural guards for big buyer page`）

**📊 強者跟蹤頁面 + ✨ 高信心精選**
- 新 Streamlit page 4 tabs（強者領先 / 領先轉強 / 反轉訊號 / 高信心精選）（`feat(streamlit): add strong-follower comprehensive page`）
- Helper module 抽出（`feat(strong-follower): add helpers for strong-follower page`）
- **Market regime banner**：自動判 bull / neutral / bear，bear 時 top-n trim（`feat(strong-follower): add market regime banner + bear-market top-n trim`）
- 高信心精選 tab：DB premium helper 找 top picks（`feat(database): add strong-follower premium helper for high-confidence top picks`），Telegram 推播帶 ✨ premium section（`feat(notify): add high-confidence premium picks section to Telegram`）
- E2E + 結構性 guards（`test(strong-follower): e2e AppTest for regime banner rendering` + 兩個 structural guards）

**big_holder_inflow strategy**
- 加進 chip-flow category（`feat(strategy): add big_holder_inflow strategy to chip-flow category`）
- Strategy count 16 → 17（`test(e2e): bump strategy count 16 → 17 for big_holder_inflow`）
- Notifier wire 結構性守護（`test(notify): structural guards for big_holder_inflow daily-notify wire`）

**概念股 YAML universe（9 主題）**
- AI + 台積電供應鏈（`feat(themes): add AI + TSMC supply concept stock YAML mappings + TDCC probe scripts`）
- 矽光子（`feat(themes): add silicon photonics concept stock YAML mapping`）
- T1-T6 一波加完：CoWoS / HBM / 機器人 / 重電 / 軍工 / 衛星（`feat(themes): add T1-T6 concept stock YAML (cowos/hbm/robot/grid/defense/leo)`）

**產業 pre-filter pills**
- Streamlit `st.pills` 取代後置 multiselect（`refactor(streamlit): replace post-filter multiselect with pre-filter pills`）
- 15 主流產業 pinned + expander「其他」收納長尾（`chore(industry-filter): exclude ETF/其他 from mainstream pills`）
- Canonical map + filter helpers + universe pre-filter（`feat(industry-filter): add canonical map + filter helpers` + `add pre-filter universe helpers + mainstream constant`）
- 加進 dashboard short-term top 3 + 短線頁（`feat(streamlit): add industry filter to ...`）

**短線頁「📋 顯示全部」escape hatch**
- 高信心 filter 太嚴格時的旁路（`feat(short-page): add 'show all picks' escape hatch under confidence filter caption`）

### Changed

- streamlit 升版以支援 `st.pills`（`chore(deps): bump streamlit for st.pills support`）
- ML wire path 加 regression test（`test(notifier): guard ml_predictor wire path against silent breakage`）

### Fixed

- TDCC opendata `_LEVEL_TOTAL` 偵測誤判 16-level（總計列），baseline 2026-05-08 重抓（`fix(shareholder)` 系列 3 commits + `chore(shareholder): refetch 2026-05-08 baseline with fixed parser`）
- TDCC fetcher 切到能用的 opendata endpoint（`fix(shareholder): point TDCC fetcher to working opendata endpoint`）
- TDCC verify=False 對齊其他 gov endpoint（`fix(shareholder): use verify=False for TDCC like other gov endpoints`）
- TDCC qryStock backfill 加 resilient retry + cooldown，token per-POST refresh 避開 throttling（`fix(backfill)` 兩 commits）
- 系統頁 `_render_system_health` UnboundLocalError（`fix(app): resolve UnboundLocalError in _render_system_health`）
- 產業 filter SQL/conn handling for cloud env（`fix(industry-filter): repair filter_sids_by_industry SQL/conn handling for cloud env`）
- `news_fetcher` 測試對齊 HTML 格式漂移（`test(news_fetcher): align test with HTML format from 842b196`）

### Docs

- 大戶偵測 feature scope（`docs: add big-buyer detection feature scope report`）
- 大哥 broker-flow feature scope（`docs: add dage broker-flow feature scope report`）

---

## Earlier versions

更早的歷史不 backfill。需要查特定功能加入的時間點：

```bash
git log --oneline --all -- <檔案路徑>
git log --grep="<keyword>" --oneline
```

例：`git log --grep="ml_predictor"`、`git log --oneline -- src/strategies.py`。
