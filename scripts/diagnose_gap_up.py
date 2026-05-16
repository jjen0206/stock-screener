"""Diagnose gap_up 策略訊號本身有沒有 edge — 不接 ML,純歷史統計。

WF ROC 0.4926 接近 random 意味著「ML 從現有 features 學不到 gap_up 隔日漲跌」。
但這不等於「gap_up 訊號本身沒 edge」— 也可能是 features 不對,或 label 口徑不符。

本 script 用三種視角測 gap_up 訊號本身:
  1. simulate_outcome 對齊 ML label(+5% target / -3% stop / 5 日 hold)→ 勝率 / EV
  2. raw D+1 close 報酬(隔日 follow-through 是否存在)
  3. raw D+5 close 報酬(週內 holding 是否存在)

對照組(baseline):同期同 universe 隨機 (sid, date),不過濾 gap_up 條件,
看 baseline 在同 +5%/-3%/5d 條件下勝率。**若 gap_up WR ≈ baseline WR → 訊號無 edge**。

分桶:
  - 缺口大小:<2 / 2-3 / 3-5 / 5-7 / >7 %
  - 量比:1.5-2 / 2-3 / 3-5 / >5
  - 大盤 regime(限 TAIEX 有資料的日期)
  - 缺口前 5 日 close trend slope(看是「強勢中加速」還是「弱勢反彈」)

CLI:
    python scripts/diagnose_gap_up.py
    python scripts/diagnose_gap_up.py --lookback-days 250 --baseline-samples 5000
    python scripts/diagnose_gap_up.py --out docs/gap_up_diagnose_raw.json

Exit:
    0 = 跑完(無論 edge 有無)
    1 = 樣本太少或 DB 空
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from src import database as db  # noqa: E402
from src.strategies import DEFAULT_GAP_UP_PARAMS  # noqa: E402
from src.universe import pure_stock_universe  # noqa: E402
from src.market_regime import compute_regime  # noqa: E402


TARGET_PCT = 0.05
STOP_PCT = 0.03
HOLD_DAYS = 5

GAP_BUCKETS = [
    ("<2%",   1.5, 2.0),
    ("2-3%",  2.0, 3.0),
    ("3-5%",  3.0, 5.0),
    ("5-7%",  5.0, 7.0),
    (">7%",   7.0, float("inf")),
]
VOL_BUCKETS = [
    ("1.5-2x", 1.5, 2.0),
    ("2-3x",   2.0, 3.0),
    ("3-5x",   3.0, 5.0),
    (">5x",    5.0, float("inf")),
]


def _load_ohlc_panel(min_date: str, db_path: str | Path | None = None) -> pd.DataFrame:
    """整批撈 daily_prices ≥ min_date(排除 TAIEX)。"""
    with db.get_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT stock_id, date, open, high, low, close, volume "
            "FROM daily_prices WHERE date >= ? AND stock_id != 'TAIEX' "
            "ORDER BY stock_id, date",
            conn,
            params=(min_date,),
        )
    return df


def _find_gap_up_fires(
    panel: pd.DataFrame,
    min_t_date: str,
    max_t_date: str,
    params: dict | None = None,
) -> pd.DataFrame:
    """純向量化跑 gap_up 條件,輸出 fires DataFrame。

    對齊 src/strategies._evaluate_gap_up 三條件:
      1. open[t] > close[t-1] × (1 + gap_pct_min/100)
      2. close[t] > open[t](收紅 K)
      3. volume[t] / mean(volume[t-5..t-1]) ≥ gap_vol_ratio_min

    回 columns:
      stock_id / date / prev_close / today_open / today_close /
      today_high / today_low / today_volume / gap_pct / vol_ratio /
      prev5_slope_pct(t-5 ~ t-1 close 線性 trend %) / atr_pct(前 14 日 ATR%)
    """
    p = {**DEFAULT_GAP_UP_PARAMS, **(params or {})}
    gap_min = p["gap_pct_min"]
    vol_min = p["gap_vol_ratio_min"]

    panel = panel.sort_values(["stock_id", "date"], kind="stable").reset_index(drop=True)
    g = panel.groupby("stock_id", sort=False)
    panel["prev_close"] = g["close"].shift(1)
    panel["vol_ma5"] = g["volume"].shift(1).rolling(5).mean().reset_index(level=0, drop=True)
    panel["gap_pct"] = (panel["open"] / panel["prev_close"] - 1) * 100
    panel["vol_ratio"] = panel["volume"] / panel["vol_ma5"]

    # prev5_slope_pct:t-5 ~ t-1 close 線性 OLS slope ÷ entry × 100(%/天)
    def _slope(s: pd.Series) -> float:
        if s.isna().any() or len(s) < 5:
            return float("nan")
        x = list(range(len(s)))
        y = s.tolist()
        n = len(s)
        sx = sum(x); sy = sum(y)
        sxx = sum(xi*xi for xi in x)
        sxy = sum(xi*yi for xi, yi in zip(x, y))
        denom = n * sxx - sx * sx
        if denom == 0:
            return float("nan")
        slope = (n * sxy - sx * sy) / denom
        return slope

    # 為效能,只對候選 (符合 gap + vol 條件) 計算 slope
    panel["red"] = panel["close"] > panel["open"]
    candidate_mask = (
        (panel["gap_pct"] >= gap_min)
        & (panel["red"])
        & (panel["vol_ratio"] >= vol_min)
        & panel["prev_close"].notna()
        & panel["vol_ma5"].notna()
        & (panel["date"] >= min_t_date)
        & (panel["date"] <= max_t_date)
    )
    cand = panel[candidate_mask].copy()
    if cand.empty:
        return cand

    # 對候選對應 slope:取前 5 日 close(t-5 ~ t-1)。先 build sid→date→idx mapping
    by_sid: dict[str, pd.DataFrame] = {
        sid: sub.set_index("date")
        for sid, sub in panel.groupby("stock_id", sort=False)
    }
    slopes: list[float] = []
    atr_pcts: list[float] = []
    for _, row in cand.iterrows():
        sid_df = by_sid[row["stock_id"]]
        idx = sid_df.index.get_loc(row["date"])
        if idx < 14:
            slopes.append(float("nan"))
            atr_pcts.append(float("nan"))
            continue
        prev5 = sid_df["close"].iloc[idx-5:idx]
        slopes.append(_slope(prev5))
        # ATR(14):mean(true_range[t-14..t-1])
        sub = sid_df.iloc[idx-14:idx]
        tr = pd.concat([
            sub["high"] - sub["low"],
            (sub["high"] - sub["close"].shift(1)).abs(),
            (sub["low"] - sub["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.mean()
        atr_pcts.append(atr / row["prev_close"] * 100 if row["prev_close"] > 0 else float("nan"))

    cand["prev5_slope_per_day"] = slopes
    cand["prev5_slope_pct_per_day"] = [
        (s / row["prev_close"]) * 100 if isinstance(s, float) and not math.isnan(s) and row["prev_close"] > 0 else float("nan")
        for s, (_, row) in zip(slopes, cand.iterrows())
    ]
    cand["atr_pct"] = atr_pcts

    return cand.rename(columns={
        "open": "today_open",
        "close": "today_close",
        "high": "today_high",
        "low": "today_low",
        "volume": "today_volume",
    })[[
        "stock_id", "date", "prev_close", "today_open", "today_close",
        "today_high", "today_low", "today_volume", "gap_pct", "vol_ratio",
        "prev5_slope_pct_per_day", "atr_pct",
    ]]


def _attach_outcomes(fires: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    """對每個 fire 撈 D+1..D+5 OHLC,模擬 outcome + raw 1d / 5d return。"""
    if fires.empty:
        return fires
    panel_idx = {
        sid: sub.reset_index(drop=True)
        for sid, sub in panel.sort_values(["stock_id", "date"]).groupby("stock_id", sort=False)
    }
    outcomes: list[str] = []
    sim_returns: list[float] = []
    raw_1d: list[float] = []
    raw_5d: list[float] = []
    for _, row in fires.iterrows():
        sid_df = panel_idx.get(row["stock_id"])
        if sid_df is None:
            outcomes.append("lose"); sim_returns.append(0.0); raw_1d.append(float("nan")); raw_5d.append(float("nan"))
            continue
        # 找 t 在 sid_df 的 row idx
        idx_match = sid_df.index[sid_df["date"] == row["date"]]
        if len(idx_match) == 0:
            outcomes.append("lose"); sim_returns.append(0.0); raw_1d.append(float("nan")); raw_5d.append(float("nan"))
            continue
        t_idx = int(idx_match[0])
        future = sid_df.iloc[t_idx+1 : t_idx+1+HOLD_DAYS]
        if len(future) < HOLD_DAYS:
            outcomes.append("lose"); sim_returns.append(0.0); raw_1d.append(float("nan")); raw_5d.append(float("nan"))
            continue
        ep = float(row["today_close"])
        # simulate_outcome 路徑
        out = None
        last_close = ep
        for _, fr in future.iterrows():
            high = float(fr["high"]); low = float(fr["low"]); close = float(fr["close"])
            last_close = close
            if low <= ep * (1 - STOP_PCT):
                out = ("lose", -STOP_PCT); break
            if high >= ep * (1 + TARGET_PCT):
                out = ("win", TARGET_PCT); break
        if out is None:
            fr = (last_close - ep) / ep if ep > 0 else 0.0
            out = ("win" if fr > 0 else "lose", fr)
        outcomes.append(out[0]); sim_returns.append(out[1])
        # raw returns
        raw_1d.append(float(future["close"].iloc[0]) / ep - 1 if ep > 0 else float("nan"))
        raw_5d.append(float(future["close"].iloc[-1]) / ep - 1 if ep > 0 else float("nan"))
    out = fires.copy()
    out["outcome"] = outcomes
    out["sim_return"] = sim_returns
    out["raw_1d_return"] = raw_1d
    out["raw_5d_return"] = raw_5d
    return out


def _sample_baseline(
    panel: pd.DataFrame,
    fire_dates: set[str],
    n_samples: int,
    rng: random.Random,
) -> pd.DataFrame:
    """隨機抽 (sid, date) 同期(fire_dates 內)做 baseline,模擬同 +5%/-3%/5d。

    這個 baseline 跟 fire 同分母:同 trading days、同 universe、不過濾任何訊號。
    若 baseline WR ≈ fire WR → 訊號沒 edge。
    """
    df = panel[panel["date"].isin(fire_dates)].copy()
    if df.empty:
        return df
    n = min(n_samples, len(df))
    idx = rng.sample(range(len(df)), n)
    samples = df.iloc[idx].copy().reset_index(drop=True)
    # 對應算 outcome
    panel_idx = {
        sid: sub.reset_index(drop=True)
        for sid, sub in panel.sort_values(["stock_id", "date"]).groupby("stock_id", sort=False)
    }
    outcomes: list[str] = []
    sim_returns: list[float] = []
    raw_1d: list[float] = []
    raw_5d: list[float] = []
    for _, row in samples.iterrows():
        sid_df = panel_idx.get(row["stock_id"])
        if sid_df is None:
            outcomes.append("lose"); sim_returns.append(0.0); raw_1d.append(float("nan")); raw_5d.append(float("nan"))
            continue
        m = sid_df.index[sid_df["date"] == row["date"]]
        if len(m) == 0:
            outcomes.append("lose"); sim_returns.append(0.0); raw_1d.append(float("nan")); raw_5d.append(float("nan"))
            continue
        t = int(m[0])
        future = sid_df.iloc[t+1 : t+1+HOLD_DAYS]
        if len(future) < HOLD_DAYS:
            outcomes.append("lose"); sim_returns.append(0.0); raw_1d.append(float("nan")); raw_5d.append(float("nan"))
            continue
        ep = float(row["close"])
        out = None
        last_close = ep
        for _, fr in future.iterrows():
            high = float(fr["high"]); low = float(fr["low"]); close = float(fr["close"])
            last_close = close
            if low <= ep * (1 - STOP_PCT):
                out = ("lose", -STOP_PCT); break
            if high >= ep * (1 + TARGET_PCT):
                out = ("win", TARGET_PCT); break
        if out is None:
            rr = (last_close - ep) / ep if ep > 0 else 0.0
            out = ("win" if rr > 0 else "lose", rr)
        outcomes.append(out[0]); sim_returns.append(out[1])
        raw_1d.append(float(future["close"].iloc[0]) / ep - 1 if ep > 0 else float("nan"))
        raw_5d.append(float(future["close"].iloc[-1]) / ep - 1 if ep > 0 else float("nan"))
    samples["outcome"] = outcomes
    samples["sim_return"] = sim_returns
    samples["raw_1d_return"] = raw_1d
    samples["raw_5d_return"] = raw_5d
    return samples


def _summarize(df: pd.DataFrame, label: str) -> dict[str, Any]:
    if df.empty:
        return {"label": label, "n": 0}
    n = len(df)
    wins = int((df["outcome"] == "win").sum())
    win_rate = wins / n
    sim_ret = df["sim_return"].astype(float)
    raw_1d = df["raw_1d_return"].astype(float).dropna()
    raw_5d = df["raw_5d_return"].astype(float).dropna()
    return {
        "label": label,
        "n": n,
        "wins": wins,
        "win_rate": float(win_rate),
        "sim_avg_return": float(sim_ret.mean()),
        "sim_median_return": float(sim_ret.median()),
        "sim_ev_pct": float(sim_ret.mean() * 100),  # 一筆交易期望報酬 %
        "raw_1d_mean_pct": float(raw_1d.mean() * 100) if len(raw_1d) else float("nan"),
        "raw_1d_median_pct": float(raw_1d.median() * 100) if len(raw_1d) else float("nan"),
        "raw_1d_pos_rate": float((raw_1d > 0).mean()) if len(raw_1d) else float("nan"),
        "raw_5d_mean_pct": float(raw_5d.mean() * 100) if len(raw_5d) else float("nan"),
        "raw_5d_median_pct": float(raw_5d.median() * 100) if len(raw_5d) else float("nan"),
        "raw_5d_pos_rate": float((raw_5d > 0).mean()) if len(raw_5d) else float("nan"),
    }


def _bucket_summary(
    df: pd.DataFrame,
    by_col: str,
    buckets: list[tuple[str, float, float]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, lo, hi in buckets:
        sub = df[(df[by_col] >= lo) & (df[by_col] < hi)]
        rows.append({"bucket": label, "lo": lo, "hi": hi, **_summarize(sub, f"{by_col}={label}")})
    return rows


def _attach_regime(df: pd.DataFrame, db_path: str | Path | None = None) -> pd.DataFrame:
    """對每個 fire date 算 TAIEX regime。TAIEX 資料不足 → 'unknown'。"""
    if df.empty:
        return df
    unique_dates = sorted(df["date"].unique())
    regime_map: dict[str, str] = {}
    for d in unique_dates:
        try:
            r = compute_regime(target_date=d, db_path=db_path)
            regime_map[d] = r["regime"]
        except Exception:
            regime_map[d] = "unknown"
    df = df.copy()
    df["regime"] = df["date"].map(regime_map)
    return df


def _print_md(rows: list[dict[str, Any]], headers: list[tuple[str, str]]) -> None:
    """印 markdown table。headers = list of (col_key, display_name)。"""
    print("| " + " | ".join(h[1] for h in headers) + " |", flush=True)
    print("|" + "|".join(["---"] * len(headers)) + "|", flush=True)
    for r in rows:
        cells = []
        for k, _ in headers:
            v = r.get(k)
            if isinstance(v, float):
                if math.isnan(v):
                    cells.append("n/a")
                elif k in ("win_rate", "raw_1d_pos_rate", "raw_5d_pos_rate"):
                    cells.append(f"{v * 100:.1f}%")
                elif k.endswith("_pct") or "ev_pct" in k or "mean_pct" in k or "median_pct" in k:
                    cells.append(f"{v:+.2f}%")
                else:
                    cells.append(f"{v:+.4f}")
            else:
                cells.append(str(v))
        print("| " + " | ".join(cells) + " |", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose gap_up 訊號 edge")
    ap.add_argument("--lookback-days", type=int, default=250,
                    help="抓最近 N 個 calendar days(~1 年)")
    ap.add_argument("--baseline-samples", type=int, default=5000,
                    help="baseline 隨機抽幾筆(同期同 universe)")
    ap.add_argument("--out", default=None,
                    help="輸出 JSON path(default: 不寫檔,只印 stdout)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    db.init_db()
    rng = random.Random(args.seed)

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM daily_prices WHERE stock_id != 'TAIEX'"
        ).fetchone()
        if not row or not row["d"]:
            print("[DIAG] daily_prices 空,中止", flush=True)
            return 1
        max_date = row["d"]
        # 留 5 個交易日緩衝給 future-OHLC,t-window 結尾要早 5 天
        valid_t_max_row = conn.execute(
            "SELECT date FROM (SELECT DISTINCT date FROM daily_prices "
            "WHERE stock_id != 'TAIEX' ORDER BY date DESC LIMIT ?) "
            "ORDER BY date ASC LIMIT 1",
            (HOLD_DAYS + 1,),
        ).fetchone()
        valid_t_max = valid_t_max_row["date"]
        # min_date 涵蓋 lookback + 20 天 buffer(算 vol_ma5 + prev5 + ATR14)
        min_date_row = conn.execute(
            "SELECT date FROM (SELECT DISTINCT date FROM daily_prices "
            "WHERE stock_id != 'TAIEX' ORDER BY date DESC LIMIT ?) "
            "ORDER BY date ASC LIMIT 1",
            (args.lookback_days + 20,),
        ).fetchone()
        min_date = min_date_row["date"]

    print(f"[DIAG] panel date range: [{min_date} .. {max_date}]", flush=True)
    print(f"[DIAG] gap_up fires evaluated: t ∈ [..., {valid_t_max}](留 {HOLD_DAYS} 日 future buffer)", flush=True)

    # universe — pure_stock_universe(同 train 邏輯)
    universe = set(pure_stock_universe(min_history=20))
    print(f"[DIAG] pure_stock_universe size = {len(universe)}", flush=True)

    t0 = time.time()
    panel = _load_ohlc_panel(min_date)
    panel = panel[panel["stock_id"].isin(universe)].copy()
    print(f"[DIAG] panel rows={len(panel)} ({time.time()-t0:.1f}s)", flush=True)
    if panel.empty:
        print("[DIAG] panel 空,中止", flush=True)
        return 1

    # 過去 1 年 fire window:從 panel 最早 lookback_days 個 trading_dates 起到 valid_t_max
    unique_dates = sorted(panel["date"].unique().tolist())
    if len(unique_dates) > args.lookback_days:
        min_t_date = unique_dates[-args.lookback_days]
    else:
        min_t_date = unique_dates[0]

    t0 = time.time()
    fires_raw = _find_gap_up_fires(panel, min_t_date=min_t_date, max_t_date=valid_t_max)
    print(f"[DIAG] gap_up fires found = {len(fires_raw)} ({time.time()-t0:.1f}s)", flush=True)

    if len(fires_raw) < 50:
        print(f"[DIAG] fires 太少({len(fires_raw)} < 50),統計不可靠,中止", flush=True)
        return 1

    t0 = time.time()
    fires = _attach_outcomes(fires_raw, panel)
    fires = _attach_regime(fires)
    print(f"[DIAG] outcomes + regime attached ({time.time()-t0:.1f}s)", flush=True)

    fire_dates = set(fires["date"].unique().tolist())

    # baseline:同期同 universe 隨機抽
    t0 = time.time()
    baseline = _sample_baseline(panel, fire_dates, args.baseline_samples, rng)
    print(f"[DIAG] baseline samples = {len(baseline)} ({time.time()-t0:.1f}s)", flush=True)

    # === 主表 ===
    overall_fire = _summarize(fires, "gap_up_overall")
    overall_base = _summarize(baseline, "baseline_same_period")

    print("\n## Overall — gap_up vs Baseline (同期 random)\n", flush=True)
    _print_md(
        [overall_fire, overall_base],
        [
            ("label", "Group"), ("n", "N"), ("win_rate", "WinRate(+5/-3/5d)"),
            ("sim_ev_pct", "Sim EV"),
            ("raw_1d_mean_pct", "Raw 1d mean"),
            ("raw_1d_pos_rate", "Raw 1d pos rate"),
            ("raw_5d_mean_pct", "Raw 5d mean"),
            ("raw_5d_pos_rate", "Raw 5d pos rate"),
        ],
    )

    # === Gap size bucket ===
    print("\n## By Gap Size (gap_pct,fire only)\n", flush=True)
    gap_rows = _bucket_summary(fires, "gap_pct", GAP_BUCKETS)
    _print_md(
        gap_rows,
        [
            ("bucket", "Gap %"), ("n", "N"), ("win_rate", "WinRate"),
            ("sim_ev_pct", "Sim EV"),
            ("raw_1d_mean_pct", "Raw 1d"), ("raw_1d_pos_rate", "1d pos%"),
            ("raw_5d_mean_pct", "Raw 5d"), ("raw_5d_pos_rate", "5d pos%"),
        ],
    )

    # === Vol ratio bucket ===
    print("\n## By Vol Ratio (today_vol / 5d MA,fire only)\n", flush=True)
    vol_rows = _bucket_summary(fires, "vol_ratio", VOL_BUCKETS)
    _print_md(
        vol_rows,
        [
            ("bucket", "Vol ratio"), ("n", "N"), ("win_rate", "WinRate"),
            ("sim_ev_pct", "Sim EV"),
            ("raw_1d_mean_pct", "Raw 1d"), ("raw_1d_pos_rate", "1d pos%"),
            ("raw_5d_mean_pct", "Raw 5d"), ("raw_5d_pos_rate", "5d pos%"),
        ],
    )

    # === Regime bucket ===
    print("\n## By Market Regime (TAIEX MA20/MA60,部份日期可能無 TAIEX → unknown)\n", flush=True)
    regime_rows: list[dict[str, Any]] = []
    regimes_counter = Counter(fires["regime"].tolist())
    for reg in ("bull", "weak_bull", "sideways", "bear", "unknown"):
        sub = fires[fires["regime"] == reg]
        regime_rows.append({"bucket": reg, **_summarize(sub, f"regime={reg}")})
    _print_md(
        regime_rows,
        [
            ("bucket", "Regime"), ("n", "N"), ("win_rate", "WinRate"),
            ("sim_ev_pct", "Sim EV"),
            ("raw_1d_mean_pct", "Raw 1d"), ("raw_1d_pos_rate", "1d pos%"),
            ("raw_5d_mean_pct", "Raw 5d"), ("raw_5d_pos_rate", "5d pos%"),
        ],
    )

    # === Prev 5d slope bucket(看缺口前是否強勢) ===
    if "prev5_slope_pct_per_day" in fires.columns:
        slope_buckets = [
            ("strong down", -float("inf"), -1.0),
            ("mild down",   -1.0, 0.0),
            ("flat / up",   0.0, 1.0),
            ("strong up",   1.0, float("inf")),
        ]
        print("\n## By Prev-5d Trend Slope (close / prev_close % per day,fire only)\n", flush=True)
        slope_rows: list[dict[str, Any]] = []
        for lab, lo, hi in slope_buckets:
            sub = fires[(fires["prev5_slope_pct_per_day"] >= lo) & (fires["prev5_slope_pct_per_day"] < hi)]
            slope_rows.append({"bucket": lab, **_summarize(sub, f"slope={lab}")})
        _print_md(
            slope_rows,
            [
                ("bucket", "Prev 5d slope"), ("n", "N"), ("win_rate", "WinRate"),
                ("sim_ev_pct", "Sim EV"),
                ("raw_1d_mean_pct", "Raw 1d"), ("raw_1d_pos_rate", "1d pos%"),
                ("raw_5d_mean_pct", "Raw 5d"), ("raw_5d_pos_rate", "5d pos%"),
            ],
        )

    # === 寫 JSON ===
    if args.out:
        payload = {
            "params": {
                "lookback_days": args.lookback_days,
                "target_pct": TARGET_PCT,
                "stop_pct": STOP_PCT,
                "hold_days": HOLD_DAYS,
                "gap_pct_min": DEFAULT_GAP_UP_PARAMS["gap_pct_min"],
                "gap_vol_ratio_min": DEFAULT_GAP_UP_PARAMS["gap_vol_ratio_min"],
                "panel_min_date": min_date,
                "panel_max_date": max_date,
                "fire_t_min_date": min_t_date,
                "fire_t_max_date": valid_t_max,
            },
            "overall_fire": overall_fire,
            "overall_baseline": overall_base,
            "by_gap": gap_rows,
            "by_vol": vol_rows,
            "by_regime": regime_rows,
            "regimes_counter": dict(regimes_counter),
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[DIAG] 寫 JSON → {args.out}", flush=True)

    # === 軍師結論線索 ===
    fire_wr = overall_fire["win_rate"]
    base_wr = overall_base["win_rate"]
    delta = (fire_wr - base_wr) * 100
    print(
        f"\n[DIAG] gap_up WR={fire_wr*100:.2f}% vs baseline WR={base_wr*100:.2f}% "
        f"→ delta={delta:+.2f} percentage points",
        flush=True,
    )
    print(
        f"[DIAG] gap_up Sim EV={overall_fire['sim_ev_pct']:+.2f}% "
        f"vs baseline {overall_base['sim_ev_pct']:+.2f}%",
        flush=True,
    )
    print(
        f"[DIAG] gap_up Raw 1d mean={overall_fire['raw_1d_mean_pct']:+.2f}% "
        f"vs baseline {overall_base['raw_1d_mean_pct']:+.2f}%",
        flush=True,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
