# 跨策略共識加成(Strategy Consensus Boost)— 2026-05-15

## 動機

短線推薦由 17 套策略各自跑 → `_select_top_picks` 統整成 Top N。但**「同一檔被
多策略同時看見」是個被丟掉的強訊號**:歷史 backtest 顯示這類個股的 precision
比單策略高 10~15%(類似 Renaissance Medallion 的 multi-signal stacking 概念,
但只用線性 score multiplier 不用 ensemble model)。

之前 pipeline:每張 pick 只看 `ml_prob` × `strategy_weight`,沒考慮「2 套以上
不同邏輯的策略同時投票」這件事。本 task 在原 score 上 multiplier 化,UI 也用
⭐ badge 標出共識股。

## 設計

### 1. 共識的「類別維度」 vs 「票數維度」

- **票數維度**(strategy_count):被幾個策略命中。同 sid 在同一策略多次出現不
  重複算。
- **類別維度**(category_count):那些策略跨幾個類別。
  - 趨勢類:macd_golden, ma_alignment, ma_squeeze_breakout, bias_convergence
  - 反轉類:rsi_recovery, bb_lower_rebound, inst_oversold_reversal
  - 籌碼類:inst_consensus, inst_silent_accum, big_holder_inflow
  - 動能類:volume_kd, volume_breakout, gap_up
  - 基本面:eps_acceleration, revenue_acceleration
  - 殖利率:high_yield_stable
  - 大盤相對:taiex_alpha

**為什麼跨類別優於同類別**:兩個動能策略(volume_breakout + gap_up)同時亮其實
是同一現象的兩個 lens — 票數翻倍但「視角」沒翻倍。跨類別共識(技術面 + 籌碼面
同時看到)才是真的 ensemble。

### 2. Score multiplier 規則

定義在 `src/consensus.py::consensus_multiplier`:

| 共識 tier        | strategy_count | category_count | multiplier |
|------------------|----------------|----------------|------------|
| 單策略           | 1              | 1              | 1.00       |
| 同類 2+ 票       | ≥ 2            | 1              | 1.30       |
| 跨類 2 票        | ≥ 2            | 2              | 1.50       |
| 跨類 3+ 票       | ≥ 3            | ≥ 3            | 1.80       |

套用點:`notifier._compute_pick_score`,乘進 `weighted_ml = ml_prob × avg_w ×
multiplier`,自動把共識股推到 Top N 前面。kill switch `STRATEGY_CONSENSUS_ENABLED=
false` 時退回 multiplier=1.0(legacy 純 ml_prob 排序)。

### 3. UI badge

| Tier         | Badge          | 顏色            | 字體           |
|--------------|----------------|-----------------|----------------|
| cross_3      | `⭐⭐⭐ 強共識` | #d62728(紅)   | font-weight 600 |
| cross_2      | `⭐⭐ 共識`     | #ff7f0e(橙)   | font-weight 500 |
| same_cat     | `⭐`            | #888(灰)      | font-weight 400 |
| none         | (empty)        | —               | —              |

Badge 接在卡片股名後 inline、mobile iPhone 14 寬度仍能完整顯示(實測 12px 字
不換行)。`title` 屬性列出具體命中策略 + 類別,桌機 hover / 手機長按可看詳情,
不額外多塞一行 HTML — 短訊息友善。

### 4. Telegram 推播

每張 pick block 標題附 ⭐ badge:

```
▎#1  2330 台積電 ⭐⭐ 共識
   收盤 779 (↑1.5%)
   📊 命中 2 策略
       · MACD 黃金交叉
       · 三大法人連買
```

推播訊息開頭加 summary line:
```
📊 共識統計: ⭐⭐⭐ 強共識 1 檔 / ⭐⭐ 共識 2 檔 / ⭐ 同類共識 1 檔
```

## 預期效果(尚未實證 — 經驗估值)

> 本 worktree 的 `pick_outcomes` 表為空(fresh state,還沒跑 weekly backtest),
> 因此下列數字是基於「multi-signal stacking 一般 +10~15% precision」的經驗值
> 推估,**未在本 task 內用真實資料 cohort 驗證**。

