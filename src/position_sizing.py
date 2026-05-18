"""部位管理 / Kelly 倉位建議模組。

設計原則:
- 「軍師」式建議,不自動下單,只算出建議部位 % + 股數 + 推理。
- 半 Kelly / 四分之一 Kelly 預設(`kelly_multiplier=0.25`)— Kelly 公式
  full bet 在實務上太激進(估錯參數就翻車),分數 Kelly 是業界共識。
- ml_prob 視為 win_rate proxy(在 calibration_wire 後可信);若沒給就退回
  最近 30 天 pick_outcomes 的歷史 win_rate。
- 單檔絕對上限 `max_single_pct`(預設 20%),避免重押。
- 結果負 / 0 → 視為「不建議進場」,suggest_position_size 回 fraction=0。

Kill-switch:env `POSITION_SIZING_ENABLED=true`(預設 on)。off 時 notifier
推播會跳過部位建議行,callers 應該檢查 `is_enabled()`。

外部 API:
- `kelly_fraction(win_rate, win_loss_ratio, kelly_multiplier=0.25) -> float`
- `suggest_position_size(sid, ml_prob, confidence, total_capital,
                         max_single_pct=0.20, db_path=None) -> dict`
- `is_enabled() -> bool`
- `get_recent_win_stats(days=30, db_path=None) -> dict` — 從 pick_outcomes 撈
  最近 N 天的 win_rate + win/loss ratio

回傳格式(suggest_position_size):
    {
        "sid": "2330",
        "ml_prob": 0.64,
        "win_rate": 0.64,
        "win_loss_ratio": 1.5,
        "kelly_raw": 0.40,            # 純 Kelly fraction
        "kelly_adjusted": 0.10,       # × kelly_multiplier 後
        "position_pct": 0.10,         # min(kelly_adjusted, max_single_pct)
        "capped_by": "kelly",         # "kelly" or "max_single"
        "suggested_amount": 100000,   # position_pct × total_capital
        "current_price": 590.0,       # 從 daily_prices 撈最新
        "suggested_shares": 169,      # 整股(TW 一張 1000 股,這裡回「股」)
        "suggested_lots": 0,          # 整張(TW 1 張 = 1000 股)
        "kelly_multiplier": 0.25,
        "rationale": "ML 64% → Kelly 40% → 1/4 Kelly = 10%(< 上限 20%)",
    }
"""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path

from src import database as db

logger = logging.getLogger(__name__)


_DEFAULT_KELLY_MULTIPLIER = 0.25
_DEFAULT_WIN_LOSS_RATIO = 1.5  # 主公拍板:沒歷史資料時的 fallback(2:1 風報比的保守版)
_DEFAULT_MAX_SINGLE_PCT = 0.20
_TWSE_SHARES_PER_LOT = 1000


def is_enabled() -> bool:
    """讀 env POSITION_SIZING_ENABLED(預設 true)。

    runtime 讀(不在 import time 鎖死)讓測試 monkeypatch.setenv 即時生效。
    """
    raw = os.getenv("POSITION_SIZING_ENABLED", "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def kelly_fraction(
    win_rate: float,
    win_loss_ratio: float,
    kelly_multiplier: float = _DEFAULT_KELLY_MULTIPLIER,
) -> float:
    """Kelly Criterion fraction(× kelly_multiplier)。

    Kelly:
        f* = (b·p − q) / b
            = p − (1 − p) / b
        其中 p = win_rate, q = 1 − p, b = win_loss_ratio(贏一倍 / 輸一倍)

    Args:
        win_rate: [0, 1],預期勝率。
        win_loss_ratio: > 0,平均贏 / 平均輸的倍率(例如 +6% / -3% = 2.0)。
        kelly_multiplier: 通常 0.25(quarter Kelly)或 0.5(half Kelly)。
            預設 0.25 — 對 win_rate / win_loss_ratio 估計誤差較有韌性。

    回傳:
        fraction ∈ [0, 1]。負值(劣勢)→ 回 0。> 1 → clamp 到 1(不可能,但安全)。
    """
    if not (0.0 <= win_rate <= 1.0):
        raise ValueError(f"win_rate 必須 ∈ [0,1],got {win_rate}")
    if win_loss_ratio <= 0.0:
        raise ValueError(f"win_loss_ratio 必須 > 0,got {win_loss_ratio}")
    if kelly_multiplier <= 0.0:
        raise ValueError(f"kelly_multiplier 必須 > 0,got {kelly_multiplier}")

    p = float(win_rate)
    q = 1.0 - p
    b = float(win_loss_ratio)
    f_raw = p - q / b
    if f_raw <= 0.0:
        return 0.0
    adjusted = f_raw * float(kelly_multiplier)
    return max(0.0, min(1.0, adjusted))


