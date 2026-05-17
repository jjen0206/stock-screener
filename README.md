# Stock Screener — 多策略台股推薦系統

> 個人使用、零成本的 Streamlit Dashboard。把「短線量價技術 + 中長線基本面 + 籌碼面 + ML 勝率預測 + 警示股風險標註」整合在一個手機 App 般的介面。

**這不是投資建議工具。** 是「資料整理 + 訊號掃描」,最終決策還是主公自己判斷。

---

## 是什麼 / 為什麼存在

| 問題 | 解法 |
|---|---|
| 17 套策略各跑各的,看不完 | **跨策略共識** + ML 排名,推 Top N |
| 大盤多空全照同樣強度推 | **TAIEX regime gating**:多頭推 10 檔 / 盤整 5 檔 / 空頭 2 檔 |
| ML 顯「勝率 70%」實際 50% | **isotonic probability calibration** 校正過 |
| 違約交割 / 全額股偷偷躺在 picks 裡 | **annotate-only filter**:標 ⚠️ + 軟降權,不替主公做隱藏決定 |
| 冷題材成分股還在推 | **題材熱度 5 日動能** 算 heat_score,冷題材 hard exclude |
| 卡片只顯結果不顯原因 | **SHAP 解釋** 每張 pick 列 Top 3 features + 貢獻方向 |
| 每張卡片都得自己重算指標 | **nightly precompute** → SQLite `daily_picks` 表,App 端 0ms 命中 |

目標:打開手機點主畫面圖示,1 秒內看到當日 ✨ 高信心精選 + Top N 推薦。

---

## Quick Start(本機)

```bash
git clone <repo-url> stock-screener
cd stock-screener

pip install -r requirements.txt          # production
pip install -r requirements-dev.txt      # 跑 pytest / ruff 才裝

cp .env.example .env                      # 填 FINMIND_TOKEN 等
streamlit run app.py
```

開瀏覽器到 `http://localhost:8501`。iPhone Safari「加到主畫面」就當 App 用。

第一次 boot 會從 `data/twse_snapshot/*.csv` 把 nightly 預跑結果 preload 進 SQLite cache(無需立刻打 API),5 秒內可看推薦。

---

## Streamlit Pages(19 個)

| 分組 | 頁面 | 用途 |
|---|---|---|
| **首頁** | 🏠 首頁 | 短線 Top 3 + 長線 Top 3 + 大盤 regime banner |
| **推薦** | 🔥 短線 | 11 個量價/技術策略 × ML 過濾 × 產業 pre-filter pills |
|  | 💎 長線 | 基本面 + 估值 + 殖利率 + 千張大戶 |
|  | ⭐ 關注 | 手動關注的 sid,自動跑所有策略對位 |
| **個股** | 🔍 個股 | 單 sid K 線 + KD/MACD/RSI + 法人 |
|  | 📊 個股深度 | **互動 plotly K 線**(MA20/60 + Volume + RSI/MACD/KD/Stoch + BB) + ⭐ picks 標記 / 🎯 持倉價位 / 千張大戶趨勢 + ⚠️ 警示紀錄 + SHAP |
| **市場** | 🌡️ 市場熱度 | 漲跌家數 / 量能榜 |
|  | 📊 大盤 | TAIEX 走勢 + breadth indicators |
|  | 👥 大戶入場 | TDCC 千張大戶 3 tabs |
|  | 📊 強者跟蹤 | 4 tabs(強者領先/領先轉強/反轉/✨ 高信心)+ regime banner |
| **回測** | 📈 回測 | 對 picks 跑 d1/d3/d5/d10 模擬 |
|  | 📊 策略歷史 | 4 sub-tabs,含 vectorbt grid search 結果 |
|  | 📈 績效分析 | **D 真實交易 log + 策略 attribution + 組合回測**:user_positions 已平倉的 equity curve / drawdown / Sharpe + 每筆平倉歸因到 daily_picks 命中策略 + multiselect 策略(聯集/交集)+ holding_days 回測 + Jaccard 相關性 heatmap |
| **追蹤** | 💼 交易紀錄 | 手動倉位 + 損益 + 勝率 |
|  | 🛡️ 持倉管理 | **真倉風險管理:ATR 停損停利 + drawdown 警報 + Kelly 部位建議** |
|  | 🚨 警報設定 | **G 個股價格警報:價位 ≥/≤ / 漲跌幅 % / 除權息,30 分 cron 自動推 Telegram + Discord** |
|  | 🧪 實測追蹤 | paper trades(自動 entry,30 分 cron 觸發進場/停損/突破 alerts)|
| **系統** | 📋 系統結論 / ⚙️ 系統 / ⚙️ 設定 | 資料新鮮度 / ML 模型狀態 / token 設定 |

