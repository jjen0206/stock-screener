# Watchlist 部分股票遺失調查 (2026-05-16)

## 主公回報症狀

「我的關注」頁面有**部分股票遺失**(不是全部沒了,是少了一些)。

## Step 1 — 實作位置定位

- **Page**: `app.py:_page_watchlist()` (line 4555)
- **DB schema**: `src/database.py:189` `CREATE TABLE watchlist (stock_id, added_at, note)`
- **持久化模組**: `src/watchlist_snapshot.py` — SQLite ↔ CSV 互轉
- **遠端同步**: `src/github_sync.py` — push/fetch GitHub Contents API, **預設分支 = `watchlist-sync`**
- **Boot 路徑**: `app.py:917` → `watchlist_snapshot.safe_boot_load()`
- **Dump 觸發點**: `src/database.py:_dump_watchlist_snapshot` (在 `add_to_watchlist` /
  `bulk_add_to_watchlist` / `remove_from_watchlist` 後呼叫)

主公雲端關注清單真正的「source of truth」= GitHub `watchlist-sync` 分支的
`data/twse_snapshot/watchlist.csv`。本地 main 上的同名 CSV 是**種子 seed**,只
給 GH Actions runner 用。

## Step 2 — 遺失模式分析(git history)

`git log --all data/twse_snapshot/watchlist.csv` 顯示完整時間線:

| 時間 | Commit | 訊息 | 行數 |
|---|---|---|---|
| 2026-04-30 06:30 | `430648e` | watchlist seed | 2 |
| 2026-04-30 → 05-15 | (累積) | auto-sync from cloud app | 4 → **19** |
| **2026-05-15 09:26** | **`6639390`** | **auto-sync from cloud app** | **19** ← 主公真正的關注清單 |
| **2026-05-16 22:42** | **`73b4cf0`** | **auto-sync from cloud app** | **4** ← **regression!** |

### 黑名單:`6639390 → 73b4cf0` 之間遺失的 15 檔

```
3105, 6223, 3017, 2449, 2344, 2337, 4971,
2303, 2379, 3034, 7810, 4442, 2484, 3711, 2308
```

### 關鍵證據:回到 4 檔的 `added_at` 完全等於 main 種子 CSV

`73b4cf0` 的 4 檔時間戳:
```
2454,2026-04-30T22:29:17+00:00
2317,2026-04-30T22:29:17+00:00
2330,2026-04-30T22:29:17+00:00
3680,2026-04-30T22:29:17+00:00
```

跟 main `data/twse_snapshot/watchlist.csv` **逐字節相同**。也跟 `c65d5ed`
(2026-04-30 23:19 — `chore(snapshot): backfill 90-day history`)寫進 main 的
4-stock 種子完全一致。

→ 結論:**SQLite 在某次容器啟動時被重置成種子 CSV (4 檔),之後任何
`add_to_watchlist` / `remove_from_watchlist` 都會觸發 `_dump_watchlist_snapshot`,把
SQLite 內的 4 檔 push 上 watchlist-sync,把遠端真實的 19 檔覆蓋掉。**

## Step 3 — Root Cause

### 失敗路徑

`src/watchlist_snapshot.py:safe_boot_load` (cloud boot 入口):

```python
try:
    remote_csv = fetch_watchlist_from_github()
except Exception:
    load_from_csv(...)              # ← 載入 main 種子 (4 檔)
    return "fallback-fetch-exception"

if remote_csv is None:               # PAT 缺 / 404 / 認證失敗
    load_from_csv(...)               # ← 載入 main 種子 (4 檔)
    return "fallback-no-remote"
```

任何遠端 fetch 失敗(網路抖動 / GitHub 暫時 5xx / PAT 暫時驗證失敗)→
fallback 進 main 種子 CSV (4 檔) → 之後使用者任一動作觸發
`_dump_watchlist_snapshot` → push 上 watchlist-sync **覆蓋遠端真實 19 檔狀態**。

### 為什麼 idempotent load 沒救?

`load_from_csv` / `load_from_string` 本身是 idempotent on `stock_id` (ON CONFLICT
DO UPDATE SET note),只「加」不「減」。但這裡是 fallback 從 **不同來源**(main 種子
而非 watchlist-sync)灌進空 SQLite，所以 SQLite 變成「殘缺的 4 檔」狀態,而非真實狀態。

