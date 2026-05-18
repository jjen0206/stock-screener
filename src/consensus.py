"""跨策略共識(strategy consensus)— 同一檔被多策略同時看見的訊號加成。

歷史背景:
    短線 picks 由 17 套策略各自跑 → `_select_top_picks` 統整成 top N。
    但「同一檔被多策略同時看見」其實是強訊號 — 經驗 precision +10~15%。
    這個模組把這個資訊抽出來,讓 _compute_pick_score 乘上 multiplier,
    UI 也能用 badge 標出共識股。

設計重點:
    1. 同類策略共識(例如兩個趨勢類)權重小於跨類別共識
       — 「兩種視角同時看到」 > 「同一視角的兩個版本看到」。
    2. 一檔被同一策略命中多次(不會發生於目前 strategies.py,但為防守)
       不重複算票。
    3. 沒落在分類表的策略 fallback 進 "未分類" 一類(避免 KeyError 噴掉
       整條 pipeline);同樣不會多算跨類別共識。

公開 API:
    - compute_strategy_consensus(picks_by_strategy) -> dict[sid → meta]
    - consensus_multiplier(meta) -> float
    - consensus_badge(meta) -> tuple[str, str]  # (badge_text, tier_label)
    - has_signal_conflict(strategy_keys) -> bool
    - STRATEGY_CATEGORIES: dict[str, str]
    - STRATEGY_NATURE: dict[str, str]  # reversal / trend / neutral
    - STRATEGY_CONSENSUS_ENABLED: bool kill switch
    - SIGNAL_CONFLICT_PENALTY_ENABLED: bool kill switch
"""
from __future__ import annotations

import os
from typing import Iterable, Mapping


# 跟 src/strategies.py::STRATEGY_CATEGORY 等價 — 寫一份在這裡是因為:
#   (a) 模組獨立,test 不需要 import strategies.py(它會連 SQL / pandas)。
#   (b) 共識的「類別維度」可能跟 regime filter 的維度未來會分岔(例如把
#       「籌碼」再切成「外資 / 投信 / 千張戶」三類來算更細的 consensus),
#       這時這份要獨自演化,不該 reuse strategies.py 那份。
# 任何時候只要 strategies.py 那份有改,記得同步這裡。
STRATEGY_CATEGORIES: dict[str, str] = {
    # 趨勢類
    "ma_alignment": "趨勢",
    "macd_golden": "趨勢",
    "ma_squeeze_breakout": "趨勢",
    "bias_convergence": "趨勢",
    # 反轉類
    "rsi_recovery": "反轉",
    "bb_lower_rebound": "反轉",
    "inst_oversold_reversal": "反轉",
    # 籌碼類
    "inst_consensus": "籌碼",
    "inst_silent_accum": "籌碼",
    "big_holder_inflow": "籌碼",
    # 動能類
    "volume_kd": "動能",
    "volume_breakout": "動能",
    "gap_up": "動能",
    # 基本面
    "eps_acceleration": "基本面",
    "revenue_acceleration": "基本面",
    # 殖利率
    "high_yield_stable": "殖利率",
    # 大盤相對
    "taiex_alpha": "大盤",
}


# === 策略「性質」維度(獨立於 STRATEGY_CATEGORIES) ===
# reversal:均值回歸 / 超賣反彈 — 預期反方向修正
# trend   :動能突破 / 多頭排列 — 預期同方向延續
# neutral :籌碼 / 基本面 / 殖利率 / 事件 — 跟方向訊號正交,不會跟 reversal/trend 衝突
#
# 為什麼跟 STRATEGY_CATEGORIES(趨勢/反轉/籌碼/動能/...)分開:
#   - CATEGORIES 給 UI 顯示 + regime filter 用 — 切的是「投資哲學」維度(5+ 類)
#   - NATURE 給 conflict detection 用 — 切的是「訊號方向」維度(3 類)
#   - 例:volume_kd 在 CATEGORIES 是「動能」,但 KD 從超賣回升其實是反轉味道 →
#     拍不定 → 歸 neutral(保守,寧可不衝突)
#   - 例:bias_convergence 在 CATEGORIES 兩處不同步(consensus.py 標趨勢、
#     strategies.py 標反轉)— 不動既存欄位,NATURE 直接歸 reversal(語意正解)
STRATEGY_NATURE: dict[str, str] = {
    # reversal — 反轉 / 均值回歸 / 超賣反彈
    "rsi_recovery": "reversal",
    "bb_lower_rebound": "reversal",
    "bias_convergence": "reversal",
    "inst_oversold_reversal": "reversal",
    # trend — 動能突破 / 多頭排列 / 缺口延續
    "ma_alignment": "trend",
    "macd_golden": "trend",
    "ma_squeeze_breakout": "trend",
    "volume_breakout": "trend",
    "gap_up": "trend",
    # neutral — 籌碼 / 基本面 / 殖利率 / 事件 / 大盤 alpha / 模糊不定
    "volume_kd": "neutral",         # KD 從超賣回升 vs 趨勢延續,拍不定 → 保守
    "taiex_alpha": "neutral",       # 相對大盤 alpha,跟 reversal/trend 正交
    "inst_consensus": "neutral",
    "inst_silent_accum": "neutral",
    "big_holder_inflow": "neutral",
    "high_yield_stable": "neutral",
    "ex_dividend_swing": "neutral",
    "eps_acceleration": "neutral",
    "revenue_acceleration": "neutral",
    "revenue_announce_anticipation": "neutral",
}


