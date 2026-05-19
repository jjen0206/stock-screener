# 用 cron-job.org 外部觸發 Morning Brief — 設定 SOP

> 一次性設定 ~15 分鐘。設定完每天 08:30 (台北) 會自動推播盤前快訊,不再靠 GitHub Actions 內建 schedule。

## 為什麼要這樣搞 (Background)

### 痛點:GitHub Actions schedule 不可靠

GHA 內建 `schedule:` event 在全球高峰時段會被 drop / 大幅延遲。實測案例:

- **2026-05-19 雙班全 drop**:morning-brief 主班 `30 0 * * 1-5` UTC (08:30 TW) + safety net 都被 drop, 實際延遲 1 hr 43 min 才執行 → 主公開盤後才收到快訊, 失去盤前 30 分鐘提前部署的意義。
- GitHub 官方文件明說 schedule "may be delayed during periods of high loads of GitHub Actions workflow runs", 但實務上「delayed」可能變成「skipped」, 沒任何警告。

### 解法:外部 cron 透過 GitHub API 觸發

| 方案 | 可靠度 | 成本 | 設定複雜度 |
|------|--------|------|----------|
| GHA schedule (現況) | ❌ 5/19 雙班 drop | 免費 | 0 |
| **cron-job.org → GH dispatches API** | ✅ SLA 99.99% | 免費 (個人版) | 一次性 15 min |
| 自架 server cron | ✅ | 需付 VPS | 高 |

**選 cron-job.org**:免費 + 不用自架 + 有 web UI 看執行 log + 失敗會 email 通知。

### 雙層保險架構

```
08:30 TW (00:30 UTC)  ← cron-job.org (主) - 透過 dispatches API trigger
07:47 TW (23:47 UTC -1d)  ← GHA cron 第一層 fallback (預期常 delay,delay 到 08:30 後被 dedup skip)
08:17 TW (00:17 UTC)  ← GHA cron 第二層 fallback (預期常 delay,delay 到 08:30 後被 dedup skip)
```

正常情況下 cron-job.org 08:30 準時 trigger → workflow 跑完 → 後續 GHA cron 若延遲 fire 也會被 dedup step 認出「8 小時內已有 success run」直接 skip, **不會雙推**。

若 cron-job.org 整個掛了 → GHA 兩條 cron 至少有一條會在當天某時段 fire (即使延遲), 主公仍會收到推播 (晚一點但有)。

---

## Step 1: 申請 GitHub Personal Access Token (PAT)

cron-job.org 需要一個 PAT 才能呼叫 GH API trigger workflow_dispatch。

### 1.1 進入 PAT 設定頁

1. 登入 GitHub → 右上頭像 → **Settings**
2. 左側選單最底:**Developer settings**
3. 左側:**Personal access tokens** → **Fine-grained tokens** (不要選 classic, 權限太大)
4. 點右上 **Generate new token**

### 1.2 填寫 token 設定

| 欄位 | 值 |
|------|-----|
| Token name | `cron-job-org-morning-brief` (好辨識) |
| Expiration | **90 days** (到期前 cron-job.org 會推送失敗 email, 主公收到就來換新 token) |
| Resource owner | `jjen0206` (個人帳號) |
| Repository access | **Only select repositories** → 勾選 `jjen0206/stock-screener` (最小權限) |

### 1.3 設定 Permissions (核心 — 只給最少權限)

捲到 **Repository permissions** section, 全部維持 `No access` **除了**:

- **Actions** → **Read and write** (必要 — 才能 trigger workflow_dispatch)
- **Metadata** → **Read-only** (Fine-grained token 強制必選, 灰色不能改)

**不要勾 contents / workflows / pull requests / 其他任何權限**。給太多 = token 外洩風險擴大。

### 1.4 產出 token

1. 點底部 **Generate token**
2. **★立刻複製 token (`github_pat_...` 開頭, 約 90 字元)★** — 一旦離開這頁就再也看不到, 只能重新申請
3. 暫時貼到 password manager / 安全的記事本

---

## Step 2: 註冊 cron-job.org + 設定任務

### 2.1 註冊

1. 開 https://cron-job.org
2. 點右上 **Sign up** (免費版, 不需信用卡)
3. 用主公的 email + 密碼註冊
4. 收信 confirm email

### 2.2 建立 Cronjob

登入後 → 左側 **Cronjobs** → 點右上 **CREATE CRONJOB** (橘色按鈕):

#### 「Common」分頁

| 欄位 | 值 |
|------|-----|
| Title | `Morning Brief (TW Stock pre-market)` |
| URL | `https://api.github.com/repos/jjen0206/stock-screener/actions/workflows/morning-brief.yml/dispatches` |

#### 「Schedule」分頁

選 **Custom Schedule**, 用以下設定 (cron-job.org UI 是分鐘 / 小時 / 日 / 月 / 星期勾選, 不是純 cron expression):

| 欄位 | 值 |
|------|-----|
| Timezone | **UTC** (cron-job.org 預設 UTC, 不要改) |
| Days of month | every (全勾) |
| Days of week | **Mon, Tue, Wed, Thu, Fri** (週末不勾, 台股不開) |
| Hours | **0** (只勾 0 點) |
| Minutes | **30** (只勾 30 分) |

等同 cron expression `30 0 * * 1-5` UTC = **08:30 Asia/Taipei 週一到週五**。

> 為何選 08:30:台股 09:00 開盤, 留 30 min 給 (a) GH dispatches event 觸發 workflow ~10s + workflow 執行 ~3-5 min, (b) 主公看完訊息決定要不要追單。

#### 「Advanced」分頁

