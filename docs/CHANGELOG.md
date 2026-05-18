# Changelog

按主題 group，最新放上面。日期 = commit 日，非 release tag。
歷史更早的變更不在此列，需要溯源走 `git log`。

---

## 2026-05-18 — 🎯 個股軍師判讀（白話化整合 verdict）

### Added
- **`src/individual_stock_verdict.py`** — 整合 K 線形態 / 警示 / 大盤 regime / ML 機率 / 策略共識 / 題材熱度 / 持倉狀態 → `🟢 可進場` / `🟡 觀望` / `🔴 不進場` 結論:
  - `compute_verdict(sid, db_path)` — 純資料,回完整 dict:`verdict / verdict_color / score / reasons_pro / reasons_con / action_suggestion / entry_zone / stop_loss / take_profit / signals`
  - `render_stock_verdict(sid)` — Streamlit render:大字 banner + 進場/不進場理由 list + 軍師建議 + 🟢 才給的三欄(進場價/停損/停利)
  - `verdict_tag_for_card(sid)` — 卡片 / 推播用短標(`🟢 可進場`)
  - `latest_pattern_phrase(hits)` + `PATTERN_MEANINGS` — K 線形態白話化(三紅兵 → 「強勢多頭」)
  - Kill-switch `STOCK_VERDICT_ENABLED=true`(預設 on)
- **`app.py::_page_stock_detail`** — Header 後、Tabs 前插入「🎯 軍師判讀」section,主公一眼看到結論
- **`app.py::_render_detail_patterns_section`** — 形態 section 全面白話化:
  - 「最近一根」改顯「✓ 三紅兵(★★★) — 強勢多頭」
  - 30 日次數表加「白話解釋」欄(主公看不懂三紅兵就看「強勢多頭」)
- **`src/ui_cards.py::render_pick_card`** — 卡片加「🎯 軍師判讀:🟢 可進場」短標(`_verdict_tag_cached` `@st.cache_data ttl=60` 避免 138 卡 N×6 SQL)
- **`src/notifier.py::format_pick_block`** — 推播 pick block 加「🎯 軍師判讀」行,主公手機收訊息一眼看
- **`tests/test_individual_stock_verdict.py`** — 21 unit test:kill switch / 紅燈警示強制 🔴 / 大盤 bear / 已持倉強制 🟡 / 🟢 才給 entry_zone / verdict_tag_for_card / 訊號不足 graceful return + complete keys schema
- **`tests/test_page_stock_detail_verdict.py`** — 7 wire + AppTest smoke:公開 API guard / page 接線 / 卡片接線 / notifier 接線 / pattern 白話 helper / 違約股 AppTest 「不進場」字樣出現 / kill switch off 不顯 banner

### 設計考量
- **白話為主**:主公看「三紅兵 4 / 槌子線 2」看不懂 → 強制翻譯成「強勢多頭」「跌深反彈訊號」
- **紅燈一定擋**:default_settlement / full_cash active → 強制 🔴(不管其他訊號)
- **已持倉強制觀望**:不重複進場,改提醒守停損/停利
- **🟢 才給數字**:🟡 / 🔴 不給進場價區間,避免主公被「沒結論的數字」誤導
- **mobile-first**:banner 大字 + 三色塊;卡片短標 14px 粗體;推播一行白話

---

## 2026-05-17 — 🤖 E ML / 訊號強化(v4 features:籌碼 / 多時間軸 / 產業相對強度)

### Added
- **`src/ml_features.py`** — v4 10 個新 features 純函式 module(籌碼類 3 / 多時間軸 5 / 產業 2):
  - 籌碼:`concentration_change_rate`(千張戶集中度月變化,平滑分母)/ `institutional_continuity`(外資+投信同向連續天數,帶符號)/ `inst_divergence`(0-1,1.0=一買一賣)
  - 多時間軸:`ma5_above_ma20_pct` / `ma20_above_ma60_pct`(近 60 日占比 0-1)/ `momentum_5d` / `momentum_20d` / `momentum_60d`(% 報酬)
  - 產業:`industry_relative_strength`(該股 5d 漲幅 − 同產業平均)/ `industry_rank_pct`(產業內 percentile 0-1)
  - `_INDUSTRY_RETURNS_CACHE`(target_date × industry)讓全市場 predict_batch 跨 sid 共用,O(M) → O(industries)
- **`src/ml_predictor.FEATURE_NAMES` 從 16 擴成 26**,v4 features 一律 append 在尾部維持 backward-compat
- **`MODEL_VERSION` v3 → v4** + `V3_FEATURE_COUNT = 16` 常數
- **Kill-switch `ML_NEW_FEATURES_ENABLED=false`** → v4 features 全 fallback 0.0(dict shape 仍 26 keys,model 不炸)
- **`tests/test_ml_features_new.py`**(36 test):每 feature 正常路徑 + edge case(< 3 檔產業 / 空 inst_df / ma 不足 / null latest)+ 2 個 SQL-backed test(_load_industry_for_sid / _load_industry_returns_5d / cache 命中)
- **`tests/test_ml_predictor_new_features.py`**(20 test):FEATURE_NAMES 順序契約(v3 前綴不變)+ v4 mock model 26 features 不炸 + kill-switch 各值(true/false/0/off/no)+ _aligned_feature_names slice v2/v3/v4 + 真實 extract_features smoke(kill-switch off → v4 全 0、on → momentum > 0)

### Changed
- `tests/test_ml_features_v3.py::test_aligned_feature_names_new_v3_model_uses_all_16` adjust 為 slice `FEATURE_NAMES[:16]`(v4 升版後 FEATURE_NAMES 26 個)
- `tests/test_ml_features_v3_structural.py::test_model_version_is_v3` 改名 `..._or_later`,容忍 v3 / v4(`MODEL_VERSION` 升版只升不降)

### 試水溫結果(2026-05-17 walk-forward,row split,大盤 cache 152k+ rows)
| Model | OLD ROC (v3) | NEW ROC (v4) | Δ | 達 +0.02? |
|---|---|---|---|---|
| short_pick | 0.6417 | 0.6472 | +0.0055 | ❌ |
| **big_holder_inflow** | 0.5952 | 0.6183 | **+0.0231** | ✅ |
| macd_golden | 0.5882 | 0.5971 | +0.0089 | ❌ |

`big_holder_inflow` 達標 — 籌碼類新 features 直接 hit 該策略訊號源。short_pick / macd_golden Train ROC 顯著升(0.88 → 0.93)但 WF ROC 邊際 — v3 base 已抓主要 signal,v4 新 features 對非籌碼策略邊際貢獻有限。

### Notes
- 不重訓全 17 model — 會 timeout。weekly cron(`ml-weekly-retrain.yml` 週日 03:00 TW)有 walk-forward A/B gate,下次自然吃 v4 features;失敗 model 自動 rollback `.pre_retrain.bak`,production 不會壞。
- 詳細決策 / Backward-compat / kill-switch 說明見 `docs/ml-features-upgrade-2026-05-17.md`

---

## 2026-05-17 — 📈 D 績效分析（真實交易 log + 策略組合回測 + 策略 attribution）

### Added
- **`src/performance_analysis.py`** — 主公真實平倉損益引擎(讀 `user_positions` is_open=0):
  - `compute_user_pnl(conn, start_date, end_date)` — 每筆已平倉 P&L df(含 long/short side、holding_days、pnl_pct)
  - `compute_user_win_rate(conn, window_days=30)` — 真實滾動勝率（跟 pick_outcomes 系統推薦勝率分開)
  - `compute_attribution(conn, window_days_before=5, after=5)` — 對每筆平倉找對應 daily_picks(同 sid + entry_date ± 5 天)→ 歸因到觸發策略;一筆命中 N 策略 → P&L 平均分 1/N 避免雙計
  - `compute_drawdown_curve(conn)` — equity / peak / drawdown / drawdown_pct 時序 df
  - `compute_summary_metrics(conn)` — total_pnl / win_rate / sharpe(× √252)/ max_drawdown / median_holding_days
  - `best_strategy_by_pnl(attribution, min_count=1)` — 軍師判讀「主公真實表現最好的策略」(排除 _unknown bucket)
  - Kill-switch `PERFORMANCE_ENABLED=true`(預設 on)
