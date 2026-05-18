# Telegram Bot Serve — 設計決策(ADR）

日期:2026-05-18
作者:軍師(實作)+ 主公(spec)
狀態:Accepted

## 背景

`src/notifier.py:2482` 預留 dispatch hook,但 `scripts/telegram_bot_serve.py` 一直
沒生出來,等於把 inline keyboard / callback_query / 雙向問答全部擱置。主公每天
用 Telegram,需要「路上隨手打 2330,bot 即時回答」這條能力。

## 1. Daemon 怎麼跑

| 選項 | Pros | Cons |
| --- | --- | --- |
| A. 本機 PowerShell scheduled task | 本機 dev 友好;延遲低 | 主公機關了就掛;需固定 IP / port forward |
| B. Streamlit Cloud background task | 一機到底 | Free tier sleep 後背景死,推不上來 |
| C. Cloudflare Workers + Telegram webhook | serverless 零成本、無需 daemon | 要學 Workers + 額外 secret 管理,首次成本高 |
| **D. GHA cron `*/5 * * * *` `getUpdates` 純拉模式** | **完全免費、跟現有 22 個 workflow 同 stack、零部署、零學習** | 延遲最多 5 min(對隨手問股票完全夠) |

**決定:選 D**(軍師偏好,主公在 spec 確認)。理由:

1. **零新基礎設施** — 跟 news-notify / intraday-alerts / data-health-alert 同 cron pattern,沒人需要學新工具
2. **狀態持久化天然就有 commit-snapshot 機制** — update_id offset 跟 watchlist / news 一樣走 `data/twse_snapshot/` CSV 進 repo
3. **GH runner 是 ephemeral container** — 不必擔心 long-running daemon 內存 / 殭屍 / log rotation
4. **延遲 5 min 對 use case 完全夠** — 主公問 2330,公車到站前能拿到回答即可

## 2. 狀態保存

需要持久化的東西:
- `last_update_id` — Telegram `getUpdates?offset={last+1}` 用,沒這個會重複處理同一條訊息
- 未來可擴(目前不做):per-user mute、訂閱頻道等

選用方案:
- **table**: `telegram_bot_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)`
  — 通用 key-value,未來加新狀態不用改 schema
- **CSV snapshot**: `data/twse_snapshot/telegram_bot_state.csv`
  — 每次 GHA run 結束 dump + commit + push,雲端 boot 走 `preload_snapshots()` 還原
- **e2e test** 守住:
  - dump → load roundtrip 不掉資料
  - boot path 從 CSV 還原進 SQLite(regression guard,避免「持久化壞掉但沒人發現」)
  - 記憶規則: 「persistence push 路徑須兩層保護」適用

## 3. 指令解析

維持單一 entry `parse_intent(text: str)`,回 dataclass:

| 輸入 | intent | 範例 |
| --- | --- | --- |
| 純 4-6 位數字 (`2330`, `2330.TW`) | `STOCK_QUERY` | 該股最新價 + verdict + 1-2 條訊號 |
| `強者跟蹤` / `今天表現` / `關注` / `持倉` / `健康` | `PAGE_DIGEST` | 對應頁面 top 5 摘要 |
| `/help` / 不認識 | `HELP` | 列出可用指令 |
| 其他自然語言 | `FREEFORM` | 走 `ai_assistant.ask_about_market(text)`(若含 sid token 改 stock) |

每條 handler 自帶 fallback,有資料就回資料、無資料就回「(無資料)」,不 raise。

## 4. 不做的事(scope guard)

- 不做 webhook 模式(不開 HTTP server)
- 不做 multi-user / mute / 訂閱 / DM 廣播
- 不做 retry queue(GHA 每 5 min 拉,自然 retry)
- 不做 LLM cache(已經有 ai_assistant graceful)

## 5. 工時

預估 30-60 min MVP。實際:見 commit 時間戳。