def get_recent_win_stats(
    days: int = 30,
    db_path: str | Path | None = None,
) -> dict:
    """從 pick_outcomes 最近 N 天統計 win_rate + win_loss_ratio。

    定義(對齊 backtest.simulate_outcome 口徑):
    - win_rate = mean(hit_target)(d1~d10 命中 +3% 視為「贏」)
    - win_loss_ratio = mean(return_d5 | return_d5 > 0) / |mean(return_d5 | return_d5 < 0)|
      若沒有負報酬樣本 → fallback `_DEFAULT_WIN_LOSS_RATIO`

    Args:
        days: lookback,預設 30 天。
        db_path: 給測試 monkeypatch 用。

    回傳:
        {"n": int, "win_rate": float, "win_loss_ratio": float,
         "since": "YYYY-MM-DD" or "", "is_fallback": bool}

    no rows → fallback {win_rate=0.5, win_loss_ratio=1.5, is_fallback=True}
    """
    from datetime import date, timedelta
    db.init_db(db_path)
    since = (date.today() - timedelta(days=int(days))).isoformat()
    try:
        with db.get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT hit_target, return_d5 FROM pick_outcomes "
                "WHERE pick_date >= ? AND hit_target IS NOT NULL",
                (since,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        logger.exception("[POSITION_SIZING] get_recent_win_stats SQL 失敗,fallback")
        rows = []

    if not rows:
        return {
            "n": 0,
            "win_rate": 0.5,
            "win_loss_ratio": _DEFAULT_WIN_LOSS_RATIO,
            "since": since,
            "is_fallback": True,
        }

    n = len(rows)
    n_wins = sum(1 for r in rows if r["hit_target"] and float(r["hit_target"]) > 0)
    win_rate = n_wins / n if n > 0 else 0.5

    wins = [
        float(r["return_d5"]) for r in rows
        if r["return_d5"] is not None and float(r["return_d5"]) > 0
    ]
    losses = [
        abs(float(r["return_d5"])) for r in rows
        if r["return_d5"] is not None and float(r["return_d5"]) < 0
    ]
    if wins and losses:
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        wlr = avg_win / avg_loss if avg_loss > 0 else _DEFAULT_WIN_LOSS_RATIO
    else:
        wlr = _DEFAULT_WIN_LOSS_RATIO

    return {
        "n": n,
        "win_rate": float(win_rate),
        "win_loss_ratio": float(wlr),
        "since": since,
        "is_fallback": False,
    }


def _latest_close(sid: str, db_path: str | Path | None = None) -> float | None:
    """撈該 sid 在 daily_prices 最新一筆 close,沒有 → None。"""
    try:
        with db.get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT close FROM daily_prices WHERE stock_id=? "
                "ORDER BY date DESC LIMIT 1",
                (str(sid).strip(),),
            ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row or row["close"] is None:
        return None
    try:
        return float(row["close"])
    except (TypeError, ValueError):
        return None


def suggest_position_size(
    sid: str,
    ml_prob: float | None,
    confidence: str | None = None,
    total_capital: float = 1_000_000.0,
    max_single_pct: float = _DEFAULT_MAX_SINGLE_PCT,
    kelly_multiplier: float = _DEFAULT_KELLY_MULTIPLIER,
    win_loss_ratio: float | None = None,
    db_path: str | Path | None = None,
) -> dict:
    """對單 sid 算建議部位大小。

    流程:
      1. 取 win_rate:有 ml_prob 用之(視為 calibrated 機率);沒則用最近 30 天
         pick_outcomes 的 win_rate。
      2. 取 win_loss_ratio:caller 給就用,沒給用 get_recent_win_stats 的歷史
         (再沒 fallback 1.5)。
      3. Kelly fraction × kelly_multiplier(預設 0.25)。
      4. 上限 max_single_pct(預設 20%)。
      5. confidence='weak' → 額外打 0.5 折(降級訊號半倉)。
      6. 撈 daily_prices 最新 close,算建議股數(整股)。

    回傳 dict(見模組 docstring)。kelly_raw=0 → position_pct=0, shares=0。
    """
    if total_capital <= 0:
        raise ValueError(f"total_capital 必須 > 0,got {total_capital}")
    if not (0.0 < max_single_pct <= 1.0):
        raise ValueError(f"max_single_pct 必須 ∈ (0,1],got {max_single_pct}")

    # === win_rate / win_loss_ratio 來源 ===
    stats = get_recent_win_stats(db_path=db_path)
    if ml_prob is not None:
        try:
            mlp = float(ml_prob)
        except (TypeError, ValueError):
            mlp = None
        else:
            if math.isnan(mlp) or mlp < 0.0 or mlp > 1.0:
                mlp = None
    else:
        mlp = None
    win_rate = mlp if mlp is not None else stats["win_rate"]
    wlr = float(win_loss_ratio) if win_loss_ratio is not None else stats["win_loss_ratio"]

    # === Kelly 計算 ===
    kelly_raw_full = kelly_fraction(win_rate, wlr, kelly_multiplier=1.0)
    kelly_adjusted = kelly_raw_full * float(kelly_multiplier)

    # 降級訊號(weak)— 半倉
    weak_discount = 0.5 if (confidence or "").lower() == "weak" else 1.0
    kelly_adjusted *= weak_discount

    # 上限
    if kelly_adjusted >= max_single_pct:
        position_pct = max_single_pct
        capped_by = "max_single"
    else:
        position_pct = kelly_adjusted
        capped_by = "kelly"
    if position_pct < 0.0:
        position_pct = 0.0

    suggested_amount = position_pct * float(total_capital)

    # 撈 close → 股數 / 張數
    current_price = _latest_close(sid, db_path=db_path)
    suggested_shares = 0
    suggested_lots = 0
    if current_price and current_price > 0 and suggested_amount > 0:
        suggested_shares = int(suggested_amount // current_price)
        suggested_lots = suggested_shares // _TWSE_SHARES_PER_LOT

    # rationale 字串(中文,給 notifier / UI 顯示)
    if kelly_raw_full <= 0:
        rationale = (
            f"ML/勝率 {win_rate*100:.0f}% × R:R {wlr:.1f} → "
            f"Kelly ≤ 0,軍師建議:不進場"
        )
    elif capped_by == "max_single":
        rationale = (
            f"ML/勝率 {win_rate*100:.0f}% → Kelly {kelly_raw_full*100:.0f}% "
            f"× 1/{int(1/kelly_multiplier)} = {kelly_adjusted*100:.1f}%,"
            f"碰單檔上限 {int(max_single_pct*100)}%"
        )
    else:
        rationale = (
            f"ML/勝率 {win_rate*100:.0f}% → Kelly {kelly_raw_full*100:.0f}% "
            f"× 1/{int(1/kelly_multiplier)} = {position_pct*100:.1f}%"
            f"(< 上限 {int(max_single_pct*100)}%)"
        )

    return {
        "sid": str(sid).strip(),
        "ml_prob": mlp,
        "win_rate": float(win_rate),
        "win_loss_ratio": float(wlr),
        "kelly_raw": float(kelly_raw_full),
        "kelly_adjusted": float(kelly_adjusted),
        "position_pct": float(position_pct),
        "capped_by": capped_by,
        "suggested_amount": float(suggested_amount),
        "current_price": current_price,
        "suggested_shares": int(suggested_shares),
        "suggested_lots": int(suggested_lots),
        "kelly_multiplier": float(kelly_multiplier),
        "max_single_pct": float(max_single_pct),
        "confidence": confidence,
        "rationale": rationale,
        "stats_n": int(stats["n"]),
        "stats_is_fallback": bool(stats["is_fallback"]),
    }


# ---------------------------------------------------------------------------
# EV-based 半 Kelly 倉位(P2-8 加;對應 Phase 1 score_to_ev mapping)
#
# 跟上面的 Kelly fraction 不同:這支不需要 win_rate / win_loss_ratio,而是
# 直接拿 `score_to_ev` 翻譯出來的 EV(期望報酬 fraction)做分段線性 sizing。
# 設計理由:主公拿到 pick 時 EV 已校準成「進場期望賺/賠 X%」,直接 map 到
# 「倉位 N%」對主公語意最直白(EV 高 → 多投,EV 微正 → 試水溫,EV 負 → 不進)。
#
# 分段表(全 portfolio 5% 上限,符合「半 Kelly」實務 — 一檔不到 5%):
#   EV > +3%        → 5%   (滿倉 — 訊號最強)
#   +1% ≤ EV ≤ +3%  → 2~5% (線性內插)
#   0   ≤ EV < +1%  → 1~2% (小試水溫)
#   EV < 0          → 0%   (不該進場)
# ---------------------------------------------------------------------------

_EV_FULL_CAP: float = 0.05         # 倉位上限 5%(滿倉)
_EV_MID_HI: float = 0.02           # 中段倉位上限 2%
_EV_LOW_LO: float = 0.01           # 試水溫倉位下限 1%
_EV_TIER_HI: float = 0.03          # EV > 3% → 滿倉
_EV_TIER_MID: float = 0.01         # EV > 1% → 中高段
_EV_TIER_LO: float = 0.0           # EV ≥ 0 → 至少試水溫


def compute_suggested_position(ev: float | None) -> float:
    """EV-based 半 Kelly 倉位建議。

    Args:
        ev: 期望報酬 fraction(0.023 = +2.3%);None / NaN → 回 0.0。

    Returns:
        建議倉位 fraction ∈ [0.0, 0.05]。

    分段公式(連續):
        ev < 0:          0.0
        0 ≤ ev < 1%:     線性 1% → 2%
        1% ≤ ev ≤ 3%:    線性 2% → 5%
        ev > 3%:         5%(滿倉)

    `score_to_ev` 在沒 calibration 時走線性 fallback(score×5% - (1-score)×3%),
    所以即便 mapping CSV 缺失也能穩定產出非零 EV(score>0.375 起)。
    """
    if ev is None:
        return 0.0
    try:
        e = float(ev)
    except (TypeError, ValueError):
        return 0.0
    if e != e:  # NaN
        return 0.0

    if e < _EV_TIER_LO:
        return 0.0
    if e >= _EV_TIER_HI:
        return _EV_FULL_CAP
    if e >= _EV_TIER_MID:
        # 線性 [1%, 3%] EV → [2%, 5%] position
        ratio = (e - _EV_TIER_MID) / (_EV_TIER_HI - _EV_TIER_MID)
        return _EV_MID_HI + ratio * (_EV_FULL_CAP - _EV_MID_HI)
    # 0 ≤ e < 1%:線性 [0, 1%] → [1%, 2%]
    ratio = (e - _EV_TIER_LO) / (_EV_TIER_MID - _EV_TIER_LO)
    return _EV_LOW_LO + ratio * (_EV_MID_HI - _EV_LOW_LO)


def render_position_str(pos: float | None) -> str:
    """渲染倉位 fraction 成顯示字串。

    Examples:
        render_position_str(0.035) → '建議倉位 3.5%'
        render_position_str(0.05)  → '建議倉位 5.0%'
        render_position_str(0.0)   → '建議倉位 0%'(不進場)
        render_position_str(None)  → '建議倉位 —'
    """
    if pos is None:
        return "建議倉位 —"
    try:
        p = float(pos)
    except (TypeError, ValueError):
        return "建議倉位 —"
    if p != p:  # NaN
        return "建議倉位 —"
    if p <= 0.0:
        return "建議倉位 0%"
    return f"建議倉位 {p * 100:.1f}%"


__all__ = [
    "is_enabled",
    "kelly_fraction",
    "get_recent_win_stats",
    "suggest_position_size",
    "compute_suggested_position",
    "render_position_str",
]