- **`src/strategy_backtest.py`** — 策略組合回測 + 命中相關性(走 daily_picks,不重算 screener):
  - `backtest_combination(conn, strategies, start_date, end_date, holding_days=5, mode='union'/'intersect')` — 任一/全部命中,從 daily_prices 算 entry(pick_date close)→ holding_days 個交易日後 close,回 n_trades / win_rate / total_return_pct / max_drawdown_pct / sharpe(× √(252/holding_days))/ trades list
  - `compute_strategy_correlation(conn, strategies, days=180)` — Jaccard `|A ∩ B| / |A ∪ B|` heatmap df(對角 1.0)
- **App「📈 績效分析」分頁**(`app.py::_page_performance`,4 tab):
  - Tab 1「💰 真實交易」:4 metric(總損益 / 勝率 / Sharpe / Max DD)+ equity curve plotly + drawdown plotly + 每筆平倉 df(進場/出場日、價、股數、P&L、持有天數)
  - Tab 2「🎯 策略 attribution」:bar chart 每策略貢獻 P&L(綠正紅負)+ 表格(勝率/平均報酬/筆數)+ 軍師判讀「主公真實表現最好的策略是 X,建議多看 X 類推薦」
  - Tab 3「🔬 策略組合回測」:multiselect 策略 + 聯集/交集 toggle + holding_days 滑桿(1~30)+ 開始/結束日 + Run 按鈕 → 5 metric + 交易明細(matched 策略中文標籤)
  - Tab 4「🧭 策略相關性」:Jaccard heatmap(Viridis,0~1)+ 區間天數滑桿
- **Weekly brief 整合**:
  - `src/system_brief.py::_build_real_performance(conn)` 新 helper,build_system_brief() 加 `real_performance` key
  - `format_brief_for_telegram` 加「📈 *本週真實績效*」section:本週平倉筆數 / P&L / WR / 表現最佳策略
- **`tests/test_performance_analysis.py`**(22 test):empty df / long+short winner / 排除 open / date range filter / kill-switch / win_rate 滾動窗 / attribution 單策略歸因 + 多策略平均分 + _unknown bucket / drawdown peak/dd 計算 / summary_metrics sharpe / best_strategy 排除 unknown + min_count + None
- **`tests/test_strategy_backtest.py`**(14 test):union/intersect mode / entry+exit 價算 P&L / holding_days / kill-switch / 無策略 / 錯 mode / 錯 holding_days / 缺 exit price skip / Jaccard 對角 1 + disjoint 0 + 完全重疊 1
- **`tests/test_page_performance.py`**(11 test):page function 存在 + callable / PAGES 含「📈 績效分析」+ 位置 / dispatch / page source 引用兩個 module + 四個 tab + STRATEGY_LABELS + drawdown_curve + best_strategy_by_pnl + kill-switch

### Notes
- 跟既有「📈 簡易回測」/「🧪 實測追蹤」區隔清楚 — 簡易回測重跑 screener(慢但 fresh)、實測追蹤是 ML 過濾驗證(系統 seed),績效分析純讀 user_positions + daily_picks(主公拍板真倉 + 預跑命中)
- mobile-first:plotly responsive,4 column metric 在 iPhone 直接疊
- 沒實際平倉資料時 Tab 1 顯「尚無已平倉交易,先到🛡️ 持倉管理新增/平倉」,不擋畫面
- 整合 weekly brief 後主公週日 10:00 收到 Telegram 同時看到「系統推薦勝率 + 自己真實買賣勝率」,差距大就表示主公手感跟系統推薦脫鉤
## 2026-05-17 — 💬 C AI 軍師 + Discord slash commands + Telegram inline keyboard

### Added
- **`src/ai_assistant.py`** — Gemini 2.5 Flash Lite 對「今天所有資料」做綜合判讀:
  - `collect_stock_context(sid)` 攤平 `daily_prices` / `institutional` / `shareholder_concentration` / `news` / `stock_warnings` / `daily_picks` / `pick_shap_explanations` / `company_profiles` / `user_positions`
  - `collect_market_context()` 拉 `compute_regime` + `get_us_sentiment` + `compute_theme_heat` top 3 + 警示概況 + 今日 picks 數
  - `build_stock_prompt` / `build_market_prompt` 固定軍師人設(嚴謹 + 主動提示風險 + 不替主公做隱藏決定),結尾固定加「⚠️ 僅供研究,非投資建議」
  - `ask_about_stock(sid, question)` / `ask_about_market(question)` 公開入口,失敗 / quota / 缺 key 一律 graceful 回 fallback 訊息(不 raise)
  - Kill-switch `AI_ASSISTANT_ENABLED=true`(預設 on)
- **`src/discord_bot.py`** — Discord Slash Commands HTTP interaction handler(不需常駐 bot daemon):
  - `get_slash_command_definitions()` 7 個指令:`/picks` `/watchlist` `/chart {sid}` `/stats` `/positions` `/alert {sid} {type} {value}` `/ask {question}`
  - `register_commands(...)` 一次性 PUT 到 Discord API
  - `verify_signature(...)` Ed25519 驗章(PyNaCl,缺套件 / 缺 key graceful 回 False)
  - `handle_interaction(payload)` 主入口,dispatch + 每個 _cmd_* 純函式回 Discord 規格 response dict
  - `_sparkline()` 8 階 Unicode block ASCII K 線(行動裝置友善)
  - Kill-switch `DISCORD_BOT_ENABLED=true`(預設 on,缺 `DISCORD_APPLICATION_ID` / `DISCORD_BOT_TOKEN` / `DISCORD_PUBLIC_KEY` 任一 → 自動停用)
- **`scripts/discord_bot_register.py`** — 一次性 PUT slash command 定義到 Discord(支援 `DISCORD_GUILD_ID` 即時生效模式)
- **`scripts/discord_bot_serve.py`** — 輕量 `http.server` 接 Discord interaction webhook(本機 + ngrok 或 Cloudflare Workers 部署)
- **`src/notifier.py`** Telegram inline keyboard 支援:
  - `build_stock_inline_keyboard(sid)` 一排 4 按鈕「📊 K 線 / ⭐ 加關注 / 🚨 設警報 / 💬 問軍師」,callback_data 走 `<action>:<sid>` 格式(< 64 byte 守 Telegram 限制)
  - `send_telegram_message_with_keyboard(text, keyboard, ...)` send_telegram_message 的姊妹版,帶 `reply_markup`
  - `answer_callback_query(...)` 點按鈕後 30s 內 ACK(避免 Telegram client 一直轉圈)
  - `handle_callback_query(update)` dispatch 4 個 action:`watch` 寫 watchlist / `chart` 回 ASCII sparkline / `alert` 提示路徑 / `ask` 走 `ai_assistant.ask_about_stock`
- **`app.py` 新頁「💬 問軍師」** + 個股深度頁底部加 inline `_render_detail_ask_ai_section`(對該 sid 一鍵問軍師)
- **3 個 test 檔(共 65 個 test)**:
  - `tests/test_ai_assistant.py`(14 test):kill-switch / context 蒐集 / prompt 組裝 / Gemini 失敗 graceful / happy path mock
  - `tests/test_discord_bot.py`(23 test):is_enabled / slash command schema / verify_signature graceful / handle_interaction dispatch 7 個指令 / `_detect_sid` + `_sparkline` 純函式
  - `tests/test_telegram_inline_keyboard.py`(13 test):keyboard 結構 / `reply_markup` payload / `answerCallbackQuery` / `handle_callback_query` 4 個 action

### Config 新增
- `DISCORD_APPLICATION_ID` / `DISCORD_BOT_TOKEN` / `DISCORD_PUBLIC_KEY` — Discord slash commands 必備三件套(缺任何一個就停用 bot,不影響既有 `DISCORD_WEBHOOK_URL` 推播)
- `AI_ASSISTANT_ENABLED=true` / `DISCORD_BOT_ENABLED=true` — 預設 on,出事可 kill

