"""大盤環境感知:讀 TAIEX daily_prices 算 close vs MA20/MA60 → 4 種 regime。

regime 分類:
  bull       多頭   close > MA20 > MA60       全策略開
  weak_bull  弱多頭 close > MA20, < MA60      偏向反轉/基本面/殖利率/動能
  sideways   盤整   close < MA20, > MA60      偏向反轉/籌碼
  bear       空頭   close < MA20 < MA60       偏向籌碼/殖利率/獨立行情

公式直接用日線 close + 20/60 日 SMA;TAIEX 在 daily_prices 表 stock_id='TAIEX'。
資料不足(< 60 天 TAIEX history)→ 回 'unknown' regime,App 端走「全策略」 fallback。
"""
from __future__ import annotations

from datetime import date as _date
from typing import TypedDict

from src import database as db


class RegimeInfo(TypedDict):
    regime: str          # 'bull' / 'weak_bull' / 'sideways' / 'bear' / 'unknown'
    label: str           # 中文「多頭」「弱多頭」「盤整」「空頭」「未知」
    badge_emoji: str     # "📈" / "⚠️" / "🟡" / "🔴" / "❔"
    close: float | None
    ma20: float | None
    ma60: float | None
    target_date: str | None


_REGIME_LABELS: dict[str, tuple[str, str]] = {
    "bull":      ("多頭",   "📈"),
    "weak_bull": ("弱多頭", "⚠️"),
    "sideways":  ("盤整",   "🟡"),
    "bear":      ("空頭",   "🔴"),
    "unknown":   ("未知",   "❔"),
}


def _classify(close: float, ma20: float, ma60: float) -> str:
    """純邏輯:close vs MA20 / MA60 4 象限。"""
    above20 = close > ma20
    above60 = close > ma60
    if above20 and above60:
        return "bull"
    if above20 and not above60:
        return "weak_bull"
    if not above20 and above60:
        return "sideways"
    return "bear"


def compute_regime(
    target_date: str | None = None,
    db_path: str | None = None,
) -> RegimeInfo:
    """讀 TAIEX 算當前 regime。target_date None → 取 SQLite 最新日期。

    資料不足 60 天 → regime='unknown'(close/ma20/ma60 = None)。
    """
    with db.get_conn(db_path) as conn:
        if target_date is None:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM daily_prices "
                "WHERE stock_id='TAIEX'"
            ).fetchone()
            if not row or not row["d"]:
                return _empty_regime(None)
            target_date = row["d"]

        rows = conn.execute(
            "SELECT date, close FROM daily_prices "
            "WHERE stock_id='TAIEX' AND date <= ? "
            "ORDER BY date DESC LIMIT 60",
            (target_date,),
        ).fetchall()

    if len(rows) < 60:
        # 至少要 60 天才能算 MA60
        return _empty_regime(target_date)

    closes = [float(r["close"]) for r in rows if r["close"] is not None]
    if len(closes) < 60:
        return _empty_regime(target_date)

    close = closes[0]
    ma20 = sum(closes[:20]) / 20
    ma60 = sum(closes[:60]) / 60
    regime = _classify(close, ma20, ma60)
    label, emoji = _REGIME_LABELS[regime]
    return RegimeInfo(
        regime=regime, label=label, badge_emoji=emoji,
        close=close, ma20=ma20, ma60=ma60, target_date=target_date,
    )


def _empty_regime(target_date: str | None) -> RegimeInfo:
    label, emoji = _REGIME_LABELS["unknown"]
    return RegimeInfo(
        regime="unknown", label=label, badge_emoji=emoji,
        close=None, ma20=None, ma60=None, target_date=target_date,
    )


def filter_strategies_by_regime(
    strategy_keys: list[str],
    regime: str,
    strategy_category: dict[str, str],
    regime_filter: dict[str, set[str] | None],
) -> list[str]:
    """根據 regime 過濾 strategy_keys。

    - regime in regime_filter 且對應值 = None → 全開(回原 keys)
    - regime 對應一個 set[str] (categories)→ 留 category 在 set 內的 strategies
    - regime 不在 regime_filter(unknown 等)→ 全開保守
    """
    cats = regime_filter.get(regime)
    if cats is None:
        return list(strategy_keys)
    return [
        k for k in strategy_keys
        if strategy_category.get(k) in cats
    ]


__all__ = [
    "RegimeInfo",
    "compute_regime",
    "filter_strategies_by_regime",
]
