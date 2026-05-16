# TWSE / TPEx 警示股 — 違約交割已透過 MOPS RSS 覆蓋(2026-05-16 update)

**狀態**:2026-05-16 加入 MOPS 重大訊息 RSS 過濾「違約」關鍵字後,違約交割
silent miss 缺口**已關閉**。本文件改成記錄整體現況 + 剩餘限制。

## 警示分類覆蓋現況

| 警示分類 | 主要來源 | 狀態 |
|---|---|---|
| 處置股 (disposition) | TWSE `/v1/announcement/punish` + TPEx `tpex_disposal_information` | ✅ 已覆蓋 |
| 注意股 (attention) | TWSE `/v1/announcement/notice` + `notetrans` + TPEx `tpex_trading_warning_information` | ✅ 已覆蓋 |
| 變更交易方法 (method_changed) | TWSE `/v1/exchangeReport/TWT85U` + TPEx `tpex_cmode.AlteredTrading=Ｙ` | ⚠️ 已覆蓋,TWSE 欄位陽春(見下) |
| 全額交割 (full_cash) | TPEx `tpex_cmode.ManagedStock=Ｙ` / `SuspensionOfTrading=Ｙ` | ⚠️ 僅 TPEx 上櫃,TWSE 上市股無法區分 |
| **違約交割 (default_settlement)** | **MOPS 重大訊息 RSS `mopsrss201001.xml`** | ✅ **2026-05-16 加入** |

## 違約交割覆蓋方案(2026-05-16 加入)

**Endpoint**:`https://mopsov.twse.com.tw/nas/rss/mopsrss201001.xml`

- MOPS 公開資訊觀測站重大訊息 RSS,滾動最近 100 筆。
- 編碼 cp950(XML 宣告 big5,實際內容有 big5 / cp950 通用區段)。
- Parser 過濾標題或描述含「違約」關鍵字(`違約交割` / `違約`)。
- 涵蓋全市場:TWSE 上市 + TPEx 上櫃 + 興櫃 + 公開發行公司。
- 違約事件年數筆,日常多 0 rows 屬正常,**不在 baseline raise 偵測內**。
- 提取 `(NNNN)` 4-6 碼證券代號自 RSS item title。
- `pubDate` (RFC 822) → `announced_date` (ISO date)。
- `source_url` 用 item `<link>`(指向 MOPS 該則公告詳情頁)。

### 為什麼選 MOPS RSS,不選其他方案

- ❌ TWSE OpenAPI v1 swagger 143 paths 無對應違約 endpoint。
- ❌ TWSE 違約公告專區 `https://www.twse.com.tw/zh/announcement/bfigtu.html`
  是 SPA,需 selenium(違反專案規則)。
- ❌ TPEx OpenAPI v1 也無對應 endpoint。
- ✅ MOPS RSS 是純 XML,無需 browser automation,跟現有 fetcher 框架一致
  (HTTP GET + parse)。

### 偽陽性風險

「違約」單詞也可能命中:
- 公司澄清違約傳聞(實際無違約)
- 違約金 / 違約條款相關公告(無關投資人交割)

評估:MOPS 重大訊息中「違約」單詞出現頻率本就極低(年數筆),且寧抓多
不漏掉(主公曾踩過違約股 → silent miss 不可接受,偽陽性可接受;真不確定
可看 `reason` 欄位的全文判讀)。

## TWT85U(TWSE 變更交易)欄位限制(仍存在)

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

## 防呆:baseline 偵測

`fetch_and_parse_all()` 結尾防呆:若 TWSE punish + TWT85U **兩條基線同時 0 rows**
→ raise。這兩條源歷史上一定有資料,同時 0 表示 OpenAPI 整體壞掉(URL/schema 變、
被擋等),會觸發 CI exit 1 警報主公。

(notice / notetrans / TPEx 三條 + MOPS 違約允許 0 rows = 假日沒事件,不在 baseline 內)

## MOPS RSS fetch 失敗策略

MOPS 偶爾 502 / 空頁。`_fetch_mops_rss_text` 走 retry 3 次(指數退避),
連 3 次都失敗 → fetcher 在 MOPS 區段**只 log warning,不 raise**(讓 TWSE/TPEx
其他警示仍寫入)。日後改用 GitHub Actions cron + 監控 by_type 出現 0 即可
人工檢查。