### Notes
- AI 軍師 prompt 固定加「⚠️ 僅供研究,非投資建議」結尾;不替主公做隱藏決定(只給判讀+選項)
- Discord slash commands 採 webhook 互動模式而非長駐 `discord.py` daemon — Streamlit Cloud 跑不起來常駐;主公自己挑 Cloudflare Workers / Vercel / 本機 + ngrok 跑 `scripts/discord_bot_serve.py`
- Telegram inline button 對應的 bot serve loop 仍待主公接 webhook(現階段先有 `handle_callback_query` 函式,連線層後續再補)

---

## 2026-05-17 — 📈 個股深度頁 K 線視覺化(plotly 互動 chart + 標記層)

### Added
- **`src/chart_renderer.py`** — 互動 plotly K 線渲染模組:
  - `render_candlestick_chart(sid, days=120, indicators=[...], *, db_path, df)` — 主圖 OHLC + MA20(藍)/ MA60(橘)/ Bollinger(灰虛線),副圖動態 row 配置(Volume / RSI 14 含 30/70 線 / MACD 含 DIF/DEA/HIST / KD 9-3-3 / Stoch 14-3-3),主圖 60% / 副圖均分 40%,iPhone 友善高度自適應
  - `mark_pick_dates(fig, sid, ...)` — 從 `daily_picks` 撈該 sid 命中日期,範圍內標 ⭐ annotation,dedup by date(同日多策略只標一次)
  - `mark_position_levels(fig, sid, ...)` — 從 `user_positions` 撈該 sid open position,加 entry(綠虛線)/ stop_loss(紅虛線)/ take_profit(藍虛線)三條 hline,右端 annotation 顯數值
  - `mark_pattern_signals(fig, sid, days, ...)` — 軟相依 `src.candlestick_patterns`(B task PR),try/except graceful skip,API 對不上 / 模組沒 merge 不炸
  - `compute_bollinger(close, period, num_std)` / `compute_kd(df, period)` / `compute_stoch(df, period, smooth_k, smooth_d)` 三個 helper(Stoch 補 indicators 缺,用 SMA 平滑差於 KD 的 EMA-like)
- **App「📈 K 線」tab(個股深度頁)**(`app.py::_render_detail_kline_tab`):從原本 60 天靜態圖進化成互動 plotly chart:
  - lookback 滑桿(60 / 120 / 180 / 360 天)
  - 指標 multiselect(MA20 / MA60 / Bollinger / Volume / RSI / MACD / KD / Stoch)
  - 標記 toggle:⭐ picks 歷史 / 🎯 持倉價位 / 🕯️ K 線形態
  - mobile-first plotly responsive,iPhone 直接 swipe / pinch zoom,hover 模式 `x unified` 一次看完所有指標
- **「⚠️ 警示」第 5 tab**:個股深度頁 4 tabs → 5 tabs(K線 / 籌碼 / ML 解釋 / 新聞 / 警示),警示紀錄 section 從 tabs 上方移進獨立 tab。乾淨股 tab 內補「✅ 此股近 90 天無警示紀錄」正向訊息,有警示時保留原色塊
- **`tests/test_chart_renderer.py`**(16 test):empty DB fallback / Candlestick trace 存在 / dynamic subplot row 數(只 Volume → 2 row,Volume+RSI+MACD+KD → 5 row)/ 主圖類指標只 1 row / caller-supplied df / Bollinger upper>mid>lower + insufficient → NaN / KD/Stoch 都在 0–100 範圍 + insufficient → NaN / mark_pick_dates 標星 + dedup + 範圍外排除 + 無 pick no-op / mark_position_levels 三條 hline + 別檔 no-op + 無 open no-op / mark_pattern_signals 模組缺 graceful
- **`tests/test_page_stock_detail_chart_tab.py`**(5 test):K 線 tab call chart_renderer 4 API / 個股深度頁 5 tabs(含 ⚠️ 警示)/ K 線 tab 控制項 key prefix 都在 / chart_renderer public API export / AppTest smoke(灌 120 天 OHLCV + render plotly tab 0 exception)

### Notes
- 主圖 +130 px per 副圖,5 副圖全開時總高 ~970 px(iPhone 12 寬度看 OK,桌機更舒服)
- 不依賴 `src.candlestick_patterns`(B task 還沒 merge)— mark_pattern_signals 走 try/except import,API 名稱 detect_patterns / detect 都試,都失敗就 silently return fig
- ⭐ pick annotation 用 `yref="y domain"` + `y=1.0` + `yanchor="bottom"` 浮在主圖上緣,不擋 OHLC

---

## 2026-05-17 — 📈 B 進場 / 出場時機強化(K 線形態 + 動態停損 + 獲利了結警報)

### Added
- **`src/candlestick_patterns.py`** — K 線形態判讀(純幾何 + ratio,不依賴 ta lib):
  - `detect_three_white_soldiers(df, lookback=3)` — 三紅兵
  - `detect_hammer(df)` — 槌子線(下影 ≥ 2× 實體,實體在 K 棒上端)
  - `detect_engulfing(df)` — 看漲吞噬
  - `detect_morning_star(df)` — 晨星(三日反轉)
  - `detect_flag(df, lookback=10)` — 旗形(高震盪 base + 突破)
  - `detect_doji(df)` — 十字星(body / range ≤ 5%)
  - `detect_all_patterns(sid, df) -> list[dict]` — 全 detector 聚合,每個 dict 帶 `name / label / bias (bull|bear|neutral) / confidence (1-3 ★) / sid`
  - Kill-switch `PATTERN_DETECTION_ENABLED=true`(預設 on)
- **`src/trailing_stop.py`** — 動態停損(only-up,不會鬆動原停損):
  - `compute_trailing_stop(entry, current, atr, multiplier=2.0, high_water_mark=None, current_stop=None, side='long')` — 純算式,回 `{new_stop, high_water_mark, raised, rationale}`
  - 觸發門檻:current_price ≥ entry + 1×ATR 才啟動上移(避免進場馬上拉緊停損)
  - `update_position_trailing_stop(position_id)` — 撈 user_positions + 最新 daily_prices + ATR,UPSERT 回 DB(同時若 raised 則 stop_loss 也更新)
  - `batch_update_trailing_stops()` — 對所有 open user_positions 跑一輪,回 summary dict(`checked / updated / skipped_no_data / raised_positions`)
  - 新欄位 `user_positions.high_water_mark / trailing_stop`(migration helper `_migrate_user_positions_add_trailing`)
  - Kill-switch `TRAILING_STOP_ENABLED=true`(預設 on)
- **`src/take_profit_alerts.py`** — 達標警報 + 分批了結建議:
  - `check_take_profit_hit() -> list[dict]` — 掃 open positions,回 stop_loss / take_profit / trailing_stop / partial_exit_5 / partial_exit_10 alerts(severity: danger / warn / info)
  - `partial_exit_suggestion(pnl_pct)` — +5% 賣 1/3,+10% 再賣 1/3,留 1/3 跑趨勢
  - Kill-switch `TAKE_PROFIT_ALERT_ENABLED=true`(預設 on)
- **`src/notifier.py`** wire 進主推播:
  - `_enrich_picks_with_patterns(picks)` — in-place 注入 `pick["patterns"]` list,format_pick_block 顯 bull bias top-2 形態行 `📊 形態: 三紅兵(★★) · 槌子線(★)`
  - `_format_trailing_stop_update(...)` + `_format_take_profit_alerts(...)` — daily_notify 結尾 section,顯所有上移的停損 / 達標 alert
  - `notify_top_picks` pipeline 加 pattern enrich step,失敗 graceful skip 不擋主推播
- **`scripts/morning_brief.py`** wire 進盤前快訊:
  - `_build_take_profit_alert_lines(channel)` — 放最前面強警報(TP/SL 達標主公看到就要處理)
  - `has_any_change(...)` 加 TP alert 觸發條件
