# 任務清單 (TASKS)

> Claude Code 每次工作開始前先看這份檔案,完成任務後更新狀態。
> 狀態: `[ ]` 未開始 / `[~]` 進行中 / `[x]` 已完成 / `[!]` 卡住或待確認

---

## 第 0 階段:環境與骨架 (預估 0.5 天)

- [x] T0.1 建立 Python 虛擬環境 (uv 或 venv) — 2026-04-27 完成,使用 venv,Python 3.14
- [x] T0.2 建立 `requirements.txt`,寫入核心相依套件 — 2026-04-27,額外補裝 tqdm(finmind 漏依賴)
- [x] T0.3 建立 `.env.example` 與 `.gitignore` — 2026-04-27,本機 .env 已建,FinMind 走無 token 模式
- [x] T0.4 建立目錄結構 (src/, tests/, data/, docs/) — 2026-04-27,含 __init__.py 與 .gitkeep
- [x] T0.5 寫 `src/config.py` 載入環境變數 — 2026-04-27,五個常數齊備,缺值印 warning 不拋例外
- [x] T0.6 寫一個最小的 `app.py`,顯示 "Hello Streamlit",確認可執行 — 2026-04-27,HTTP 200 + health=ok 驗證通過
- **驗收**:`streamlit run app.py` 能啟動並在瀏覽器看到頁面 ✅ 已通過

---

## 第 1 階段:資料層 (預估 2–3 天)

- [x] T1.1 寫 `src/database.py`:SQLite 初始化、建表(stocks, daily_prices, financials, institutional, sync_log) — 2026-04-27
- [x] T1.2 寫 `src/data_fetcher.py` 的 FinMind 介接:取得台股清單、日線資料 — 2026-04-27,直接打 v4 endpoint
- [x] T1.3 在 data_fetcher 加入快取邏輯:先查 DB,沒有才打 API — 2026-04-27,用 sync_log 區間判斷,只補缺的頭/尾
- [x] T1.4 加入三大法人買賣超抓取 — 2026-04-27,含 pivot(外資/投信/自營商加總)
- [~] T1.5 加入財報資料(月營收、季 EPS、ROE)抓取 — 2026-04-27 月營收已寫;季 EPS/ROE 程式已寫但**未實測**(無 token 模式可能被 FinMind 拒絕,函式已加降級邏輯回空 DF) → 待主公升級 token 後再驗證
- [ ] T1.6 加入 yfinance 美股資料抓取(基本款) — **P2 / 暫緩**(首版只做台股,跑通整條鏈路後再啟用)
- [x] T1.7 寫 `tests/test_database.py` 測試基本 CRUD — 2026-04-27,含 init_db 冪等、各表 upsert、sync_log 區間擴展
- [x] T1.8 寫 `tests/test_data_fetcher.py` 測試快取行為(用 mock) — 2026-04-27,33 個測試全綠
- **驗收**:能用 Python REPL 取得 2330 (台積電) 過去 60 日資料,第二次呼叫不再打 API ✅ 已通過(2024 Q1 共 56 筆;第二次走 [CACHE])

### T1.5 卡點記錄
- [~] T1.5-S 季財報實測:程式已實作 + UI「📊 更新財報資料」按鈕已就位;主公升級 token 後在雲端按按鈕即可實打驗證。
- [x] T1.5-D 配息抓取(`fetch_dividend`)已實作 — 2026-04-27,**意外發現無 token 模式也能用**(2330 實打成功,2015~2026 共 12 筆配息),含 sync_log 快取 + 同年多筆加總邏輯。

---

## 第 2 階段:技術指標 (預估 1–2 天)

- [x] T2.1 寫 `src/indicators.py`,實作以下函式(輸入 DataFrame,輸出 Series 或 DataFrame): — 2026-04-27,自刻不依賴 ta library
  - [x] `sma(df, period)` — 簡單移動平均
  - [x] `ema(df, period)` — 指數移動平均(adjust=False,從第 1 日有值)
  - [x] `kd(df, n=9)` — KD 隨機指標(台股 9-3-3,起始值 50)
  - [x] `macd(df, fast=12, slow=26, signal=9)` — MACD(HIST 採台股慣例 ×2)
  - [x] `rsi(df, period=14)` — RSI(Wilder 平滑,首期用 SMA)
  - [x] `bollinger(df, period=20, num_std=2)` — 布林通道(σ 採 ddof=0,對齊 TA-Lib)
- [x] T2.2 寫 `tests/test_indicators.py`:用已知資料驗證指標數值正確 — 2026-04-27,27 個測試,共 60 passed
- **驗收**:對台積電 2330 計算 KD,結果與券商 App 顯示誤差 < 0.5 — ✅ 程式驗收通過(資料 2024-Q1 56 筆),**待主公手動對拍券商 App**

