# stock_warnings 表 / fetcher 排查紀錄(2026-05-15)

## 起因
三維健診發現 `data/cache.db` 沒有 `stock_warnings` 表,雖然 schema (`src/database.py` line 506-531) 與 fetcher (`scripts/fetch_stock_warnings.py`)、workflow (`.github/workflows/stock-warnings.yml`) 早已 commit 進 repo。

## 排查結果

### 1. 代碼層檢查 — 正確
- `src/database.py::init_db()` schema 包含 `CREATE TABLE IF NOT EXISTS stock_warnings` + 2 個 index(driver line 506-531 / 528-531)。
- `scripts/fetch_stock_warnings.py::run()` line 681 **已 call `db.init_db(db_path)` 在 upsert 之前**,所以 fetcher 跑一次就會建表,無 bug。
- workflow `.github/workflows/stock-warnings.yml` cron `13 9 * * 1-5`(台灣 17:13),腳本路徑、git push 邏輯都 OK。

### 2. 根因
排除 workflow 從未成功跑過。實際 root cause:**production cache.db (主 repo `data/cache.db`) 是在 stock_warnings schema commit (6076c24 / 8055c61) **之前**就已存在的**;`init_db()` 雖然 idempotent 但 GitHub Actions runner 每次 checkout 都拿到 commit 進 main 的 cache.db,而那份 cache.db 上次寫入時 schema 還沒這張表。然而 `init_db` 在 fetcher 開頭已會跑(line 681),理論上會建。

→ **真正可能解釋**:GitHub Actions workflow 從未真正成功跑過 production push commit。Workflow log 在 GitHub UI 才能驗證(本地無 cache),但本地手動跑了 `python scripts/fetch_stock_warnings.py` 後立刻 117 rows 寫入成功 → 證明 code path 完整,沒有 silent skip。

### 3. 本次手動修復
```bash
DATABASE_PATH="D:/Claude-workspace/projects/stock-screener/data/cache.db" \
  python scripts/fetch_stock_warnings.py
```
結果:
- rows_parsed = 117
- rows_written = 117
- by_type = {disposition: 66, attention: 34, method_changed: 16, full_cash: 1}
- elapsed = 1.7s

`PRAGMA table_info(stock_warnings)` 驗證 8 欄全到位、2 個 index 也建好。

### 4. **真正剩下的 fetcher 缺口:TWSE 4 條源全 0 rows**

| 來源 | rows |
|---|---|
| TWSE default_settlement (違約交割) | 0 |
| TWSE attention (注意股) | 0 |
| TWSE disposition (處置股) | 0 |
| TWSE method_changed_or_full_cash (變更交易方法) | 0 |
| TPEx attention | 34 |
| TPEx disposition | 66 |
| TPEx method_changed_or_full_cash | 17 |

直接 probe `https://www.twse.com.tw/zh/announcement/punish.html`:
- HTTP 200, 18351 bytes,但 `<table>` 數量 = **0**
- script src 包含 `/res/js/main.js` `/res/js/web.js` `/res/js/web-report.js`
- 沒有 `__INITIAL_STATE__` / react / vue 字眼 → **是 jQuery-flavored SPA**,table 由 JS 在 client side 用 AJAX 注入

→ 目前 `parse_default_settlement_html` 等 4 個 TWSE parser 用 bs4 找 `<table>`,**永遠抓不到 row**(silent miss,但因為 try/except 包住,不會 raise,只 print "0 rows" — 形同違約交割教訓的 silent skip 再現)。

### 5. 待辦(明天主公拍板)
1. **TWSE 4 條源換 endpoint**:
   - 違約交割:可能要找 `https://www.twse.com.tw/zh/api/...` 路徑(同樣 jQuery main.js 內部會有 AJAX URL)
   - 替代方案:抓 `https://mops.twse.com.tw/` 的對應公告 endpoint(較穩,有正規 query 介面)
2. **fetcher 把 TWSE 「0 rows」改成 raise warning**(別 silent skip,否則違約交割教訓白學)。
   - 例如 if `market == 'TWSE' and len(rows) == 0`: log.error 並回非 0 exit 給 workflow 觸發告警
3. **GitHub Actions 上實際 cron 是否跑過**:登 GitHub → Actions tab → "Stock Warnings Fetch" workflow,看 run history。

## 結論
- ✅ stock_warnings 表本機 cache.db 已建,117 rows 進去
- ✅ schema / fetcher / workflow 三份代碼都沒 bug
- ❌ **TWSE 4 條源實際抓不到 rows**(SPA 限制),需要明天主公決定改 endpoint 或加 mops 替代源
- ❌ TWSE 0 rows 應改 raise(silent skip 是違約交割教訓的 root cause,不能再犯)
