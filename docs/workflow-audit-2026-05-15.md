# GitHub Actions Workflow Audit — 2026-05-15

> Round 1 即時清理:盤點 16 條 workflow 用途、cron 排程合理性、潛在整合機會。
> **本輪只盤點不刪改**;整合建議列為「下一步候選」,主公確認後 Round 2 再動。

## 16 條 workflow 一覽

| # | Workflow | Cron (UTC / TPE) | 用途 | 資料變動頻率 | 對齊 |
|---|---|---|---|---|---|
| 1 | `backfill-dividend.yml` | `13 14 * * 0` (週日 22:13 TPE) | 8-shard 全市場配息回補 | 年級 | ✓ 週跑(年級資料用週跑足夠) |
| 2 | `backfill-history.yml` | manual only | 一次性 90 天歷史回補 | n/a | ✓ 不該排程 |
| 3 | `backfill-revenue.yml` | `13 14 * * 1` (週一 22:13 TPE) | 8-shard 全市場月營收回補 | 月級 | ✓ 週跑(月公布資料週跑足夠) |
| 4 | `backtest-weekly.yml` | `0 18 * * 5` (週六 02:00 TPE) | pick_outcomes evaluate | 週級盤後 | ✓ |
| 5 | `daily-notify-only.yml` | manual only | Fast-path notify(reuse 資料) | n/a | ✓ 救急用 |
| 6 | `daily-notify.yml` | `13 14 * * 1-5` (週一-五 22:13 TPE) | 主推播 + 全 fetch + market_update | 每日 | ✓ |
| 7 | `data-health-alert.yml` | `0 1 * * *` (每天 09:00 TPE) | 5 表新鮮度掃,stale 推 alert | 每日 | ✓ |
| 8 | `intraday-alerts.yml` | `*/30 * * * 1-5` (週一-五每 30 分) | paper_trades 停損/突破 | 盤中 | ✓ |
| 9 | `ml-weekly-retrain.yml` | `0 19 * * 6` (週日 03:00 TPE) | Walk-forward A/B gate retrain | 週級 | ⚠️ 與 #12 重疊 |
| 10 | `morning-refetch.yml` | `30 1 * * 2-6` (週二-六 09:30 TPE) | 補抓 22:13 那輪未 publish 的資料 | 每日 | ✓ |
| 11 | `news-notify.yml` | `0 * * * *` (每小時整點) | TWSE 重大訊息推播 | hourly | ✓ |
| 12 | `retrain-ml.yml` | `13 14 * * 0` (週日 22:13 TPE) | Accuracy-gate retrain(舊版) | 週級 | ⚠️ 與 #9 重疊 |
| 13 | `stock-warnings.yml` | `13 9 * * 1-5` (週一-五 17:13 TPE) | TWSE/TPEx 警示股紀錄 | 每日盤後 | ✓ |
| 14 | `weekly-brief.yml` | `0 2 * * 0` (週日 10:00 TPE) | 週報 Telegram 推播 | 週級 | ✓ |
| 15 | `weekly-shareholder-fetch.yml` | `0 18 * * 6` (週日 02:00 TPE) | TDCC 集保股權分散 | 週級 | ✓ |
| 16 | `weekly-targets.yml` | `13 14 * * 0` (週日 22:13 TPE) | 全市場法人目標價 | 週級 | ✓ |

**結論:資料變動頻率對齊合理** — 月/年級資料(dividend / monthly_revenue)都是週跑,沒有每天浪費 quota 抓。每日盤後資料(daily_prices / institutional / warnings)都是收盤後跑。

## 同時段 cron 衝突檢查

**週日 22:13 TPE (UTC 14:13) 同時觸發**:
- `backfill-dividend.yml` (8 shard,~5 min wall clock)
- `retrain-ml.yml` (~10-15 min)
- `weekly-targets.yml` (~30-60 min)

