# TWSE / TPEx 警示股 — 違約交割尚未覆蓋(silent miss 警告)

**狀態**:2026-05-16 主公拍板 fix(bs4 → OpenAPI JSON)後仍存在的覆蓋缺口。
**影響等級**:中(picks pipeline 依舊抓不到「違約交割」公告 → 主公曾踩過的違約股
還是不會被 stock_warnings.default_settlement 擋住)。

## 背景

2026-05-15 健診四件套發現:`scripts/fetch_stock_warnings.py` 的 TWSE 4 條 bs4 HTML
parser 全部抓到 0 rows(原 punish.html / notice.html / disposition.html /
method.html 都是 jQuery SPA,bs4 看不到 row)。違約股 silent miss 是 hard risk
(違約交割教訓 root cause)。

2026-05-16 修復:把 TWSE 4 條源從 bs4 全部換成 OpenAPI v1 JSON:

| 警示分類 | TWSE OpenAPI endpoint | 狀態 |
|---|---|---|
| 處置股 (disposition) | `/v1/announcement/punish` | ✅ 已替換,29 rows 進來 |
| 注意股 (attention) | `/v1/announcement/notice` | ✅ 已替換(假日會 0 rows,合理) |
| 注意股累計次數 | `/v1/announcement/notetrans` | ✅ 已替換(補充來源) |
| 變更交易方法 (method_changed) | `/v1/exchangeReport/TWT85U` | ⚠️ 已替換,但欄位陽春 |
| **違約交割 (default_settlement)** | **無對應 endpoint** | ❌ **未覆蓋** |

## 違約交割無 OpenAPI endpoint 確認

2026-05-16 抓 `https://openapi.twse.com.tw/v1/swagger.json` 全 143 paths
grep keywords(`punish`, `default`, `settlement`, `違約`, `bfigtu`, `處分`),
**無任何 endpoint 命中違約交割**。

TWSE 違約公告專區 HTML 頁:`https://www.twse.com.tw/zh/announcement/bfigtu.html`
(SPA,需 browser automation;違反專案禁用 selenium 原則)。

TPEx 同樣狀況:OpenAPI v1 無對應 endpoint,只有 SPA 頁
`https://www.tpex.org.tw/web/bulletin/announcement/default.php`。

## TWT85U(變更交易)欄位限制

`/v1/exchangeReport/TWT85U` schema 只有 3 欄:
- `Code`(證券代號)
- `Name`(證券名稱)
- `PeriodicCallAuctionTrading`(分盤集合競價 flag,`"**"` 或空白)

缺失:
- 無 `Date` 欄位 → fetcher 用今天 UTC date 當 `announced_date` / `effective_from`
- 無 reason 描述 → 全標 "TWSE 變更交易方法"(分盤額外標 "分盤集合競價")
- 無解除日 → `effective_to = NULL`(視為仍生效)
- 無法區分 `full_cash` vs 一般變更方法 → 全進 `method_changed`,picks 統一 soft 降權

這代表「全額交割」的 TWSE 上市股,目前**不會**被歸到 `full_cash`(硬擋),
只會被歸到 `method_changed`(soft 降權)。TPEx `tpex_cmode` 還是會正確判斷
`ManagedStock=Ｙ` → `full_cash`,上櫃覆蓋正常。

## 影響評估

### 對 picks 的影響
- **disposition / attention / method_changed**:這次修復前 silent miss、現在會擋,大進步。
- **default_settlement(違約交割)**:本次修復前**也是 silent miss**(原 bs4 把 punish.html
  錯標為 default_settlement 卻 0 rows),修復後**狀態相同**,只是現在誠實 log warning。
- **full_cash(全額交割)**:TWSE 上市股這條也沒覆蓋(同上 TWT85U 欄位限制),
  TPEx 上櫃正常。

### 主公的違約股事件
那檔股票若是 TWSE 上市違約交割,picks 依舊不會擋,需主公人工 watchlist。

## 後續可選方案(留 TODO)

優先級由高到低:

### 方案 A:抓 TWSE 公文公告 RSS / JSON list(待研究)
TWSE `/zh/announcement/announcement/list.html` 是公文公告總表,**有可能**有 RSS
或 JSON list endpoint。值得花 30 分鐘 web search + 抓 swagger 確認。

### 方案 B:解析公開資訊觀測站(mops)違約交割重大訊息
mops.twse.com.tw 有重大訊息分類 RSS,違約交割屬重大訊息範疇,搜「違約交割」
關鍵字過濾。

### 方案 C:每週手動同步主公 watchlist(現況)
最低成本,但仰賴主公自己盯著新聞。違約事件不頻繁(年數筆),不算太糟。

### 方案 D:跑 selenium 抓 SPA(不推)
違反專案禁用 selenium 規則,CI 也跑不起來,不建議。

## 怎麼通知主公

`scripts/fetch_stock_warnings.py` 每次跑都會 log warning:

```
[WARNINGS] default_settlement(違約交割):TWSE/TPEx OpenAPI v1 皆無對應
endpoint,本次 run 不抓;見 docs/twse-warnings-still-broken.md
```

CI workflow `stock-warnings.yml` 跑 fetcher 時這條會出現在 GitHub Actions log
最上面。主公巡 daily-fetch / weekly summary 時可看見。

## 修復後 baseline 偵測

`fetch_and_parse_all()` 結尾新增防呆:若 TWSE punish + TWT85U **兩條基線同時 0 rows**
→ raise。這兩條源歷史上一定有資料,同時 0 表示 OpenAPI 整體壞掉(URL/schema 變、
被擋等),會觸發 CI exit 1 警報主公。

(notice / notetrans / TPEx 三條允許 0 rows = 假日沒事件,不在 baseline 內)
