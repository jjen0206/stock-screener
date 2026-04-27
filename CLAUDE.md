# CLAUDE.md

> 此檔案會被 Claude Code 啟動時自動讀取,作為整個專案的最高行為準則。
> 修改此檔案前請先確認影響範圍。

## 專案目標

打造一個**個人使用、零成本**的台股 / 美股選股工具,同時支援:
- **短期炒股**:基於價量、技術指標、籌碼面的日線級別選股
- **長期投資**:基於財報、估值、現金流的基本面選股

最終形態是 Streamlit Dashboard,使用者透過手機瀏覽器(加到主畫面)使用。
**不做原生 App**,理由詳見 `docs/PRD.md` 第 2 節。

## 技術棧 (鎖定,不要更換)

- **語言**:Python 3.11+
- **介面**:Streamlit
- **資料來源**:FinMind(台股主力)+ yfinance(美股)
- **資料庫**:SQLite(本機快取)
- **圖表**:plotly(K 線圖)+ Streamlit 原生元件
- **通知**:LINE Notify(可選)
- **依賴管理**:uv(優先)或 pip + requirements.txt

## 行為準則

### 1. 溝通風格
- 使用**繁體中文**回應使用者(使用者偏好設定)。
- 程式碼註解也用繁體中文,變數與函式名用英文。
- 簡短直接,先給結論再給細節。
- 不需要每次都說「好的、沒問題」這類客套話。

### 2. 開發流程
- **必看 TASKS.md**:開始任何工作前,先讀 `docs/TASKS.md`,確認當前任務狀態。
- **任務驅動**:完成一個任務就更新 TASKS.md(打勾、加註備註),不要一次做完才更新。
- **小步提交**:每完成一個可運行的模組就停下來給使用者驗證,不要悶頭做完整個專案。
- **真實驗證**:寫完程式碼必須實際執行(`python -m pytest` 或 `streamlit run`),不要只說「應該能跑」。

### 3. 程式碼品質
- 每個模組(.py 檔)都要有 docstring 說明用途。
- 函式參數需有 type hint。
- 測試:核心邏輯(指標計算、選股條件)需有 pytest 單元測試。
- **禁止過度工程**:這是個人工具,不需要 OAuth、不需要微服務、不需要 Docker(除非使用者要求)。
- 抓資料一律走快取邏輯:先查 SQLite,沒有才打 API。

### 4. 安全與隱私
- API token 一律放 `.env`,絕不寫死在程式碼裡。
- `.env` 必須加進 `.gitignore`。
- 提供 `.env.example` 給使用者參考。

### 5. 不要做的事
- 不要主動建立 Docker、CI/CD、k8s 設定(除非任務明確要求)。
- 不要使用付費 API(Polygon、Alpha Vantage 付費版等)。
- 不要寫 React / Flutter / 任何前端框架。
- 不要設計登入、會員、訂閱機制。
- 不要產出大量 Markdown 文件,除非使用者要求。

## 目錄結構約定

```
專案根目錄/
├── CLAUDE.md              # 本檔案
├── README.md              # 使用者操作手冊
├── .env.example           # 環境變數範例
├── .gitignore
├── requirements.txt       # 或 pyproject.toml
├── docs/
│   ├── PRD.md            # 產品需求
│   └── TASKS.md          # 任務清單(可勾選)
├── src/
│   ├── __init__.py
│   ├── config.py         # 設定載入
│   ├── data_fetcher.py   # FinMind / yfinance 資料抓取
│   ├── database.py       # SQLite 快取邏輯
│   ├── indicators.py     # KD / MACD / RSI 等指標
│   ├── screener_short.py # 短線選股邏輯
│   ├── screener_long.py  # 長線選股邏輯
│   └── notifier.py       # LINE Notify(可選)
├── tests/
│   └── test_*.py
├── data/
│   └── cache.db          # SQLite 檔案(gitignore)
└── app.py                # Streamlit 入口
```

## 任務開始檢查清單

收到使用者指令時,依序確認:

1. 讀 `docs/TASKS.md` 確認任務狀態
2. 確認當前任務的「驗收條件」
3. 規劃要動哪些檔案,先告訴使用者預計改動範圍
4. 動手實作 + 測試
5. 更新 TASKS.md
6. 給使用者一個**可立即執行的指令**做驗證(例如 `streamlit run app.py`)

## 與使用者的互動慣例

- 使用者不一定每次都看完所有輸出,**重要結論放最前面**。
- 給長指令時用程式碼區塊,讓使用者可以直接複製。
- 卡住的時候直接問,不要瞎猜。可以問的問題例如:「FinMind token 是否已申請?」「你想先做台股還是美股?」
- 完成階段性任務後,主動建議下一步。