| 欄位 | 值 |
|------|-----|
| Request method | **POST** ★必選★ (GH API 規定) |
| Request body | `{"ref": "main"}` (告訴 GH 要 trigger main branch 上的 workflow) |
| Headers | 加兩條, 見下方 |

**Headers** (按 Add header 兩次):

| Name | Value |
|------|-------|
| `Authorization` | `Bearer github_pat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` ← 貼 Step 1.4 拿到的 token |
| `Accept` | `application/vnd.github.v3+json` |

#### 「Notifications」分頁 (強烈建議開)

勾 **Notify on failure** + 填主公的 email。cron-job.org 任何一次 trigger 失敗 (token 過期 / GH API down / 4xx 5xx) 都會即時寄信。

### 2.3 儲存

點底部 **CREATE** 按鈕。回到 Cronjobs 列表會看到新項目, 狀態應該是綠色 ●。

---

## Step 3: 手動測試一次 (馬上驗證)

### 3.1 在 cron-job.org 手動 trigger

Cronjobs 列表 → 點剛建立的項目 → 右上 **EXECUTE NOW** 按鈕。

預期結果:
- cron-job.org 「History」分頁出現一筆紀錄, **HTTP status = 204** (GH dispatches API 成功就是 204 No Content, 不是 200)
- 若 status 401 → token 錯 / token 沒給 Actions 權限
- 若 status 404 → repo path 寫錯, 檢查 `jjen0206/stock-screener` + `morning-brief.yml` 拼字
- 若 status 422 → ref 寫錯, 應該是 `{"ref": "main"}` (注意是 JSON, 有引號)

### 3.2 確認 GH Actions 收到 trigger

1. 開 https://github.com/jjen0206/stock-screener/actions/workflows/morning-brief.yml
2. 應該在 30 秒內出現新的 workflow run, **觸發來源欄會顯示 `workflow_dispatch`** (不是 schedule)
3. 點進去看 job log, 確認跑完是綠勾 ✓

### 3.3 確認 Telegram / Discord 收到推播

主公手機 / Discord 應該在 workflow 跑完後 (~3-5 min) 收到盤前快訊訊息。**注意**:此次是手動 trigger, 訊息內容會反映「現在」當下的 picks 變動, 不是明早 08:30 的, 主公看完直接忽略即可。

---

## Step 4: 設定 health monitor (此 PR 已加, 自動生效)

`.github/workflows/morning-brief-health-monitor.yml` 已加進 repo, 每天 09:30 TW (預期推播後 1 hr) 跑 `scripts/check_morning_brief_health.py`, 查最近 24 hr 有沒有成功的 morning-brief run, **沒有就推 Telegram 警告** + 附上手動 trigger 的 GH UI 連結。

主公不用設定, merge 後第二天 09:30 TW 自動生效。

---

## Step 5: PAT 到期換新 (90 天後)

cron-job.org 會在 token 過期前後寄 failure email。流程:

1. 重複 Step 1 申請新 token (建議 token name 加日期, e.g. `cron-job-org-morning-brief-2026Q3`)
2. 開 cron-job.org → 編輯既有 cronjob → Advanced → 把 `Authorization` header 的 value 換成新 token (其他設定不動)
3. 點 EXECUTE NOW 測一次, 確認 204
4. 回 GitHub Developer settings → 把舊 token 標記 **Revoke** (防呆)

---

## Troubleshooting

### Q1: cron-job.org 顯示 204 但 GH Actions 沒收到 trigger?

- 確認 workflow yaml 有 `on: workflow_dispatch:` (我們有, 第 15 行)
- 確認 ref 是 `main` 而不是已刪除的 branch
- 確認 workflow file 在 default branch (main) 上 — feature branch 上的 workflow_dispatch 不會被外部 API trigger 到, 必須先 merge 到 main

### Q2: 401 Unauthorized?

- Token 字串貼錯 / 多了空白 / 少字
- Token 過期 (PAT 預設 90 天, 主公該收到 GH email 警告)
- Token 沒給對權限 (Actions: Read and write 必選)
- header value 漏寫 `Bearer ` 前綴 (注意 Bearer 後面有一個空格)

### Q3: 404 Not Found?

- URL 拼字錯, 標準格式:`https://api.github.com/repos/{owner}/{repo}/actions/workflows/{filename}/dispatches`
- workflow filename 對的是 `morning-brief.yml` 不是 `Morning Brief`

### Q4: 想要改時間 / 加多個 trigger?

- cron-job.org 免費版可建立 50 個 cronjob, 主公可以複製這個再改時間做 backup (例如 08:25 一個 + 08:30 一個), 反正 dedup step 會 skip 重複的 8h 內已成功 run, 不會雙推。

### Q5: cron-job.org 整個被買掉 / 倒閉怎辦?

替代方案 (備而不用):
- **EasyCron** (有免費版, UI 類似) — https://www.easycron.com
- **GitHub Actions cron 第二層 fallback** — 已保留 `47 23 * * 0-4` + `17 0 * * 1-5` 兩條 GHA cron, 即使外部 cron 全掛, GHA 仍有保底機會 (雖然偶爾會 drop, 但不會天天都 drop)

---

## 自我檢查清單

主公照這份做完後, 確認:

- [ ] PAT 申請完, token 字串貼進 password manager
- [ ] cron-job.org 帳號註冊完成
- [ ] cronjob 設定完 (Title / URL / Schedule / Method=POST / Body / Headers)
- [ ] 手動 EXECUTE NOW 一次, cron-job.org 顯示 204, GH Actions 有新 run
- [ ] Telegram / Discord 收到測試推播
- [ ] cron-job.org 通知功能開啟 (notify on failure → email)

全部打勾 → **明早 08:30 (台北) 自動推播**, 不再依賴 GHA 內建 schedule 的 best-effort SLA。