### 為什麼 _dump_watchlist_snapshot 沒擋住?

`dump_to_csv` 任何條件都會寫,沒有「我現在的 SQLite 是否可信」的概念。push 也沒
做「不要造成大量遺失」的保險。

### Memory rule 對照

主公自己定的規則:「持久化必須 e2e 測試 + boot wiring regression」。本次違規:
- ✅ boot wiring 確實接到 `safe_boot_load`
- ✅ `safe_boot_load` 失敗時不 crash
- ❌ **沒有任何測試守住「fallback 後不可造成 remote regression」這條 invariant**
- ❌ push 路徑沒有 sanity check「我這次 push 會不會弄丟一票股」

## Step 4 — 修法

### 4-A. `push_watchlist_to_github` 加 regression guard (主要修法)

`src/github_sync.py:push_watchlist_to_github` 已經在 PUT 之前 GET 過 remote
(`_get_remote_file` 取 sha)。在這個位置順手 parse remote 跟 local 兩份 CSV,計算
`stock_id` 集合 diff:

- 若 `lost = remote − local` 數量 >= **3**(threshold) → 拒絕 push,log error
- 一般 user 一次只移除 1-2 檔,大量遺失基本上 = boot fallback regression
- 若使用者真的想砍掉很多,UI 顯示 toast 失敗 + 提示去 GitHub 直接編 CSV

這條規則對其他 csv(trades / paper_trades)不適用 — 它們有 timestamp 隨時新增,
舊 row 不會被「遺失」。所以實作放在 `push_watchlist_to_github` wrapper,而非
generic `_push_csv_generic`。

### 4-B. 還原遺失的 15 檔

直接把 `6639390` 的 watchlist.csv 還原進 main 種子:

```bash
git show 6639390:data/twse_snapshot/watchlist.csv > data/twse_snapshot/watchlist.csv
```

下次 boot 時:
- 若 watchlist-sync 還是 4 檔 (regression 狀態) → fetch_watchlist_from_github
  得到 4 檔 → SQLite 灌 4 檔 → 不會自動補上 15 檔
- **所以同時要 push 還原後的 CSV 上 watchlist-sync 分支**

但 watchlist-sync 分支的 PAT push 只有雲端有,本地不直接 push 雲端分支。改採:
- 把 main 種子改回 19 檔
- 主公下次雲端 boot 時:雲端拉 watchlist-sync 仍是 4 檔,但既然此次 push guard
  生效,只要主公本地跑一次「git pull → streamlit 啟動 → 看 watchlist 19 檔
  → 主公在雲端按一次 ☆ 加入 (任何動作觸發 dump)」就會被 push guard 攔下「regression
  detected」。
- **更直接的方法**:雲端 UI 加「⚙️ 從 main 種子強制 sync」按鈕,讓主公能手動拉
  19 檔回來。但這次先以還原 main + push guard 為主修,讓主公直接在本地用 streamlit
  匯出 csv → commit 到 main 修這份本地檔,workflow 不會破壞。

### 4-C. 規避未來再發生

- e2e test:模擬 fetch_remote 失敗 → fallback → 模擬 user add → dump → 驗證
  `push_watchlist_to_github` 回 False 且沒打 HTTP
- e2e test:遠端 18 檔 / 本地 4 檔 → push 拒絕
- e2e test:遠端 18 檔 / 本地 17 檔(移除 1 檔)→ push 通過
- e2e test:遠端 18 檔 / 本地 20 檔(純新增)→ push 通過

## Step 5 — 主公復原步驟

1. `git pull origin main` (本機抓最新)
2. 用 streamlit 啟動 → 確認本機 watchlist 19 檔都在
3. 主公也可在 GitHub 網頁編 `data/twse_snapshot/watchlist.csv` 直接補進 watchlist-sync
   分支 (push guard 不擋手動 web edit)

## 未來改進 (out of scope)

- Boot 端再加一層「成功路徑/fallback 路徑」狀態旗標,fallback 時整個 session 拒推
  雲端,只更新本機 — 是更嚴的保護,但這次先以 regression guard 為主,等觀察到
  threshold-3 還不夠用再加。
- `bulk_add_to_watchlist` 反向(批次刪除)介面引入時,要記得提供「明確跳過 guard」
  的 explicit flag,而非靜默通過。


---

## Followup (主公提供關鍵資訊後第二次調查)

