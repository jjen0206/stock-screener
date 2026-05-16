# daily_picks 9 天歷史的根因(2026-05-16)

## TL;DR

`daily_picks` 表只有 9–10 天歷史**不是 bug,是 table 新**。
- Schema 在 2026-05-04 commit `4b4d24f` 加入
- 既有資料區間:2026-04-30 ~ 2026-05-15(10 天)
- 沒有 retention 政策在清舊資料 — `INSERT INTO daily_picks` 的 PK 是
  `(trade_date, universe, strategy, sid, params_hash)`,每天新資料 append 不覆寫舊日期

## 確認過的非根因

| 假設 | 驗證 | 結論 |
|----|----|----|
| (b) retention 政策在清舊資料 | `grep -rn "DELETE FROM daily_picks\|trade_date <"` | 無;`clear_daily_picks_for_date` 只清「**同一天**」舊資料,跨天不動 |
| (c) UNIQUE constraint 覆蓋每次跑 | 看 `CREATE TABLE daily_picks` PK | PK 含 `trade_date`,不同日期 row 並存 |
| 雲端 preload 蓋掉 SQLite | 看 `preload_snapshots` | 走 UPSERT,merge 不刪除 |

## 真正根因 (a):table 新建

| 事件 | 日期 | Commit |
|----|----|----|
| `daily_picks` schema 加入 | 2026-05-04 | `4b4d24f feat(precompute): daily_picks 表 + precompute_strategies 腳本(Part 1/4)` |
| `daily_picks.csv` snapshot 首次出現 | 2026-05-04 | `c433ec7 chore(precompute): daily strategies snapshot` |
| 最早 `trade_date` 紀錄 | 2026-04-30 | 來自 `--backfill N` 模式回填(precompute_strategies.py 支援 lookback) |

也就是說:**2026-05-04 之前根本沒這張表**,所以「為什麼只有 9 天」這問題的答案
是「table 才 13 天大」。後續會自然每天累積,3 個月後就會有 ~60 個交易日歷史。

## 修法

- ✅ **無需修任何 code** — 沒 retention 在清,nothing to fix
- ✅ Document only:就是這份檔案
- ❌ 不需要改 `INSERT OR IGNORE` / `ON CONFLICT DO NOTHING`(本來就是 PK upsert 不
  跨天衝突)
- ❌ 不需要 `--backfill` 拉更舊歷史(歷史的全策略 backtest 走 `pick_outcomes`,
  daily_picks 主要是 streamlit 0ms cache + alerts 用,只要近 30~60 天就夠)

## 健診 list 上的「9 天歷史」對應

| 健診項 | 預期回答 |
|----|----|
| daily_picks 只 9 天 → 補 backfill? | **不補**。table 新,後續會自然累積 |
| daily_picks 只 9 天 → retention 問題? | **不是**。沒 retention 在清 |
| daily_picks 只 9 天 → 改 INSERT OR IGNORE? | **不需要**。PK 含 trade_date,不會衝突 |

## 相關檔案

- `src/database.py:240` `CREATE TABLE daily_picks` schema
- `src/database.py:2606` `clear_daily_picks_for_date`(per-day clear,跨天保留)
- `scripts/precompute_strategies.py:181` 寫入 daily_picks 的唯一路徑
- `.github/workflows/daily-notify.yml:56` 每日跑 precompute 的 workflow

## SHAP 補歷史

對應於補 SHAP 的需求(`pick_shap_explanations` 只覆蓋 1–3 天),走
`scripts/backfill_pick_shap.py` 補。SHAP cache 是另一張表,跟 daily_picks
獨立,不互相影響。
