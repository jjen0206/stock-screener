# Quota Alerts Reference — 2026-05-19

> 兩個 backfill workflow (PR #20 financials / PR #27 company_profiles) 撞外部
> 服務配額後,改 alert-based manual trigger。本文件記錄配額限制、重啟步驟、
> 如何停用 alert,以及未來何時可改回完全自動。

## 1. 配額限制簡介

### 1.1 financials backfill (PR #20)

- **資料源**:FinMind API
- **配額**:**月** ~10,000 calls(免費 plan;每月 1 號重置)
- **耗用率**:全市場 ~2060 檔 / 批 200 檔 × FinMind throttle ~0.5s/檔 ≈ 5-10 min/批
- **症狀**:撞 quota 時 FinMind 回 `status_code=402`,backfill_financials.py 已加
  fail-fast,但 schedule `*/30` 後續 cron 仍持續 fire,跑 noop 浪費 GHA runner。
- **跑滿條件**:`financials ∩ pure_stock universe` coverage >= 99%
  (~2040 / 2060 檔有 quarterly 財報)
- **預估**:跑完全市場約 10 次 dispatch(每次間隔 30 min)

### 1.2 company_profiles backfill (PR #27)

- **資料源**:Gemini API (LLM 生 description/uniqueness/moat)
- **配額**:**日** 1500 req(免費 tier;每天 00:00 PT 重置 = ~15:00 Asia/Taipei
  PDT / ~16:00 PST)
- **耗用率**:單批 500 檔 × ~5s sleep + ~2s Gemini ≈ 58 min/批
- **症狀**:撞 quota 時 Gemini 回 429,backfill_company_profiles.py 也已 fail-fast,
  但 hourly schedule 後續 cron fire 連續燒 GHA runner。
- **跑滿條件**:`company_profiles` 表 `description IS NOT NULL` 達到全 universe
  ~2715 檔
- **預估**:5 批 × ~1 hr ≈ 5 hr(每天只能跑一批,因 daily limit)

## 2. 重啟 step-by-step

主公收到 Telegram + Discord 提醒後,流程如下:

### 2.1 開 GitHub Actions UI

1. 瀏覽 https://github.com/jjen0206/stock-screener/actions
2. 左側 workflow 列表選:
   - **company_profiles** → `Backfill Company Profiles LLM (one-shot manual)`
   - **financials** → `Backfill Financials (one-shot manual)`

### 2.2 點 "Run workflow" 按鈕

GH UI 右上角會有藍色 `Run workflow` 按鈕(下拉選單),點開後填參數:

#### company_profiles 參數對照

| input | 第一批 | 第二批 | 第三批 | 第四批 | 第五批 |
| --- | --- | --- | --- | --- | --- |
| `batch_start` | 0 | 500 | 1000 | 1500 | 2000 |
| `batch_end` | 500 | 1000 | 1500 | 2000 | 0 (= 跑到底) |
| `universe` | `pure_stock` | (同) | (同) | (同) | (同) |
| `llm_call` | `true` | (同) | (同) | (同) | (同) |
| `sleep` | `5.0` | (同) | (同) | (同) | (同) |
| `regenerate` | `false` | (同) | (同) | (同) | (同) |

> 每批跑前可先去 Settings > Gemini console 確認 quota 剩餘量。

#### financials 參數對照

預設參數通常即可(只填 `years=5`, `force=false`,留空 `limit` 全跑)。
script 內 `--max-stocks=200` 自動單批 200 檔,跑完 push 再進下一批前可等 30 min
讓 FinMind throttle window 過。

### 2.3 監看 log

按下 Run 後,workflow 進 queue → 跑 → 完成。展開個別 step 看 console:

- `Coverage guard` step output `[GUARD] coverage=X.X%` → 知道目前進度
- `Run financials backfill` / `Run company_profiles backfill` step → script 主 log
- `Verify backfilled rows` → 確認新增了多少
- `Upload snapshot to GitHub Release` → 跑完自動 push parquet 上 release

## 3. 如何停用 alert

如果不想再收到提醒,把 `.github/workflows/quota-reset-alerts.yml` 內的
`schedule:` 區塊移除即可(或整檔刪除)。`workflow_dispatch:` 留著,以便偶爾
手動測試。

```yaml
on:
  # schedule:               ← 把這 5 行刪掉
  #   - cron: "30 16 * * *"
  #   - cron: "0 1 1 * *"
  workflow_dispatch:
    ...
```

## 4. 何時可改回完全自動

只有當外部配額升級後才適合恢復原本的 schedule:

- **financials**:升 FinMind paid plan(月配額 → 100,000+ calls / month),
  或換成同樣免費但更寬鬆的 plan。
- **company_profiles**:升 Gemini paid tier(daily limit → 千萬級),或改用
  其他 LLM provider(OpenAI / Claude API)。

恢復方法:

1. 復原兩個 backfill workflow 的 `schedule:` 區塊
   (移除 disabled 註解,加回 `- cron: "*/30 * * * *"` / `- cron: "0 * * * *"`)
2. 刪除 `.github/workflows/quota-reset-alerts.yml`(或只移除 schedule)
3. 更新本文件加註「已恢復自動 schedule,日期 X」

## 5. Cron 對應表(備忘)

| cron (UTC) | Asia/Taipei | 觸發內容 |
| --- | --- | --- |
| `30 16 * * *` | 00:30 每天 | company_profiles alert |
| `0 1 1 * *` | 09:00 每月 1 號 | financials alert |

## 6. 相關檔案

- `.github/workflows/quota-reset-alerts.yml` — alert workflow 本身
- `scripts/alert_quota_reset.py` — 推送 script(Telegram + Discord)
- `.github/workflows/backfill-financials-once.yml` — financials backfill
  (schedule 已 disabled)
- `.github/workflows/backfill-company-profiles-llm-once.yml` —
  company_profiles backfill(schedule 已 disabled)
