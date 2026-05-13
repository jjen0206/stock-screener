# Changelog

按主題 group，最新放上面。日期 = commit 日，非 release tag。
歷史更早的變更不在此列，需要溯源走 `git log`。

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