Mobile-first 設計 — 主公 iPhone Safari「加到主畫面」當 App 用,全域 CSS 字體放大 1.4×、卡片寬度自適應,警示 badge 在窄屏不被截斷。

---

## 🛡️ 風險管理 / 部位管理

主公拍板「不只挑股,更要管錢」(2026-05-17)— 推播 / 持倉頁加軍師建議。

**Kelly 部位建議**(`src/position_sizing.py`):
- 把 ml_prob(經 isotonic calibration 後)當 win_rate proxy + 最近 30 天歷史 win/loss ratio 算 Kelly
- 1/4 Kelly(`kelly_multiplier=0.25`)— 業界共識,full Kelly 估錯參數就翻車
- 單檔上限 20%(`max_single_pct`)— 不重押 + 弱訊號 weak 再 × 0.5
- 推播每檔 pick 加 `💰 軍師建議:投入總部位 X%(~N 張)`

**ATR 停損停利**(`src/risk_management.py`):
- 停損 = entry − ATR(14) × 2.0(預設)
- 停利 = entry + ATR(14) × 4.0(預設,2:1 R:R)
- 持倉頁建倉 form 自動算,主公可手動覆寫
- 推播每檔 pick 加 `🎯 停損 X / 停利 Y`

**Drawdown 警報**:
- 整體 P&L(realized + unrealized)vs 總 invested
- > 10% loss 黃燈(警告暫停加碼)/ > 20% loss 紅燈(軍師建議停手 + 全面檢視)
- daily-notify(22:13)+ morning-brief(08:30)都檢查並推播

**Kill-switch**:
- `POSITION_SIZING_ENABLED=false` → 軍師建議行不出現
- `RISK_MGMT_ENABLED=false` → 停損停利不自動算 + drawdown 警報關閉

### B 進場 / 出場時機強化(2026-05-17)

**K 線形態判讀(`src/candlestick_patterns.py`)** — 6 形態:三紅兵、槌子線、看漲吞噬、晨星、旗形、十字星。
- 每形態回 `{label, bias (bull/bear/neutral), confidence (★/★★/★★★)}`
- 推播 pick block 顯 top-2 bull bias:`📊 形態: 三紅兵(★★) · 槌子線(★)`
- App「📊 個股深度」加 K 線形態 section,掃近 30 日各形態出現次數

**動態停損(`src/trailing_stop.py`)**:
- 規則:`new_stop = max(原 stop, HWM − ATR × multiplier)`(only-up 永遠不下移)
- 觸發門檻:`current_price ≥ entry + 1×ATR` 才上移(避免進場馬上拉緊停損)
- `daily_notify` 結尾 batch update 所有 open positions → 推播「📈 動態停損已上移」section
- 持倉管理頁可手動觸發 / 自動觸發 toggle

**獲利了結警報(`src/take_profit_alerts.py`)**:
- 達 take_profit / stop_loss / trailing_stop → 強警報(分 severity)
- 分批了結建議:+5% 建議賣 1/3,+10% 再賣 1/3,留 1/3 跑趨勢
- `morning_brief` 開頭強警報 section(TP/SL 達標主公看到就要處理)

**Kill-switch**:
- `PATTERN_DETECTION_ENABLED=false` → K 線形態行 + 個股深度頁 section 都不顯
- `TRAILING_STOP_ENABLED=false` → 動態停損不自動更新
- `TAKE_PROFIT_ALERT_ENABLED=false` → 達標警報 section 不發

---

## 🚨 G 個股價格警報

主公拍板「持倉股不能等下次推播才知道急殺,主動設好條件、cron 跑時自動推」(2026-05-17)。

**支援類型**(在「🚨 警報設定」頁設):
- `price_above` — 當前 ≥ 目標價(突破時推)
- `price_below` — 當前 ≤ 目標價(跌破時推)
- `pct_change` — |當前 − 基準| / 基準 ≥ X%(notes 寫 `base=600` 當基準)
- `ex_dividend` — N 日內除權息(從 `dividend.ex_dividend_date` 算)
- `intraday_drop` — 持倉股當日跌幅 ≤ -3%(系統自動,無需手動設)