- **App `📊 個股深度`** 新「K 線形態」section(`_render_detail_patterns_section`):掃近 30 日各形態出現次數 + 最近一根命中標示
- **App `🛡️ 持倉管理`** 加 trailing stop 控制 expander:`🔄 每次開頁自動更新 trailing stop` toggle + `📈 立即更新` 按鈕 + raised 詳情;TP/SL 達標警報 section;持倉表格加 `Trail` 欄
- **5 個新 test 檔**(共 65 個 test cases):
  - `test_candlestick_patterns.py` — 6 detector × 各 fixture(★/★★/★★★ + 失敗條件)共 24 test
  - `test_trailing_stop.py` — compute only-up / hwm 不下移 / safety / short / DB write back / batch summary / kill-switch 共 13 test
  - `test_take_profit_alerts.py` — partial exit tier / SL/TP hit / trailing distinct / kill-switch 共 12 test
  - `test_notifier_pattern_wire.py` — wire enrich + format pick block 形態行 + trailing/TP section + structural 守住 daily_notify 結尾呼叫 共 16 test

### Notes
- mobile-first:形態行 1 行(top-2),trailing / TP alert 採「• 一行 + → 動作」格式,iPhone 加到主畫面後不擠
- 不破壞既有 pipeline:全部 incremental enrich + graceful skip,kill-switch 全 on,任何模組關掉只是該 section 不顯
- 主公規矩:不下單,只給「軍師建議」式提示;trailing stop 永遠 only-up(避免「我幫你鬆停損」這種破紀律操作)

---

## 2026-05-17 — 🚨 G 個股價格警報系統

### Added
- **`price_alerts` table**(`src/database.py`):主公手動設「2330 跌破 600 推播」等條件的警報 ledger。schema:`id / stock_id / alert_type / target_value / created_at / triggered_at / is_active / notes`,加 `idx_price_alerts_sid_active` + `idx_price_alerts_active` 兩個 index。`alert_type` 限定 `price_above` / `price_below` / `pct_change` / `ex_dividend` / `intraday_drop`。CRUD helpers:`add_alert / list_alerts / mark_triggered / delete_alert`
- **`src/price_alerts.py`** — 警報引擎:
  - `check_price_alerts(conn)` — 對 active alerts 算當前 daily close 是否觸發
  - `check_intraday_drop(conn, threshold_pct=-3.0)` — open user_positions 當日跌幅超 -3% → 急殺警報
  - `check_ex_dividend_alerts(conn, days_ahead=3)` — 持倉 + watchlist N 日內除權息提醒(從 `dividend.ex_dividend_date` 算)
  - `format_alert_message(...)` — 主公規格訊息(🚨 警報觸發 / 當前 / 達到設定價位 / 建議行動)
  - Kill-switch `PRICE_ALERT_ENABLED=true`(預設 on)
- **`scripts/intraday_alerts.py` 升級**:30 分鐘 cron 跑完 paper_trades 三條件後,順手 `check_price_alerts` + `check_intraday_drop`。打 `alert_dedup` 同日去重 + Telegram + Discord 推播 + 觸發後 `mark_triggered`(一次性 alert)
- **App 新頁「🚨 警報設定」**(`app.py::_page_price_alerts`):股票代號 + 警報類型(下拉)+ 目標值 + 備註 → 新增。兩個 tab:「🟢 進行中」(可刪)/「📜 已觸發歷史」。緊跟「🛡️ 持倉管理」之後
- **`src/notifier.py::_format_price_alerts_section`** + **`scripts/morning_brief.py::_build_price_alert_lines`** — 晚上 daily_notify 跟早上 morning_brief 推播都加「🚨 警報快訊」section(有觸發才顯)
- **4 個新 test 檔**(共 46 test cases):
  - `test_price_alerts.py` — CRUD + check_price_alerts 各類型 + kill-switch + 訊息格式 共 21 test
  - `test_intraday_drop.py` — 急殺 threshold / 多檔 / 已平倉 / kill-switch 共 7 test
  - `test_page_price_alerts.py` — page 註冊 / dispatch / wire 結構守住 共 8 test
  - `test_notifier_alert_wire.py` — wire daily-notify + morning_brief + intraday_alerts + mark_triggered 共 10 test

### Notes
- `pct_change` 警報需在 notes 寫 `base=600` 當基準價,沒寫的話 engine 視為無法評估(避免誤觸)
- `ex_dividend` 資料源是 FinMind 年度配息表,當年最新 ex 日可能還沒到 — TODO 接 TWSE 即時除權息日程更準
- 觸發後 alert `is_active=0`(一次性),要再警報需重設一筆

---

## 2026-05-17 — Company profiles LLM 預生 backfill(GH Actions 分批跑 + release dump)

### Added
- **`scripts/backfill_company_profiles.py` LLM 模式**:之前只跑 warm-up(僅填 FinMind facts),description/uniqueness/moat 全靠使用者點個股詳情頁 lazy on-demand,結果常常 NULL。新增 `--llm-call true|false`(預設 false 保留 warm-up 向後相容)+ `--regenerate` + `--batch-start/--batch-end` + `--max-stocks` + `--dump-format {csv,parquet}` + `--upload-release`(模式對齊 backfill_financials)+ `--universe watchlist` 新選項。LLM 模式撞 Gemini 429 → 看 `narrative_status='quota_exceeded'` 立即 fail-fast 整批中斷(模式對齊 financials 對 FinMindQuotaError 的處理)。沒設 `GEMINI_API_KEY` 在 LLM 模式 → 前置檢查 exit 1 不浪費 batch
- **`.github/workflows/backfill-company-profiles-llm-once.yml`**:workflow_dispatch 觸發,inputs(`universe`/`batch_start`/`batch_end`/`llm_call`/`sleep`/`regenerate`/`dump_format`/`upload_release`),timeout 120 min。`pip install google-generativeai`(雲端 Streamlit 沒裝這個輕量化),`GEMINI_API_KEY` 從 secrets 拉。跑完 parquet 上傳到 GH Release `snapshot-company-profiles-YYYY-MM-DD`,commit `.snapshot_releases.json` manifest(不 commit cache.db / parquet 本體)。**主公在 GH UI 分批跑 6 次,每批 500 檔避過 Gemini 1500 RPD free tier**
- **`src/database.py::preload_snapshots`** company_profiles section:走 `_ensure_snapshot_present('company-profiles', 'company_profiles')` 3-tier fallback(本地 parquet → release parquet → CSV),`ON CONFLICT(stock_id) DO UPDATE SET ... COALESCE(...)` 跟 `_upsert_profile` 同 partial-update 語意,雲端容器 boot 自動拉 release parquet → 個股頁 cache-hit 0 LLM call
- **`tests/test_backfill_company_profiles_llm.py`**(14 test):warm-up 不打 LLM / LLM 模式打 / 缺 API key fail / Gemini 429 fail-fast / 個別失敗不中斷 / batch-range / max-stocks / batch-start 越界 / batch-start >= batch-end / unknown universe / watchlist universe / dump CSV schema 對齊 production / 空表不 dump / regenerate 強制重打
- **`docs/backfill-company-profiles-llm-howto.md`**:主公觸發指引(GH UI 步驟、6 批分批表、Gemini 配額限制、429 / timeout / release upload fail 三種失敗 recovery、rollback 流程、token 用量估算)

### Notes
- 主公接下來怎麼 trigger:GH UI > Actions > **Backfill Company Profiles LLM (one-shot manual)** > Run workflow → 留 default(universe=pure_stock, batch_start=0, batch_end=500, llm_call=true, dump_format=parquet)→ 20-40 min 完成第一批。隔天再跑 500-1000,以此類推 6 批跑完全市場
- Token 用量:單檔 ~230 tokens,500 檔 ~115K tokens,全 2715 檔 ~625K tokens(Gemini 2.5 Flash Lite 免費月度額度的小頭)
- Rollback:`gh release download snapshot-company-profiles-{舊日期} --pattern company_profiles.parquet --dir data/twse_snapshot/`

---

## 2026-05-17 — 🛡️ 風險管理 / 部位管理系統

### Added
- **`src/position_sizing.py`** — Kelly criterion 倉位建議模組:
  - `kelly_fraction(win_rate, win_loss_ratio, kelly_multiplier=0.25)` — 預設 1/4 Kelly 防過估
  - `suggest_position_size(sid, ml_prob, confidence, total_capital, max_single_pct=0.20)` — 把 ml_prob 視為 calibrated win_rate proxy,× Kelly × 上限(預設 20%)→ 建議 % + 股數 + 張數,confidence='weak' 額外打 0.5 折
  - `get_recent_win_stats(days=30)` — 從 pick_outcomes 最近 30 天歷史 win_rate + win/loss ratio
  - Kill-switch `POSITION_SIZING_ENABLED=true`
