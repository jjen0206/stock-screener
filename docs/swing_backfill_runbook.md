# Swing 5y Backfill SOP Runbook

> **目的**:主公手動觸發 6 種 backfill,把 daily_prices / institutional / financials /
> dividend / monthly_revenue / company_profiles 補滿 5 年(swing 模組所需)
> **配套**:`docs/swing_implementation_plan.md`(主 spec,Phase 0c-B 開工的 single source of truth)
> **不重啟自動 cron**:本 SOP 全部走 `workflow_dispatch` 手動觸發。
> 已暫停的 financials / company_profiles cron(memory `[[project-backfill-quota-alert-pivot-2026-05-19]]`)不重新啟用。

---

## 0. 觸發前 checklist(主公必看)

1. **FinMind quota 重置**:每月 1 號 09:00 台北。Phase 0c-B 5y backfill 會吃滿配額,建議排在 **6/1 09:00 之後** 開跑(本 doc 撰寫 2026-05-19,離下次重置 13 天)。
2. **Gemini quota 重置**:每天 00:00 台北(15 RPM / 1500 RPD free tier)。company_profiles 走 warm-up 模式(不用 LLM)即可避開,**不要動 LLM 模式**。
3. **GH Actions 月度配額**:免費帳 2000 min/月。本 SOP 全套估 ~800-1500 min,**單月吃滿一半以上 — 分週跑**。
4. **既有 backfill cron 是否暫停**:
   - financials cron:**已暫停**(PR #29 已 pivot 為 alert-based)
   - company_profiles cron:**已暫停**(PR #29 同上)
   - dividend cron:仍週日 14:13 UTC 跑(8-shard / quota fail-fast 已加固),**不動**
   - revenue cron:仍週一 15:13 UTC 跑(同上),**不動**
   - 主公手動觸發 swing 5y backfill 時,避免跟既有 cron 時段衝突

---

## 1. 推薦觸發順序

依「資料依賴 + 配額友善」順序,逐 step 跑:

| Step | Backfill                  | Workflow                                                | 預估時間        | 為何排這順序 |
|------|---------------------------|--------------------------------------------------------|----------------|--------------|
| 1    | daily_prices(5y)         | `backfill-history.yml`                                  | 60-120 min(8-shard 並發)| 一切技術 features 的基礎;先把它跑完 Phase A 才能寫 weekly_resample |
| 2    | institutional(5y)        | `backfill-institutional-once.yml`                       | 60-120 min(per-date,無 shard)| 法人籌碼 — 不依賴 daily_prices,可跟 step 1 並發排隊 |
| 3    | financials(5y quarterly) | `backfill-financials-once.yml`                          | 30-60 min(單 process)| 基本面 toggle 需要 ROE;cron 已暫停,主公手動觸發 |
| 4    | dividend(5y)             | `backfill-dividend.yml`(workflow_dispatch)            | 30-60 min(8-shard)| 既有 cron 週日跑,但 5y backfill 全市場一次性手動觸發更快 |
| 5    | monthly_revenue(5y)      | `backfill-revenue.yml`(workflow_dispatch)             | 30-60 min(8-shard)| 同 4 |
| 6    | company_profiles(warm-up)| `backfill-company-profiles-llm-once.yml`(warm-up 模式)| 20-40 min     | universe-based facts,不依賴前面;**用 warm-up 不用 LLM** |

**並發限制**:GH Actions 免費帳 20 conc job 上限。step 1 (8 shard) + step 2 (1 job) + step 4 (8 shard) 可以同時跑(共 17 jobs)。step 3 / 5 / 6 串列即可。

---

## 2. 逐 step 觸發 SOP

### Step 1:daily_prices 5y backfill

**Path**:GH UI → Actions → "Backfill 90-day history" → Run workflow

**輸入參數**:
```
days: 1825              # 5 年 = 1825 天(含週末)
min_existing: 1260      # 純交易日 ≈ 1260 → 已有 < 1260 天的 stock 才會補
limit:                  # 留空 = 全 universe(~2700 檔)
no_institutional: false # 同步補 institutional(雖然 step 2 也會,但 watchlist 提前補)
```

**預期 log**:
```
[WARN] [BACKFILL] --days=1825 > 1000(swing 5y 模式)。預估每 shard 30-90 min,FinMind quota 可能撞牆 — 確認在 quota 重置窗口內。詳見 docs/swing_backfill_runbook.md
[PRELOAD] 從 daily_prices.csv 讀回 N 筆 / M 檔股票
[BACKFILL] shard 0/8: ~340 / 2700 檔
[BACKFILL] universe=340, 已有 >=1260 天 = 0, 待補 = 340, 範圍 2021-05-19~2026-05-19
[BACKFILL] 50/340 (price ok=50 fail=0, ...) 2.0/s ETA 2.5 min
...
```

**驗收 SQL**(本機跑或在 workflow log 看 verify step):
```sql
SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(date), MAX(date) FROM daily_prices;
-- 期望: > 3,000,000 rows / > 2,000 stock_id / MIN 2021-05~ / MAX 今天

SELECT COUNT(DISTINCT stock_id) FROM (
  SELECT stock_id FROM daily_prices GROUP BY stock_id HAVING COUNT(*) >= 1260
);
-- 期望: > 1500 檔
```

**失敗 retry**:
- 單 shard 失敗 → workflow 標紅但其他 shard 繼續 → 主公看哪個 shard 失敗,改 `--limit` 對該 shard 再跑一次(GH UI 改 input 重觸發即可,但 matrix 模式只能全跑;若只想跑 shard 3,在 UI 手動 cancel 其他 shard)
- 全部 shard 都 quota 爆 → 等 6/1 09:00 重置;**不要 4 小時內反覆觸發**(FinMind 限額時鐘是 1 hr 滾動,等 1-2 hr 試一次比連續轟好)

---

### Step 2:institutional 5y backfill

**Path**:GH UI → Actions → "Backfill Institutional (one-shot manual)" → Run workflow

**輸入參數**:
```
start: 2021-05-19       # 5 年前
end: LATEST             # sentinel,自動代換成今天
sleep: 1.5              # 加速(預設 2.5;5y backfill 量大,sleep 1.5 較快)
dump_format: parquet
upload_release: true
```

**預期 log**:
```
[BACKFILL-INST] --end=LATEST → 2026-05-19
[WARN] [BACKFILL-INST] 2021-05-19 ~ 2026-05-19 = 1826 天 (> 1000 天,swing 5y 模式)。預估 60-120 min,可能撞 GH timeout-minutes:120 -> 分段跑(每段 <= 800 天)。詳見 docs/swing_backfill_runbook.md
[BACKFILL-INST] 區間 2021-05-19 ~ 2026-05-19: 1304 工作日,已有 X,待補 Y
...
```

**timeout 風險**:GH workflow timeout 120 min;5y = 1304 工作日 × 5 秒 ≈ 110 min,**接近上限**。若 120 min 觸頂:
- 分段跑:第一次 `start=2021-05-19 end=2023-05-31`,第二次 `start=2023-06-01 end=LATEST`
- script 內建 fail-fast(> 25% fail 就 break),不會空跑到 timeout

**驗收 SQL**:
```sql
SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(date), MAX(date) FROM institutional;
-- 期望: > 2,000,000 rows / > 2,000 stock_id / 範圍 2021-05~ 今天
```

---

### Step 3:financials 5y backfill

**Path**:GH UI → Actions → "Backfill Financials (one-shot manual)" → Run workflow

**輸入參數**(全部用 default 即可):
```
years: 5                # default
force: false            # default,已有的 sid 跳過
limit:                  # default 空
sleep: 0.5              # default
dump_format: parquet    # default
upload_release: true    # default
```

**預期 log**:
```
[BACKFILL-FIN] universe=2127 已有=1073 待補=1054 範圍=2021-04-19~2026-05-19
[BACKFILL-FIN] 50/1054 ok=45 empty=3 fail=2 0.5/s ETA=33.5m
...
[BACKFILL-FIN DONE] 耗時 35.2 分鐘, ok=900 empty=80 fail=74 / 1054 檔
[VERIFY] financials.quarterly: 30000 rows / 1873 不同 stock_id / 範圍 2021Q1 ~ 2026Q1
```

**Single-process 注意**:financials 沒 8-shard,單 worker 跑。如果 FinMind quota 撞牆,fail-fast 提早結束;主公等下次 quota 窗口再跑(會自動 skip 已有的 sid)。

**驗收 SQL**:
```sql
SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(period), MAX(period)
FROM financials WHERE period_type='quarterly';
-- 期望: > 30,000 rows / > 1,800 stock_id / 範圍 2021Q1 ~ 2026Q1
```

---

### Step 4:dividend 5y backfill(手動觸發,雖然每週日有 cron)

**Path**:GH UI → Actions → "Backfill Dividend (weekly 8-shard)" → Run workflow

**輸入參數**(default 即 5y):
```
years: 5                # default
limit:                  # default 空
```

**8-shard 並發**:每 shard ~250 檔 / ~5-10 min。total ~ 30-60 min(吃 FinMind quota,實際看時鐘可能 30 min 內)。

**驗收 SQL**:
```sql
SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(year), MAX(year) FROM dividend;
-- 期望: > 8,000 rows / > 1,500 stock_id
```

---

### Step 5:monthly_revenue 5y backfill

**Path**:GH UI → Actions → "Backfill Monthly Revenue (weekly 8-shard)" → Run workflow

**輸入參數**(default 即 5y):
```
years: 5                # default
limit:                  # default 空
```

**注意**:revenue workflow `max-parallel: 4`(memory 提醒 5/11 cron cancelled 教訓,降併發避 runner contention)。8 shard 但實際同時跑 4 個 → 整體時間 ~ 60 min。

**驗收 SQL**:
```sql
SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(period), MAX(period)
FROM financials WHERE period_type='monthly_revenue';
-- 期望: > 100,000 rows / > 1,800 stock_id
```

---

### Step 6:company_profiles warm-up(無 LLM)

**Path**:GH UI → Actions → "Backfill Company Profiles (LLM, one-shot manual)" → Run workflow

**輸入參數**:
```
universe: pure_stock     # ~2715 檔
llm_call: false          # **必填 false** — 不用 Gemini,只填 FinMind facts
batch_start: 0
batch_end: 0             # 0 = 跑到 universe 末尾
sleep: 0.0
```

**預期 log**:
```
[BACKFILL-CP] warm-up 模式(僅 facts): 2715 檔 (universe=pure_stock batch=0:2715)
[BACKFILL-CP] 50/2715 ok=48 fail=2 1.5/s ETA=29.4m
...
[BACKFILL-CP DONE] 耗時 30.5 分鐘, ok=2680 fail=35 / 2715 檔
```

**驗收 SQL**:
```sql
SELECT COUNT(*) FROM company_profiles WHERE industry IS NOT NULL;
-- 期望: > 2,500 檔
```

---

## 3. 失敗排查

### 3.1 FinMind quota 爆(status=402)

**症狀**:script 在 N 檔後突然 break,log 看到「FinMind quota 爆」/「FinMindQuotaError」。

**原因**:
- 免費 token:600 req/hr → 5y backfill 一次跑 2000+ 檔很容易撞牆
- Daily/monthly quota 累計 → 即使 1 小時內請求數低,日累計超過也擋
- 8-shard 並發共用同 token → 觸發機率更高(實際是 per-token 限額,不是 per-process)

**處理**:
1. 等下次 quota 重置窗口(monthly = 每月 1 號 09:00 台北 / hourly = 60 min 滾動)
2. **不要連續觸發** — FinMind 對連續觸發有「累計觀察期」,等 1-2 小時試一次比 10 分鐘狂試好
3. script 本身有 `delta_rows == 0 && ok > 100 && pre_rows < 100` 守門員(`backfill_dividend.py:303`),會自動 exit 1 防止假成功

### 3.2 GH Actions workflow timeout(120 min)

**症狀**:單 job 跑超過 120 min 被 GH cancel,workflow 標紅。

**處理**:
- daily_prices 8-shard 已設 timeout 350 min(2026-05 加固),不太會撞
- institutional 設 120 min,5y 可能撞 → 用 step 2 分段跑法
- financials 設 120 min,~35 min 跑完通常 OK
- 若反覆撞,主公手動拆分 batch — 各 script 都支援 `--batch-start --batch-end` 或 `--limit`

### 3.3 shard CSV push race

**症狀**:8 shard 同時 push 到 main → race。

**處理**:已內建 rebase + retry(每 shard 10 次,backoff 5/10/15/...s)。若 10 次都 fail → workflow 標紅但 SQLite 資料還在(下次 aggregate 跑時會看到)。

### 3.4 silent fail(ok 很高但 delta_rows = 0)

**症狀**:log 顯示 ok=2060 但 SQLite 沒新 row。

**處理**:
- `backfill_dividend.py` / `backfill_revenue.py` 已加 silent fail guard(memory `[[fix-silent-fails]]`)
- 2026-05-18 fix `strict=True` 路徑,FinMind 回空回應會 raise 而非 swallow
- 若仍踩到:check `data_fetcher.py` 是否回 empty df 但沒 raise(可能 token quality 變,新 dataset 限制)

---

## 4. 驗收(全部 step 跑完後)

主公在本機 SQLite cache 跑(或 workflow log 看 verify step):

```sql
-- 整套驗收
SELECT
  (SELECT COUNT(*) FROM daily_prices) AS dp_rows,
  (SELECT COUNT(DISTINCT stock_id) FROM daily_prices) AS dp_stocks,
  (SELECT COUNT(*) FROM institutional) AS inst_rows,
  (SELECT COUNT(DISTINCT stock_id) FROM institutional) AS inst_stocks,
  (SELECT COUNT(*) FROM financials WHERE period_type='quarterly') AS fin_rows,
  (SELECT COUNT(DISTINCT stock_id) FROM financials WHERE period_type='quarterly') AS fin_stocks,
  (SELECT COUNT(*) FROM financials WHERE period_type='monthly_revenue') AS rev_rows,
  (SELECT COUNT(*) FROM dividend) AS div_rows,
  (SELECT COUNT(*) FROM company_profiles WHERE industry IS NOT NULL) AS cp_facts;

-- swing 模組關鍵指標:過 1260 交易日門檻檔數
SELECT COUNT(*) AS swing_ready_stocks FROM (
  SELECT stock_id FROM daily_prices GROUP BY stock_id HAVING COUNT(*) >= 1260
);
-- 期望 > 1500;這數字是 swing 策略真實可用 universe
```

**Pass 標準**:
- dp_rows > 3M / dp_stocks > 2000 / swing_ready_stocks > 1500
- inst_rows > 2M / inst_stocks > 2000
- fin_rows > 30k / fin_stocks > 1800
- rev_rows > 100k
- div_rows > 8000
- cp_facts > 2500

**Pass 後**:Phase 0c-B 完成,Phase A(features)可開工。

---

## 5. 跟既有 backfill 的關係

| 既有 backfill                | 本 SOP 是否動?                                |
|------------------------------|-----------------------------------------------|
| `backfill-dividend.yml` cron(週日 14:13 UTC)| **不動** — 仍照跑;手動觸發是 step 4 補強 |
| `backfill-revenue.yml` cron(週一 15:13 UTC)| **不動** — 仍照跑;手動觸發是 step 5 補強 |
| `backfill-financials-once.yml` cron(已暫停)| **不重啟** — 改 alert-based,主公看到 alert 才手動觸發 |
| `backfill-company-profiles-llm-once.yml` cron(已暫停)| **不重啟** — 同上 |
| `daily-notify.yml`(週一~五 22:13 台北)| **不動** — 既有短線推播,不混 swing |
| `morning-refetch.yml`        | **不動** — 既有早盤 refetch |

---

## 6. SOP 變更歷史

- 2026-05-19 v1:Phase 0c-B 初版,亮(諸葛亮 task)撰寫;對齊 `docs/swing_implementation_plan.md` 主 spec。
