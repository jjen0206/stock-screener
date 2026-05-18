"""把 raw score / ml_prob 翻譯成期望報酬 (EV %),從 pick_outcomes 校準。

主公看不懂 0.832 raw score → 改成「EV +2.3%」直接告訴他「進場期望賺 2.3%」。
不改 score 計算本身,只改「翻譯顯示」這層。

# 資料源
- 訓練 mapping:`data/twse_snapshot/daily_picks.csv` (ml_prob) JOIN
  `data/twse_snapshot/pick_outcomes.csv` (return_d5)
- Bucket:10 個 quantile bucket(score 0-10%, 10-20%, ..., 90-100%),
  每 bucket 平均 return_d5 當該 bucket EV
- 策略獨立:某策略 closed_trades > 100 → 用該策略自己的 mapping;
  否則 fallback 全市場 mapping

# Mapping 持久化
weekly cron(`scripts/precompute_score_ev_mapping.py`)dump 到
`data/twse_snapshot/score_to_ev.csv`,schema:

    strategy, bucket_lo, bucket_hi, avg_ev, n_samples

`strategy='__global__'` 是全市場合一 mapping(fallback)。

# Fallback 鏈
1. Per-strategy bucket lookup(strategy ≥ 100 samples)
2. Global bucket lookup(≥ 30 samples)
3. `strategy_backtest.avg_return` 該策略歷史平均
4. 線性 fallback `score * 0.05 - (1-score) * 0.03`(原 notifier.py 公式)

# 單位
EV 一律回傳 **fraction**(0.023 = 2.3%),跟 `notifier.py` 既有
`ev` 欄一致 — caller render 時 ×100 加百分號。
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import pandas as pd

from src import config

logger = logging.getLogger(__name__)

# 校準參數
_BUCKET_N: int = 10
_GLOBAL_MIN_SAMPLES: int = 30
_STRATEGY_MIN_SAMPLES: int = 100
_GLOBAL_KEY: str = "__global__"

# CSV schema
_CSV_COLUMNS = ["strategy", "bucket_lo", "bucket_hi", "avg_ev", "n_samples"]


def _csv_path(snapshot_dir: str | Path | None = None) -> Path:
    if snapshot_dir is None:
        snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    return Path(snapshot_dir) / "score_to_ev.csv"


def _join_picks_outcomes(
    picks: pd.DataFrame, outcomes: pd.DataFrame
) -> pd.DataFrame:
    """JOIN daily_picks + pick_outcomes → (ml_prob, strategy, return_d5)。

    daily_picks 是 (trade_date, sid, strategy),pick_outcomes 是
    (pick_date, sid, strategy) — 三 key 對齊 inner join。return_d5
    為 percent(e.g., 2.38 表 +2.38%),除以 100 變 fraction。
    """
    p = picks[picks["ml_prob"].notna()][
        ["trade_date", "sid", "strategy", "ml_prob"]
    ].copy()
    o = outcomes[outcomes["return_d5"].notna()][
        ["pick_date", "sid", "strategy", "return_d5"]
    ].copy()
    if p.empty or o.empty:
        return pd.DataFrame(columns=["ml_prob", "strategy", "return_d5"])

    p["sid"] = p["sid"].astype(str)
    o["sid"] = o["sid"].astype(str)
    m = o.merge(
        p,
        left_on=["pick_date", "sid", "strategy"],
        right_on=["trade_date", "sid", "strategy"],
        how="inner",
    )
    # return_d5 percent → fraction(讓 EV 結果同單位)
    m["return_d5"] = m["return_d5"].astype(float) / 100.0
    m["ml_prob"] = m["ml_prob"].astype(float)
    return m[["ml_prob", "strategy", "return_d5"]].reset_index(drop=True)


def _bucket_table(scores: Iterable[float], returns: Iterable[float]) -> pd.DataFrame:
    """切 10 個 quantile bucket → 每 bucket (lo, hi, avg_ev, n)。

    duplicates='drop' 容許 score 分布偏(部分 bucket 邊界相同)。
    bucket_lo / bucket_hi 包含全範圍(第一 bucket lo = -inf,最後 hi = +inf)
    讓查找永遠落得到一桶。
    """
    df = pd.DataFrame({"score": list(scores), "ret": list(returns)})
    df = df[df["score"].notna() & df["ret"].notna()]
    if len(df) < _GLOBAL_MIN_SAMPLES:
        return pd.DataFrame(columns=_CSV_COLUMNS[1:])

    try:
        df["bucket"] = pd.qcut(df["score"], _BUCKET_N, duplicates="drop")
    except (ValueError, TypeError):
        return pd.DataFrame(columns=_CSV_COLUMNS[1:])

    grouped = df.groupby("bucket", observed=True)["ret"].agg(["count", "mean"])
    rows: list[dict] = []
    intervals = list(grouped.index)
    for i, iv in enumerate(intervals):
        lo = float("-inf") if i == 0 else float(iv.left)
        hi = float("inf") if i == len(intervals) - 1 else float(iv.right)
        rows.append({
            "bucket_lo": lo,
            "bucket_hi": hi,
            "avg_ev": float(grouped.loc[iv, "mean"]),
            "n_samples": int(grouped.loc[iv, "count"]),
        })
    return pd.DataFrame(rows, columns=_CSV_COLUMNS[1:])


def build_score_to_ev_mapping(
    picks: pd.DataFrame,
    outcomes: pd.DataFrame,
) -> pd.DataFrame:
    """從 picks + outcomes 算 mapping。

    Returns long-form DataFrame:
        strategy, bucket_lo, bucket_hi, avg_ev, n_samples

    strategy='__global__' 為全市場 fallback;其餘為樣本 ≥ 100 的策略。
    """
    joined = _join_picks_outcomes(picks, outcomes)
    if joined.empty:
        return pd.DataFrame(columns=_CSV_COLUMNS)

    frames: list[pd.DataFrame] = []
    g = _bucket_table(joined["ml_prob"], joined["return_d5"])
    if not g.empty:
        g.insert(0, "strategy", _GLOBAL_KEY)
        frames.append(g)

    for strat, sub in joined.groupby("strategy"):
        if len(sub) < _STRATEGY_MIN_SAMPLES:
            continue
        t = _bucket_table(sub["ml_prob"], sub["return_d5"])
        if t.empty:
            continue
        t.insert(0, "strategy", str(strat))
        frames.append(t)

    if not frames:
        return pd.DataFrame(columns=_CSV_COLUMNS)
    return pd.concat(frames, ignore_index=True)[_CSV_COLUMNS]


def dump_mapping_to_csv(
    mapping: pd.DataFrame,
    snapshot_dir: str | Path | None = None,
) -> Path:
    """Mapping DataFrame → CSV。回 written path。"""
    path = _csv_path(snapshot_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mapping is None or mapping.empty:
        # 寫空頭 schema 也算合法(讓 loader graceful skip)
        pd.DataFrame(columns=_CSV_COLUMNS).to_csv(path, index=False)
    else:
        mapping[_CSV_COLUMNS].to_csv(path, index=False)
    return path


def _load_mapping_df(snapshot_dir: str | Path | None = None) -> pd.DataFrame:
    """讀 CSV → DataFrame;CSV 不存在 / 壞 → 空表(讓 caller fallback)。"""
    path = _csv_path(snapshot_dir)
    if not path.exists():
        return pd.DataFrame(columns=_CSV_COLUMNS)
    try:
        df = pd.read_csv(path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as ex:
        logger.warning("[SCORE_TO_EV] 讀 mapping CSV 失敗:%s", ex)
        return pd.DataFrame(columns=_CSV_COLUMNS)
    # 對齊 schema(舊版檔多/少欄都不炸)
    for col in _CSV_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[_CSV_COLUMNS]


@lru_cache(maxsize=4)
def _load_mapping_cached(snapshot_dir_str: str | None) -> dict:
    """快取版 — 把 DataFrame 轉成 dict[strategy → list[(lo, hi, ev, n)]]
    減少每次 score_to_ev 呼叫的 lookup 成本(daily_notify 一輪 N picks
    全走 mapping,N>50 時 dict lookup 比 DataFrame filter 快數十倍)。
    """
    snapshot_dir: str | Path | None = snapshot_dir_str  # type: ignore[assignment]
    df = _load_mapping_df(snapshot_dir)
    out: dict[str, list[tuple[float, float, float, int]]] = {}
    if df.empty:
        return out
    for strat, sub in df.groupby("strategy"):
        rows: list[tuple[float, float, float, int]] = []
        for _, r in sub.iterrows():
            try:
                rows.append((
                    float(r["bucket_lo"]),
                    float(r["bucket_hi"]),
                    float(r["avg_ev"]),
                    int(r["n_samples"]) if pd.notna(r["n_samples"]) else 0,
                ))
            except (TypeError, ValueError):
                continue
        rows.sort(key=lambda x: x[0])
        if rows:
            out[str(strat)] = rows
    return out


def invalidate_cache() -> None:
    """週 cron / test reset mapping 後呼叫。"""
    _load_mapping_cached.cache_clear()


def _strategy_backtest_avg(strategy_key: str) -> float | None:
    """Fallback #2:strategy_backtest.csv 該策略 avg_return(同 fraction)。"""
    try:
        path = config.PROJECT_ROOT / "data" / "twse_snapshot" / "strategy_backtest.csv"
        if not path.exists():
            return None
        df = pd.read_csv(path)
        sub = df[df["strategy"] == strategy_key]
        if sub.empty:
            return None
        # avg_return 已是 fraction(e.g., 0.00766)
        return float(sub["avg_return"].mean())
    except Exception as ex:  # noqa: BLE001
        logger.debug("[SCORE_TO_EV] strategy_backtest fallback 失敗:%s", ex)
        return None


