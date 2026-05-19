# 推播精英化盤點報告 2026-05-19

> 軍師寫給主公拍板，**不立即動手**。
> 任務：把推播從「多到看不過來」收斂成「Top 5 精英」。
> 報告涵蓋全盤點、欄位選項、軍師判讀、4 個方案對比。
>
> ⚠️ **盤點視角**：本報告以 `origin/main` 為基準（worktree 落後 main，已從 main 拉檔對齊）。

---

## 摘要（一頁版）

- **實際存在的 19 個推播相關 workflow**（main 為準）：4 個只 fetch（資料管線）、13 個會推 Telegram + Discord、1 個只推 Telegram（ml-weekly-retrain）、1 個 5 min 雙向 bot polling。
- **主公列的 4 個「剛加」workflow 確認都在 main**：`monthly-strategy-report.yml`、`heartbeat-check.yml`（= cron_health_alert 概念）、`morning-brief-health-monitor.yml`、`quota-reset-alerts.yml`。軍師第一輪盤點誤判為「不存在」是因為 worktree 落後 main，已修正。
- **「最有信心」目前實際排序公式**：`ml_prob × strategy_weight × consensus_multiplier × theme_multiplier`，定義在 `src/notifier.py:303 _compute_pick_score`。沒有獨立 `conviction_score` 欄位（grep 0 hits），主公提到的 (a) 選項在 codebase 不存在，需另起 PR 才有。
- **目前主要雜訊源**：`intraday-alerts.yml`（盤中每 30 分鐘）、`daily-notify.yml`（22:13 四個 section + 多 caption）、`news-notify.yml`（每小時，目前已暫停推播）。
- **「監控類」雜訊低**：`heartbeat-check` / `morning-brief-health-monitor` / `data-health-alert` 都 silent-on-healthy，正常狀態完全不推。
- **軍師推薦方案 B（精英版）**：morning_brief Top 5（含警示）/ daily_notify Top 5 短 + Top 3 長 / intraday 只推持倉股 / news 改 daily / 加突發大跌警報。詳見 Part D。

---

## Part A：當前推播全盤點

### A.1 主表（依台北時間排序，main 為準）

