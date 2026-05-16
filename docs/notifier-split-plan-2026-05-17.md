# notifier.py 拆模組計畫 — Round 2 評估

**日期**: 2026-05-17
**現況**: `src/notifier.py` 2073 lines(已經超出 1000 行門檻)
**結論**: **暫不拆**。寫進 plan,等後續專屬 PR 再做。

---

## 為什麼這次不拆

1. **wire 測試大量依賴 `notifier.X` 符號**(用 `inspect.getsource()` 或 `hasattr`):
   ```
   test_dynamic_weighting_wire.py:  notifier._compute_pick_score / _select_top_picks (getsource)
   test_notifier_consensus_wire.py: hasattr(notifier, "_LAST_CONSENSUS")
   test_notifier_warning_wire.py:   hasattr(notifier, "_LAST_ANNOTATED_WARNINGS")
   test_notifier_theme_heat_wire.py: hasattr(notifier, "_LAST_THEME_HEAT/_EXCLUDED")
   test_entry_range_wire.py:        notifier._select_top_picks / format_pick_block (getsource)
   test_big_holder_inflow_wire.py:  notifier._select_top_picks (getsource)
   ```
2. **模組級 cache `_LAST_*` 跨 `_select_top_picks`(寫)→ `format_top_picks_message`(讀)**。
   若 pipeline 拆出去,cache 變兩個檔共用,容易出 import cycle / state mismatch。
3. **time-box**:Round 2 任務 ETA 3-5 小時包含 dead code / fixture refactor / docs。
   拆模組單獨就要 1-2 小時 + 全套 wire test 驗證,放這輪不划算。

## 拆法(留給後續 PR)

### Target structure

```
src/
├── notifier.py            # 只剩 send_*, notify_*, module-level cache 跟 high-level orch
├── pick_pipeline.py       # _compute_pick_score, _select_top_picks, compute_top_picks
└── notifier_format.py     # format_pick_block, format_top_picks_message, format_yesterday_recap,
                           #   _format_short_picks_section, _format_footer_block, caption helpers
```

### 行數估計

| 模組 | 行數 | 主要符號 |
|---|---|---|
| `notifier.py` (拆後) | ~600 | send_telegram_message, send_discord_message, notify_top_picks, notify_short_picks, notify_multi_strategy, notify_manual_picks, module-level cache, PEP562 `__getattr__` |
| `pick_pipeline.py` (新) | ~600 | `_compute_pick_score`, `_select_top_picks`, `compute_top_picks` + 內部 helpers |
| `notifier_format.py` (新) | ~900 | `format_pick_block`, `format_premium_picks_block`, `format_big_holder_movers_block`, `format_yesterday_recap`, `format_top_picks_message`, `_regime_gating_caption`, `_format_short_picks_section`, `_consensus_summary_line`, `_format_footer_block`, `_enrich_picks_with_shap`, `format_news_block`, `format_news_message`, `format_short_picks`, `format_multi_strategy_picks`, `format_manual_picks` |

### 必做的 backward-compat 措施

**不能直接拆 — 一定要在 `notifier.py` 重新 export**,否則 wire test 全爆:

```python
# notifier.py(拆後)
from src.pick_pipeline import (
    _compute_pick_score, _select_top_picks, compute_top_picks,
)
from src.notifier_format import (
    format_pick_block, format_top_picks_message, format_yesterday_recap,
    format_premium_picks_block, format_news_block, format_news_message,
    format_short_picks, format_multi_strategy_picks, format_manual_picks,
)
```

### 模組級 cache 的搬法

`_LAST_*` 共有 4 個(`_LAST_ANNOTATED_WARNINGS / _LAST_CONSENSUS / _LAST_THEME_HEAT / _LAST_THEME_EXCLUDED`),被 `_select_top_picks`(在 pick_pipeline)**寫**,被 `format_top_picks_message`(在 notifier_format)**讀**。

**選項 A**:cache 留在 `notifier.py`,pick_pipeline / notifier_format 都 `from src import notifier` 再 `notifier._LAST_CONSENSUS = ...` 賦值。可行,但會有循環 import 風險(format_pick_block 跟 notifier 的 import 順序)。

**選項 B**(推薦):**抽到 `pick_pipeline.py`**,format module `from src.pick_pipeline import _LAST_*`。
- notifier 再從 pick_pipeline re-export 給 wire test。
- 寫 vs 讀都在同一個 module 內,語意更乾淨。
- wire test `hasattr(notifier, "_LAST_CONSENSUS")` 仍能 work,因為 notifier re-export 後 `notifier._LAST_CONSENSUS` 是 attribute access — Python 會去 globals() 查到 re-exported 符號。
  **但**:`notifier._LAST_CONSENSUS = X` 賦值會寫到 notifier globals,不會同步 pick_pipeline globals。如果有 caller 走 setattr → 拆後語意斷掉。需確認**沒人這樣用**(grep 沒看到 caller 賦值,只看到 `notifier.foo` 讀 + `monkeypatch.setattr(notifier, ...)`)。
- monkeypatch.setattr 不會出問題,因為它走 setattr 路徑,wire test 只用 `hasattr`,不用 `setattr`。

### Pre-flight checklist(拆之前驗證)

- [ ] `grep -rn "notifier\._LAST_\|notifier\.[a-z_]*pick" src/ tests/ scripts/ app.py` 確認 caller 都是讀,不是寫。
- [ ] `grep -rn "monkeypatch.setattr(notifier" tests/` 看 monkeypatch 鎖在哪些符號。
- [ ] 跑 `pytest tests/test_*_wire.py -q` 開拆前綠 baseline。

### 拆完驗證

- [ ] `pytest tests/test_*_wire.py -q` 全綠(13+ wire test file)
- [ ] `pytest tests/test_notifier*.py -q` 全綠
- [ ] `pytest tests/test_e2e_smoke.py -q` 158 passed

### 估計風險 / 時間

- **時間**:2-3 小時(含 wire test 全套驗證)
- **風險**:中高
  - 主要風險:wire test 看 `inspect.getsource()` 的 substring,移函式後檔位變,但 substring 還在 → 通常 OK
  - 次要風險:`_LAST_*` re-export 語意斷裂(setattr 在 notifier globals,讀的人從 pick_pipeline globals 拿) → 上面 §選項 B 已分析

## 結論

**這輪 skip,進 backlog**。理由:不是不拆,是這次任務 budget 不夠做完整驗證,
而 notifier.py 是日推主 pipeline,炸到主公推播會被嫌。

下次有空寫專屬 PR(`refactor(notifier): split pick_pipeline + notifier_format`),
跟著上面 plan 做就 OK。