GitHub Actions 並發本身 OK(parallel runner),**但三個都會 commit/push 到 main**,有 race condition 風險。每個 workflow 各自有 `concurrency.group`,但 group 名不同,所以**互不擋**。

> 建議:若主公看到過 weekly push 失敗或亂序的情況,Round 2 加一個共用 `git-push-main` concurrency group 或排錯時段。

**週日 02:00 / 02:13 TPE 也有兩個 weekly push**:
- `weekly-shareholder-fetch.yml` (週日 02:00 TPE)
- `backtest-weekly.yml` (週六 02:00 TPE)

注解寫「錯一天避免 race」,設計合理。

## 整合候選(本輪不動)

### Candidate #1:`retrain-ml.yml` ↔ `ml-weekly-retrain.yml` 二合一

**現況**:
- `ml-weekly-retrain.yml`(週日 03:00 TPE):走 walk-forward A/B gate(嚴格 OOS)
- `retrain-ml.yml`(週日 22:13 TPE):走 random split accuracy gate(寬鬆 backward-compat)

注解明寫:「**週日早上 03:00 跑完後,如果 KEEP 結果推上 main,當天 22:13 retrain-ml 重跑只是 random split 二次驗證**」。

**問題**:
- 同一週訓兩次,等於 22:13 那次的 model 覆蓋掉早上嚴格 gate 過的 model
- random-split gate 比 walk-forward 寬鬆,理論上 22:13 那次更可能 KEEP,反而把 03:00 那個更嚴的版本蓋掉
- 兩個 workflow 都 commit `models/*.pkl` 到 main → push race 風險(雖然 22:13 那個會 cancel-in-progress: false)

**整合建議(下一步,Round 2 再做)**:
- (a) 砍掉 `retrain-ml.yml`,只留 `ml-weekly-retrain.yml`(嚴格版)
- (b) 或是 `retrain-ml.yml` 改成 `workflow_dispatch only`(緊急手動 retrain),不排 cron
- 主公拍板選哪個

### Candidate #2:三個 weekly backfill 統一排程協定

`backfill-dividend`(週日 22:13)、`backfill-revenue`(週一 22:13)— **設計已經分散到不同天**,沒重疊。

注解寫「跟 backfill-financials.yml 同模式,差在不同 cron 時間(週一 vs 週六 vs 週日),三個 backfill workflow 分散在不同天避免 8×3=24 並發」 — 但目前 `backfill-financials.yml` 不存在,可能是被刪掉或未合進來。**現狀無需動**,但若未來新加可參照此分散原則。

## 不該動的(明確各司其職)

- `daily-notify.yml` + `daily-notify-only.yml`:後者是純 manual fast-path 救急,不可砍
- `daily-notify.yml` + `morning-refetch.yml`:不同時段、不同職責(主推 vs 隔日補抓),合理拆分
- `news-notify.yml`:hourly 跑,public repo Actions 分鐘無限,沒 quota 壓力

## 行動項

| 行動 | Owner | 時機 |
|---|---|---|
| 確認 retrain-ml 整合方向 (a) 砍掉 / (b) 改 manual | 主公 | Round 2 前拍板 |
| 監測週日 22:13 三條 workflow push race(觀察 1-2 週) | 自動觀察 | 持續 |
| 若觀察到 race → 加 `git-push-main` 共用 concurrency group | Round 2 | 視觀察結果 |

## 結論

**16 條 workflow 整體健康**:
- ✓ 排程頻率對齊資料變動頻率(年/月級 → 週跑;日級 → daily;盤中 → */30)
- ✓ 每個都有 `concurrency.group` 防同 workflow 多開
- ✓ 都有 `workflow_dispatch` manual fallback
- ⚠️ 唯一明確重複的是 retrain-ml × 2(walk-forward vs random split)
- ⚠️ 週日 22:13 三條同時 push main 有理論 race 風險,但目前未觀察到實際失敗

**本輪不刪不改任何 workflow**,等主公拍板 retrain-ml 整合方向後 Round 2 再動。