def _linear_fallback(score: float) -> float:
    """Fallback #3:跟 notifier.py 原公式同 — 固定 5%/3% 假設。"""
    return score * 0.05 - (1.0 - score) * 0.03


def _lookup_bucket(
    rows: list[tuple[float, float, float, int]],
    score: float,
) -> float | None:
    """在 sorted buckets 找 score 落點 → 回 avg_ev。"""
    for lo, hi, ev, _n in rows:
        if lo <= score <= hi:
            return ev
    return None


def score_to_ev(
    score: float | None,
    strategy_key: str | None = None,
    *,
    snapshot_dir: str | Path | None = None,
) -> float | None:
    """主 API:給 raw score / ml_prob 回 EV (fraction)。

    Args:
        score: raw score(目前實作 = ml_prob,[0, 1] 區間)
        strategy_key: 可選的策略 key — 有 per-strategy mapping 就用,否則 fallback
        snapshot_dir: 可選的 snapshot_dir override(test 用 tmp_path)

    Returns:
        EV fraction(0.023 = +2.3%)— `None` 表示無法估算(score 為 None)。

    Fallback 鏈:
        1. Per-strategy bucket(strategy ≥ 100 samples)
        2. Global bucket(全市場 ≥ 30 samples)
        3. strategy_backtest.avg_return 該策略歷史平均
        4. 線性 fallback(score × 0.05 - (1-score) × 0.03)
    """
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s != s:  # NaN
        return None

    # cache key 用 str(snapshot_dir) — lru_cache 不能吃 Path
    key = str(snapshot_dir) if snapshot_dir is not None else None
    try:
        mapping = _load_mapping_cached(key)
    except Exception as ex:  # noqa: BLE001
        logger.warning("[SCORE_TO_EV] load mapping 失敗:%s", ex)
        mapping = {}

    # 1. Per-strategy bucket
    if strategy_key and strategy_key in mapping:
        ev = _lookup_bucket(mapping[strategy_key], s)
        if ev is not None:
            return ev

    # 2. Global bucket
    if _GLOBAL_KEY in mapping:
        ev = _lookup_bucket(mapping[_GLOBAL_KEY], s)
        if ev is not None:
            return ev

    # 3. strategy_backtest 歷史平均
    if strategy_key:
        ev = _strategy_backtest_avg(strategy_key)
        if ev is not None:
            return ev

    # 4. 線性 fallback
    return _linear_fallback(s)


