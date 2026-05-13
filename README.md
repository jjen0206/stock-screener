# Stock Screener — 主公的個人台股分析系統

> 個人使用、零成本的台股 Streamlit Dashboard，把短線/長線/籌碼/AI 預測整合在一個頁面裡。

不是投資建議工具，是「資料整理 + 訊號掃描」。最終決策還是主公自己判斷。

## 主要功能（Streamlit pages）

| 頁面 | 用途 |
|---|---|
| 🎯 **短線推薦** | 11 個量價/技術策略 + ML 勝率過濾，附產業 pre-filter pills + 「📋 顯示全部」escape hatch |
| 💎 **長線推薦** | 基本面 + 估值 + 殖利率 + 千張大戶集中度 |
| ⭐ **關注名單** | 手動加的觀察股，自動跑所有策略對位 |
| 🌡️ **市場熱度** | 漲跌家數、成交額分布、量能榜 |
| 📈 **大盤** | TAIEX 走勢 + breadth indicators |
| 👥 **大戶入場** | TDCC 千張大戶當週/連續/加碼排行 3 tabs |
| 📊 **強者跟蹤** | 4 tabs：強者領先 / 領先轉強 / 反轉訊號 / ✨ 高信心精選；附 regime banner（bull/neutral/bear）|
| 🔍 **個股** | 單一 sid K 線 + KD/MACD/RSI + 法人 + 千張大戶趨勢 |
| 📒 **交易紀錄** | 手動記倉位、計算損益、勝率統計 |
| 📐 **實測追蹤** | 過去 picks 的 N 日報酬回溯 |
| ⚙️ **系統** | 資料新鮮度、ML 模型狀態、cache hit rate |
| ⚙️ **設定** | API token、Telegram、ML 門檻 |

詳細欄位定義見 `docs/PRD.md`。

## 資料源（全部免費）

| 資料 | 來源 |
|---|---|
| 日線 OHLC + 法人 | FinMind 免費版 / TWSE OpenAPI |
| 季 EPS / PE / PB | TWSE OpenAPI（`BWIBBU_d` + `t187ap14_L`）|
| ROE | PB/PE 反推（Du Pont 簡化）|
| 配息 | FinMind `TaiwanStockDividend` |
| 千張大戶集中度 | TDCC opendata + qryStock（per-POST token refresh）|
| 概念股 universe | `data/themes/*.yaml`（9 主題：AI、台積電供應鏈、矽光子、CoWoS、HBM、機器人、重電、軍工、衛星）|

省掉 FinMind Backer NT$699/月。ROE 反推驗證：TSMC PE=32.99, PB=10.46 → ROE ≈ 31.7%，與市場公認 28-30% 高度吻合。

## 自動化排程（GitHub Actions）

| 排程 | 時間 | 用途 |
|---|---|---|
| **每日 Telegram 推播** | 週一~五 **22:13 Asia/Taipei** | 跑短線/長線 picks → Telegram + Discord，含 ✨ 高信心精選區段 |
| **每週六凌晨千張大戶** | 週日 **02:00 Asia/Taipei**（cron `0 18 * * 6` UTC）| 抓 TDCC 集保「股權分散表」上週五公布資料，commit `shareholder_concentration.csv` |
| **每週日 ML 重訓** | 週一 02:00 台北 | 通用 + per-strategy 模型，accuracy 退化 > 5pp 拒絕 commit |
| **盤中 news snapshot** | 每小時 | TWSE 重大訊息快照 |

22:13 那個非整點是故意的——避開 GitHub Actions 全球整點高峰排程延遲。

## 開發前準備

```bash
# 安裝相依
pip install -r requirements.txt

# .env 填 token（複製 .env.example）
FINMIND_TOKEN=...           # 留空可走免費 OpenAPI 路徑
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
DISCORD_WEBHOOK_URL=...     # 備援推播

# 跑起來
streamlit run app.py
```

開瀏覽器到 `http://localhost:8501`。手機加到主畫面就當 App 用。

## 部署到 Streamlit Community Cloud（免費）

