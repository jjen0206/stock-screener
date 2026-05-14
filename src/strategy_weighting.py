"""動態策略權重 — 根據 pick_outcomes 近 30 天命中率算 weight。

設計初衷:主公拍板「過去命中高的策略應該排前面，命中低的排後」。
讓 _compute_pick_score 的 ml_prob 排序乘 weight 倍率,把策略歷史表現
反映到當日推播順序。

公式(主公定案):
  - 撈該 strategy 近 30 天 pick_outcomes
  - WR = SUM(hit_target) / N(用既有 hit_target 0/1 binary）
  - weight = clip(WR / 0.5, MIN_W, MAX_W)  ← 命中率 50% = 基準 1.0
  - N < MIN_N → weight = 1.0(資料不足保守用基準)

clamp 上下限避免極端,讓任何單一策略不至於把 ml_prob 排序完全蓋過。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta


# === Constants (主公拍板,寫死) ===

MIN_N = 30          # 樣本不足門檻:N < 30 → 用 1.0 基準
LOOKBACK_DAYS = 30  # 撈近 30 天 pick_outcomes
BASELINE_WR = 0.5   # WR 50% = 基準 weight 1.0
MIN_WEIGHT = 0.5    # weight clamp 下限(極差策略)
MAX_WEIGHT = 1.5    # weight clamp 上限(極佳策略)
DEFAULT_WEIGHT = 1.0


def get_strategy_weights_30d(
    conn: sqlite3.Connection,
    today_iso: str | None = None,
) -> dict[str, float]:
    """回傳 dict[strategy_key, weight]。

    撈 pick_outcomes 近 30 天(by pick_date)所有策略 fire,算 hit_target
    平均當作 WR,套公式生 weight。

    today_iso:`YYYY-MM-DD` — None 取系統今天。讓 test 可注入固定日期。
    回 {} 表 pick_outcomes 完全空(系統還沒跑過任何 backtest)。

    caller 拿 dict 後對 missing key 預設 DEFAULT_WEIGHT(1.0),確保
    沒在 dict 內的新策略不會被當 0 倍懲罰。
    """
    if today_iso is None:
        today_iso = date.today().isoformat()
    since = (
        date.fromisoformat(today_iso[:10]) - timedelta(days=LOOKBACK_DAYS)
    ).isoformat()

    rows = conn.execute(
        """
        SELECT strategy,
               COUNT(*) AS n,
               AVG(hit_target) AS wr
        FROM pick_outcomes
        WHERE pick_date >= ? AND hit_target IS NOT NULL
        GROUP BY strategy
        """,
        (since,),
    ).fetchall()

    weights: dict[str, float] = {}
    for r in rows:
        # sqlite3.Row 跟 tuple 都支援 by-index;by-name 走 row[col] 對 Row 有效
        if hasattr(r, "keys"):
            strategy = r["strategy"]
            n = r["n"]
            wr = r["wr"]
        else:
            strategy, n, wr = r[0], r[1], r[2]
        if n is None or n < MIN_N or wr is None:
            weights[strategy] = DEFAULT_WEIGHT
            continue
        raw = wr / BASELINE_WR
        weights[strategy] = max(MIN_WEIGHT, min(MAX_WEIGHT, raw))
    return weights


def get_strategy_weight_details(
    conn: sqlite3.Connection,
    today_iso: str | None = None,
) -> list[dict]:
    """回傳給 Streamlit「📋 系統結論」頁顯示用的明細 list。

    each row = {strategy, n, wr, weight, verdict},依 weight desc 排序。
    跟 get_strategy_weights_30d 同一份 query,只是格式為人類可讀的 dict。
    """
    if today_iso is None:
        today_iso = date.today().isoformat()
    since = (
        date.fromisoformat(today_iso[:10]) - timedelta(days=LOOKBACK_DAYS)
    ).isoformat()

    rows = conn.execute(
        """
        SELECT strategy,
               COUNT(*) AS n,
               AVG(hit_target) AS wr
        FROM pick_outcomes
        WHERE pick_date >= ? AND hit_target IS NOT NULL
        GROUP BY strategy
        ORDER BY strategy
        """,
        (since,),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        if hasattr(r, "keys"):
            strategy = r["strategy"]
            n = r["n"]
            wr = r["wr"]
        else:
            strategy, n, wr = r[0], r[1], r[2]
        if n is None or n < MIN_N or wr is None:
            weight = DEFAULT_WEIGHT
            verdict = "🌱 樣本不足"
        else:
            raw = wr / BASELINE_WR
            weight = max(MIN_WEIGHT, min(MAX_WEIGHT, raw))
            if weight >= 1.2:
                verdict = "🔥 加權"
            elif weight <= 0.8:
                verdict = "🥶 降權"
            else:
                verdict = "— 中性"
        out.append({
            "strategy": strategy,
            "n": int(n) if n is not None else 0,
            "wr": float(wr) if wr is not None else None,
            "weight": float(weight),
            "verdict": verdict,
        })
    out.sort(key=lambda x: -x["weight"])
    return out
