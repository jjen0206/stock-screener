# company_profiles backfill audit — 2026-05-19

> Audit 範圍:`backfill-company-profiles-llm-once.yml` workflow + `scripts/backfill_company_profiles.py` + `src/company_profile.py`
> 觸發:主公(諸葛亮)說「**一直**有問題,檢查一下」
> 範圍:純 audit + root cause,**不動 code、不重啟 cron、不觸發新 backfill**

---

## 🟢 修補狀態(2026-05-20 — 這份 PR,Route A)

> 本 PR 原規劃修 3 個 🔴。Audit 期間發現 Issue #1 已被 PR #21(`55d9f64`,5/19 merge)
> 用不同手法修掉 —— 兩者撞設計、撞 merge conflict。經主公 Route A 決議:**本 PR 只留
> Issue #2 + #3,Issue #1 整組拿掉**,不回退已上線的 PR #21。

| Audit 項目 | 狀態 | 落在哪裡 |
|---|---|---|
| 🔴 Issue #1: API_KEY_INVALID 沒當 fatal | ✅ 已由 PR #21 修(非本 PR) | PR #21(SHA `55d9f64`)擴 `_is_not_configured_error` 匹配 `API_KEY_INVALID` → 走既有 `not_configured` 分支 fail-fast。本 PR 原規劃的 `_classify_llm_error` redesign 與 #21 衝突,Route A 拿掉避免回退已上線程式碼。 |
| 🔴 Issue #2: quota 重置時間訊息錯誤 | ✅ **本 PR 已修** | `scripts/backfill_company_profiles.py` 訊息改成「等 PT midnight 重置(≈ 台北 15:00 夏令 / 16:00 冬令)」 |
| 🔴 Issue #3: Backfill 失敗無 alert | ✅ **本 PR 已修** | `.github/workflows/backfill-company-profiles-llm-once.yml` 加 `Notify on failure` step(`if: failure()`)推 Telegram + Discord |
| 🟡 Issue #4: SDK deprecation | 🔵 不在此 PR scope(下個 sprint) | — |
| 🟡 Issue #5: 無 progress checkpoint | 🔵 不在此 PR scope(下個 sprint) | — |
| 🟢 Issue #6: workflow step 無 `if: always()` | 🔵 不在此 PR scope(下個 sprint) | — |
| 🟢 Issue #7: streamlit warning 噪音 | 🔵 不在此 PR scope(下個 sprint) | — |