### T2 對拍數值(2330,2024-03-29 收盤 779.0,共 56 筆 cache)
| 指標 | 數值 |
|------|------|
| K(9) | 63.363 |
| D(9) | 65.102 |
| RSI14 | 63.698 |
| DIF | 24.009 |
| DEA | 27.861 |
| HIST | -7.703 |
| MA5 | 777.800 |
| MA20 | 766.400 |
| BB 上 | 802.568 |
| BB 中 | 766.400 |
| BB 下 | 730.232 |

⚠️ MACD 因 EMA 是漸近收斂,僅 56 筆樣本下與券商 App(通常用上百筆以上)可能誤差 > 0.5;KD/RSI/SMA/BB 在 56 筆下已穩定。

---

## 第 3 階段:選股邏輯 (預估 2 天)

- [x] T3.1 寫 `src/screener_short.py`:短線選股 — 2026-04-27
  - [x] 預設策略:量 > 5 日均量 1.5 倍 + KD 黃金交叉 + 法人連 3 日買超
  - [x] 函式簽名 `screen_short(date, params: dict) -> pd.DataFrame`
  - [x] 參數可調(`DEFAULT_SHORT_PARAMS` 集中,UI 可直接讀)
- [x] T3.2 寫 `src/screener_long.py`:長線選股 — 2026-04-27
  - [x] 預設策略:近 3 年 ROE > 15% + PE < 產業平均/pe_max + 連 5 年配息 + 殖利率 > 4%
  - [x] 函式簽名 `screen_long(params: dict) -> pd.DataFrame`
  - [x] 防呆:financials/dividend 缺資料時回空 DF + stderr warning,不拋例外
- [x] T3.3 寫 `tests/test_screener.py`,用模擬資料驗證選股邏輯 — 2026-04-27,18 個新測試
- **驗收**:跑短線選股,輸出 5–20 檔股票;跑長線選股,輸出 10–30 檔股票
  - 程式驗收 ✅(78 passed);實機驗收待 stocks/financials/dividend 抓取就位後重做

---

## 第 4 階段:Streamlit 介面 (預估 2–3 天)

### T4-A 階段(2026-04-27 完成)

- [x] T4.1 重寫 `app.py`,加入 sidebar 切換「短線 / 長線 / 個股查詢 / 設定」 — sidebar 四項齊備
- [x] T4.4 個股查詢頁:輸入股票代碼,顯示 K 線圖(plotly)+ 指標 + 籌碼 — 2026-04-27,K 線+量+均線+布林+KD/MACD/RSI 分頁+摘要
- [x] T4.6 加入手機版 layout 調整(欄位自適應) — 部分,使用 st.columns 自動疊 + use_container_width
- [x] T4.7 在頁尾加風險警語 — 2026-04-27,每頁底部都看得到

### T4-B 階段(2026-04-27 完成)

- [x] T4.2 短線頁:顯示今日推薦清單,參數可在 sidebar 調整 — 2026-04-27,sidebar 三個參數 + 上方控制列(日期/範圍/執行) + 進度條 + 結果表(可點列跨頁)
- [x] T4.3 長線頁:顯示口袋名單,附 ROE / PE / 殖利率欄位 — 2026-04-27,因缺資料先做占位頁(策略說明 + 申請 token 連結 + 試用按鈕)
- [x] T4.5 加入「資料更新」按鈕,觸發增量抓取 — 2026-04-27,sidebar 加「更新 50 檔大型股」按鈕
- 新增 `src/universe.py`:TW_TOP_50 大型股清單 + load_watchlist()
- **驗收**:在電腦瀏覽器和 iPhone Safari 都能順暢操作 — T4-A 程式驗收 ✅(5 個 AppTest smoke pass);手機實機驗收待主公測試

### T4-A 已知小問題
- Streamlit 1.56 對 `use_container_width=True` 印 deprecation warning(2025-12-31 後移除),功能仍正常。未來統一改 `width='stretch'`(現在改會違反 PRD/TASKS 指示用 use_container_width)。

---

## 第 5 階段:部署與打磨 (預估 1 天)

- [ ] T5.1 寫 README,說明如何在本機啟動
- [ ] T5.2 部署到 Streamlit Community Cloud(可選)
- [ ] T5.3 在 iPhone 上測試「加到主畫面」流程,確認圖示與全螢幕正常
- [ ] T5.4 寫一個 `scripts/daily_update.py`,可用 cron 每天排程跑
- **驗收**:從手機主畫面點圖示能進入,且當日有最新資料

---

## 第 6 階段:選做功能