**運作流程**:
- 每 30 分鐘 `intraday-alerts.yml` cron 跑 `scripts/intraday_alerts.py`
- engine(`src/price_alerts.py`)對 active alerts 比對最新 daily close
- 觸發 → 寫 `alert_dedup`(同日去重)+ 推 Telegram + Discord + `mark_triggered`(一次性)
- daily-notify(22:13)+ morning-brief(08:30)推播都加「🚨 警報快訊」section

**Kill-switch**:`PRICE_ALERT_ENABLED=false` → engine 全部回 []、不推任何 alert

---

## 17 套策略

| 類別 | 策略 |
|---|---|
| **趨勢** | `ma_alignment`(多頭排列)、`macd_golden`(MACD 黃金交叉)、`ma_squeeze_breakout`(均線糾結突破)|
| **反轉** | `bias_convergence`(乖離收斂)、`bb_lower_rebound`(布林下軌反彈)、`rsi_recovery`(RSI 回升)|
| **籌碼** | `inst_consensus`(三大法人連買)、`inst_silent_accum`(主力默默吸貨)、`inst_oversold_reversal`(法人反轉)、`big_holder_inflow`(千張戶進場)|
| **動能** | `volume_kd`(量價 KD)、`volume_breakout`(量爆突破)、`gap_up`(跳空缺口)|
| **基本面** | `eps_acceleration`(EPS 加速)、`revenue_acceleration`(營收加速)|
| **殖利率** | `high_yield_stable`(高殖利率穩健)|
| **大盤相對** | `taiex_alpha`(獨立行情)|

每套策略獨立 RF + isotonic calibration + per-strategy ML threshold(`STRATEGY_ML_THRESHOLDS` in `src/strategies.py`)。大盤 regime 控制哪些 category 開放(bull 全開 / bear 只剩籌碼+殖利率+大盤)。

詳細策略邏輯與 ML 設計見 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 資料源(全部免費)

| 資料 | 來源 |
|---|---|
| 日線 OHLC + 法人 | FinMind 免費版 / TWSE OpenAPI v1 |
| 季 EPS / PE / PB | TWSE OpenAPI(`BWIBBU_d` + `t187ap14_L`)|
| ROE | PB/PE 反推(Du Pont 簡化,TSMC 驗證 ROE ≈ 31.7% 對齊市場 28-30%)|
| 配息 | FinMind `TaiwanStockDividend` |
| 千張大戶集中度 | TDCC opendata(每週)+ qryStock(12 週 backfill,token-per-POST refresh)|
| 重大訊息新聞 | TWSE `t187ap04_L` OpenAPI(每小時 cron)|
| 警示股(處置/注意/變更/全額)| TWSE `/v1/announcement/*` JSON + TPEx `tpex_*` |
| **違約交割股** | **MOPS RSS** `mopsrss201001.xml`(2026-05-16 加入,關掉 silent miss)|
| 法人目標價 | yfinance Analyst Estimates(A)+ Gemini news 解析(B fallback)|
| 概念股 universe | `data/themes/*.yaml`(9 主題:AI、台積電供應鏈、矽光子、CoWoS、HBM、機器人、重電、軍工、衛星)|

省掉 FinMind Backer NT$699/月,所有 endpoint 都走免費路徑。

---

## 自動化(17 個 GitHub Actions cron)

| Workflow | 時間 | 用途 |
|---|---|---|
| `daily-notify.yml` | 週一~五 **22:13 Asia/Taipei** | fetch → precompute → backtest yesterday → push Telegram + Discord(含 ✨ 高信心精選 + 大戶 + 昨日複盤)|
| `daily-notify-only.yml` | manual | 不重 fetch,只跑推播(快速 dry-run)|
| `morning-brief.yml` | 平日 08:30 TPE | 開盤前 30 分鐘 — 重抓警示 + news,推「警示更新 / 新增 picks / 美股情緒」變動;無變動推極簡訊。kill-switch `MORNING_BRIEF_ENABLED=false` |
| `morning-refetch.yml` | 平日 9:00 TPE | 早盤前重抓 TWSE 市場資料 |
| `news-notify.yml` | 每小時 | 重大訊息掃 + 白名單推播 |
| `stock-warnings.yml` | 平日 17:13 TPE | TWSE/TPEx 警示股 + MOPS 違約交割 |
| `intraday-alerts.yml` | 每 30 分鐘 | active paper_trades 觸停損/進場/突破 → Telegram |
| `data-health-alert.yml` | 每日 | 主要 table 新鮮度檢查,超過閾值推警告 |
| `weekly-shareholder-fetch.yml` | 週日 02:00 TPE | TDCC 千張大戶週快照 |
| `weekly-targets.yml` | 週日 | 全市場法人目標價 |
| `weekly-brief.yml` | 週日 | 週度系統 brief |
| `backtest-weekly.yml` | 週日 | 全策略 d1/d3/d5/d10 回測 |
| `ml-weekly-retrain.yml` | 週日 03:00 TPE | 通用 + per-strategy 模型 walk-forward A/B gate(ROC < 舊 -0.02 → rollback)|
| `retrain-ml.yml` | manual only | 緊急 random-split gate(2026-05-16 從 schedule 移除避免覆蓋 weekly)|
| `backfill-*.yml`(5 條)| manual | history / institutional 22 月 / dividend / revenue / financials / pick_shap 一次性補抓 |