def score_to_ev_for_pick(
    score: float | None,
    matched_strategies: list[str] | None = None,
    *,
    snapshot_dir: str | Path | None = None,
) -> float | None:
    """便利包裝:一個 pick 命中多策略時挑「樣本最大」的策略 mapping。

    matched_strategies 排序假設前面的優先(notifier 慣例);若都不在
    per-strategy mapping 就退回 global。
    """
    if score is None:
        return None
    key: str | None = None
    if matched_strategies:
        cached = _load_mapping_cached(
            str(snapshot_dir) if snapshot_dir is not None else None
        )
        for s in matched_strategies:
            if s in cached:
                key = s
                break
    return score_to_ev(score, strategy_key=key, snapshot_dir=snapshot_dir)


def render_ev_str(ev: float | None) -> str:
    """渲染 EV fraction 成顯示字串。

    Examples:
        render_ev_str(0.023)  → 'EV +2.3%'
        render_ev_str(-0.005) → 'EV -0.5%'
        render_ev_str(None)   → 'EV —'(沒 mapping 也顯示佔位)
        render_ev_str(0.0)    → 'EV +0.0%'
    """
    if ev is None:
        return "EV —"
    try:
        v = float(ev)
    except (TypeError, ValueError):
        return "EV —"
    if v != v:
        return "EV —"
    sign = "+" if v >= 0 else ""
    return f"EV {sign}{v * 100:.1f}%"


__all__ = [
    "build_score_to_ev_mapping",
    "dump_mapping_to_csv",
    "score_to_ev",
    "score_to_ev_for_pick",
    "render_ev_str",
    "invalidate_cache",
]
