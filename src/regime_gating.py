"""大盤 regime gating — 空頭時自動縮短線推薦量 / 拉高 confidence threshold。

歷史 backtest 顯示大盤空頭時所有策略 hit rate 一起掉 — 不分種類。本模組根據
TAIEX 5MA / 20MA / 60MA + 60MA 斜率 判 3-tier regime,給 notifier 動態縮減
推薦數量 + 拉高 ML threshold uplift,避免空頭環境硬推。

3-tier regime:
  - bull   多頭   5MA > 20MA > 60MA 且 60MA 斜率向上
  - range  盤整   兩條均線交錯或 60MA 斜率平(含 correction 多頭中短期跌破)
  - bear   空頭   5MA < 20MA < 60MA 且 60MA 斜率向下

對照 src.market_regime(4-tier bull/weak_bull/sideways/bear)— 本模組獨立判
斷因為 gating 需要看 5MA + 60MA 斜率,跟既有「close vs MA20/MA60」邏輯不同。

Kill-switch:env REGIME_GATING_ENABLED=false 關掉 gating(notifier 走原 top_n
+ 基本 threshold,不縮也不拉門檻)。預設 true。
"""
from __future__ import annotations

import os
import sqlite3
from typing import TypedDict


# 各 regime 對應的 gating 參數(主公拍板 2026-05-15)
# short_pick_max_count: 多頭 10 / 盤整 5 / 空頭 2 — 空頭時硬縮 2 檔避免亂推
# long_pick_max_count : 多頭 10 / 盤整 7 / 空頭 5 — 長線抗跌,縮幅較小
# confidence_threshold_uplift: 加到 base ML threshold,空頭只放最有信心的
_REGIME_PARAMS: dict[str, dict] = {
    "bull": {
        "short_pick_max_count": 10,
        "long_pick_max_count": 10,
        "confidence_threshold_uplift": 0.0,
        "caption": "📈 大盤多頭",
    },
    "range": {
        "short_pick_max_count": 5,
        "long_pick_max_count": 7,
        "confidence_threshold_uplift": 0.05,
        "caption": "📊 大盤盤整",
    },
    "bear": {
        "short_pick_max_count": 2,
        "long_pick_max_count": 5,
        "confidence_threshold_uplift": 0.15,
        "caption": "📉 大盤空頭",
    },
}

# 空頭警語(caption 後額外加)— 主公拍板 2026-05-15
_BEAR_WARNING = (
    "⚠️ 大盤趨勢偏空,系統已縮減推薦數量並拉高信心門檻,請主公保守操作"
)

# 資料不足 fallback 走 range(中性 — 不該因為 DB 缺值就盲目壓 bear 或放 bull)
_FALLBACK_REGIME = "range"

# 60MA 斜率閾值:用「60MA 今天 vs 60MA 20 天前」變化率
# 變化率 > +0.5%  → 向上
# 變化率 < -0.5%  → 向下
# 介於兩者 → 平(歸 range)
_SLOPE_UP_THRESHOLD = 0.005
_SLOPE_DOWN_THRESHOLD = -0.005


class RegimeGatingParams(TypedDict):
    regime: str
    short_pick_max_count: int
    long_pick_max_count: int
    confidence_threshold_uplift: float
    caption: str


def is_enabled() -> bool:
    """讀 env REGIME_GATING_ENABLED;預設 true。

    任何非「false / 0 / no」字串都當 true(避免 typo 意外關 gating)。
    """
    raw = os.getenv("REGIME_GATING_ENABLED", "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def _classify_regime(
    ma5: float, ma20: float, ma60: float, ma60_prev: float,
) -> str:
    """純邏輯分類 3-tier regime。

    ma60_prev = 20 個交易日前的 60MA(用來算斜率變化率)。

    - bull:5MA > 20MA > 60MA 且 60MA 斜率向上(20d 變化率 > +0.5%)
    - bear:5MA < 20MA < 60MA 且 60MA 斜率向下(20d 變化率 < -0.5%)
    - range(其餘):任一條件不滿足(交錯 / 斜率平 / correction 等)
    """
    # 斜率變化率:今天 60MA vs 20 天前 60MA
    if ma60_prev > 0:
        slope_pct = (ma60 - ma60_prev) / ma60_prev
    else:
        slope_pct = 0.0

    if ma5 > ma20 > ma60 and slope_pct > _SLOPE_UP_THRESHOLD:
        return "bull"
    if ma5 < ma20 < ma60 and slope_pct < _SLOPE_DOWN_THRESHOLD:
        return "bear"
    return "range"


def _fetch_taiex_closes(
    conn: sqlite3.Connection, as_of: str | None, min_rows: int = 80,
) -> list[float]:
    """讀 TAIEX 最近 N 筆 close;由舊到新排序。

    min_rows = 80 → 算 60MA + 20d 前 60MA 至少要 80 天(60 + 20)。
    不足 → 回空 list,caller 走 fallback regime。
    """
    if as_of is None:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM daily_prices WHERE stock_id='TAIEX'"
        ).fetchone()
        if not row or not row["d"]:
            return []
        as_of = row["d"]

    rows = conn.execute(
        "SELECT date, close FROM daily_prices "
        "WHERE stock_id='TAIEX' AND date <= ? AND close IS NOT NULL "
        "ORDER BY date DESC LIMIT ?",
        (as_of, min_rows),
    ).fetchall()

    if len(rows) < min_rows:
        return []

    # rows 是 date DESC,反轉成 ASC(由舊到新)讓 caller 算 MA / 斜率方便
    closes = [float(r["close"]) for r in reversed(rows)]
    return closes


def get_regime_gating_params(
    conn: sqlite3.Connection, as_of: str | None = None,
) -> RegimeGatingParams:
    """讀 TAIEX → 算 5MA/20MA/60MA + 60MA 斜率 → 回 gating params。

    參數:
      conn: 已開啟的 SQLite connection(caller 負責 close)
      as_of: ISO date 字串,None = 取 TAIEX 最新一天
    回:
      RegimeGatingParams dict — 含 regime / max counts / threshold uplift / caption

    Kill-switch:env REGIME_GATING_ENABLED=false 時 → 永遠回 bull params
      (等於不做 gating,notifier 走原 top_n + base threshold)。

    資料不足(< 80 天 TAIEX history)→ 走 fallback regime='range'。
    """
    if not is_enabled():
        # kill-switch off → 等同 bull(不縮、不拉 threshold)
        return _build_params("bull")

    closes = _fetch_taiex_closes(conn, as_of=as_of)
    if not closes:
        return _build_params(_FALLBACK_REGIME)

    # closes 由舊到新 — 用最末 5/20/60 天算 MA;ma60_prev 取 20 天前的 60MA
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    # 60MA 20 天前 = closes[-80:-20] 的平均
    ma60_prev = sum(closes[-80:-20]) / 60

    regime = _classify_regime(ma5, ma20, ma60, ma60_prev)
    return _build_params(regime)


def _build_params(regime: str) -> RegimeGatingParams:
    """根據 regime 組 RegimeGatingParams;bear 時 caption 加警語。"""
    spec = _REGIME_PARAMS[regime]
    caption = spec["caption"]
    if regime == "bear":
        caption = f"{caption}\n{_BEAR_WARNING}"
    return RegimeGatingParams(
        regime=regime,
        short_pick_max_count=spec["short_pick_max_count"],
        long_pick_max_count=spec["long_pick_max_count"],
        confidence_threshold_uplift=spec["confidence_threshold_uplift"],
        caption=caption,
    )


__all__ = [
    "RegimeGatingParams",
    "get_regime_gating_params",
    "is_enabled",
]