22:13 是故意的非整點 — 避開 GitHub Actions 全球整點高峰排程延遲。

---

## 環境變數(`.env.example`)

```ini
FINMIND_TOKEN=                # 留空走免費 OpenAPI 路徑
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DISCORD_WEBHOOK_URL=          # 備援,Telegram 失敗時補
DATABASE_PATH=data/cache.db
DEFAULT_MARKET=TW
```

可選 kill-switch(全預設 on):

```ini
WARNING_ANNOTATE_ENABLED=true      # 警示股標註 + 軟降權
STRATEGY_CONSENSUS_ENABLED=true    # 跨策略共識 ×1.05~1.15
REGIME_GATING_ENABLED=true         # 大盤 regime 縮量 + ML threshold uplift
THEME_HEAT_ENABLED=true            # 題材熱度 5 日動能,冷題材 hard exclude
ML_CALIBRATION_ENABLED=true        # isotonic probability 校正
POSITION_SIZING_ENABLED=true       # Kelly 軍師部位建議行
RISK_MGMT_ENABLED=true             # ATR 停損停利 + drawdown 警報
PATTERN_DETECTION_ENABLED=true     # K 線形態判讀(三紅兵 / 槌子 / 吞噬 / 晨星 / 旗形 / 十字星)
TRAILING_STOP_ENABLED=true         # 動態停損(only-up)+ daily-notify batch update
TAKE_PROFIT_ALERT_ENABLED=true     # TP/SL/trailing 達標警報 + 分批了結建議(+5%/+10%)
PRICE_ALERT_ENABLED=true           # G 個股價格警報 + 持倉急殺 + 除權息提醒
PERFORMANCE_ENABLED=true           # D 績效分析 + 策略 attribution + 組合回測
```

每個出事可立刻設 `=false` 退化整個 module。

---

## 部署到 Streamlit Community Cloud(免費)