| # | Workflow | cron (UTC) | 台北時間 | Script | TG | Discord | 訊息長度估計 | 雜訊 |
|---|---|---|---|---|---|---|---|---|
| 1 | quota-reset-alerts | `30 16 * * *` + `0 1 1 * *` | 每天 00:30 / 每月 1 號 09:00 | `alert_quota_reset.py` | ✅ | ✅ | 短（5-8 行模板，含手動 trigger URL） | 🟢 |
| 2 | telegram-bot-poll | `*/5 * * * *` | 每 5 min | `telegram_bot_serve.py --once` | ✅（雙向） | ❌ | 視主公發問 | 🟢（被動，主公發才回） |
| 3 | data-health-alert | `0 1 * * *` | 每天 09:00 | `data_health_alert.py` | ✅ | ✅ | 短（stale 才推，全好 silent） | 🟢 |
| 4 | morning-brief | `30 0 * * 1-5` | 交易日 08:30（主班用 cron-job.org 外部 cron，GHA 是 fallback） | `morning_brief.py` | ✅ | ✅ | 中-長（變動才推；無變動極簡） | 🟡 |
| 5 | morning-refetch | `30 1 * * 2-6` | 09:30 | `daily_fetch.py` + `daily_market_update.py` | ⚠️ 間接 | ⚠️ 間接 | 短（觸目標價才推） | 🟡 |
| 6 | morning-brief-health-monitor | `30 1 * * 1-5` | 交易日 09:30（08:30 推播 + 1 hr buffer） | `check_morning_brief_health.py` | ✅（**只 TG**） | ❌ | 短（24h 內無成功 run 才推；全好 silent） | 🟢 |
| 7 | monthly-strategy-report | `0 1 1 * *` | 每月 1 號 09:00 | `monthly_strategy_report.py` | ✅ | ✅ | 中（上月各策略 N/WR/AvgD5/Sharpe，含 TAIEX baseline 比較） | 🟢（一月一次） |
| 8 | heartbeat-check（cron_health_alert）| `0 10 * * *` | 每天 18:00（週日加 `--weekly-checkpoint`）| `cron_health_alert.py` | ✅ | ✅ | 短（stale 才推 + 週日定期 checkpoint；平日全好 silent）| 🟢 |
| 9 | stock-warnings | `13 9 * * 1-5` | 17:13 | `fetch_stock_warnings.py` | ❌ | ❌ | 0（fetch only，下游 morning_brief 才推） | 🟢 |
| 10 | weekly-shareholder | `0 18 * * 6` | 週日 02:00 | `fetch_shareholder_concentration.py` | ❌ | ❌ | 0（fetch only） | 🟢 |
| 11 | backtest-weekly | `0 18 * * 5` | 週六 02:00 | `backtest_picks.py` | ❌ | ❌ | 0（fetch only） | 🟢 |
| 12 | ml-weekly-retrain | `0 19 * * 6` | 週日 03:00 | `train_ml_model.py` + A/B gate | ✅ | ❌ | 短（1 行 summary） | 🟢 |
| 13 | weekly-brief | `0 2 * * 0` | 週日 10:00 | `send_weekly_brief.py` | ✅ | ✅ | 中（5 sections × 3-5 行） | 🟢 |
| 14 | weekly-targets | `13 14 * * 0` | 週日 22:13 | `fetch_analyst_targets.py --scope=all` | ✅ | ✅ | 中（異動股全列） | 🟡 |
| 15 | intraday-alerts | `*/30 * * * 1-5` | 交易日每 30 min | `intraday_alerts.py` | ✅ | ✅ | 短（每條 1-2 行；無觸發 silent） | 🔴 |
| 16 | daily-notify | `13 14 * * 1-5` | 交易日 22:13 | `daily_notify.py` → `notify_top_picks` | ✅ | ✅ | **最長**（4 sections + 多 caption） | 🔴 |
| 17 | daily-notify-only | 手動 | 主公補推 | 同上（fast path） | ✅ | ✅ | 同上 | N/A |
| 18 | news-notify | `0 * * * *` | 每小時整點 | `news_notify.py` | ⚠️ **暫停** | ⚠️ **暫停** | 中（5 則為一批） | 🟢（已關） |
| 19 | retrain-ml | 手動 | 主公觸發 | 同 ml-weekly-retrain（random-split gate） | ❌ | ❌ | 0 | N/A |

### A.2 主公列的 4 個 workflow — 已在 main，已對齊

軍師第一輪 grep 漏掉是因為 worktree 在 `claude/kind-swartz-d14442` branch，main 已領先 5 個推播 workflow + 4 個 script。實際對應如下：

| 主公列 | main 上實際 workflow | 行為 |
|---|---|---|
| `monthly_report`（月初 09:00） | `monthly-strategy-report.yml`（cron `0 1 1 * *` = 每月 1 號 09:00 台北） | 推上個月每策略 N/WR/AvgD5/Sharpe，含 TAIEX baseline |
| `heartbeat_alert`（每天 18:00） | `heartbeat-check.yml`（cron `0 10 * * *` = 每天 18:00 台北） | 掃 `sync_log_heartbeat.csv`，stale 才推；週日強制 weekly checkpoint |
| `quota_reset_alerts` | `quota-reset-alerts.yml`（每天 00:30 → company_profiles / 每月 1 號 09:00 → financials） | 配額重置時推「可手動 trigger backfill」+ GH Actions URL |
| `cron_health_alert` | 同 `heartbeat-check.yml`（這就是 cron_health 概念實作；script 名 `cron_health_alert.py`） | 見上 |

**額外 main 上有但主公沒列的 2 個**：
- `morning-brief-health-monitor.yml`（09:30 守門 morning-brief 08:30 是否成功，**只推 Telegram**，主公拍板 2026-05-19 三層防護 PR #28）
- `telegram-bot-poll.yml`（5 min 雙向 bot daemon，PR #2，主公路上隨手問股票）

### A.3 訊息範例截錄