def _kill_switch() -> bool:
    """讀 env var STRATEGY_CONSENSUS_ENABLED — 預設 on,設成 "false"/"0" 才關。

    每次呼叫都重讀(讓 test monkeypatch 立即生效)— overhead 微不足道。
    """
    raw = os.environ.get("STRATEGY_CONSENSUS_ENABLED", "true").strip().lower()
    return raw not in ("false", "0", "no", "off", "")


def _conflict_penalty_enabled() -> bool:
    """讀 env var SIGNAL_CONFLICT_PENALTY_ENABLED — 預設 on,設成 "false"/"0" 才關。

    給 ops 留逃生口 — 若哪天發現衝突檢誤殺真共識可即時關閉,不用 hot-patch code。
    """
    raw = os.environ.get(
        "SIGNAL_CONFLICT_PENALTY_ENABLED", "true",
    ).strip().lower()
    return raw not in ("false", "0", "no", "off", "")


# Module-level alias 給 monkeypatch / 外部 import 觀察狀態用。讀 _kill_switch()
# 才是真實判斷依據;這個值只在 import 時 snapshot 一次。
STRATEGY_CONSENSUS_ENABLED = _kill_switch()
SIGNAL_CONFLICT_PENALTY_ENABLED = _conflict_penalty_enabled()


def has_signal_conflict(strategy_keys: Iterable[str] | None) -> bool:
    """同一檔同時被 reversal 派 + trend 派策略命中 → 訊號衝突。

    語意:reversal 預期反方向修正、trend 預期同方向延續 — 兩者同時亮 = 反方向訊號
    加總 → 共識加成沒意義甚至有害,該扣掉 bonus。

    neutral(籌碼/基本面/事件)不算衝突 — 跟方向訊號正交,reversal+neutral 或
    trend+neutral 仍視為有效共識。

    沒在 STRATEGY_NATURE 的策略(未來新增還沒分類)→ 當 neutral 處理(保守)。
    Empty / None / 單一策略 → False(不可能衝突)。
    """
    if not strategy_keys:
        return False
    natures = {STRATEGY_NATURE.get(k, "neutral") for k in strategy_keys}
    return "reversal" in natures and "trend" in natures


def _normalize_picks(
    picks_by_strategy: Mapping[str, Iterable],
) -> dict[str, set[str]]:
    """把 input dict 攤平成 `dict[sid → set[strategy_name]]`。

    Input value 可以是 list[pick_dict]、list[str]、list[tuple] 都接;只要
    元素有 .get("sid") 或本身是 str / 第一個元素是 sid 就能解析。
    """
    sid_to_strats: dict[str, set[str]] = {}
    for strat, items in (picks_by_strategy or {}).items():
        if not strat or not items:
            continue
        for item in items:
            sid: str | None = None
            if isinstance(item, str):
                sid = item
            elif isinstance(item, Mapping):
                sid = item.get("sid") or item.get("stock_id")
            elif isinstance(item, (tuple, list)) and item:
                head = item[0]
                if isinstance(head, str):
                    sid = head
            if not sid:
                continue
            sid = str(sid)
            sid_to_strats.setdefault(sid, set()).add(strat)
    return sid_to_strats


