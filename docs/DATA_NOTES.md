# 資料來源差異與已知限制

## yfinance 抓台股 OHLC 與 TWSE 官方有差(2026-04 觀察)

實測 2330 台積電 2024-03-29 收盤:
- TWSE 官方:**779.0**(我們從 FinMind 拿到的也是 779.0,因為 FinMind 後端就是 TWSE 收盤)
- yfinance:**776**(差 3 元 ≈ 0.4%)

差異來源推測:
- yfinance 的台股資料源是 Yahoo Finance,可能用了股價調整(除權息回推)、或抓取時點不同。
- 對「指標計算」影響不大(差幾元 KD/RSI 結果差 < 0.5),但若做「歷史價格回測」要意識到這個來源差異。

**結論與行動**:
- 首版專注台股 → 一律用 FinMind(對齊 TWSE 官方),yfinance 暫緩(T1.6 標 P2)。
- 未來啟用 yfinance 抓美股時,**不要拿 yfinance 抓台股當備援**,差異會累積成 bug。
- 若有跨資料源比對需求,寫一個 cross_check 工具明確標出差異,而不是隱式 fallback。

## FinMind 無 token 模式限制

- `TaiwanStockPrice` / `TaiwanStockInfo` / `TaiwanStockMonthRevenue` / `TaiwanStockInstitutionalInvestorsBuySell`:✅ 可用
- `TaiwanStockFinancialStatements`(季 EPS / ROE):⚠️ 多半被拒(會員/付費 dataset),`fetch_quarterly_financials` 已內建降級邏輯回空 DataFrame。
- 配息(`TaiwanStockDividend` 等):**尚未實作抓取**,長線選股(T3.2)目前一律回空 + warning,需升級 FinMind token 後補上 `fetch_dividend`。

頻率限制:
- 無 token:嚴格(實測幾百次/小時就會被擋)
- 有 token(免費版):1500 次/小時
- → 一律走 `data_fetcher` 的快取邏輯(sync_log),避免重複呼叫。
