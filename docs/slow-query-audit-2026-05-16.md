# Slow Query / Missing Index Audit — 2026-05-16

> Round 3 健診子任務 4+5。對主要 cross-sid ranking helpers + sid-scoped query 跑 timing + `EXPLAIN QUERY PLAN`,找出 missing index。

## TL;DR

**找到 1 個 critical missing index + 1 個未來會用到的 preemptive index**:

- ★ `idx_daily_picks_sid ON daily_picks(sid)` — 修 4 個 ranking query(2 條 > 100ms)的 correlated subquery full scan
- 預先補 `idx_pick_shap_explanations_sid ON pick_shap_explanations(sid, pick_date DESC)` — 表目前 0 rows,backfill 後會用

兩條都已加進 `src/database.py:SCHEMA`,`init_db()` 下次跑會 IDEMPOTENT 補上。

---

## 環境

- DB:`data/cache.db`(從 `data/twse_snapshot/*.csv` preload)
- Rows:`stocks` 2,715 / `daily_prices` 152,594 / `institutional` 5,114 / `shareholder_concentration` 9,531 / `daily_picks` 8,489 / `pick_outcomes` 4,438 / `news` 3,585 / `pick_shap_explanations` 0
- Methodology:每 query 跑 5 次取 avg,> 100ms 標 ★

---

## Timing(before)

| Query | avg ms | min ms | max ms | rows | flag |
|---|---:|---:|---:|---:|---|
| `get_top_inst_consensus(min_days=2)` | 14.6 | 13.4 | 15.5 | 3 |  |
| `get_top_inst_consensus(min_days=3)` | 9.9 | 9.0 | 10.9 | 1 |  |
| `get_top_shareholder_movers(limit=30)` | **163.0** | 161.7 | 164.4 | 30 | **★** |
| `get_strong_follower_composite(min_inst_days=2)` | 20.8 | 20.2 | 21.6 | 1 |  |
| `get_consecutive_shareholder_increases(weeks=2)` | 40.4 | 39.0 | 43.4 | 15 |  |
| `get_pick_history_for_sid('00625K')` | 3.4 | 3.2 | 3.7 | 7 |  |
| `get_news_for_sid('2330', days=7)` | 2.5 | 2.3 | 2.7 | 16 |  |
| `get_top_shareholder_concentration(limit=30)` | **256.8** | 249.2 | 264.6 | 30 | **★** |

---

## 根因(2 個 > 100ms query)

`get_top_shareholder_movers` 跟 `get_top_shareholder_concentration` 共用 `_RANKING_SELECT`,其中對每筆結果做 correlated subquery 撈 ML prob:

```sql
(SELECT MAX(ml_prob) FROM daily_picks
   WHERE sid = sc.sid AND ml_prob IS NOT NULL) AS ml_prob
```

`EXPLAIN QUERY PLAN`(before):

```
SEARCH sc USING INDEX idx_shareholder_concentration_week (week_end=?)
SEARCH s USING INDEX sqlite_autoindex_stocks_1 (stock_id=?) LEFT-JOIN
CORRELATED SCALAR SUBQUERY 1
  SEARCH daily_prices USING INDEX sqlite_autoindex_daily_prices_1 (stock_id=?)
CORRELATED SCALAR SUBQUERY 2
  SEARCH daily_picks                       ← full scan 8,489 rows × 30 outer = 254k rows
USE TEMP B-TREE FOR ORDER BY
```

`daily_picks` 現有 indexes:

- PK `(trade_date, universe, strategy, sid, params_hash)` — leading col 不是 sid,沒法用
- `idx_daily_picks_lookup(trade_date, universe, params_hash)` — 同樣不含 sid

所以 sid-based subquery 走 full scan,每 outer row 重掃一次,8489 × 30 = **254k** row 掃描。

---

## 修法

加 `CREATE INDEX idx_daily_picks_sid ON daily_picks(sid)`(非 partial,順便也覆蓋 `get_pick_history_for_sid` 的 `WHERE p.sid=?`)。

`EXPLAIN QUERY PLAN`(after):

```
SEARCH sc USING INDEX idx_shareholder_concentration_week (week_end=?)
SEARCH s USING INDEX sqlite_autoindex_stocks_1 (stock_id=?) LEFT-JOIN
CORRELATED SCALAR SUBQUERY 1
  SEARCH daily_prices USING INDEX sqlite_autoindex_daily_prices_1 (stock_id=?)
CORRELATED SCALAR SUBQUERY 2
  SEARCH daily_picks USING INDEX idx_daily_picks_sid (sid=?)   ← 改吃 index
USE TEMP B-TREE FOR ORDER BY
```