- [x] G1.1 Telegram Bot 整合,每日收盤後 push 短線推薦(LINE Notify 已停服) — 2026-04-27,新檔 src/notifier.py + scripts/daily_notify.py;Streamlit sidebar 加「📲 測試 Telegram」按鈕(只在 secrets 有 token 時顯示);設定頁加 BotFather → /newbot → getUpdates 教學;14 個新測試
- [x] G1.2 排程內自動抓資料(讓 G1.1 推送有真實內容) — 2026-04-27,新檔 scripts/daily_fetch.py + .github/workflows/daily-notify.yml;workflow 兩個 step:fetch 50 檔 → notify;cron `0 14 * * 1-5`(台北 22:00 週一~五);4 個新測試
- [ ] G2.1 自選股清單(SQLite 存使用者標記)
- [x] G3.1 簡易回測(過去 1 年策略勝率 + 報酬) — 2026-04-27,新檔 src/backtester.py + Streamlit「📈 簡易回測」分頁,含 6 metric + 累積報酬曲線(疊 0050 對比) + 交易明細;**簡易版,無交易成本/滑價/資金管理**;11 個新測試
- [ ] G4.1 大盤情緒儀表板(加權、外資買賣超、VIX)

---

## 已知問題 / 待澄清

- [ ] Q1:使用者是否已有 FinMind 帳號?需先註冊取得 token。
- [x] Q2:已決定 — 改用 **Telegram Bot**(LINE Notify 在 2025/04 停服)。
- [x] Q3:已決定 — **首版只做台股**,跑通整條鏈路後再加美股(T1.6 已標 P2)。

---

## 完成記錄

(任務完成時在這裡記日期與備註,例如:)
- 2026-04-27 T0.1 ~ T0.6 完成,Streamlit Hello World 可跑(HTTP 200 驗證)
- 2026-04-27 已決定先用「無 token 模式」開發,未來再升級補 FinMind token
- 2026-04-27 環境:Python 3.14 + venv,tqdm 已加進 requirements(finmind 1.9.x 漏依賴)
- 2026-04-27 T1.1~T1.4、T1.7、T1.8 完成;T1.5 月營收完成、季財報程式就位但未實測(標 [!]);T1.6 暫緩
- 2026-04-27 真實打 FinMind 無 token API 取台積電 2024 Q1 日線成功(56 筆),快取第二次完全不打 API
- 2026-04-27 T2.1 + T2.2 完成,六個指標自刻,60 passed(新增 27 個測試),最後一日數值待主公手動對拍券商 App
- 2026-04-27 T2 對拍通過:K/D/MA 用 TWSE 官方資料 0 誤差,RSI 差 0.083 < 容差 0.5
- 2026-04-27 觀察:yfinance 抓台股 OHLC 與 TWSE 有差(2330 2024-03-29 yf=776 vs TWSE=779),記錄於 docs/DATA_NOTES.md
- 2026-04-27 T3.1~T3.3 完成,加 dividend 表 schema,長線選股目前缺資料時防呆回空,78 passed
- 2026-04-27 T4-A 完成:sidebar 路由 + 個股查詢頁(K 線/MA/BB + KD/MACD/RSI 分頁 + 摘要) + 設定頁(token 狀態+cache 內容);AppTest 5 個 smoke pass
- 2026-04-27 部署到 Streamlit Cloud,網址 https://jjen-stock-screener.streamlit.app;GitHub repo https://github.com/jjen0206/stock-screener
- 2026-04-27 T4-B 完成:短線推薦頁 + 長線占位頁 + sidebar「更新 50 檔大型股」按鈕;新增 src/universe.py(50 大型股清單);AppTest 4 個 smoke pass
- 2026-04-27 解 T1.5 卡點:實作 fetch_dividend + fetch_long_term_data,sidebar 加「📊 更新財報資料」按鈕;意外發現 dividend dataset 無 token 也能用;87 passed
- 2026-04-27 介面字體放大兩輪(1.25x → 1.4x),老花友善;CSS 注入 + Plotly font/tickfont 同步放大
- 2026-04-27 G3 簡易回測完成:src/backtester.py + 「📈 簡易回測」分頁;98 passed(新增 11 個測試);實機 0 筆是預期(cache 缺 institutional)
- 2026-04-27 修 backtest 兩個 bug:夏普 √(252/hold_days) 年化、累積報酬曲線改 date 軸 + 加直方圖;summary 加年化報酬/波動率欄位
- 2026-04-27 修 KeyError(雲端 module cache 落後場景):UI 全改 .get() 防呆
- 2026-04-27 G1 Telegram 推播完成:notifier 模組 + CLI 腳本 + sidebar 按鈕 + 設定頁教學 + README GitHub Actions 範本;115 passed