1. push 到 GitHub(public / private 都行)。
2. [share.streamlit.io](https://share.streamlit.io) → Create app → 選 repo → branch `main` → Main file `app.py`。
3. **Advanced settings → Python version: 3.11**。
4. **Settings → Secrets** 貼 `.streamlit/secrets.toml.example` 內容。
5. Deploy。容器重啟會清 SQLite cache,boot 時走 `data/twse_snapshot/*.csv` preload 還原。

雲端 IP 被 TWSE 擋,所以資料抓取統一交給 GitHub Actions(Azure IP 不被擋),雲端 App 只讀 commit 進來的 CSV snapshot。

---

## 專案結構

```
.
├── app.py                       # Streamlit 入口(17 pages 一個檔)
├── src/                         # 業務邏輯
│   ├── data_fetcher.py          # FinMind / TWSE OpenAPI
│   ├── database.py              # SQLite schema + helpers(3.6k 行,15 表)
│   ├── strategies.py            # 17 個 screen_* + metadata
│   ├── ml_predictor.py          # RF + per-strategy routing
│   ├── ml_calibration.py        # isotonic / platt probability calibration
│   ├── ml_shap.py               # TreeExplainer SHAP cache
│   ├── ml_walkforward.py        # expanding-window CV + A/B gate
│   ├── notifier.py              # Telegram / Discord push orchestration
│   ├── discord_notifier.py      # Discord webhook sender
│   ├── warnings_filter.py       # annotate-only 警示股標註
│   ├── consensus.py             # 跨策略共識 multiplier
│   ├── theme_heat.py            # 題材熱度 hard exclude
│   ├── regime_gating.py         # TAIEX bull/range/bear → 推薦數縮量
│   ├── market_regime.py         # 4-tier regime → strategy category filter
│   ├── universe.py              # pure_stock / with_etf / TW_TOP_50
│   ├── indicators.py            # KD / MACD / RSI / BBands
│   ├── intraday.py              # 即時報價(yfinance fallback)
│   ├── individual_sections.py   # 個股深度 page 元件
│   ├── ui_cards.py / ui_format.py
│   └── ...
├── scripts/                     # cron 入口 + 一次性 backfill
│   ├── daily_notify.py          # 推播 main
│   ├── precompute_strategies.py # nightly 17 策略預跑
│   ├── daily_market_update.py   # 全市場財報 / TWSE 重抓
│   ├── backfill_*.py            # history / dividend / financials / institutional / pick_shap
│   ├── intraday_alerts.py       # 30 分 cron paper_trades 觸發 alerts
│   ├── eval_walkforward.py      # ML walk-forward CV
│   ├── vbt_grid_search.py       # vectorbt 策略 grid
│   └── audit/                   # 校準 / A/B 比較
├── tests/                       # pytest(1400+ tests)
├── data/
│   ├── cache.db                 # SQLite,gitignore
│   ├── themes/                  # 9 主題 YAML
│   └── twse_snapshot/           # nightly dump CSV → commit → cloud preload
├── models/
│   ├── short_pick.pkl + .meta.json    # 通用 RF
│   ├── per_strategy/                  # samples ≥ 100 才訓
│   └── calibrators/                   # isotonic 校正器
├── .github/workflows/           # 17 cron + 5 backfill
└── docs/
    ├── PRD.md
    ├── TASKS.md
    ├── ARCHITECTURE.md          # 高階架構 + DB schema + kill switches
    ├── CHANGELOG.md
    └── *-audit-*.md             # 健診紀錄
```

---

## ML 勝率預測(Stage 2B v2 + isotonic calibration)

每張 pick 卡片右上「🤖 N%」是校正後 AI 勝率。**「高信心模式」toggle** 用 per-strategy threshold 過濾低分 pick。

兩層模型,inference 優先 per-strategy → fallback 通用:

```
models/
├── short_pick.pkl              # 通用 RF
├── short_pick.meta.json
├── per_strategy/               # samples ≥ 100 才 trained
│   ├── ma_alignment.pkl + .meta.json
│   └── ...
└── calibrators/                # 跟 base model 同生命週期
    └── ma_alignment.pkl
```

每週日 03:00 TPE 由 `ml-weekly-retrain.yml` 跑 walk-forward 重訓,ROC AUC < 舊 -0.02 → rollback 不 commit 新 model。

`STRATEGY_ML_THRESHOLDS`(實測 winner):ma_alignment 0.55、bias_convergence 0.65、macd_golden 0.60、bb_lower_rebound 0.50、volume_breakout 0.65。`gap_up` 2026-05-15 下架 ML 過濾(WF ROC 0.4926 ≈ random),改 rule-based `gap_vol_ratio_max=3.0` 收緊到 sweet spot。

---

## 變更紀錄

詳見 [docs/CHANGELOG.md](docs/CHANGELOG.md)。架構深度文件:[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 常見問題

**Q: 雲端開頁要 10 秒?**
A: 短線頁按執行 > 1s 通常代表 nightly precompute 沒 commit `daily_picks.csv`。檢查 GH Actions `Daily Telegram Push` 是否綠燈、`data/twse_snapshot/daily_picks.csv` 最後 commit 日期。

**Q: 想加新策略?**
A: 在 `src/strategies.py` 加 `screen_xxx(...)` → 補進 `STRATEGY_LABELS` / `STRATEGY_CATEGORY` / 必要時 `STRATEGY_RR_PARAMS` / `STRATEGY_ML_THRESHOLDS`。`tests/test_strategies.py` 加 case。

**Q: Claude Code 改了我不想改的檔案?**
A: `git diff` 檢查 → `git checkout -- <file>` 還原。全程 git 追蹤。

**Q: 違約股保護到底擋不擋?**
A: **不擋,只標註**。SEVERE(違約/全額)走 `ml_prob × 0.3` 沉到末段但仍顯;SOFT(注意/處置/變更)走 `× 0.7`。理由:主公規矩「主動提示風險,但不替主公做隱藏決定」。

---

## 風險警語

本工具僅為**個人研究與資料整理**用途,**不構成任何投資建議**。歷史資料和回測表現不保證未來獲利,投資請自行評估風險。

---

## License / Contact

個人專案,不對外發佈。