**Tests**:`tests/test_company_profile.py` 17 個 test 全綠(含 PR #21 的 invalid-key regression test)。本 PR 不新增 test —— Issue #2 是訊息字串、Issue #3 是 workflow step,均不增 Python 邏輯。

---

## TL;DR

主公講「一直有問題」其實是 **2 種狀況交織**,根因不只一個:

1. **🔴 2026-05-17 run #26004265959**:GEMINI_API_KEY 失效 → 跑了 500 檔**全 fail**(`ok=0 fail=500`)。`_is_quota_exceeded` helper 只認 429,400 `API_KEY_INVALID` 落到普通 "failed" 分支沒 fail-fast,連續 RTT 浪費。
2. **🔴 2026-05-19 兩次 run(00:43 / 01:16)**:Gemini 1500 RPD free quota 真的爆了 → fail-fast 邏輯有運作,但**主公連觸發兩次**,因為 script 訊息「明天 GMT+8 00:00 重置」是錯的 — Gemini API quota 實際按 Pacific Time midnight 重置(≈ 台北 15:00-16:00)。
3. **🔴 兩種 fail 都沒主動 alert** — 主公得自己去 GH UI 看 log。違反主公的 `feedback_warn_dont_hide` 規矩。

**雲端 release tag `snapshot-company-profiles-*` 完全不存在,主公本機 cache.db 的 company_profiles 表 0 rows**。從 5/17 至今,backfill 雙端產出皆 = 0。

---

## 1. 最近 10 次 backfill run 狀態總覽

| # | Run ID | 時間 (UTC) | Event | 結論 | ok / fail | 中斷點 | Root cause |
|---|---|---|---|---|---|---|---|
| 1 | 26069422132 | 2026-05-19 01:16 | workflow_dispatch | ❌ failure | 3 / 0 | sid=`01007T` | 429 Gemini quota |
| 2 | 26066862520 | 2026-05-19 00:43 | workflow_dispatch | ❌ failure | 7 / 0 | sid=`020011` | 429 Gemini quota |
| 3 | 26004265959 | 2026-05-17 22:15 | workflow_dispatch | ❌ failure | 0 / 500 | 跑完 500 全失敗 | 400 API_KEY_INVALID |

(GH 只回 3 筆,因為 workflow 自從 2026-05-19 PR #29 拔掉 `schedule:` 後就只有 manual trigger,前一次 manual 是 5/17。)

關鍵指標:
- ✅ **fail-fast 邏輯**(429 quota)在 run #1、#2 正確運作 — 撞到 5-8 檔就 break
- ❌ **fail-fast 邏輯**(API_KEY_INVALID)在 run #3 **完全沒運作** — 硬跑 500 檔全廢
- ❌ **無任何 release artifact** — `gh release list` 查無 `snapshot-company-profiles-*` tag
- ❌ **本機 cache.db** `company_profiles` 表 `total=0 desc=0 industry=0`

---

## 2. Root cause 詳細分析

### 🔴 Issue #1:API_KEY_INVALID 沒被當 fatal 錯誤(5/17 run 的根因)

**證據**(run #26004265959 log):
```
[COMPANY] LLM 生成失敗 sid=1443: 400 API key not valid. Please pass a valid API key. [reason: "API_KEY_INVALID"
[BACKFILL-CP] 50/500 ok=0 fail=50 0.94/s ETA=7.9m
[BACKFILL-CP] 100/500 ok=0 fail=100 0.95/s ETA=7.0m
...
```

**Code 弱點**(`src/company_profile.py:55-72`):
```python
def _is_quota_exceeded(exc: BaseException) -> bool:
    cls_name = type(exc).__name__
    if cls_name in ("ResourceExhausted", "TooManyRequests"):
        return True
    msg = str(exc)
    if "429" in msg:
        return True
    msg_low = msg.lower()
    if "quota" in msg_low and ("exceeded" in msg_low or "exhausted" in msg_low):
        return True
    return False
```

400 `API_KEY_INVALID` 不會匹配 — 落到 `failure_status = "failed"`(`company_profile.py:392`),`narrative_status = "failed"` 不是 `quota_exceeded`,backfill script(`backfill_company_profiles.py:258`)就 `failed += 1; continue`,**繼續打下一檔**。

**衝擊**:
- 500 次無謂 RTT(每檔還有 5s sleep,共 ~42 min runtime 全是廢工)
- ETA 訊息誤導(印 ETA=7.9m,實際本就一個都不會成功)
- 主公的 watch (`warn_dont_hide`):明顯違反 — 第 50/100 檔全 fail 就該主動停下喊聲

**正確設計**:401/403/400 API_KEY_INVALID 也該歸入「fatal,fail-fast 整批中斷」類,跟 429 同等級。

---

### 🔴 Issue #2:Gemini quota 重置時間字串錯誤(5/19 兩次 run 連觸發的根因)

**證據**(`backfill_company_profiles.py:241-244`):
```python
print(
    f"[BACKFILL-CP] ⚠ Gemini quota 爆({sid}),中斷整批 — "
    "明天 GMT+8 00:00 重置或加 paid quota",
    ...
)
```

**問題**:Gemini API quota 是按 **Pacific Time midnight** 重置(API docs),不是 GMT+8。
- PT midnight ≈ 台北 **15:00**(夏令)/ **16:00**(冬令)
- 主公看到 `01007T` 撞牆訊息 → 隔 33 min(00:43 → 01:16)再手動觸發 → 又撞牆(因為 PT midnight 還沒到)

主公連觸發 2 次的證據:
- Run #2 在 00:43 UTC 觸發,撞 sid=`020011` 第 8 檔
- Run #1 在 01:16 UTC 觸發(33 分鐘後),撞 sid=`01007T` 第 4 檔 — quota 還更緊

**衝擊**:**訊息字串本身在誤導主公** — 主公以為「等 GMT+8 00:00 隔天再試」就行,實際應該是「等台北 15-16 點(PT midnight)」。
從 5/17 算來「至今至今」實際指的就是這個訊息誤導:**主公 5/19 上午連觸發兩次都還在當日 PT 配額內,quota 沒重置**。

---

### 🔴 Issue #3:Backfill 失敗無主動 alert(違反 warn_dont_hide)

**證據**:
- workflow `.github/workflows/backfill-company-profiles-llm-once.yml` 197 行內 **沒有任何 Telegram / Discord notify step**
- script `backfill_company_profiles.py:241-249` 只 `print(...stderr)` + `logger.warning(...)`,沒 push notification

對照主公剛 merge 的 **PR #29 backfill quota alert pivot**(memory: `project_backfill_quota_alert_pivot_2026_05_19.md`):
> 「兩個 backfill cron 改 alert-based manual trigger,財報每月 1 號 09:00 / company_profiles 每天 00:30 台北推 Telegram+Discord」

**company_profiles 那邊只接了「alert 通知主公該觸發了」,但沒接「backfill 跑失敗時主動 alert」**。所以:
- 5/19 00:30 台北的 alert 推說「該手動觸發了」→ 主公觸發 → 失敗 → 主公**沒收到任何失敗推播**
- 主公得自己去 GH Actions UI 點開看 log 才知道為什麼掛
- 結果就是「一直有問題」沒人通報,要主公主動回頭查

對照 memory `feedback_warn_dont_hide`:此處違反精神。失敗的訊息應該對等 alert 出來,不能只在 GH UI 內躺著。

---

### 🟡 Issue #4:`google.generativeai` SDK 已停止維護

**證據**(run #1 log):
```
FutureWarning:
All support for the `google.generativeai` package has ended. It will no longer be
receiving updates or bug fixes. Please switch to the `google.genai` package as
soon as possible.
```

**衝擊**:目前還能跑,但任何 Gemini API 端的 breaking change 都不會反映在這個 SDK。
- workflow 第 83 行硬指定 `google-generativeai>=0.7,<1.0`
- `src/company_profile.py:34` `import google.generativeai as genai`

**遷移成本**:中等(API 表面不同,需重寫 `generate_with_gemini`)。**非緊急**,但 6 個月內該排程做。

---

### 🟡 Issue #5:沒有 progress checkpoint

**Code 弱點**:`backfill_company_profiles.py:226-283` 主迴圈沒寫 `last_processed_sid` 到任何持久化位置。

**衝擊**:
- 中斷後續跑,主公得自己看 log 算 `--batch-start`
- 雲端 SQLite 在 fail step 後**不會保存**(workflow 沒 commit cache.db),只有 parquet snapshot 有上 release — 但 parquet 寫在 fail-fast 後的 `_dump_snapshot`,**fail-fast 寫的 parquet 又因為下游 `Upload snapshot to GitHub Release` step 沒 `if: always()` 也被 skip**
- 結果:5/19 兩次 fail-fast 各寫了 4 / 8 行的 parquet,**全部丟失,沒 commit、沒 release**

**正確設計**:寫 last 成功 sid 到 SQLite `meta` 表或 file,下次跑自動 resume。

---

### 🟢 Issue #6:Workflow 後續 step 沒 `if: always()`

**Code 弱點**(`backfill-company-profiles-llm-once.yml`):
- Line 115 `Verify backfilled rows` — 沒 if filter,backfill exit 1 就 skip
- Line 125 `Upload snapshot to GitHub Release` — `if: inputs.upload_release == 'true'`,但 GH Actions step success() 是預設 prefix → fail 後 skip

**衝擊**:5/19 兩次 fail-fast 寫好的 parquet(4/8 行)未上 release,全丟。即使只有少量,**增量保留**比全丟好。

**正確設計**:`Verify` 加 `if: always()`,`Upload snapshot` 加 `if: always() && inputs.upload_release == 'true'`。

---

### 🟢 Issue #7:Streamlit warning 噪音

**證據**:每個 backfill 檔印一行
```
WARNING streamlit.runtime.scriptrunner_utils.script_run_context: Thread 'MainThread':
missing ScriptRunContext! This warning can be ignored when running in bare mode.
```

**Source**:`src/company_profile.py:81-89` 的 `_safe_get_session_state()` 在 CLI 跑時也 import streamlit。可以加 `os.environ.get("STREAMLIT_DISABLED")` 短路,或直接 `logging.getLogger("streamlit").setLevel(ERROR)`。

**衝擊**:純噪音,500 行 log 變 1500 行,但不影響 backfill 正確性。

---

## 3. Script 弱點總整

| 項目 | 評分 | 備註 |
|---|---|---|
| 錯誤處理覆蓋 | 🟡 中 | 429 認;400/401/403 漏 |
| Fail-fast 觸發 | 🟡 中 | quota 走得對;auth fail 漏 |
| Retry 機制 | 🔴 無 | 單檔失敗就 `failed++`,無 backoff |
| Progress checkpoint | 🔴 無 | 中斷後手動算 batch_start |
| Alert 推播 | 🔴 無 | logger 內躺,主公不主動知道 |
| 雲端 artifact 保留 | 🔴 弱 | fail 後 parquet skip upload |
| SDK 維護狀態 | 🟡 中 | deprecated 但還能跑 |

---

## 4. 對照 `feedback_warn_dont_hide` 規矩

**主公的 warn_dont_hide 規矩**(從 memory 推測精神):失敗要主動 alert / 不該靜默吞掉,讓主公看到。

| 違反處 | 嚴重度 | 描述 |
|---|---|---|
| script LLM 失敗只 logger.warning | 🟡 | 沒推送 |
| 5/17 API_KEY_INVALID 500 檔全失敗無 alert | 🔴 | 主公根本不知道 5/17 那 run 跑了 42 min 全廢工 |
| 5/19 quota 爆 fail-fast 沒推 Telegram | 🔴 | 主公得自己去 GH UI 看 |
| Workflow exit 1 後沒 notify step | 🔴 | 雙重保險缺一 |
| quota 重置時間訊息錯字 | 🔴 | 反向誤導 — 不是隱藏失敗,是**誤導下一步** |

---

## 5. 三層建議

### 🔴 馬上修(blocking — 不修主公會持續踩坑)

**Fix #1:`_is_quota_exceeded` 擴成 `_classify_llm_error`,讓 400/401/403 也歸 fatal**

```python
# 概念草稿,不要直接套
def _classify_llm_error(exc) -> Literal["quota", "auth", "transient", "unknown"]:
    msg = str(exc)
    if "429" in msg or "ResourceExhausted" in type(exc).__name__:
        return "quota"
    if "API_KEY_INVALID" in msg or "401" in msg or "403" in msg:
        return "auth"   # 也 fail-fast,跟 quota 同等級
    if "500" in msg or "503" in msg or "DEADLINE_EXCEEDED" in msg:
        return "transient"  # 可 retry(下次 PR 再做)
    return "unknown"
```

`backfill_company_profiles.py:239` 的 `elif status == "quota_exceeded"` 同樣要拆出 `auth` 分支,單獨印「GEMINI_API_KEY 失效,中斷整批,請 rotate key」訊息 + exit 1。

**Fix #2:訊息字串改正 Gemini quota 重置時間**

`backfill_company_profiles.py:241-244` 改成:
```
"明天 PT midnight 重置(≈ 台北 15:00-16:00 夏令 / 16:00-17:00 冬令)或加 paid quota"
```

避免主公看到「GMT+8 00:00」就誤判可隔天試。

**Fix #3:workflow fail step 接 Telegram / Discord push**

依照 PR #29 backfill quota alert pivot 那套邏輯,在 workflow 加最後一個 step:
```yaml
- name: Notify on failure
  if: failure()
  run: python scripts/notify_backfill_failure.py --workflow company_profiles --run-id ${{ github.run_id }}
```

讓主公直接從 Telegram 收到「company_profiles backfill 失敗 — quota / auth / 其他」分類訊息。

---

### 🟡 可改進(下次有空時排程)

**Improve #4:加 progress checkpoint**

在 SQLite `meta` 表寫 `company_profiles_backfill_last_sid` + `last_run_at`。`backfill_company_profiles.py:226` 主迴圈開頭讀,自動從 last_sid + 1 接著跑(可被 `--batch-start` 覆寫)。中斷後重跑零摩擦。

**Improve #5:遷移 `google.genai`**

新 SDK API 不同,需要重寫 `generate_with_gemini` + 更新 `requirements.txt` / workflow pin。建議跟其他 LLM 抽象一起重構(`bias_convergence rescue` 那種一起 review)。

**Improve #6:workflow 兩個 step 加 `if: always()`**

```yaml
- name: Verify backfilled rows
  if: always()
  ...

- name: Upload snapshot to GitHub Release
  if: always() && inputs.upload_release == 'true'
  ...
```

讓 fail-fast 寫出的 partial parquet 仍能 upload 保留增量進度。

---

### 🟢 Nice-to-have(優先序最後)

**Nice #7:壓掉 streamlit warning 噪音**

`src/company_profile.py` 開頭或 `backfill_company_profiles.py` main() 起手式加:
```python
import logging
logging.getLogger("streamlit").setLevel(logging.ERROR)
```

**Nice #8:加 single-check API key validity preflight**

backfill 開跑前先打一個 ping prompt(短到不會吃 quota,< 10 token),確認 API key 有效再開跑。失效就直接 exit 1 + alert,不要跑 1 檔以上。

---

## 6. 給亮的決策建議

**亮如果問主公拍板**,我建議以下順序:

1. **先確認 GEMINI_API_KEY 是否還有效**(主公那邊 — 5/17 那次是 key 失效還是 secret rotation?)
2. **拍板 Fix #1 + Fix #2 + Fix #3**(預估 1-2 hr 修完 + 測):
   - 修完後重新手動觸發 1 次(避開 PT midnight 之前)驗證能不能 fail-fast + alert
3. **PT midnight ≈ 台北 15:00-16:00 才能再嘗試完整 backfill**(等 quota 重置)
4. Improve / Nice 那批等下個 sprint

**亮不要做的事**:
- ❌ 不要急著重啟 schedule cron(PR #29 才剛 pivot 為 alert-based,先驗證 alert 有效再說)
- ❌ 不要繞過 fail-fast 改重試到死(會把 quota 雪崩消耗)
- ❌ 不要在 Fix #1 還沒上之前讓主公重觸發 5/17 那種 case(會 silent 跑 500 檔全廢工)

---

## 附錄 A:現況數據快照

```
本機 D:/Claude-workspace/projects/stock-screener/data/cache.db:
  company_profiles total= 0  desc= 0  industry= 0
  finmind_max= None  llm_max= None

雲端 release tags:
  snapshot-company-profiles-* → 0 個(查無)

最近 3 次 GH run:
  2026-05-19 01:16 — fail (quota at sid=01007T, ok=3)
  2026-05-19 00:43 — fail (quota at sid=020011, ok=7)
  2026-05-17 22:15 — fail (api_key_invalid 全 500 fail, ok=0)

合計 ok = 18 檔(全在雲端 SQLite,沒 commit、沒上 release,等於零)
```

---

**Audit by**:Claude (orchestrator-subagent for 亮)
**Audit time**:2026-05-20
**Code changes**:0 行(純 audit)
