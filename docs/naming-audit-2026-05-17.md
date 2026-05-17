# 命名一致性審計 — Round 2

**日期**: 2026-05-17
**範圍**: kill-switch env vars + notifier.py 模組級 cache vars
**結論**: 多數一致,標記出**1 處 outlier**,**不強制改**(避免動 prod env / GitHub Actions secrets)。

---

## 1. Kill-switch env var 命名

對齊 5 個近期加的 feature flag:

| Env var | Pattern | Module |
|---|---|---|
| `STRATEGY_CONSENSUS_ENABLED` | `[FEATURE_NOUN]_ENABLED` | `src/consensus.py` |
| `ML_CALIBRATION_ENABLED` | `[FEATURE_NOUN]_ENABLED` | `src/ml_calibration.py` |
| `REGIME_GATING_ENABLED` | `[FEATURE_NOUN]_ENABLED` | `src/regime_gating.py` |
| `THEME_HEAT_ENABLED` | `[FEATURE_NOUN]_ENABLED` | `src/theme_heat.py` |
| **`WARNING_ANNOTATE_ENABLED`** | `[DOMAIN]_[VERB]_ENABLED` ⚠️ outlier | `src/warnings_filter.py` |

### Outlier 分析

`WARNING_ANNOTATE_ENABLED` 用「名詞_動詞_ENABLED」(警示_標註_啟用),
其他 4 個都是「名詞_ENABLED」。

**Why 沒對齊**:
- 歷史:原本叫 `WARNING_EXCLUDE_ENABLED`(2026-05-15 之前),那時的設計是
  warning 走 hard-exclude(filter out 不顯)。
- 同一天主公拍板「軍師不替主公做隱藏決定」,設計改成 annotate-only,只標
  ⚠️ + 排序往後,但仍出現在推薦中。
- env var 跟著改名 `_EXCLUDE_` → `_ANNOTATE_`,保留動詞表達當前語意。

**對齊選項**(任選或不做):
1. **改成 `WARNINGS_FILTER_ENABLED`**(對齊 module 名 `warnings_filter`,跟其他
   `[FEATURE_NOUN]_ENABLED` 一致)。
2. **保留現名**(動詞表達 annotate 語意比較精準,將來若加 `WARNING_*` 系列
   flag 比較清楚)。

**建議:不改**。理由:
- 這個 env var 已經寫進 GitHub Actions workflow secrets + 本機 `.env`,
  改名要同步多處,風險高,收益低。
- 動詞語意可讀性 OK,看到 `WARNING_ANNOTATE_ENABLED` 一目瞭然「警示要不要標」。
- 不一致只是字串 1 個 outlier,沒造成實際 bug。

---

## 2. notifier.py 模組級 cache 命名

```
_LAST_ANNOTATED_WARNINGS  : list[dict]
_LAST_CONSENSUS           : dict[str, dict]
_LAST_THEME_HEAT          : dict[str, dict]
_LAST_THEME_EXCLUDED      : dict[str, list[str]]
```

**Pattern**: `_LAST_[FEATURE_RESULT]` — 都是「最後一次 `_select_top_picks` 跑完
的某 feature 結果 snapshot」。一致。

### 沒有 outlier,但有歷史 rename

- `_LAST_ANNOTATED_WARNINGS` 之前叫 `_LAST_EXCLUDED_WARNINGS`(同 §1 的歷史)。
- `_LAST_THEME_EXCLUDED` 名字含 `EXCLUDED` 是因為**冷題材是真的 hard exclude**
  (2026-05-15 主公二次拍板,從 ×0.7 改 hard exclude),所以動詞精準。

`THEME_EXCLUDED` 跟 `ANNOTATED_WARNINGS` 動詞不同(excluded vs annotated)是
**故意**的,反映兩個 feature 行為差異:
- 警示股:標註不擋
- 冷題材:擋掉不推

---

## 3. 動作項目

無強制改動。若未來新增 feature flag,**請依循 `[FEATURE_NOUN]_ENABLED` pattern**
(consensus / calibration / regime_gating / theme_heat 都遵循)。

> "Don't fix what isn't broken — but document the inconsistency."
