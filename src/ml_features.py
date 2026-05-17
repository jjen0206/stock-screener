"""v4 進階特徵 — 籌碼變化率 / 多時間軸動能 / 產業相對強度。

對齊 `src/ml_predictor.py` v3 既有 5 個 features 的設計原則:
- 純函式（接 list[dict] / pd.Series / pd.DataFrame），不直接打 SQL，方便單測
- 任何缺資料 / 算術錯誤 → 各自 fallback 0.0（不 drop 整列 row）
- Helper signatures 對外 expose 給 ml_predictor.extract_features wire 用
- 不假設輸入排序，內部 sort/check

8 個新 features（v4 升 v3 五個的延伸）:

籌碼類（3）:
  - concentration_change_rate     千張大戶集中度週/月變化率
                                  (latest holders_pct - 4 週前) / max(0.5%, 4 週前)
                                  跟 holders_pct_change_4w 不同:用 max(0.005, base) 平滑
                                  防小分母爆值。
  - institutional_continuity      外資 + 投信「同向連續淨買 / 淨賣」天數（取 max）
                                  +N = 連 N 天淨買、-N = 連 N 天淨賣、0 = 不一致 / 缺資料
  - inst_divergence               外資 vs 投信背離指標
                                  near 1.0 = 一買一賣（風險）、0 = 同方向 / 任一缺資料

多時間軸（5）:
  - ma5_above_ma20_pct            近 60 日 5MA > 20MA 的天數占比（0-1）
  - ma20_above_ma60_pct           近 60 日 20MA > 60MA 占比（0-1）
  - momentum_5d / momentum_20d / momentum_60d
                                  各時間軸動能 = (close_now / close_N_ago - 1) × 100

產業相對強度（2）:
  - industry_relative_strength    該股 5 日漲幅 − 同產業所有股 5 日平均漲幅（% 點）
  - industry_rank_pct             該股在產業內 5 日漲幅 percentile（0-1）

任何缺資料 / 同產業 < 3 檔等邊界 → fallback 0.0；不 drop row。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)


# === 籌碼類 ===

def compute_concentration_change_rate(rows: list[dict]) -> float:
    """千張戶集中度變化率（latest vs 4 週前）。

    跟 holders_pct_change_4w 區別：
      - holders_pct_change_4w  = (latest - old) / old，old 很小時爆值
      - concentration_change_rate = (latest - old) / max(0.005, old)，平滑

    args:
      rows: 5 週 shareholder_concentration（asc 排序，index 0=4 週前、-1=latest）

    returns:
      float（typically -0.5 ~ +0.5）；資料不足 / latest|old NULL → 0.0
    """
    if len(rows) < 5:
        return 0.0
    latest = rows[-1].get("holders_pct")
    old = rows[0].get("holders_pct")
    if latest is None or old is None:
        return 0.0
    denom = max(0.005, float(old))  # 至少 0.5% 防小分母
    return (float(latest) - float(old)) / denom


def compute_institutional_continuity(inst_df: pd.DataFrame) -> float:
    """外資 + 投信「同向連續淨買 / 淨賣」最大天數（取 abs 較大 → 帶符號）。

    取近 10 天逐日掃,只要外資 + 投信淨買賣同向（同 sign 或同為 0 也算）
    就累加天數,計到方向變了為止。+N = 連 N 天兩家共同淨買、-N = 共同淨賣。

    args:
      inst_df: asc 排序的 institutional 表（含 foreign_buy_sell, trust_buy_sell）

    returns:
      float（典型 -5 ~ +5，極端可達 ±10）；缺資料 / 全 0 → 0.0
    """
    if inst_df is None or inst_df.empty:
        return 0.0
    if "foreign_buy_sell" not in inst_df.columns or "trust_buy_sell" not in inst_df.columns:
        return 0.0

    # 從最新一天往前掃
    tail = inst_df.tail(10).copy()
    f = tail["foreign_buy_sell"].fillna(0).astype(float).tolist()
    t = tail["trust_buy_sell"].fillna(0).astype(float).tolist()
    if not f:
        return 0.0

    # 從最後一天起算 streak
    streak_dir = 0  # 1 = 共同淨買、-1 = 共同淨賣
    streak = 0
    for i in range(len(f) - 1, -1, -1):
        fi = f[i]
        ti = t[i]
        # 兩家「同 sign 共同淨買 / 賣」— 任一為 0 不算同向，break
        if fi > 0 and ti > 0:
            cur_dir = 1
        elif fi < 0 and ti < 0:
            cur_dir = -1
        else:
            break
        if streak_dir == 0:
            streak_dir = cur_dir
            streak = 1
        elif cur_dir == streak_dir:
            streak += 1
        else:
            break
    return float(streak_dir * streak)


def compute_inst_divergence(inst_df: pd.DataFrame) -> float:
    """外資 vs 投信淨買超背離指標（5 日累計觀察）。

    回 [0, 1] 區間（不帶 sign）:
      - 0.0  完全同向或任一家近 5 日沒動 / 缺資料
      - 1.0  完全反向（外資強買 N 張、投信強賣 N 張，量級相當）

    公式:|f5 + t5| / (|f5| + |t5|)，再用 1 - 這個值反向（越背離越高）。
    """
    if inst_df is None or inst_df.empty:
        return 0.0
    if "foreign_buy_sell" not in inst_df.columns or "trust_buy_sell" not in inst_df.columns:
        return 0.0
    tail = inst_df.tail(5)
    f5 = float(tail["foreign_buy_sell"].fillna(0).sum())
    t5 = float(tail["trust_buy_sell"].fillna(0).sum())
    denom = abs(f5) + abs(t5)
    if denom <= 0:
        return 0.0
    cohesion = abs(f5 + t5) / denom  # 同向 → 1.0、完全反向 → 0.0
    return max(0.0, min(1.0, 1.0 - cohesion))


# === 多時間軸 ===

def compute_ma_above_pct(close: pd.Series, fast: int, slow: int, lookback: int = 60) -> float:
    """近 lookback 日內，fast MA > slow MA 的天數占比（0-1）。

    args:
      close:  asc 排序的 close 序列（至少 lookback + slow 天才有意義）
      fast/slow: 快/慢 MA 期數
      lookback: 統計窗口

    returns:
      float（0-1）；資料不足 → 0.0
    """
    if close is None or len(close) < slow + 5:
        return 0.0
    ma_fast = close.rolling(fast).mean()
    ma_slow = close.rolling(slow).mean()
    # 取最後 lookback 天比對（dropna 確保兩條都有值）
    diff = (ma_fast - ma_slow).dropna().tail(lookback)
    if diff.empty:
        return 0.0
    return float((diff > 0).mean())


def compute_momentum_n(close: pd.Series, n: int) -> float:
    """N 日動能 = (close_now / close_n_ago - 1) × 100（%）。

    資料不足 / close_n_ago ≤ 0 → 0.0。
    """
    if close is None or len(close) < n + 1:
        return 0.0
    now = float(close.iloc[-1])
    ago = float(close.iloc[-(n + 1)])
    if ago <= 0:
        return 0.0
    return (now / ago - 1.0) * 100.0


# === 產業相對強度 ===

def compute_industry_relative_strength(
    sid_return_5d: float,
    industry_returns_5d: Iterable[float],
) -> float:
    """該股 5 日漲幅 − 同產業平均 5 日漲幅（% 點）。

    args:
      sid_return_5d:  該股近 5 日漲幅（%）
      industry_returns_5d: 同產業所有股的 5 日漲幅 list（含本股）

    returns:
      float（典型 -10 ~ +10）；產業樣本 < 3 → 0.0
    """
    arr = [r for r in (industry_returns_5d or []) if r is not None]
    if len(arr) < 3:
        return 0.0
    avg = sum(arr) / len(arr)
    return float(sid_return_5d) - float(avg)


def compute_industry_rank_pct(
    sid_return_5d: float,
    industry_returns_5d: Iterable[float],
) -> float:
    """該股在產業內 5 日漲幅 percentile（0-1）。

    1.0 = 全產業最強、0.0 = 最弱。產業樣本 < 3 → 0.5（中性）。
    """
    arr = sorted([r for r in (industry_returns_5d or []) if r is not None])
    if len(arr) < 3:
        return 0.5
    # 用 strict-less rank：嚴格小於該股報酬的個數 / 總數
    n_below = sum(1 for r in arr if r < sid_return_5d)
    return float(n_below) / float(len(arr))


# === SQL helpers（給 ml_predictor.extract_features 用，純讀邏輯，可獨立測） ===

def _load_industry_for_sid(
    stock_id: str,
    db_path: str | Path | None = None,
) -> str | None:
    """讀 stocks.industry，sid 不存在 → None。"""
    from src import database as db

    try:
        with db.get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT industry FROM stocks WHERE stock_id=?",
                (stock_id,),
            ).fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("[ml_features] _load_industry_for_sid 失敗:%s", e)
        return None
    if not row:
        return None
    return row[0] if row[0] else None


def _load_industry_returns_5d(
    industry: str,
    target_date: str,
    db_path: str | Path | None = None,
    limit_sids: int = 50,
) -> list[float]:
    """撈同產業內所有 sid 的近 5 日漲幅（%），給 industry_relative_strength 用。

    為避免大產業（電子零組件 200+ 檔）每 sid × 每 target_date 都 N 次 SQL，
    用單一 query 撈所有同產業 sid 的近 5+1 日 close,在 Python 算 5 日報酬。
    limit_sids:safety cap，太多 sid 直接砍。

    回 list[float]（百分比）；產業沒 sid / 全部缺資料 → [].
    """
    from src import database as db

    if not industry:
        return []
    try:
        with db.get_conn(db_path) as conn:
            sid_rows = conn.execute(
                "SELECT stock_id FROM stocks WHERE industry=? LIMIT ?",
                (industry, limit_sids),
            ).fetchall()
            sids = [r[0] for r in sid_rows if r[0]]
            if not sids:
                return []

            # 撈每個 sid 在 target_date 前的 close（取最後 6 個值算 5 日報酬）
            # 用 IN(...) 一次撈，再在 Python 端 group
            placeholders = ",".join("?" * len(sids))
            rows = conn.execute(
                f"SELECT stock_id, date, close FROM daily_prices "
                f"WHERE stock_id IN ({placeholders}) AND date <= ? "
                f"ORDER BY stock_id, date DESC",
                sids + [target_date],
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("[ml_features] _load_industry_returns_5d 失敗:%s", e)
        return []

    by_sid: dict[str, list[float]] = {}
    for r in rows:
        sid = r[0]
        close = r[2]
        if close is None or close <= 0:
            continue
        lst = by_sid.setdefault(sid, [])
        if len(lst) < 6:  # 只要最近 6 天
            lst.append(float(close))

    returns: list[float] = []
    for sid, closes in by_sid.items():
        if len(closes) < 6:
            continue
        # closes 是 DESC 排序：closes[0] = latest，closes[5] = 5 天前
        latest = closes[0]
        ago = closes[5]
        if ago <= 0:
            continue
        returns.append((latest / ago - 1.0) * 100.0)
    return returns


# === Industry feature cache: target_date × db_path 共用 ===
# 同 target_date 對全市場 predict_batch 時,同產業的 N 檔股票會反覆撈同樣的
# industry_returns_5d list — 跨 sid cache 在 (target_date, industry) 一次即可。
_INDUSTRY_RETURNS_CACHE: dict[tuple[str, str, str], list[float]] = {}


def get_industry_returns_5d_cached(
    industry: str,
    target_date: str,
    db_path: str | Path | None = None,
) -> list[float]:
    """cached wrapper around _load_industry_returns_5d。

    cache key = (target_date, industry, str(db_path))。同產業同日的 N 檔 sid
    只算一次 — 對全市場 predict_batch 顯著省 SQL（O(M) → O(industries)）。
    """
    key = (target_date, industry or "", str(db_path) if db_path else "")
    if key in _INDUSTRY_RETURNS_CACHE:
        return _INDUSTRY_RETURNS_CACHE[key]
    arr = _load_industry_returns_5d(industry, target_date, db_path=db_path)
    _INDUSTRY_RETURNS_CACHE[key] = arr
    return arr


def _reset_industry_cache() -> None:
    """測試用:清掉 industry returns cache。"""
    _INDUSTRY_RETURNS_CACHE.clear()


# === 對外 feature names（給 ml_predictor.FEATURE_NAMES append 用） ===
NEW_FEATURE_NAMES: list[str] = [
    # 籌碼
    "concentration_change_rate",
    "institutional_continuity",
    "inst_divergence",
    # 多時間軸
    "ma5_above_ma20_pct",
    "ma20_above_ma60_pct",
    "momentum_5d",
    "momentum_20d",
    "momentum_60d",
    # 產業相對強度
    "industry_relative_strength",
    "industry_rank_pct",
]


__all__ = [
    "NEW_FEATURE_NAMES",
    # 籌碼
    "compute_concentration_change_rate",
    "compute_institutional_continuity",
    "compute_inst_divergence",
    # 多時間軸
    "compute_ma_above_pct",
    "compute_momentum_n",
    # 產業
    "compute_industry_relative_strength",
    "compute_industry_rank_pct",
    "get_industry_returns_5d_cached",
    "_load_industry_for_sid",
    "_load_industry_returns_5d",
    "_reset_industry_cache",
]
