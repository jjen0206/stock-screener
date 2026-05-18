# Signal Conflict Penalty — 60-day Backtest 對比 (2026-05-18)

> Phase 2 #P2-7 — 反轉 × 趨勢衝突檢測對勝率 / AvgRet 的實證效果。

## TL;DR

**通過 spec gate,merge 採用。**

- Δ WR = **+3.66pp** (gate ≥ 0.5pp ✅)
- Δ AvgRet = **+0.78pp** (gate > 0 ✅)
- 衝突 picks WR 26.5% / AvgRet -1.87% — 明顯比同期 solo (34.2%) 還差
- 同期 consensus_ok picks WR 39.8% / AvgRet +0.96% — 真共識,值得保留 bonus

衝突檢把「假共識」精準篩出來,不是亂砍。

## 方法

1. 從 `pick_outcomes` 撈最近 60 天有 `return_d5` 的 picks (2515 列)
2. 按 `(pick_date, sid)` group → 2084 張 unique picks
3. 按 `STRATEGY_NATURE` 分類:
   - `solo` — 只命中 1 個策略 (1713 張)
   - `consensus_ok` — 多策略命中 + 無 reversal × trend 衝突 (269 張)
   - `conflict` — 多策略命中 + 同時含 reversal 派 + trend 派 (102 張)
4. 對每群算扣成本 WR / AvgRet (round-trip 0.6%)

## 結果

### 三群分組(扣 0.6% round-trip cost)

| Group | Fires | Wins | WR | AvgRet (d5, cost-adj) |
|-------|-------|------|-------|-----------------------|
| solo (count=1, 無共識) | 1713 | 585 | **34.2%** | +0.02% |
| consensus_ok (NEW 仍給 bonus) | 269 | 107 | **39.8%** | +0.96% |
| conflict (NEW 不給 bonus) | 102 | 27 | **26.5%** | **-1.87%** |

**洞察**:
- `consensus_ok` 的 WR (39.8%) 顯著高於 `solo` (34.2%) — 真共識有用
- `conflict` 的 WR (26.5%) **低於 solo** — 給 bonus 反而把垃圾推上去
- 把 conflict 群剝離後,「進到 bonus 的 picks」純度大幅提升

### OLD vs NEW 對比

| Mode | Fires | Wins | WR | AvgRet |
|------|-------|------|-------|--------|
| OLD: 所有多策略 picks 都吃 bonus (consensus_ok + conflict) | 371 | 134 | 36.1% | +0.18% |
| NEW: 衝突 picks 不吃 bonus (僅 consensus_ok) | 269 | 107 | **39.8%** | **+0.96%** |
| **Δ (NEW − OLD)** | -102 | -27 | **+3.66pp** | **+0.78pp** |

### 命中策略數分布

| 命中 N 策略 | 張數 |
|-------------|------|
| 1 | 1713 |
| 2 | 319 |
| 3 | 46 |
| 4 | 4 |
| 5 | 2 |

371 張 multi-strategy picks 中 **102 張 (27.5%)** 是衝突 — 比例不算低,證明改善空間實在。

## 衝突 picks 的典型模式

按 d5 ret 由低到高列前 20 張(全在 -5% 以下),最常見組合:
- `bias_convergence` + `macd_golden` × 9 次 — 「乖離收斂(反轉)」遇上「MACD 黃金交叉(趨勢)」
- `bb_lower_rebound` + `gap_up` × 多次 — 「布林下軌反彈」遇上「跳空缺口」
- `rsi_recovery` + `volume_breakout` 類

直覺解釋:這些組合是反向訊號加總 — 一邊預期回歸均值、一邊預期動能延續,中和後沒方向感,容易雙吐 (停損/停利雙觸)。

## STRATEGY_NATURE 完整對照

| Nature | Strategies |
|--------|------------|
| reversal | rsi_recovery, bb_lower_rebound, bias_convergence, inst_oversold_reversal |
| trend | ma_alignment, macd_golden, ma_squeeze_breakout, volume_breakout, gap_up |
| neutral | volume_kd, taiex_alpha, inst_consensus, inst_silent_accum, big_holder_inflow, high_yield_stable, ex_dividend_swing, eps_acceleration, revenue_acceleration |

**設計拍板**:
- `bias_convergence` → reversal:乖離收斂語意上就是均值回歸,儘管 `STRATEGY_CATEGORIES` 還把它標趨勢(歷史包袱不動)
- `volume_kd` → neutral:KD 從超賣回升 vs 趨勢延續曖昧,保守歸 neutral 避免誤判
- `taiex_alpha` → neutral:跟方向訊號正交
- 籌碼 / 基本面 / 殖利率 / 事件 → 一律 neutral(跟 reversal/trend 都不衝突)

## 程式碼變更

- `src/consensus.py`
  - 新增 `STRATEGY_NATURE` dict (3 群 18 個策略)
  - 新增 `has_signal_conflict(strategy_keys)` helper
  - 新增 `SIGNAL_CONFLICT_PENALTY_ENABLED` env kill switch
  - `consensus_multiplier`:衝突時 return 1.0(原 tier 邏輯保留)
  - `consensus_badge`:衝突時 badge 同步消失(避免 UI/multiplier 不一致)
- `tests/test_signal_conflict_penalty.py` — 26 個 test cases
- `scripts/audit/backtest_signal_conflict.py` — 本文資料來源

## 風險與後續

- **Coverage gap**:有新策略加入時忘了標 NATURE → 會 silently 當 neutral。`test_strategy_nature_covers_all_known_strategies` 守這道。
- **Cost 口徑**:用 0.6% round-trip,跟 bias_convergence rescue 同口徑。若主公後續調整,本報告 WR 數字會跟著浮動,但 ΔWR / ΔAvgRet 的方向不變(因 cost 對兩邊都套用)。
- **未來工具化**:若想看「衝突 picks 的時序分布」,可把 `scripts/audit/backtest_signal_conflict.py` 改成 weekly cron + 寫進 `signal_conflict_log` 表。**目前不做**(超出 P2-7 範疇)。

## 預期效果

下次 22:13 台北 daily_notify 推播時:
- 多策略命中且反轉 × 趨勢混合的 picks 會從 cross_2/cross_3 ⭐⭐共識 badge 降回無 badge
- 其 score 從 ×1.5 / ×1.8 降回 ×1.0,容易掉出 top-N 推播名單
- 預期推播純度提升 — 真共識(同方向) 更突出