- **`src/risk_management.py`** — 停損 / 停利 / drawdown 模組:
  - `compute_atr_stop_loss(sid, entry_price, days=14, atr_multiplier=2.0)` — ATR(14) × 2 停損
  - `compute_atr_take_profit(sid, entry_price, atr_multiplier=4.0)` — ATR(14) × 4 停利(2:1 R:R)
  - `compute_support_resistance(sid, lookback=60)` — 60 日 swing low/high
  - `drawdown_pct(positions)` — 整體 P&L vs 總 invested(open + closed 合算),> 10% warn / > 20% danger
  - `check_single_concentration(positions, max_single_pct=0.20)` — 單檔超 20% 警告
  - Kill-switch `RISK_MGMT_ENABLED=true`
- **`user_positions` table**(`src/database.py`):主公手動建倉 ledger(跟 paper_trades 區隔)。schema:`id / stock_id / entry_date / entry_price / shares / side / stop_loss / take_profit / notes / is_open / exit_date / exit_price / created_at / updated_at`,加 `idx_user_positions_sid_open` + `idx_user_positions_open` 兩個 index。CRUD helpers:`add_position / close_position / update_position / delete_position / get_open_positions / get_all_positions / get_position_pnl`
- **App 新頁「🛡️ 持倉管理」**(`app.py::_page_position_management`):新增持倉表單(自動 ATR 算停損停利,可手動覆寫) → 整體統計 metrics(總投入 / 市值 / 未實現 / 已實現)→ drawdown 警報(黃燈 / 紅燈)→ 單檔集中度警告(> 20%)→ 持倉表格(現價 / 損益 / 停損停利 / 達標狀態)→ 平倉表單 → 軍師部位建議區
- **`src/notifier.py::format_pick_block`** 加軍師建議行:`💰 軍師建議:投入總部位 X%(~N 張)` + `🎯 停損 X / 停利 Y` + `⚠️ 單檔風險上限 20%`(POSITION_SIZING_ENABLED + RISK_MGMT_ENABLED on)
- **`src/notifier.py::_enrich_picks_with_position_advice`** — notify_top_picks pipeline 加 enrich step,in-place 注入 `pick["position_advice"]` dict,失敗 graceful skip 不擋主推播
- **`src/notifier.py::_format_drawdown_alert`** + **`scripts/morning_brief.py::_build_drawdown_alert_lines`** — 整體 drawdown > 10% 黃燈 / > 20% 紅燈,daily_notify + morning_brief 都檢查並推播警報
- **6 個新 test 檔**(共 88 個 test cases):
  - `test_position_sizing.py` — Kelly 公式 / 邊界 / 上限 / weak discount / fallback 共 16 test
  - `test_risk_management.py` — ATR stop/tp / S-R / drawdown ok/warn/danger / 集中度 共 22 test
  - `test_user_positions.py` — schema + CRUD + side=short / closed PnL 共 20 test
  - `test_page_position_management.py` — page 註冊 / dispatch / wire 結構守住 共 8 test
  - `test_notifier_position_advice_wire.py` — wire + kill-switch + format 渲染 共 9 test
  - `test_notifier_drawdown_alert.py` — wire daily-notify + morning_brief + 各 severity 共 13 test

### Notes
- 個人 repo,持倉表 schema 走 SQLite CREATE TABLE IF NOT EXISTS,新欄位後續走 migration helper pattern
- mobile-first 設計:持倉頁 `st.dataframe` + `st.metric` 排版,iPhone 加到主畫面後可滑動看
- 推播口徑:「軍師」式建議(不自動下單),所有警報文案標明「軍師建議 ...」讓主公知道是建議不是命令
- Drawdown 算法:realized(closed 部位用 exit_price)+ unrealized(open 部位撈 daily_prices 最新 close)合算
- 不破壞既有 daily_notify pipeline:position advice 是 incremental enrich(失敗回 None,format 端 graceful skip)

---

## 2026-05-17 — GH Releases as bulk-snapshot storage(根治 100MB 上限)

### Added
- **`src/snapshot_release.py`** + **`tests/test_snapshot_release.py`** + **`tests/test_preload_snapshots_release.py`**:institutional 22 月 backfill 累積 ~150MB,撞 GitHub 單檔 100MB git push 上限。LFS 月流量 1GB / 額外計費,不採用。**改走 GH Releases**:單個 asset 上限 2GB、repo 總量無限、不污染 git history(clone 不會帶)、匿名 download 60/hr 對個人專案綽綽。新模組三件 API:
  - `upload_snapshot_to_release(tag, files, notes)` — gh CLI 包一層,`--clobber` idempotent overwrite
  - `download_snapshot_from_release(tag, asset, dest)` — gh CLI 優先,REST API fallback(Streamlit Cloud 沒 gh CLI 走這條)+ SHA cache idempotent skip
  - `get_latest_snapshot_tag(prefix)` — `snapshot-institutional-*` newest first
  Kill-switch `SNAPSHOT_USE_RELEASES_ENABLED=false`,預設 on。Manifest `data/twse_snapshot/.snapshot_releases.json`(tag, asset, size, sha256 對照)入 git。**38 test**(snapshot_release 25 + preload 3-tier fallback 9 + kill-switch 4):upload skip / create / clobber / asset partial failure / download idempotent SHA cache / REST fallback / SHA mismatch reject / kill-switch off / latest tag via gh+REST / make_snapshot_tag format
- **`pyarrow>=15.0.0,<22.0.0`** in requirements.txt:parquet IO(zstd 壓縮,~1/5 CSV 大小)
- **`.gitignore`** 加 `data/twse_snapshot/*.parquet` — parquet 走 release 不入 git

### Changed
- **`scripts/backfill_institutional.py`**:加 `--dump-format {csv,parquet}` + `--upload-release` flag。`--dump-csv` 保留 back-compat(等同 `--dump-format csv`)。`dump_snapshot_csv` 改 thin shim 包 `dump_snapshot(fmt=...)`,後者 parquet 路徑用 `to_parquet(compression='zstd', fallback='snappy')`。Release upload 失敗只 log warning,不 raise(snapshot 仍在 SQLite + 本地檔)
- **`scripts/backfill_financials.py`**:同上,`_dump_csv` 改 thin shim 包 `_dump_snapshot(fmt=...)`
- **`.github/workflows/backfill-institutional-once.yml`** + **`.github/workflows/backfill-financials-once.yml`**:
  - 預設 `dump_format=parquet`(避開 100MB 上限)+ `upload_release=true`
  - 移除「commit parquet → push」step,改 `gh release upload --clobber` + commit `.snapshot_releases.json` manifest
  - Tag 命名:`snapshot-{kind}-{YYYY-MM-DD}`(institutional / financials)
  - Preload step 改先試從 release 拉最新 parquet,失敗再 fallback CSV
- **`src/database.py::preload_snapshots`**:institutional + financials section 改走新 `_ensure_snapshot_present()` helper 3-tier fallback(本地 parquet → release parquet → CSV)。CSV 路徑完全保留,向後相容。release lookup 例外不爆 preload,silently 降級
- **`docs/ARCHITECTURE.md`**:加 §5.10b storage 架構小節 + kill-switch table 補 `SNAPSHOT_USE_RELEASES_ENABLED` + module map 加 `snapshot_release.py`

### Notes
- 主公接下來怎麼 trigger backfill:
  - **Institutional**:GH UI > Actions > **Backfill Institutional (one-shot manual)** > Run workflow → 留 default(`start=2024-01-02, end=2025-11-03, dump_format=parquet, upload_release=true`)→ 30-60 min 完成後到 Releases 頁看 `snapshot-institutional-2026-05-17` asset
  - **Financials**:同上,**Backfill Financials (one-shot manual)** → 30-60 min → `snapshot-financials-2026-05-17`
  - 雲端 Streamlit 下次 boot(redeploy 或定期重啟)自動 download parquet,看 `[PRELOAD] institutional pulled from release ...` log