**morning-brief**（`scripts/morning_brief.py:673-738`，盤前 08:30，HTML）

```
🌅 盤前快訊 2026-05-19

⛔ 達停利價警報
• 2330 台積電 — 已達停利 1250.00（昨收 1248.00, +0.5%）

⚠️ 警示更新 (vs 昨晚)
• 5483 中美晶 — 新增「處置股」警示，建議不進場

📰 重大 news (近 12h)
• 2330 台積電 —「Q1 法說會」08:15
• 2454 聯發科 —「股利政策公布」07:30

🔁 推薦變動 (vs 昨晚 22:13)
• 新增 #3: 6488 環球晶
• 移除: 2308 台達電
• 排序 #1: 2330 台積電 (原 #2)
```

**daily-notify**（`src/notifier.py:888-918 format_pick_block`，盤後 22:13，HTML）

```
📈 短線精選 Top 10

▎#1  2330 台積電 ⭐⭐⭐
   收盤 1248.00 ↑ +0.5%
   🔥 半導體 (今日 12 檔同類)
   📊 命中 3 策略
       · KD 黃金交叉
       · 三紅兵
       · 突破 60MA
   🤖 ML 機率 78%
   ✨ 進場 1230-1240 / 停損 1190 / 停利 1320
   ⚖️ Risk:Reward 1:2.4

▎#2  …（如上格式 × 9 筆）

📊 共識統計: ⭐⭐⭐ 強共識 3 檔 / ⭐⭐ 共識 8 檔
```

**單則訊息估計 25-40 行 × 10 picks = 約 1200-1800 chars**；加上其他 3 section（昨日複盤 / 高信心交集 / 大戶進場）整體可達 4000-6000 chars。**這是目前最重的雜訊源**。

**intraday-alerts**（`scripts/intraday_alerts.py:259-285`，盤中每 30 min，純文字）

```
⛔ 2330 台積電 跌破停損 1198.00（目前 1195.00, -0.25%）
💰 6488 環球晶 進場時機 832.00（目前 830.00）
🚀 2454 聯發科 突破壓力 900.00（目前 902.00, +0.22%）
```

**weekly-brief**（`src/system_brief.py:647-705`，週日 10:00，Markdown）

```
📋 系統結論週報 · 2026-W19

🟢 系統健康
資料新鮮 ✓ · daily +0d / inst +0d

🔥 本週發燙策略
1. KD 黃金交叉 · WR 67% / D5 +2.3% · N=18
2. 三紅兵 · WR 60% / D5 +1.8% · N=15

🥶 該休息
1. RSI 超賣反彈 · WR 33% / D5 -0.5% · N=12

🌡️ 市場狀態
regime: 🟢 多頭
法人共識 7 天趨勢：📈 12→18
千張戶進場：8 檔

🎯 觀察清單
1. [2330] 台積電 — 法人共識 + 千張戶連 3 日加碼
2. ...

📊 本週真實績效（推播後 5 天）
N=23, WR 56%, Avg +1.4%
```

### A.4 「最有信心」目前怎麼算

定義在 `src/notifier.py:303-354 _compute_pick_score`：

```python
weighted_ml = ml_prob × avg(strategy_weights) × consensus_multiplier × theme_multiplier
sort_key = (-100 if 有法人共識 else 0, -weighted_ml, -命中策略數, sid)
```

換言之，目前 Top N 排序是 **加權 ML 機率**（不是純 ml_prob、不是 EV、不是「conviction_score」這個欄位）。

---

## Part B：精英版欄位選項

### B.1「最有信心 Top 5」排序方法（5 選 1）