def compute_strategy_consensus(
    picks_by_strategy: Mapping[str, Iterable] | None,
) -> dict[str, dict]:
    """統計每 sid 被幾個策略命中 + 跨幾個類別。

    Input:
        picks_by_strategy: `dict[strategy_name → list[sid | pick_dict]]`。
            一個 strategy 對同 sid 出現多次只算 1 票(set 去重)。

    Output:
        dict[sid → {
            "strategy_count": int,        # 命中策略數
            "strategies": list[str],      # sorted 策略 keys
            "categories": list[str],      # sorted 該批策略的類別
            "category_count": int,        # 跨幾個類別(2+ = 跨類別共識)
        }]

    沒在 STRATEGY_CATEGORIES 的策略 → 算 "未分類" 一類,跟其他類分開算
    跨類;同類間多策略仍算 1 個類別。

    Empty / None 輸入 → {}。
    """
    sid_to_strats = _normalize_picks(picks_by_strategy or {})
    out: dict[str, dict] = {}
    for sid, strats in sid_to_strats.items():
        strats_sorted = sorted(strats)
        cats = {STRATEGY_CATEGORIES.get(s, "未分類") for s in strats_sorted}
        out[sid] = {
            "strategy_count": len(strats_sorted),
            "strategies": strats_sorted,
            "categories": sorted(cats),
            "category_count": len(cats),
        }
    return out


def consensus_multiplier(meta: Mapping | None) -> float:
    """根據共識 meta 算 score multiplier。

    規則:
        - 單策略 (count=1)               → ×1.0
        - reversal × trend 衝突           → ×1.0(訊號方向矛盾,bonus 沒意義)
        - 同類別 2+ 票 (count≥2, cat=1)  → ×1.3
        - 跨類別 2 票                    → ×1.5
        - 跨類別 3+ 票                   → ×1.8
        - kill switch off                → ×1.0(不加成)

    Conflict check 走 STRATEGY_NATURE 維度,跟 STRATEGY_CATEGORIES 正交 —
    例:bias_convergence(NATURE=reversal) + volume_breakout(NATURE=trend)
    雖然 cat_count=2 但會被 conflict 攔下回 1.0。

    `meta` None / 缺欄 → 1.0(防守)。
    """
    if meta is None or not _kill_switch():
        return 1.0
    try:
        count = int(meta.get("strategy_count", 0))
        cat_count = int(meta.get("category_count", 0))
    except (TypeError, ValueError):
        return 1.0
    if count < 2:
        return 1.0
    # 反轉 × 趨勢衝突 → 不加成(衝突檢有自己 kill switch)
    if _conflict_penalty_enabled() and has_signal_conflict(
        meta.get("strategies") or [],
    ):
        return 1.0
    if cat_count >= 3:
        return 1.8
    if cat_count == 2:
        return 1.5
    return 1.3  # cat_count <= 1 但 count >= 2(同類別共識)


def consensus_badge(meta: Mapping | None) -> tuple[str, str]:
    """產生 UI badge 字串 + tier label。

    Tier(讓 caller 換不同 CSS / 排序用):
        "none"       → 單策略 / 共識關閉                  badge=""
        "same_cat"   → 同類 2+ 票                          badge="⭐"
        "cross_2"    → 跨類 2 票                           badge="⭐⭐ 共識"
        "cross_3"    → 跨類 3+ 票                          badge="⭐⭐⭐ 強共識"

    意圖:
        - 同類 2 票只給單顆星不加字 — 視覺上「順帶看到」,不該跟跨類同等。
        - 跨類 ≥2 加「共識」字眼,跨類 ≥3 加「強」字 — 手機窄屏一眼能看出。
    """
    if meta is None:
        return ("", "none")
    try:
        count = int(meta.get("strategy_count", 0))
        cat_count = int(meta.get("category_count", 0))
    except (TypeError, ValueError):
        return ("", "none")
    if count < 2 or not _kill_switch():
        return ("", "none")
    # 衝突訊號 → badge 跟 multiplier 同步消失,避免 UI/multiplier 不一致
    if _conflict_penalty_enabled() and has_signal_conflict(
        meta.get("strategies") or [],
    ):
        return ("", "none")
    if cat_count >= 3:
        return ("⭐⭐⭐ 強共識", "cross_3")
    if cat_count == 2:
        return ("⭐⭐ 共識", "cross_2")
    return ("⭐", "same_cat")


def summarize_consensus_counts(
    consensus_map: Mapping[str, Mapping] | None,
) -> dict[str, int]:
    """統計各 tier 各有幾檔 — 給推播訊息開頭 summary 用。

    Output 鍵:cross_3 / cross_2 / same_cat / none(單策略)。每個 tier 都有
    key(沒中也填 0)讓 caller 不用判斷 None。
    """
    out = {"cross_3": 0, "cross_2": 0, "same_cat": 0, "none": 0}
    if not consensus_map:
        return out
    for meta in consensus_map.values():
        _, tier = consensus_badge(meta)
        out[tier] = out.get(tier, 0) + 1
    return out