- Tag 命名約定 `snapshot-{kind}-{YYYY-MM-DD}`,同日多次跑同 kind → 直接 `--clobber` 覆寫,不開新 tag
- Rollback:雲端容器手動跑 `gh release download snapshot-institutional-{舊日期} --pattern institutional.parquet --dir data/twse_snapshot/` 即可回到舊版

---

## 2026-05-17 — Backfill workflow 儲存/配額硬問題(parquet + fail-fast + 分批)

### Fixed
- **`fix(backfill-inst): 改 parquet+zstd 避過 GitHub 100MB 上限`**(已被 GH Releases 根治取代):`scripts/backfill_institutional.py --dump-format parquet` 寫 `institutional.parquet` 用 zstd lvl 9 — 22 月全市場 ~280MB CSV 壓到 ~30MB。**注意**:本日同步加上的 GH Releases 路徑已將 parquet 改不入 git(走 release CDN),`.gitignore` 規則已調整。新增 `pyarrow>=15.0.0` 進 requirements
- **`fix(backfill-fin): 402 quota 爆 fail-fast + 分批 dispatch`**:`scripts/backfill_financials.py` 加 `--batch-start N --batch-end N` + `--max-stocks N`(default 200)讓主公手動跑多批避過 FinMind quota 撞牆。`src/data_fetcher.py` 新 `FinMindQuotaError(FinMindAPIError)` — `_api_call` 偵測 status=402 不再走 long-backoff,直接 raise;`fetch_quarterly_financials` 對 quota 特例 propagate(非 quota 仍 swallow 回空 DF)。`src/_retry.py::with_retry` 加 `no_retry_exceptions` param 跳過特定 exception 的重試。Backfill 中途撞 quota 立刻中斷整批 exit 1,workflow 看到紅燈。9 test:quota propagation / quota abort / with_retry no-retry / batch range / max-stocks cap / batch beyond universe / batch_start >= batch_end + 既有 6 test 不破

---

## 2026-05-17 — 健診歷史補齊 backfill(daily-picks 三件套)

### Added
- **`scripts/backfill_pick_shap.py`** + **`.github/workflows/backfill-pick-shap-once.yml`**(`8497e26`):健診發現 `pick_shap_explanations` 只覆蓋 3 個 pick_date(10 rows),而 `daily_picks` 已有 10 個 trade_date(2026-04-30 ~ 2026-05-15)— daily-notify 沒跑那幾天的 Streamlit / Telegram「為什麼分數高」section 完全空。新 script per (date, sid, strategy) 補算,走 production same routing(per-strategy ML threshold → per-strategy model;否則 general)。默認 skip-existing(idempotent),`--force` 強制覆寫,`--dump-csv` 寫進 snapshot。一次性 dispatch workflow 90 天 backfill ~2 min。9 test:skip-existing / --force / no_model / no_feats / per-strategy routing / general fallback / UPSERT / 空 todo / 日期錯誤 exit 2
- **`scripts/backfill_financials.py`** + **`.github/workflows/backfill-financials-once.yml`**(`22547c9`):健診發現 `financials.quarterly` 只覆蓋 1073 / 2127 純股(~50%),長線基本面策略在 1054 檔上跑不了。2026-05-06 commit 967905e 刪掉舊 backfill script 的判斷(「daily_market_update 會 cover」)實測不成立 — daily_market_update 對全市場逐一打 FinMind 常因 quota / token / FinMind dataset 不存在而 skip。新 script per-stock loop,走 pure_stock universe,默認跳過已有資料(`--force` 重抓),FinMind 失敗 > 25% 提前中斷避免撞 quota。Workflow 2000 待補檔 × throttle 預估 30-60 min,給 120 min timeout。9 test 覆蓋
- **`docs/daily-picks-retention-2026-05-16.md`**(`315157e`):健診發現 `daily_picks` 只有 10 個 trade_date。**確認非 bug**:schema 在 2026-05-04 commit 4b4d24f 才加入,table 才 13 天大。沒 retention 政策在清(PK 含 trade_date),後續會自然累積。無需修 code,只 document

---

## 2026-05-16 — Round 2 健診四件套(silent miss + perf + workflow dedupe + watchlist guard)

> 跨 6 個 commit 的健診修復,主公 5 月 16 日針對「資料源 silent miss + workflow 衝撞 + 推播 cold import 慢 + watchlist push 把 19 檔覆蓋成 4 檔」批次處理。

### Added
- **`feat(warnings): add MOPS default settlement coverage`**(`f461622`):TWSE/TPEx OpenAPI v1 沒對應違約交割 endpoint(`bfigtu.html` 是 SPA),加 MOPS 公開資訊觀測站重大訊息 RSS `mopsrss201001.xml` 過濾「違約」/「違約交割」關鍵字。涵蓋全市場(TWSE+TPEx+興櫃+公開發行),取代 OpenAPI 兩家皆無的缺口。MOPS 0 rows 不在 baseline raise 偵測內(違約事件年數筆,日常 0 rows 屬正常)。Live 驗證 2026-05-16:MOPS 0 rows,total 160 rows,by_type 新增 `default_settlement` 鍵。6 test:parse_filters_by_keyword / extracts_metadata / empty_xml / no_keyword_skip / fallback_date / run_writes_rows / baseline_zero_does_not_raise
- **`feat(backfill): warm-up script for company_profiles FinMind facts`**(`af5d355`):健診 audit 出「company_profiles 0 rows」 — 該 table 是 lazy on-demand cache,只在 UI 開個股深度頁時填,批次/CI 環境永遠 0 rows。新 `scripts/backfill_company_profiles.py` 對 universe(預設 TW_TOP_50,可切 pure_stock)呼叫 `get_company_profile(sid, llm_call=False)` 填 FinMind facts(industry / market / listing / foreign_limit)— **不打 Gemini**,narrative 仍 lazy load。TW_TOP_50 驗證 ok=50 fail=0

### Fixed
- **`fix(warnings): replace TWSE bs4 parser with OpenAPI JSON endpoints`**(`490ca35`):TWSE 4 條 HTML 源(punish/notice/disposition/method.html)在健診四件套全 0 rows — 全是 jQuery SPA,bs4 看不到 row,等於警示股 silent miss。改全 TWSE OpenAPI v1 JSON(swagger 143 paths 確認),沿用 TPEx 成熟 JSON 解析:
  - `/announcement/punish` → disposition(29 rows 進來,原本 0)
  - `/announcement/notice` → attention(假日 0 rows 正常)
  - `/announcement/notetrans` → attention(累計次數補充,1 row)
  - `/exchangeReport/TWT85U` → method_changed(13 rows;欄位陽春無 Date)
  順便修正原本 bs4 版把 `punish.html` 標 `default_settlement` 的分類錯(punish 在 TWSE 命名是「處置」)。違約交割本身改走 MOPS(見上)。**防呆**:`fetch_and_parse_all()` 結尾偵測 TWSE punish + TWT85U baseline 兩條源同時 0 rows → raise(歷史必有資料,同時 0 = endpoint 整體壞掉,CI exit 1)。notice/notetrans/TPEx 三條允許 0 rows(假日合理)
- **`fix(watchlist): guard against boot-fallback regression overwriting remote`**(`4b23f86`):主公 2026-05-16 回報關注少幾檔。`git log --all data/twse_snapshot/watchlist.csv` 顯示 watchlist-sync 分支從 19 檔(6639390)在 37 小時內掉到 4 檔(73b4cf0),剩 4 檔時間戳完全等於 main 種子。**根因**:`safe_boot_load` 在 `fetch_watchlist_from_github` 失敗(網路抖動 / 5xx / PAT 暫驗證失敗)會 fallback 載入 main 種子(少數幾檔),SQLite 變殘缺,任一 add/remove 觸發 `_dump_watchlist_snapshot` → push 殘缺狀態覆蓋 watchlist-sync 真實 19 檔。**修法**:`push_watchlist_to_github` GET remote 後 regression guard,若新 push 相比 remote 遺失 ≥ `WATCHLIST_LOSS_THRESHOLD`(預設 3)檔 → 拒推、log error。trades / paper_trades / analyst_targets 是累積式 snapshot,沒接此 guard。**復原**:把 main 種子 watchlist.csv 回到 6639390 的 19 檔狀態。11 條新 `test_github_sync.py` + 2 條 `test_watchlist.py` e2e。調查全文:`docs/watchlist-loss-investigation-2026-05-16.md`
- **`chore(watchlist): remove invalid sid 9999 from seed CSV`**(`007527a`):seed 內有歷史殘留的測試 sid 9999,趁此次復原一併清掉

