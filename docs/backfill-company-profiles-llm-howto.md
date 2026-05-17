# Backfill Company Profiles (LLM) — 主公觸發指引

> 一次性用 Gemini LLM 補滿 `company_profiles` 表的 description / uniqueness / moat
> 三欄,讓個股詳情頁 0 LLM call 即可秒開敘述。
>
> 2026-05-17 加入。對應 `scripts/backfill_company_profiles.py --llm-call true` +
> `.github/workflows/backfill-company-profiles-llm-once.yml`。

## 背景

`company_profiles` 表是 lazy on-demand cache — 沒人點個股詳情頁就 0 列。
LLM 敘述靠 `get_company_profile(sid, llm_call=True)` 一次打 Gemini 5-10 秒。

跑全 ~2715 檔 pure_stock universe ≈ 30 分 ~ 1 小時。**不可能**塞進
Streamlit 的 dispatch task / cron(會 timeout / 不穩),所以走 **GitHub
Actions workflow_dispatch** 背景跑。

跑完 dump `company_profiles.parquet` 上 GH Release(`snapshot-company-profiles-YYYY-MM-DD`),
雲端容器下次 boot 從 `preload_snapshots()` 自動拉回來,個股頁 cache-hit 0
LLM call。

## 為什麼分批?

**Gemini 2.5 Flash Lite 免費額度**(2026 年限制):

| 限制 | 數值 |
|---|---|
| RPM (requests/min) | 15 |
| RPD (requests/day) | 1500 |
| TPM (tokens/min) | 250,000 |

2715 檔一次跑會撞 RPD 上限。**分批跑 500 檔/run**,5-6 個 workflow runs
分散在幾天內完成最穩。也避過 GH Actions 單 job 120 min timeout(理論上跑得完
500 檔但留 buffer 較安全)。

## 主公在 GH UI 怎麼觸發

1. 開 **GitHub repo → Actions tab**
2. 左欄選 **Backfill Company Profiles LLM (one-shot manual)**
3. 右上角 **Run workflow** ▾ button
4. 填參數(default 已調好):

   | 欄位 | Default | 說明 |
   |---|---|---|
   | `universe` | `pure_stock` | 跑全市場純股(可改 `watchlist` 跑主公自選名單,或 `tw_top_50` 測試) |
   | `batch_start` | `0` | 從 universe 的第 N 檔開始(分批用) |
   | `batch_end` | `500` | 跑到第 N 檔結束 |
   | `llm_call` | `true` | 開 Gemini LLM(false = 只填 FinMind facts 不打 LLM) |
   | `sleep` | `1.0` | 每檔間 sleep 秒數(Gemini 15 RPM,**不要動**) |
   | `regenerate` | `false` | 強制重打(忽略 cache 已有 narrative);第一次 backfill 留 false 即可 |
   | `dump_format` | `parquet` | dump 格式;parquet ~1/5 CSV 大小 |
   | `upload_release` | `true` | 跑完上傳到 GH Release |

5. 按綠色 **Run workflow** 觸發
6. 進入 job 看 log 即時跑

## 分批跑的流程

跑全 2715 檔 pure_stock 建議 **6 批,每批 500 檔**:

| Run # | batch_start | batch_end | 預估時間 |
|---|---|---|---|
| 1 | 0 | 500 | 20-40 min |
| 2 | 500 | 1000 | 20-40 min |
| 3 | 1000 | 1500 | 20-40 min |
| 4 | 1500 | 2000 | 20-40 min |
| 5 | 2000 | 2500 | 20-40 min |
| 6 | 2500 | 0 (= 跑到底) | 10-20 min |

**每批間隔 1 天**(Gemini RPD 1500 跨日重置),否則第 2 批會立刻撞 quota。

或者:**先跑 `watchlist` 看看主公自選名單**(通常 10-30 檔,< 5 min)驗證
workflow 設定沒問題,再開始全市場分批。

## 預期跑多久 / 每檔 token 用量

- 單檔 Gemini call:1 個 prompt(~80 tokens)+ 1 個 JSON response(~150 tokens)≈ **230 tokens/檔**
- 500 檔 ≈ **115,000 tokens** ≈ 免費 250K TPM 的一半,跑 30 分鐘 spread,沒壓力
- 全 2715 檔 ≈ **625K tokens** ≈ 免費月度 token 額度的小頭(主公用 Gemini 也只跑這個,沒其他大量消耗)
- 速度:5-10 s/call(Gemini API + 1 s sleep)→ 500 檔 ≈ 50-90 min;parquet dump + upload ≈ 1 min

## 失敗如何處理

### 1. 撞 Gemini 429 quota

Workflow log 會出現:
```
[BACKFILL-CP] ⚠ Gemini quota 爆(2330),中斷整批 — 明天 GMT+8 00:00 重置或加 paid quota
```
- 已寫進 cache.db 的部分**不會丟**(每檔成功後立刻 commit 進 SQLite)
- 等明天 GMT+8 00:00 RPD 重置再跑同 batch_start / batch_end(已寫的 sid
  會被 `get_company_profile()` cache 命中,直接 skip 不再打 LLM)

### 2. 撞 GH Actions 120 min timeout

Workflow 會被 cancel,但同樣已寫進 cache.db 的不會丟。
- 改小 batch size(`batch_end - batch_start = 300` 比 500 安全)
- 重新觸發從 cancel 點繼續(已寫的 sid 會 skip)

### 3. `GEMINI_API_KEY` 沒設

Workflow 第一步就 exit 1,log 顯示:
```
[BACKFILL-CP] ⚠ --llm-call true 但 GEMINI_API_KEY 未設定,exit
```
- 去 **GitHub repo → Settings → Secrets and variables → Actions** 加 `GEMINI_API_KEY`
- Gemini API key 從 https://aistudio.google.com/apikey 拿(免費)

### 4. Release upload 失敗

不影響 backfill 本身(cache.db 仍有資料)。Workflow log 會 warning:
```
[BACKFILL-CP] release upload failed(snapshot 仍在 SQLite + 本地檔)
```
雲端容器 boot 時就走 `lazy on-demand` 路徑(慢但能跑)。
通常下一次 backfill 重新觸發即可。

## 驗證跑完後雲端有沒有拿到

1. 看 **GitHub repo → Releases** 頁應該有 `snapshot-company-profiles-YYYY-MM-DD` tag
2. Streamlit Cloud 下次 boot 時 log 應該出現:
   ```
   [PRELOAD] company_profiles pulled from release snapshot-company-profiles-2026-05-17
   ```
3. 隨便點一檔個股詳情頁 → 敘述應該秒出(不是 spinner 5-10 秒)

## Rollback

回到舊版 release:
```sh
gh release download snapshot-company-profiles-2026-05-10 \
  --pattern company_profiles.parquet \
  --dir data/twse_snapshot/
```
重啟雲端容器即可。