## Timing(after)

| Query | avg ms (before → after) | 提升倍數 |
|---|---|---:|
| `get_top_shareholder_movers` | 163.0 → **4.8** | **34×** |
| `get_top_shareholder_concentration` | 256.8 → **4.8** | **53×** |
| `get_strong_follower_composite` | 20.8 → 18.0 | 1.2× |
| `get_top_inst_consensus(min_days=2)` | 14.6 → 6.5 | 2.2× |
| `get_consecutive_shareholder_increases` | 40.4 → 12.0 | 3.4× |
| `get_pick_history_for_sid` | 3.4 → 2.6 | 1.3× |
| `get_news_for_sid` | 2.5 → 2.4 | — |

所有 query 都 < 20ms,主要 cross-sid ranking helper 全 < 5ms。

---

## 其他表的 index 盤點(沒問題,但記錄一下)

| Table | Rows | 現有 index | 結論 |
|---|---:|---|---|
| `daily_prices` | 152,594 | PK(stock_id, date) + idx_date | 充足 |
| `institutional` | 5,114 | PK(stock_id, date) + idx_date | 充足(`inst_consensus` query 走 full scan 但只 5k rows 也 6.5ms)|
| `shareholder_concentration` | 9,531 | PK(sid, week_end) + idx_week | 充足 |
| `news` | 3,585 | UNIQUE url_hash + idx_sent + idx_sid_date | 充足 |
| `pick_outcomes` | 4,438 | PK(pick_date, sid, strategy) + idx_date | 充足(JOIN 走 3-col PK)|
| `paper_trades` | 27 | UNIQUE(sid, entry_date) + idx_status | 表小,index 充足 |
| `analyst_targets` | 443 | PK(stock_id, source) + idx_sid | 充足 |
| `target_hit_log` | 0 | PK + idx | 待累積後再回頭看 |
| `alert_dedup` | 0 | PK | 待累積 |
| `stock_warnings` | 0 | PK + idx_sid_type + idx_effective_to | 待累積(2026-05-16 違約 endpoint 修完後會有)|
| `pick_shap_explanations` | **0** | PK + idx_date | **★ 預先補 `(sid, pick_date DESC)`** — backfill 2026-05-17 之後會有 ~3000 rows,`get_shap_for_sid_latest` 是 `WHERE sid=? ORDER BY pick_date DESC LIMIT 1` |

---

## Action items

✅ 加 `idx_daily_picks_sid` 到 `src/database.py:SCHEMA`(commit 同一輪)
✅ 加 `idx_pick_shap_explanations_sid` 到 `src/database.py:SCHEMA`(preemptive)
✅ 兩條都 IDEMPOTENT `CREATE INDEX IF NOT EXISTS`,`init_db()` 下次跑(任何 cron / streamlit boot)自動補上;production cache.db 不需手動 migrate

🔲 監測待累積表的 query pattern(target_hit_log / alert_dedup / stock_warnings),累積後重跑此 audit
🔲 `get_strong_follower_composite` 18ms 在容忍範圍,但 query 內部 3 個 `SELECT COUNT(*) FROM joined` 算 rank normalization,將來資料量大可能變慢 — 短期不動

---

## 重跑驗證指令

```bash
python -c "
import sys, time, statistics
sys.path.insert(0, '.')
from src import database as db
for name, fn in [
    ('movers', lambda: db.get_top_shareholder_movers(limit=30)),
    ('concentration', lambda: db.get_top_shareholder_concentration(limit=30)),
    ('inst_consensus', lambda: db.get_top_inst_consensus(min_days=2, limit=30)),
    ('strong_follower', lambda: db.get_strong_follower_composite(min_inst_days=2, limit=30)),
    ('consecutive_sh', lambda: db.get_consecutive_shareholder_increases(weeks=2, limit=30)),
]:
    t = [time.perf_counter() for _ in range(5)]; [fn() for _ in range(5)]
    timings = []
    for _ in range(5):
        t0 = time.perf_counter(); fn()
        timings.append((time.perf_counter()-t0)*1000)
    print(f'{name:20s} avg={statistics.mean(timings):6.1f}ms')
"
```