### Changed
- **`perf(notifier): lazy-load pandas + screener_short + skip streamlit when not in runtime`**(`7034b39`):cold import `src.notifier` 1.011s → 0.198s(~80% 減少)。
  - `src/config.py`:skip `import streamlit` unless streamlit 已在 sys.modules。CLI / pytest / scheduled scripts 無 Streamlit context,`st.secrets` 反正用不到 — 省 ~0.5s
  - `src/notifier.py`:defer `pandas` + `screen_short` via PEP 562 module `__getattr__`。第一次 attribute access(真實或 monkeypatch)cache 進 module globals 讓內部 LOAD_GLOBAL 找得到。`send_telegram_message` 自己不碰 pandas/screener_short — 只有 `notify_short_picks` 用,所以 caller 像 `data_health_alert.py` / `intraday_alerts.py` / `analyst_target_alerts.py` 付 0.2s 而非 1.0s
  - 全 1438 test pass(含 137 notifier/wire/regime/theme/consensus + entry_range 涵蓋)
- **`chore(workflows): retrain-ml.yml → manual-only`**(`a6a50dc`):兩個 workflow 訓相同 model(short_pick + per_strategy)但 gate 嚴鬆不一 — `ml-weekly-retrain` 週日 03:00 walk-forward A/B gate(strict OOS),`retrain-ml` 週日 22:13 random-split accuracy gate(lenient)。22:13 lenient 跑 19 小時後覆蓋 03:00 strict 跑出的 artifacts。Resolution per `docs/workflow-audit-2026-05-15.md` Candidate #1 option (b):
  - `retrain-ml.yml`:remove `cron`,只留 `workflow_dispatch`,name 改 `ML Retrain (manual emergency, random-split gate)`。Job body 不動
  - `ml-weekly-retrain.yml`:不動,維持唯一 schedule 重訓
  - audit doc 同步更新標 Candidate #1 closed

### Notes
- 本批 6 commit 集中處理「資料 silent miss / cold import 慢 / 模型 artifact 覆蓋 / watchlist push 覆蓋」四類 production 風險,主公拍板優先做完才換 Round 3 寫文檔
- `docs/twse-warnings-still-broken.md` 改寫成「整體現況 + 剩餘限制」(MOPS 加入後不再是 broken-bug 文件)

---

## 2026-05-17 — 盤前快訊(`scripts/morning_brief.py`)

### Added
- **`scripts/morning_brief.py`** 新腳本:每交易日 08:30 Asia/Taipei 推播,讓主公開盤前 30 分鐘看到隔夜變動
  - **重抓 warnings**(包 `fetch_stock_warnings.run`)— 拿 MOPS 凌晨更新
  - **重抓 news**(`fetch_and_store_news`)— 拿 news_fetcher 每小時 cron 之外的早盤新增
  - **find_newly_warned_picks**:對昨晚 picks 的 sid 跑 `stock_warnings` active query → ⚠️ 標出
  - **find_recent_picks_news**:撈昨晚 picks 在 < 12h 內、subject 含 `IMPORTANT_NEWS_KEYWORDS`(重大/違約/裁員/下修/召回/處置/減資/破產 等 30+ 關鍵字)的 news → 📰 標出
  - **diff_picks**:今晨重跑 `_select_top_picks` vs 昨晚比對 → added / removed / reranked
  - **fetch_us_market_sentiment**:yfinance 抓 `^DJI` / `^IXIC` / `^GSPC` 平均 → bearish (≤ -1%) / neutral / bullish (≥ +0.5%)
  - **無變動模式**:三項都空 + sentiment 非 bearish → 推「✅ 盤前無重大變動」一行極簡訊,避免吵主公
  - **kill-switch**:`MORNING_BRIEF_ENABLED=false` → exit 0 不推
  - Telegram 走 HTML(`parse_mode="HTML"`)避開 Markdown entity 解析坑(沿用 news_notify 模式);Discord 走 Markdown
- **`.github/workflows/morning-brief.yml`** 新 workflow
  - `cron: "30 0 * * 1-5"`(00:30 UTC = 08:30 台北,週一~五)
  - `concurrency: morning-brief` 避免兩輪 cron 重疊
  - `timeout-minutes: 15`(refetch ~30s + 推播 ~3s,留 buffer)
  - 跑前先 `db.preload_snapshots()` — fresh container 必須拿到 `daily_picks.csv` 才能做 diff
  - 不 commit / 不 push snapshot(快訊只讀不寫)
- **`tests/test_morning_brief.py`** 25 個 test
  - kill-switch on/off(預設 on,各種 off 值)
  - news 關鍵字 filter(命中 / 不命中)+ < 12h 過濾
  - warning 比對(active hit / 空 / 過期 expired)
  - diff_picks(added / removed / reranked / 兩邊都空)
  - has_any_change(警示 → True / bullish → False / bearish → True)
  - format 訊息(無變動極簡 / 完整 / telegram HTML / discord markdown)
  - fetch_us_market_sentiment(yfinance 缺失 graceful / fake ticker 跌幅判 bearish)
  - run_morning_brief 整合(dry_run + skip_refetch + 無 secrets 不 call send)
  - schema 對齊 production(monkeypatch `config.DATABASE_PATH` + `db.init_db()`)

### Notes
- 不重複推昨晚 22:13 整套(太吵)— 只推變動
- 個別查詢失敗(網路 / TWSE / yfinance)全 graceful — 主邏輯不會被單點故障擋住
- 跟既有 daily_notify(22:13)/ news_notify(每小時)/ stock-warnings 完全獨立,互不影響

---

## 2026-05-16 — Runtime log 持久化(`logs/`)

### Added
- **`src/logging_setup.py`** 新模組:`setup_file_logging(script_name, level, mirror_print)` helper
  - 寫 `logs/{Asia/Taipei date}-{script}.log`,自動跨日 rotate(新檔)
  - stdout `StreamHandler` 同步保留,GH Actions log 不受影響
  - `mirror_print=True` 把 `print()` 也 tee 進檔(daily_notify / intraday_alerts 等混用 print 的 script 開啟)
  - `_Tee` 對 Windows cp950 console 的 `UnicodeEncodeError` 容錯(emoji 用 ascii replace fallback,檔案永遠 utf-8)
  - Idempotent:同 script 重複 setup 不疊 handler;換 script 名稱會把舊 file handler 清掉
- **`tests/test_logging_setup.py`**:8 個 test 涵蓋寫檔 / stdout 保留 / 日期 prefix / rotation / idempotent / format / auto-mkdir

### Changed
- 7 隻 cron 入口加 `setup_file_logging(...)`:
  - `scripts/daily_notify.py` / `scripts/intraday_alerts.py` / `scripts/data_health_alert.py`
  - `scripts/fetch_analyst_targets.py` / `scripts/fetch_stock_warnings.py` / `scripts/news_notify.py`
  - `scripts/daily_fetch.py` / `scripts/daily_market_update.py`
  - 都只在 `main()` argparse 之後 call,**不動既有業務邏輯**
- `.gitignore` 加 `logs/`:runtime log 不入 repo,GH Actions artifact 可選擇性 upload

### Notes
- `scripts/cleanup_artifacts.py` 早就有 `logs/*.log > 7 天 rm` 邏輯(2026-05-15 已加),本輪不重複處理
- 本機驗證:`python scripts/daily_notify.py --dry-run --no-telegram --no-discord` → `logs/2026-05-16-daily_notify.log` 生出,271 行 utf-8 完整保留 emoji
- GH Actions runner 上 logs/ 會在 job 結束後消失(除非顯式 upload-artifact),這次任務只做持久化機制 + .gitignore,artifact upload 留下一輪

