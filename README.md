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

## 部署到 Streamlit Community Cloud(免費)

1. 確認專案已推上 GitHub(public 或 private 皆可)。
2. 到 [share.streamlit.io](https://share.streamlit.io) 用 GitHub 帳號登入。
3. 點 **Create app** → 選 repo → branch `main` → **Main file path: `app.py`**。
4. **Advanced settings → Python version: 3.11**(雲端目前最高支援 3.11)。
5. **Settings → Secrets** 貼上 `.streamlit/secrets.toml.example` 的內容(FinMind 留空即無 token 模式)。
6. 點 **Deploy**,1–3 分鐘後拿到 `https://stock-screener-XXXXX.streamlit.app` 網址。

> 升級 FinMind token 後不必重新部署,只要回 Settings → Secrets 改 `FINMIND_TOKEN` 並 reboot app 即可。
> 雲端容器重啟會清空 SQLite cache,首次查詢會重抓。

## 免費版完整功能(省 NT$699/月)

**完全不用付費 token** 也能跑長線選股!資料來源:

| 資料 | 來源 | 成本 |
|---|---|---|
| 日線價格 + 三大法人 | FinMind 免費版 | 0 元 |
| **PE / PB / 殖利率** | **TWSE OpenAPI**(`/v1/exchangeReport/BWIBBU_d`) | 0 元 |
| **季 EPS** | **TWSE OpenAPI**(`/v1/opendata/t187ap14_L`) | 0 元 |
| **ROE** | **PB 反推**(Du Pont 簡化:ROE ≈ EPS_TTM/BVPS = PB/PE) | 0 元 |
| 歷年配息 | FinMind 免費版(`TaiwanStockDividend`) | 0 元 |

替代了 FinMind Backer **NT$699/月** 的付費 dataset(`TaiwanStockFinancialStatements`)。

ROE 反推驗證:TSMC PE=32.99, PB=10.46 → ROE = 10.46/32.99 ≈ **31.7%**,
與市場公認 TSMC 2024 ROE ≈ 28-30% 高度吻合。

使用方式:Streamlit 側欄按 **「📊 更新財報資料(免費版)」**,等 1-2 分鐘抓完 50 檔,
切「長線口袋名單」→ 按「執行長線選股」即可。

## Telegram 推播(每日選股自動傳)

### 1. 建 Bot 拿 token + chat_id
- 找 [@BotFather](https://t.me/BotFather) → `/newbot` → 拿 token
- 先傳一句話給你新 bot,再開 `https://api.telegram.org/bot<token>/getUpdates` 看 `result[0].message.chat.id`

### 2. 寫進 Streamlit Secrets / `.env`
```
TELEGRAM_BOT_TOKEN = "1234567890:AAH..."
TELEGRAM_CHAT_ID = "123456789"
```
雲端要 Reboot app;本機重啟 streamlit。Sidebar 出現「📲 測試 Telegram」按下確認通。

### 3. 設定每日排程(GitHub Actions,免費)

新增 `.github/workflows/daily-notify.yml`:

```yaml
name: Daily Telegram Push

on:
  schedule:
    # 14:00 UTC = 22:00 Asia/Taipei (台股 13:30 收盤後 8.5 小時)
    - cron: "0 14 * * 1-5"
  workflow_dispatch:  # 也允許手動觸發測試

jobs:
  notify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: python scripts/daily_notify.py
        env:
          FINMIND_TOKEN: ${{ secrets.FINMIND_TOKEN }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

到 GitHub repo → **Settings → Secrets and variables → Actions** 加上面 3 個 secrets,即可開啟每日排程。

### 4. 主機 cron(替代方案)
```
0 22 * * 1-5  cd /path/to/stock-screener && .venv/bin/python scripts/daily_notify.py
```
Windows 用「工作排程器」綁同樣指令。

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