| 編號 | 方法 | 公式 | 優點 | 缺點 |
|---|---|---|---|---|
| **a** | `conviction_score` 加成最高 5 | ❌ **此欄位 codebase 不存在**（grep 0 hits） | — | 要先建欄位才能選；不建議 |
| **b** | `ml_prob` 純最高 5 | `sort by ml_prob desc` | 最直觀；ML 信心明確 | 忽略策略共識 / 題材 / 法人 → 可能集中在 1-2 個策略 |
| **c** | `EV`（期望報酬 = ml_prob × R - (1-ml_prob)×1）最高 5 | `sort by ev desc` | 直接反映期望賺多少 | EV 高常 = R:R 高 = 停損深，倉位難放大；若 risk_reward 欄缺失會 fallback |
| **d** | `consensus_multiplier ≥ 1.5x`（真共識）內 `ml_prob` 最高 5 | 兩層 filter：先濾共識，再排 ML | **最嚴格** — 確保 ML+ 跨策略雙重 confirm | 共識股可能 < 5 檔，需 fallback；對單策略強勢股不友善 |
| **e** | **目前的 weighted_ml**（_compute_pick_score 現況） | `ml_prob × strategy_w × consensus_mult × theme_mult` | 已上線；綜合多訊號 | 主公拍板要砍掉就要說，不然其實已是「精英排序」 |

**軍師建議**：**選 (d) 真共識內 ml_prob**，理由：

- 主公要的是「最有信心」，共識多策略命中 = 多個獨立訊號同向，這比單 ml 純高分更可信。
- (e) 雖然已是 weighted，但 theme_multiplier 會把熱題材 ×1.3 推上去，主公可能不喜歡題材股佔太多。
- 若 (d) 不足 5 檔，**fallback 補 (e) 前段**（不用 (b)，避免漏 consensus）。

---

### B.2 顯示欄位（15 選 5-7）

主公決定保留哪些。標 ⭐ 是軍師建議**必留**，標 △ 是有也好沒也罷，標 ✗ 是建議砍。

| 編號 | 欄位 | 範例 | 軍師建議 | 理由 |
|---|---|---|---|---|
| 1 | `stock_id + 中文名` | `2330 台積電` | ⭐ 必留 | 沒這個沒辦法看 |
| 2 | 收盤 + 漲跌% | `1248.00 ↑ +0.5%` | ⭐ 必留 | 進場價判斷 |
| 3 | `ml_prob` | `🤖 ML 78%` | ⭐ 必留 | 主公拍板的核心信心指標 |
| 4 | 命中策略名（前 3） | `KD 黃金交叉 / 三紅兵` | ⭐ 必留 | 「為什麼」一行交代 |
| 5 | `consensus_multiplier` 級別 | `⭐⭐⭐ 強共識` | ⭐ 必留 | 與 (d) 排序對應，要看到 |
| 6 | 進場/停損/停利 | `進 1230-1240 / 停損 1190 / 停利 1320` | ⭐ 必留 | 操作要點，沒它推播沒用 |
| 7 | `EV` / Risk:Reward | `R:R 1:2.4` | △ 建議留 | 短一行可秀；想極簡可砍 |
| 8 | 建議倉位（`suggest_position_size`） | `建議 3% = NT$30,000` | △ 建議加 | P2-8 已實作，但目前訊息**沒秀**；加了主公更直接 |
| 9 | 題材 + 同類熱度 | `🔥 半導體 (12 檔同類)` | △ 砍掉 | 雜訊；強共識本身已隱含題材 |
| 10 | SHAP 推薦原因 | `近 20 日量縮 + RSI 反彈` | △ 砍掉 | 多 1-3 行字；ML 機率已交代 |
| 11 | 風險警示（注意 / 處置 / 違約交割） | `⚠️ 處置股` | ⭐ 必留 | 安全護欄，不能砍 |
| 12 | 大盤 regime | `regime: 🟢` | ✗ 砍 | 全篇開頭講一次就好，不要每檔重複 |
| 13 | K 線型態（三紅兵等） | `三紅兵 + 跳空` | ✗ 砍（併入 #4） | 已在「命中策略」裡 |
| 14 | watchlist marker ⭐ | `⭐ 關注中` | ⭐ 必留 | 主公自己加的關注，看一眼有沒有 hit |
| 15 | 法人目標價 | `法人目標 1320 / 共識 8 家` | △ 看主公 | 已有就秀；沒共識的不秀就好 |

**軍師推薦組合（7 欄位）**：1, 2, 3, 4, 5, 6, 11 + 浮動 14（watchlist 有才秀）

