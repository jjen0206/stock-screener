# Stock Screener — 個人選股工具

> 個人使用、零成本的台股 / 美股選股 Dashboard

## 專案結構

本專案使用 Claude Code 協作開發,所有設計文件在 `docs/`,Claude 會自動讀取 `CLAUDE.md` 作為行為準則。

```
.
├── CLAUDE.md           # Claude Code 規範(自動讀取)
├── README.md           # 本檔案
├── docs/
│   ├── PRD.md         # 產品需求
│   └── TASKS.md       # 任務清單(可勾選追蹤)
├── src/                # 程式碼
├── tests/              # 測試
├── data/               # SQLite 快取
└── app.py              # Streamlit 入口
```

## 開發前準備

### 1. 申請 FinMind Token(免費)

到 [FinMind 官網](https://finmindtrade.com/) 註冊帳號,登入後在會員頁面取得 API token。
免費版額度為 1500 次/小時,個人使用綽綽有餘。

### 2. 安裝 Claude Code

```bash
# macOS / Linux
curl -fsSL https://claude.ai/install.sh | sh

# 或使用 npm
npm install -g @anthropic-ai/claude-code
```

(請以 Anthropic 官方文件為準)

### 3. 初始化專案

```bash
cd <你想放專案的位置>
# 把這個交付包解壓縮到此處
claude
```

進入 Claude Code session 後,輸入以下指令啟動:

```
請讀 CLAUDE.md 與 docs/TASKS.md,然後從 T0.1 開始執行第 0 階段。
```

## 開發流程建議

### 每次開啟 session 時

1. Claude 會自動讀 `CLAUDE.md`
2. 你只需說:**「請接著 TASKS.md 上未完成的任務繼續」**
3. Claude 會檢查狀態、規劃變動、開始實作

### 階段性驗收

每完成一個階段(T0、T1、T2…)都會停下來等你驗證。建議:

- 不要一次讓 Claude 跑完所有任務,中間驗收能避免後期大改。
- 跑不過、結果不對時,直接告訴 Claude「T2.1 的 KD 計算結果跟券商不符,請檢查」。
- 想改變方向時,直接修改 `docs/PRD.md` 或 `docs/TASKS.md`,Claude 下次 session 會跟著調整。

### 常用指令範例

```
# 開始新任務
請執行 TASKS.md 第 1 階段的所有任務,完成後停下來讓我驗收。

# 修改方向
我想把短線策略從「量價突破」改成「乖離率收斂」,請更新 PRD 和 screener_short.py。

# 除錯
剛剛跑 streamlit run app.py 出現錯誤:[貼錯誤訊息],請排查。

# 加新功能
請執行 G1.1,但用 Telegram Bot 取代 LINE Notify(LINE Notify 已停服)。
```

## 執行專案(等程式寫好後)

```bash
# 啟動 venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate    # Windows

# 安裝相依套件
pip install -r requirements.txt

# 啟動 Streamlit
streamlit run app.py
```

開啟瀏覽器到 `http://localhost:8501`。
手機要使用,先把電腦 IP 加上 port(`http://192.168.x.x:8501`),
在 Safari/Chrome 開啟 → 加到主畫面。

## 常見問題

**Q: Claude Code 改了我不想改的檔案怎麼辦?**
A: 用 `git diff` 檢查,不滿意就 `git checkout -- <file>` 還原。建議全程開 git 追蹤。

**Q: 想換成付費 API?**
A: 修改 `CLAUDE.md` 的「不要做的事」章節,並更新 `data_fetcher.py`。

**Q: 想加更多選股策略?**
A: 在 `src/` 下新增 `screener_xxx.py`,在 `app.py` 加入下拉選單即可。

## 風險警語

本工具僅為**個人研究與資料整理**用途,不構成任何投資建議。
歷史資料和回測表現不保證未來獲利,投資請自行評估風險。