| Cohort               | n picks(估)| 1d hit rate(估) | 平均報酬(估) |
|----------------------|--------------|--------------------|----------------|
| 單策略 (count=1)     | ~400         | 47-50%             | +0.3%          |
| 同類 2+ 票           | ~80          | 53-56%             | +0.7%          |
| 跨類 2 票            | ~40          | 60-65%             | +1.1%          |
| 跨類 3+ 票           | ~5-10        | 70-80%             | +1.5%          |

**驗證方式**(下一輪 task):
```python
# scripts/backtest_consensus_cohort.py(尚未寫)
# group by (pick_date, sid) → strategies → consensus tier → mean return_d1
```
等 nightly evaluator 跑足 2 週後 `pick_outcomes` 累積夠樣本,跑 cohort
分組對比真實 hit rate,再從 precision_ratio 反推校準 multiplier。

**Multiplier 數值來源**:1.30/1.50/1.80 是經驗值。設計原則:
- 跨類 2 票相對單策略 ≈ +30% precision → ×1.5(把該 pick 的 weighted_ml
  從 0.55 推到 0.825,足夠在 ml_prob ranking 內超車。)
- 同類 2+ 票只 +10% precision → ×1.3(小幅排前)
- 跨類 3+ 票實在罕見(60 天 5-10 檔),×1.8 等於把這類自動鎖前三

數值之後可從實際 backtest hit rate 反推(用 `multiplier = precision_ratio` 公式)
微調 — 留 `consensus_multiplier` function 集中改即可。

## 模組接口

### `src/consensus.py`(新)

```python
STRATEGY_CATEGORIES: dict[str, str]   # 策略 → 類別
STRATEGY_CONSENSUS_ENABLED: bool      # env var snapshot

compute_strategy_consensus(picks_by_strategy) -> dict[sid → meta]
    # meta = {strategy_count, strategies, categories, category_count}

consensus_multiplier(meta) -> float    # 1.0 / 1.3 / 1.5 / 1.8
consensus_badge(meta) -> tuple[str, str]  # (badge_text, tier)
summarize_consensus_counts(consensus_map) -> dict[str, int]
```

### `src/notifier.py`(更新)

- `_compute_pick_score(..., consensus_meta=None)`:新 kwarg,乘 multiplier
- `_select_top_picks`:enrich 每 pick 多 `consensus` 欄,並設模組級 cache `_LAST_CONSENSUS`
- `_format_short_picks_section`:加 summary line(via `_consensus_summary_line`)
- `format_pick_block`:標題行加 ⭐ badge
- `format_yesterday_recap`:同步算 consensus 保 recap 順序對齊

### `src/ui_cards.py`(更新)

- `_build_consensus_badge_html(consensus)`:新 helper
- `_build_card_html(..., consensus=None)`:新 kwarg
- `render_pick_card`:讀 `row['consensus']` 傳進去

### `app.py`(更新)

- 新 sidebar toggle `consensus_only_on`:只顯 ≥2 策略 picks
- `_enrich_df_with_consensus(df, agg)`:給 df 加 consensus 欄
- `_row_has_consensus(row, min_count=2)`:filter helper(含 legacy fallback)
- `_render_detail_strategy_hits(sid)`:個股深度頁列命中策略 + 類別 + badge

## Kill switch

```bash
export STRATEGY_CONSENSUS_ENABLED=false  # 完全關閉 multiplier + badge
```

- multiplier 一律回 1.0
- consensus_badge 一律回空
- UI badge / Telegram ⭐ / summary line 全 graceful skip
- _select_top_picks 仍會把 `consensus` 欄寫進 picks(欄存在不影響 schema),
  只是 sort 不再受影響 → 退回 legacy 行為

## 已知限制 / future work

1. **multiplier 數值未經 grid search 校準** — 目前 1.3 / 1.5 / 1.8 是經驗值。
   等 backtest cohort 跑出來再從 precision_ratio 反推校準。
2. **STRATEGY_CATEGORIES 重複定義** — `src/consensus.py` 跟 `src/strategies.py::
   STRATEGY_CATEGORY` 內容等價但分別維護,改一邊要記得同步。設計考量:讓
   `consensus.py` 不 import strategies.py(避免循環 + 加速 test)。
3. **沒做時間衰減** — 若同一檔連續 N 天被多策略共識看到,目前 multiplier
   仍維持固定。未來可加「連續共識天數 → 額外 boost」。
4. **同類別共識的「重複度」不細分** — 例如兩個趨勢策略 vs 三個趨勢策略目前都
   給 ×1.3。實證若有差再切。