範例呈現（單筆預估 4-6 行）：

```
▎#1  2330 台積電 ⭐⭐⭐ ⭐關注
   1248.00 ↑ +0.5% · ML 78%
   📊 KD 黃金交叉 + 三紅兵 + 突破 60MA
   ✨ 進 1230-1240 / 停 1190 / 利 1320
```

Top 5 全篇預估 25-30 行 × 25 字 ≈ 700 chars（vs 目前 ~1800 chars，**減 60%**）。

---

## Part C：軍師判讀（必留 / 可砍 / 應加）

### C.1 必須保留（不能砍）

| Workflow | 為什麼 |
|---|---|
| **morning-brief** | 盤前判讀（警示更新 + 推薦變動 + 達停損/停利），是主公開盤前 5 分鐘做決策的唯一依據。砍掉等於瞎子上場。 |
| **daily-notify**（22:13） | 每日選股主推輪，砍了就沒短線 picks。但內容要瘦身（Part B）。 |
| **intraday-alerts** | **但要改成「只推持倉股 + watchlist」**（見 C.3）。目前每 30 min 全市場掃，雜訊太大；改成只推主公持倉的個股觸停損/停利/進場價，每天最多 2-5 則。 |
| **data-health-alert** | silent-on-healthy，雜訊本來就低；資料壞掉沒這個就抓瞎，必留。 |
| **weekly-brief** | 週一次系統健康度 + 發燙/休息策略，主公做策略迭代的唯一輸入。 |

### C.2 可以砍

| Workflow | 為什麼 |
|---|---|
| **news-notify**（已暫停） | 主公已關（`--no-telegram --no-discord`），DB 還寫；軍師建議**改成 daily news brief 併入 morning-brief**，把近 12h 5 則重大 news 直接秀在盤前快訊，省一個推播輪。 |
| **weekly-targets**（週日 22:13） | 法人目標價異動推播，與 weekly-brief 觀察清單高度重疊，可併入 weekly-brief 一個 section。 |
| **ml-weekly-retrain summary**（週日 03:00） | 純技術訊息，主公看了也不會操作。**改寫到 weekly-brief 開頭一行**（「本週 ML 重訓：short_pick KEEP / per_strategy 3 KEEP 1 ROLLBACK」），省一個推播。 |
| **daily-notify 的 4 個 section** | 「昨日複盤」「大戶進場 Top N」可移到 weekly-brief 或變成 footer 一行 caption；「高信心三維交集」併入 Top 5（強共識本來就會浮上來）。**剩 1 個 section：短線 Top 5**。 |

### C.3 應該加（主公沒提，但軍師認為缺）

| 新增推播 | 為什麼 |
|---|---|
| **突發大跌警報** | 持倉股盤中跌 -5%+ **立即推**（不等盤後），目前 `intraday-alerts` 有 stop_loss 但門檻是固定價，建議加一條「intraday_drop_5pct」每 30 min 掃一次。 |
| **長線推薦 Top 3**（盤後 + 短線併推） | 主公的 PRD 第 1 節寫「短期 + 長期」，但目前推播 99% 是短線。建議 daily-notify 加 1 小 section「📌 長線觀察 Top 3」（基於財報 + 估值 + 現金流），每週推 1 次或每日 3 檔。 |
| ~~cron 心跳監控~~ | **已存在**：`heartbeat-check.yml`（PR #3）+ `morning-brief-health-monitor.yml`（PR #28）兩層守門。無需新增。 |
| ~~monthly_report~~ | **已存在**：`monthly-strategy-report.yml`（PR #25）。無需新增；但目前訊息內容可能不在「精英化」格式內，主公可選擇瘦身。 |

**真正還缺的**：
- 突發大跌警報（持倉 -5%+ 即時推）— intraday-alerts 強化
- 長線 Top 3 — daily-notify 加 section

---

## Part D：4 個方案對比

### 方案 A：極簡（主公明確要求版）