---

## 2026-05-15 — Round 1 清理維護

### Changed
- **branches**:刪 8 條已合 main 的 feature branch(local + origin):`funny-khayyam`(vbt sharpe)、`wonderful-carson`(TPEx warnings)、`fervent-mclean`(annotate-only)、`gifted-nightingale`(gap_up rule)、`optimistic-kirch`(ML calibration)、`hardcore-jennings`(regime gating)、`affectionate-cray`(consensus)、`dazzling-poitras`(theme heat)
- **worktrees**:清掉 3 個已合 main 且閒置的 worktree(`cool-solomon`、`nervous-maxwell`、`clever-noether`)
- **`requirements.txt`** 加版本範圍 lock(`>=X,<NEXT_MAJOR`),補上漏宣告的 `requests`,拆 `pytest`/`ruff` 到新 `requirements-dev.txt`
- **新 `requirements-dev.txt`**:`pytest>=8.0.0,<10.0.0` + `ruff>=0.4.0,<1.0.0`(production image 不該帶測試/lint 工具)

### Added
- **`scripts/cleanup_artifacts.py`**:dry-run 預設,`--execute` 才真動;支援 VACUUM `cache.db` / 刪 `logs/*.log` > 7 天 / 刪 `models/**/*.bak` > 30 天(每組最新一份永遠保留)
- **`docs/dependency-audit-2026-05-15.md`**:requirements 變更全紀錄
- **`docs/workflow-audit-2026-05-15.md`**:16 條 GH Actions workflow 用途盤點 + cron 對齊檢查 + 下一步整合候選
- **`docs/storage-audit-2026-05-15.md`**:本機 repo 占用 ~33MB 量測 + cleanup 工具用法

### Notes
- workflow audit 列了一個明確重複(`retrain-ml.yml` × `ml-weekly-retrain.yml`),**Round 1 不動**,等主公拍板下一步
- 本輪 6 份 `*-2026-05-15.md` task spec 整合到本檔(下方),原檔刪除(commit history 可溯源)

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

## 2026-05-15 — May 15 feature wave(6 件批次合併)

> 同日合進 main 的 6 個 feature branch,整合自原本各自的 `*-2026-05-15.md` task spec(已隨 Round 1 清理刪除)。每段附原 doc 的核心結論,細節走 `git log`/`git show` 追溯。

### Added — vectorbt grid Sharpe N-膨脹修法
- **問題**:`src/vbt_backtest.py` grid search 用 `trade-level Sharpe = mean/std × sqrt(N)`,N 是 trade 筆數。N=6000+ 時 sqrt(N) ≈ 77.5× 線性放大,跨策略 / 不同 N 比較失去意義
- **修法**:新 `_compute_daily_sharpe()` helper,把 trade returns 歸到 exit 當天 reindex 完整交易日序列(沒交易日 = 0 報酬),用 daily 報酬序列算 annualized Sharpe (× sqrt(252))
- **新欄位**:`sharpe_daily` 並列舊欄 `sharpe`(deprecated)。UI 預設用新欄排序 + 顯示
- **Branch**:`claude/funny-khayyam-d33b7d`

### Added — gap_up 策略最終決策(rule-based + 拔掉 ML 過濾)
- **症狀**:gap_up walk-forward ROC AUC = 0.4926(接近 random),`max_depth=5 / min_samples_leaf=10` 微改善後仍卡住
- **發現**:gap_up 訊號**有 edge** (+7.3pp vs baseline) 但**邊緣**(48% 沒過 50%)。真正 sub-edge 在 `vol_ratio` 1.5-3x sweet spot(WR 50.3%),`>3x` 群 WR 44.8%
- **決策**:選路 B —「rule-based 過濾收緊到甜蜜點 + 下架 ML 過濾」
  - 加 `gap_vol_ratio_max=3.0` rule(+1.4pp 提升,51.6% vs 50.2%)
  - 從 `STRATEGY_ML_THRESHOLDS` 移掉 `gap_up`(WF ROC 0.49 的 model 等於 noise filter)
  - 從 `STRATEGY_RF_PARAMS` / `eval_walkforward.DEFAULT_PER_STRATEGY` 移除 → 停訓 `gap_up.pkl`
- **Backtest 252d WR**:51.6%(vs baseline 40.7%,edge +10.9pp)
- **Branch**:`claude/gifted-nightingale-700ce8`

### Added — ML 機率校準 (probability calibration)
- **問題**:RF `predict_proba` over-confidence — UI 顯「AI 勝率 70%」實際只命中 55%(誤導決策)
- **修法**:base RF 訓完留**最後 20% 樣本(time-based holdout)**fit `IsotonicRegression` calibrator;predict 時 raw_prob → calibrator.transform → 校正 prob
- **新檔**:`models/calibrators/{strategy}.pkl`,跟 base model 同生命週期 retrain
- **Branch**:`claude/optimistic-kirch-72eabe`

### Added — 大盤 Regime Gating(推薦數量 + ML threshold 動態調整)
- **動機**:歷史 backtest 顯示大盤空頭時所有策略 hit rate 一起掉 — 但舊系統推薦數量 + threshold 沒根據 regime 動態調整,空頭時還照常推 10 檔 = 拿石頭砸自己腳
- **設計**:三層 regime(本模組獨立,跟 `src/market_regime.py` 並存)
  | Regime | 條件 | 短線上限 | 長線上限 | ML threshold uplift | Caption |
  |--------|------|----------|----------|---------------------|---------|
  | bull | 5MA>20MA>60MA + 60MA 斜率 > +0.5% | 10 | 10 | 0.00 | 📈 |
  | range | MA 交錯 / 斜率平 / correction | 5 | 7 | +0.05 | 📊 |
  | bear | 5MA<20MA<60MA + 60MA 斜率 < -0.5% | 2 | 5 | +0.15 | 📉 + ⚠️ |
- **跟 `market_regime.py` 區隔**:`market_regime` 是「策略類別篩選」(4-tier 看 close vs MA);`regime_gating` 是「推薦數量 + threshold 縮量」(3-tier 看斜率)
- **Branch**:`claude/hardcore-jennings-3b729f`

### Added — 跨策略共識加成 (consensus boost)
- **動機**:17 套策略各自跑 Top N,「同一檔被多策略同時看見」是被丟掉的強訊號 — 歷史 backtest 顯示這類個股 precision 比單策略高 10~15%
- **設計**:用「**類別維度**」共識(跨策略類別 — 趨勢/反轉/籌碼/動能/基本面/殖利率/大盤相對 7 類)優於「票數維度」(同類別兩策略亮 = 同現象兩 lens)
- **計分**:在原 `score = ml_prob × strategy_weight` 上加 multiplier(2 類別 = ×1.05,3 類別 = ×1.10,4+ 類別 = ×1.15)
- **UI**:推播 + 卡片加 ⭐ badge 標出共識股
- **Branch**:`claude/affectionate-cray-b477ec`

### Added — 題材熱度動態權重(冷題材 hard exclude)
- **起因**:5 日題材表現 — 🔥 HBM/矽光子/CoWoS 噴(+5~9%);🧊 重電/國防/低軌衛星 修正(-0.6~-10%)。主公要冷題材暫時降權
- **v1**(內部):soft 降權 ×0.7
- **v2 拍板**:**hard exclude** — 冷題材成分股直接不推播。理由:soft 降權雜訊大,擋掉乾淨
- **公式**:對 `data/themes/*.yaml` 每個題材撈成分股近 5 日:
  ```
  heat_score = avg_return × 0.6 + win_rate × 0.4
  ```
  熱題材 → score multiplier ×1.10;冷題材 → 排除
- **Branch**:`claude/dazzling-poitras-209d36`

### Notes(整合 wave 共通)
- 本批合併同時觸動 picks 排序(`_select_top_picks`):consensus boost + theme heat multiplier 都在 score 階段,regime gating 在 truncate 階段。三者疊加效果預期下週開始觀察
- ML 校準 + gap_up 拔 ML threshold + regime threshold uplift 三件事一起改了「ML 介入推播」的 plumbing — 若推播數量明顯掉,先查 regime(空頭時 truncate 到 2 是 by design)

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
