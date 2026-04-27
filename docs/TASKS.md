# 任務清單 (TASKS)

> Claude Code 每次工作開始前先看這份檔案,完成任務後更新狀態。
> 狀態: `[ ]` 未開始 / `[~]` 進行中 / `[x]` 已完成 / `[!]` 卡住或待確認

---

## 第 0 階段:環境與骨架 (預估 0.5 天)

- [ ] T0.1 建立 Python 虛擬環境 (uv 或 venv)
- [ ] T0.2 建立 `requirements.txt`,寫入核心相依套件
- [ ] T0.3 建立 `.env.example` 與 `.gitignore`
- [ ] T0.4 建立目錄結構 (src/, tests/, data/, docs/)
- [ ] T0.5 寫 `src/config.py` 載入環境變數
- [ ] T0.6 寫一個最小的 `app.py`,顯示 "Hello Streamlit",確認可執行
- **驗收**:`streamlit run app.py` 能啟動並在瀏覽器看到頁面

---

## 第 1 階段:資料層 (預估 2–3 天)

- [ ] T1.1 寫 `src/database.py`:SQLite 初始化、建表(stocks, daily_prices, financials, institutional)
- [ ] T1.2 寫 `src/data_fetcher.py` 的 FinMind 介接:取得台股清單、日線資料
- [ ] T1.3 在 data_fetcher 加入快取邏輯:先查 DB,沒有才打 API
- [ ] T1.4 加入三大法人買賣超抓取
- [ ] T1.5 加入財報資料(月營收、季 EPS、ROE)抓取
- [ ] T1.6 加入 yfinance 美股資料抓取(基本款)
- [ ] T1.7 寫 `tests/test_database.py` 測試基本 CRUD
- [ ] T1.8 寫 `tests/test_data_fetcher.py` 測試快取行為(用 mock)
- **驗收**:能用 Python REPL 取得 2330 (台積電) 過去 60 日資料,第二次呼叫不再打 API

---

## 第 2 階段:技術指標 (預估 1–2 天)

- [ ] T2.1 寫 `src/indicators.py`,實作以下函式(輸入 DataFrame,輸出 Series 或 DataFrame):
  - [ ] `sma(df, period)` — 簡單移動平均
  - [ ] `ema(df, period)` — 指數移動平均
  - [ ] `kd(df, n=9)` — KD 隨機指標
  - [ ] `macd(df, fast=12, slow=26, signal=9)` — MACD
  - [ ] `rsi(df, period=14)` — RSI
  - [ ] `bollinger(df, period=20, num_std=2)` — 布林通道
- [ ] T2.2 寫 `tests/test_indicators.py`:用已知資料驗證指標數值正確
- **驗收**:對台積電 2330 計算 KD,結果與券商 App 顯示誤差 < 0.5

---

## 第 3 階段:選股邏輯 (預估 2 天)

- [ ] T3.1 寫 `src/screener_short.py`:短線選股
  - [ ] 預設策略:量 > 5 日均量 1.5 倍 + KD 黃金交叉 + 法人連 3 日買超
  - [ ] 函式簽名 `screen_short(date, params: dict) -> pd.DataFrame`
  - [ ] 參數可調(均量倍數、KD 門檻、買超天數)
- [ ] T3.2 寫 `src/screener_long.py`:長線選股
  - [ ] 預設策略:近 3 年 ROE > 15% + PE < 產業平均 + 連 5 年配息 + 殖利率 > 4%
  - [ ] 函式簽名 `screen_long(params: dict) -> pd.DataFrame`
- [ ] T3.3 寫 `tests/test_screener.py`,用模擬資料驗證選股邏輯
- **驗收**:跑短線選股,輸出 5–20 檔股票;跑長線選股,輸出 10–30 檔股票

---

## 第 4 階段:Streamlit 介面 (預估 2–3 天)

- [ ] T4.1 重寫 `app.py`,加入 sidebar 切換「短線 / 長線 / 個股查詢 / 設定」
- [ ] T4.2 短線頁:顯示今日推薦清單,參數可在 sidebar 調整
- [ ] T4.3 長線頁:顯示口袋名單,附 ROE / PE / 殖利率欄位
- [ ] T4.4 個股查詢頁:輸入股票代碼,顯示 K 線圖(plotly)+ 指標 + 籌碼
- [ ] T4.5 加入「資料更新」按鈕,觸發增量抓取
- [ ] T4.6 加入手機版 layout 調整(欄位自適應)
- [ ] T4.7 在頁尾加風險警語
- **驗收**:在電腦瀏覽器和 iPhone Safari 都能順暢操作

---

## 第 5 階段:部署與打磨 (預估 1 天)

- [ ] T5.1 寫 README,說明如何在本機啟動
- [ ] T5.2 部署到 Streamlit Community Cloud(可選)
- [ ] T5.3 在 iPhone 上測試「加到主畫面」流程,確認圖示與全螢幕正常
- [ ] T5.4 寫一個 `scripts/daily_update.py`,可用 cron 每天排程跑
- **驗收**:從手機主畫面點圖示能進入,且當日有最新資料

---

## 第 6 階段:選做功能

- [ ] G1.1 LINE Notify 整合,每日收盤後 push 短線推薦
- [ ] G2.1 自選股清單(SQLite 存使用者標記)
- [ ] G3.1 簡易回測(過去 1 年策略勝率 + 報酬)
- [ ] G4.1 大盤情緒儀表板(加權、外資買賣超、VIX)

---

## 已知問題 / 待澄清

- [ ] Q1:使用者是否已有 FinMind 帳號?需先註冊取得 token。
- [ ] Q2:LINE Notify 在 2025 年 4 月已停止服務,改用 Telegram Bot 或 Discord Webhook。
- [ ] Q3:首版專注台股還是同時做美股?(建議先台股,跑通再加美股)

---

## 完成記錄

(任務完成時在這裡記日期與備註,例如:)
- 2026-04-27 T0.1 ~ T0.6 完成,Streamlit Hello World 可跑