| 項目 | 內容 |
|---|---|
| 保留 | `morning-brief`（瘦身版）+ `daily-notify`（只 Top 5 短線） |
| 砍掉 | 其他 12 個推播全部砍 / 暫停 |
| 訊息量 | **每天 2 則** |
| 風險 | 漏警示（stock_warnings 沒併入）、漏目標價達標、漏 cron 死亡、漏盤中急跌、無週報沒法 retrospect |
| 工程量 | 低（只動 yml disable + 縮 notifier output） |

### 方案 B：軍師推薦（精英版）✅

| 項目 | 內容 |
|---|---|
| 保留 + 精簡 | `morning-brief`（Top 5 + 警示 + 近 12h news 5 則）/ `daily-notify`（Top 5 短線 + Top 3 長線觀察）/ `intraday-alerts`（**只持倉/watchlist 觸發**）/ `weekly-brief`（含 ML 重訓 summary + 法人目標異動）/ `monthly-strategy-report` / `data-health-alert` / `heartbeat-check` / `morning-brief-health-monitor` / `quota-reset-alerts` / `telegram-bot-poll`（雙向 bot 不動） |
| 新增 | 突發大跌警報（持倉 -5% 立即推）+ 長線觀察 Top 3 section（併入 daily-notify）|
| 砍掉 | `news-notify` hourly（併入 morning-brief）/ `weekly-targets` 獨立輪（併入 weekly-brief）/ `ml-weekly-retrain` 獨立推播（併入 weekly-brief）/ `daily-notify-only`（保留手動工具不變）|
| 訊息量 | **平日 2-4 則 / 週末 1 則 / 異常時 +1-2 則 / 監控類全好 silent** |
| 風險 | 中（intraday 改持倉觸發後，無持倉日就完全靜默）|
| 工程量 | 中（要動 morning_brief + daily_notify + intraday_alerts 三個 script + 縮 monthly_report 訊息格式；監控類 workflow 不動）|

### 方案 C：平衡

| 項目 | 內容 |
|---|---|
| 保留 | 全部 14 個 workflow 不砍 |
| 精簡 | **每個推播訊息長度砍 50%**（套 Part B 7 欄位 + Top 5）|
| 新增 | 無 |
| 訊息量 | **每天 5-8 則**（同今天但每則 50% 字數）|
| 風險 | 低（保留所有觸發點，只縮內容） |
| 工程量 | 中（只動 notifier format 函式，不動 workflow yml） |

### 方案 D：全自訂

| 項目 | 內容 |
|---|---|
| 機制 | 提供 `notification_config.yml` toggle 表：每個 workflow `enabled: true/false` + `top_n: 5` + `fields: [stock_id, close, ml_prob, ...]` |
| 主公操作 | 自己編輯 yml，重新 deploy |
| 訊息量 | **完全由主公決定** |
| 風險 | 主公一開始要花 30-60 min 調參 |
| 工程量 | 高（要做 config loader + 全 notifier 重構 + 文件） |

### 方案對比一表

| 維度 | A 極簡 | B 軍師推薦 | C 平衡 | D 全自訂 |
|---|---|---|---|---|
| 每日訊息數 | 2 | 2-4 | 5-8 | 自訂 |
| 漏警示風險 | 🔴 高 | 🟢 低 | 🟢 低 | depends |
| 漏盤中急跌 | 🔴 高 | 🟢 低（持倉觸發）| 🟡 中 | depends |
| 工程量 | 🟢 低 | 🟡 中 | 🟡 中 | 🔴 高 |
| 適合對象 | 主公只想要選股 | 主公要選股 + 不漏風險 | 主公要全部但嫌長 | 主公愛調 |
| 上線時程 | 1 天 | 3-5 天 | 1-2 天 | 1-2 週 |

---

## 軍師建議主公拍板順序

1. **先選 Part D 方案**（A / B / C / D）
2. **如選 B 或 C，再選 Part B.1 排序方法**（建議 d 真共識內 ml_prob，fallback (e) weighted）
3. **再勾選 Part B.2 顯示欄位**（軍師建議 7 欄組合，可加減）
4. **再決定 Part C.3 新增推播優先級**（突發大跌 > cron 心跳 > monthly_report）

拍板後軍師再開 PR 動工。