主公補:**應該有 ~10 檔**、**只有加沒有減**(從未手動刪)。這明確排除「手動刪除」根因,
重新審視 git history 後找到**更多遺失事件**:

| 時間 | Commit | 訊息 | sids 進展 |
|---|---|---|---|
| 2026-05-01 06:57 | `7bd3e8a` | auto-sync from cloud app | 7 檔含 **1101 + 2882** |
| 2026-05-06 07:07 | `f7a71b1` | auto-sync from cloud app | 7 檔(**1101 不見了**) |
| 2026-05-06 07:07 | `f0c575e` | auto-sync from cloud app | 6 檔(**2882 也不見了**) |
| 2026-05-11 08:30 | `ce55b99` | auto-sync from cloud app | 7 檔(**2308 不見了**,4 檔老股時間戳變種子值) |
| 2026-05-15 09:26 | `6639390` | auto-sync from cloud app | **19 檔**(2308 又回來) |
| 2026-05-16 22:42 | `73b4cf0` | auto-sync from cloud app | **4 檔**(本次主公回報) |

「2308 消失又出現」說明同樣的 regression 機制至少觸發過 4 次。**1101 / 2882 永久遺失**
(沒人手動再加回來)。

### 補充根因:race condition

主公 ★★★ 提示:`_dump_watchlist_snapshot` 是
1. main thread:`dump_to_csv` (寫 local) → `dump_to_string` (讀 SQLite → CSV 字串)
2. fire-and-forget thread:`push_watchlist_to_github(csv_content)`

兩個 session 在不同時間點同時 add:
- A: SQLite write sid_A → main 取 csv = {sid_A} → thread A 啟動
- B: SQLite write sid_B → main 取 csv = {sid_A, sid_B} → thread B 啟動
- thread B 先到 GH PUT {sid_A, sid_B} ✅
- thread A 後到 GH PUT {sid_A}(stale snapshot)→ 409 retry → refetch sha
  → PUT {sid_A} 又一次 → **sid_B 被覆蓋**

這個 race 只丟 1 檔,**低於 regression guard threshold (3)**,所以
threshold-3 的 guard 攔不下。

### 補充修法

1. **`_push_watchlist_worker(db_path)`** 抽成 module function,**`dump_to_string` 移到
   push thread 內執行**。讓 thread 跑時讀的 SQLite 反映「thread 啟動那刻」最新狀態
   (而非 main thread 取 snapshot 那一刻)。縮短 race window。
2. **`tests/test_watchlist.py`** 加 3 條 concurrent test:
   - `test_concurrent_add_to_watchlist_no_sid_lost`: 20 thread 同時 add 不同 sid →
     全部該在 SQLite
   - `test_concurrent_idempotent_add_same_sid`: 20 thread 同時 add 同一 sid → 1 row
   - `test_dump_runs_after_concurrent_adds_sees_all_sids`: 並發 add 後 dump_to_string
     該看到全部 sid

### 補充還原:full union 不只 19 檔

主公「只有加沒有減」→ 把 git history 中所有曾出現過的 sid 都 union 回去:
- 從前次的 19 檔基礎,**補加 1101 / 2882**(永久遺失但主公從未手動刪)
- 每個 sid 用 git history 中第一次出現時的 added_at(真實 user-add 時間),
  不再用種子的 `2026-04-30T22:29:17` 假時間戳
- **共 21 檔**

### 還發現:我自己的 e2e test 之前污染了 main 的 watchlist.csv

第一個 commit `4b23f86` 寫進的 watchlist.csv 其實是 test data(timestamps 全為
`2026-04-30T00:00:00` + 有測試用 sid `9999,note=legit add`)。原因是當時測試
**只 monkeypatch 了 `_db_inside_project`,沒 patch `SNAPSHOT_DIR` / `WATCHLIST_CSV`**,
所以 dump 真的寫到專案 `data/twse_snapshot/watchlist.csv`,而我沒檢查就 git add。

這次 followup commit 已修:
- 測試全程用 `tmp_path` + monkeypatch `SNAPSHOT_DIR` + `WATCHLIST_CSV`
- 重新跑驗證 watchlist.csv 內容跟測試前一致

教訓 → 加進 memory 「e2e test 改動 SQLite + dump path 時必須完整 monkeypatch
SNAPSHOT_DIR / WATCHLIST_CSV / DB,任何一個漏 patch 都會污染專案 data/」。