1. push 到 GitHub（public/private 都行）。
2. [share.streamlit.io](https://share.streamlit.io) 登入 → Create app → 選 repo → branch `main` → Main file path `app.py`。
3. **Advanced settings → Python version: 3.11**。
4. **Settings → Secrets** 貼 `.streamlit/secrets.toml.example` 內容。
5. Deploy。容器重啟會清空 SQLite cache，首次查詢會重抓。

## Telegram 推播

1. 找 [@BotFather](https://t.me/BotFather) → `/newbot` → token。
2. 先傳一句話給 bot → 開 `https://api.telegram.org/bot<token>/getUpdates` 拿 `chat_id`。
3. Secrets / `.env` 填 `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`。
4. Sidebar 「📲 測試 Telegram」確認通。

Discord 備援同理，把 webhook URL 放進 `DISCORD_WEBHOOK_URL`。`notify_short_picks` 會同時推 Telegram + Discord，任一成功即 exit 0。

## 預跑策略加速（Daily Picks Precompute）

App 開啟跑 `run_all_strategies` 全市場 ~11s。改用 nightly 預跑 + SQLite cache 後，App 端 default 路徑 0ms 命中。

- `.github/workflows/daily-notify.yml` 22:13 跑 `scripts/precompute_strategies.py`
- 全 11 strategies × 3 universe（pure_stock / with_etf / top_50）寫進 `daily_picks` 表
- dump `data/twse_snapshot/daily_picks.csv` commit 進 repo
- 容器 redeploy → git pull → boot preload SQLite
- App 開頁讀 `daily_picks`，slider 改過才走 runtime

手動跑：
```bash
python scripts/precompute_strategies.py              # 當日
python scripts/precompute_strategies.py --date 2026-05-04
python scripts/precompute_strategies.py --backfill 30
```

## ML 勝率預測（Stage 2B）

每張 pick 卡片右上「🤖 N%」AI 勝率。**「高信心模式」toggle**用 per-strategy threshold 過濾低分 pick。

兩層模型，inference 優先 per-strategy → fallback 通用：

```
models/
├── short_pick.pkl              # 通用 RandomForest
├── short_pick.meta.json
└── per_strategy/               # samples ≥ 100 才 trained，否則 fallback
    ├── ma_alignment.pkl + .meta.json
    ├── bias_convergence.pkl + .meta.json
    └── ...
```

過濾門檻（`STRATEGY_ML_THRESHOLDS` in `src/strategies.py`）：ma_alignment 0.55、bias_convergence 0.65、macd_golden 0.60、bb_lower_rebound 0.50、volume_breakout 0.65、gap_up 0.60。同時命中多策略取最嚴格。

重新校準：
```bash
python scripts/audit/calibrate_ml_thresholds.py --use-per-strategy-models
python scripts/audit/compare_ml_modes.py --lookback 126
```

手動訓練：
```bash
python scripts/train_ml_model.py
python scripts/train_per_strategy_ml.py --lookback 200
python scripts/train_per_strategy_ml.py --strategy ma_alignment
```

## 千張大戶 Pipeline（2026-05 新增）

TDCC 集保提供兩條資料路徑，本系統兩條都用：

- **opendata（每週公布）**：~70 大型股，全市場 level-1~15 + 總計，從 `data/twse_snapshot/shareholder_concentration.csv` 載入。`_LEVEL_TOTAL` 偵測有特別處理 16-level（總計列）。
- **qryStock（12 週 backfill）**：theme universe 補抓歷史，token-per-POST 刷新避開 throttling，cooldown retry。

DB schema 寫進 `shareholder_concentration` 表，欄位含 sid / week_end / total_holders / top1000_ratio / delta_w（與上週 diff）。長線卡片、`👥 大戶入場` page、`big_holder_inflow` strategy 都吃這份資料。

## 概念股 YAML universe

`data/themes/` 9 主題 YAML mapping：
- `ai_concept.yaml`, `tsmc_supply.yaml`, `silicon_photonics.yaml`
- `cowos_advanced_packaging.yaml`, `hbm_memory.yaml`
- `humanoid_robot.yaml`, `heavy_electric_grid.yaml`
- `defense_military.yaml`, `low_earth_orbit.yaml`

提供策略 pre-filter universe，避免每次都跑全市場 ~2360 檔。

## 變更紀錄

詳見 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

## 常見問題

**Q: Claude Code 改了我不想改的檔案？**
A: `git diff` 檢查 → `git checkout -- <file>` 還原。全程開 git 追蹤。

**Q: 想加更多策略？**
A: 在 `src/strategies/` 加新 strategy（參考 `big_holder_inflow.py`），register 到 `STRATEGY_REGISTRY`。

**Q: 雲端 cache miss？**
A: 短線頁按執行 > 1s = miss。檢查 nightly workflow 是否 commit `daily_picks.csv`、params 是否 default、universe 是否在預跑清單（pure_stock / with_etf / top_50）。

## 風險警語

本工具僅為**個人研究與資料整理**用途，**不構成任何投資建議**。歷史資料和回測表現不保證未來獲利，投資請自行評估風險。
