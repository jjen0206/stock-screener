"""排程入口:預跑 default params 全策略 × 多 universe,結果寫 daily_picks 表。

跑時機:nightly workflow 內(daily-notify.yml)放在 weekly_market_update 之後、
daily_notify 之前。雲端 Streamlit Cloud 容器 boot 時透過 snapshot CSV preload
拿到結果,App 端 _run_all_strategies_cached 命中即 0ms 回。

Universe(default params 共 3 種):
- pure_stock:純股票(過濾 ETF / 債券 / 槓反)+ 歷史 ≥20 天 — dashboard / 短線
- with_etf:全股(含 ETF / 債券)+ 歷史 ≥20 天 — 短線「📊 含 ETF」option
- top_50:TW_TOP_50 hardcoded list — 短線「快速:50 檔大型股」option

User 改 sliders → params_hash 不是 default_v1 → App 端 fallback 走 runtime
(此腳本只覆蓋 default 路徑,但是熱路徑 99% 用得到)。

Exit code:
    0 = 成功(全 universe 都跑完 + dump_daily_picks 至少一筆)
    1 = 全失敗(罕見;通常代表 SQLite 沒歷史資料)

CLI:
    python scripts/precompute_strategies.py                # 跑當日(latest trading date)
    python scripts/precompute_strategies.py --date 2026-05-04
    python scripts/precompute_strategies.py --backfill 30  # 倒推 30 天(Part 4 加)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.strategies import run_all_strategies  # noqa: E402
from src.universe import (  # noqa: E402
    TW_TOP_50, is_pure_stock, pure_stock_universe,
)


# Default params 對應「user 沒改任何 slider」的場景。
# 留 None 等 run_all_strategies 走各策略 DEFAULT_*_PARAMS。
DEFAULT_PARAMS_HASH = "default_v1"


def _compute_ml_probs(
    sids: list[str],
    trade_date: str,
    agg: dict[str, dict] | None = None,
) -> dict[str, float | None]:
    """跑 ML batch 預測對每 sid 算 prob_up;model 缺 / 載 fail → 空 dict。

    Stage 1 Part 2:每天預跑時順便算,寫進 daily_picks.ml_prob 欄,雲端 App
    直接吃 cache 不用每次重算 ~50 picks × 50ms predict_proba。

    Stage 2B:給 agg → 走 per-strategy model 路由(每 sid 用其最嚴格 threshold
    對應的 model;沒就 fallback 通用)。沒給 agg → 全 sids 走通用 model
    (Stage 1 行為)。
    """
    if not sids:
        return {}
    from src.ml_predictor import (
        load_model, predict_batch, predict_for_strategy, load_strategy_model,
    )
    from src.strategies import STRATEGY_ML_THRESHOLDS

    general_model_path = config.PROJECT_ROOT / "models" / "short_pick.pkl"
    general_model = None
    if general_model_path.exists():
        try:
            general_model = load_model(general_model_path)
        except Exception as e:  # noqa: BLE001
            print(
                f"[PRECOMPUTE] general model load 失敗: {type(e).__name__}: {e}",
                flush=True,
            )

    if general_model is None and not agg:
        # 沒通用 model + 沒 agg → 沒法路由,全 NULL
        print(
            f"[PRECOMPUTE] general model 不可用 + 沒 agg → ml_prob 全 NULL",
            flush=True,
        )
        return {}

    # 沒 agg → 退舊路徑,全 sids 走通用 model 一次
    if agg is None:
        try:
            return predict_batch(general_model, sids, trade_date)
        except Exception as e:  # noqa: BLE001
            print(
                f"[PRECOMPUTE] predict_batch (general) 失敗: {type(e).__name__}: {e}",
                flush=True,
            )
            return {}

    # 有 agg → per-strategy 路由,sid → 最嚴格 threshold strategy_name(沒就 None)
    def _routing_strategy(sid: str) -> str | None:
        info = agg.get(sid, {}) or {}
        matched = list((info.get("details") or {}).keys())
        candidates = [
            (s, STRATEGY_ML_THRESHOLDS[s])
            for s in matched
            if STRATEGY_ML_THRESHOLDS.get(s) is not None
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda kv: kv[1])[0]

    sid_to_chosen: dict[str, str | None] = {sid: _routing_strategy(sid) for sid in sids}

    # GroupBy chosen → batch predict per group
    groups: dict[str | None, list[str]] = {}
    for sid, chosen in sid_to_chosen.items():
        groups.setdefault(chosen, []).append(sid)

    strategy_model_cache: dict[str, object] = {}
    out: dict[str, float | None] = {}
    for chosen, group_sids in groups.items():
        if chosen is None:
            sm = None
        else:
            if chosen not in strategy_model_cache:
                strategy_model_cache[chosen] = load_strategy_model(chosen)
            sm = strategy_model_cache[chosen]
        try:
            probs = predict_for_strategy(
                strategy_name=chosen,
                stock_ids=group_sids,
                target_date=trade_date,
                fallback_model=general_model,
                strategy_model=sm,
            )
            out.update(probs)
        except Exception as e:  # noqa: BLE001
            print(
                f"[PRECOMPUTE] predict_for_strategy({chosen}) 失敗: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )
    return out


def _with_etf_universe(min_history: int = 20) -> list[str]:
    """20+ 天歷史所有股(含 ETF / 債券)。對應 App 短線頁「📊 含 ETF」選項。"""
    sids_with_history = set(db.stocks_with_min_history(min_history))
    if not sids_with_history:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id FROM stocks WHERE market='TW' "
            "AND name IS NOT NULL AND name != '' "
            "ORDER BY stock_id"
        ).fetchall()
    return [r["stock_id"] for r in rows if r["stock_id"] in sids_with_history]


def _build_universes() -> dict[str, list[str]]:
    """組 3 個 universe(以當下 SQLite 內容為準)。"""
    return {
        "pure_stock": pure_stock_universe(min_history=20),
        "with_etf": _with_etf_universe(min_history=20),
        "top_50": [s for s, _ in TW_TOP_50],
    }


def precompute_for_date(trade_date: str) -> dict[str, int]:
    """跑 trade_date 的 default-params strategies × 3 universe,寫進 daily_picks。

    回 {universe_key: row_count_inserted}。
    """
    db.init_db()

    print(
        f"[PRECOMPUTE] target_date={trade_date} params_hash={DEFAULT_PARAMS_HASH}",
        flush=True,
    )

    # 重跑前先清掉這天的舊資料 — 避免 universe size 變了之後留遺漏
    cleared = db.clear_daily_picks_for_date(trade_date)
    if cleared:
        print(f"[PRECOMPUTE] 清掉舊 {cleared} 筆 daily_picks", flush=True)

    universes = _build_universes()
    results: dict[str, int] = {}

    for u_key, sids in universes.items():
        if not sids:
            print(f"[PRECOMPUTE] {u_key}: universe 空,跳過", flush=True)
            results[u_key] = 0
            continue

        t0 = time.perf_counter()
        agg = run_all_strategies(trade_date, stock_ids=sids)  # default params
        elapsed = time.perf_counter() - t0
        n_picks = len(agg)
        n_signal_rows = sum(
            len((info or {}).get("details", {})) for info in agg.values()
        )

        # ML 機率(per-pick)— Stage 1 Part 2:預跑就算好寫進表,雲端 App 直接吃。
        # Stage 2B:傳 agg → 走 per-strategy model 路由(對應 STRATEGY_ML_THRESHOLDS
        # 內最嚴格 threshold 的策略 model;沒則 fallback 通用)。
        # 若 model 不存在 / load fail → ml_probs 是空 dict,daily_picks ml_prob 欄全 NULL。
        ml_probs = _compute_ml_probs(list(agg.keys()), trade_date, agg=agg)
        n_with_ml = sum(1 for v in ml_probs.values() if v is not None)

        inserted = db.dump_daily_picks(
            trade_date, u_key, agg, params_hash=DEFAULT_PARAMS_HASH,
            ml_probs=ml_probs,
        )
        results[u_key] = inserted

        # 每策略命中數摘要
        per_strategy: dict[str, int] = {}
        for info in agg.values():
            for k in (info or {}).get("details", {}):
                per_strategy[k] = per_strategy.get(k, 0) + 1

        print(
            f"[PRECOMPUTE] {u_key:<12s} "
            f"sids={len(sids):<4d} picks={n_picks:<3d} "
            f"signals={n_signal_rows:<4d} ml_prob={n_with_ml}/{n_picks} "
            f"elapsed={elapsed:5.1f}s | "
            + ", ".join(f"{k}={v}" for k, v in sorted(per_strategy.items())),
            flush=True,
        )

    return results


def dump_daily_picks_csv(snapshot_dir: Path | None = None) -> int:
    """把 daily_picks 全表 dump 成 CSV(給 nightly workflow git push 用)。

    雲端容器 boot 時 preload_snapshots 會讀 daily_picks.csv 灌進 SQLite,
    App 端 _run_all_strategies_cached 即可命中。

    回 row count(無資料 → 不寫 CSV,回 0)。
    """
    import pandas as pd

    if snapshot_dir is None:
        snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT trade_date, universe, strategy, sid, score, rank, "
            "params_hash, payload, ml_prob, computed_at FROM daily_picks "
            "ORDER BY trade_date DESC, universe, strategy, sid"
        ).fetchall()

    if not rows:
        print("[PRECOMPUTE] daily_picks 表空,不 dump CSV", flush=True)
        return 0

    df = pd.DataFrame([dict(r) for r in rows])
    csv_path = snapshot_dir / "daily_picks.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(
        f"[PRECOMPUTE] dump CSV → {csv_path} ({len(rows)} rows)",
        flush=True,
    )
    return len(rows)


def _list_recent_trading_dates(n: int) -> list[str]:
    """從 daily_prices 撈最近 N 個交易日(去重 + 降序)。"""
    if n <= 0:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_prices "
            "WHERE stock_id != 'TAIEX' "
            "ORDER BY date DESC LIMIT ?",
            (n,),
        ).fetchall()
    return [r["date"] for r in rows]


def main() -> int:
    p = argparse.ArgumentParser(description="預跑 daily_picks(default params)")
    p.add_argument(
        "--date",
        help="目標日期 YYYY-MM-DD;留空 = SQLite 內 daily_prices MAX(date)",
    )
    p.add_argument(
        "--backfill",
        type=int,
        default=0,
        help="倒推 N 天:跑最近 N 個交易日(不能跟 --date 並用)",
    )
    args = p.parse_args()

    if args.backfill and args.date:
        print(
            "[PRECOMPUTE] --date 跟 --backfill 不可同用",
            flush=True,
        )
        return 1
    if args.backfill < 0:
        print("[PRECOMPUTE] --backfill 必須 ≥ 0", flush=True)
        return 1

    db.init_db()
    # GitHub Actions runner fresh container,要 preload snapshot CSV 確保有歷史
    preload = db.preload_snapshots()
    if preload:
        print(f"[PRECOMPUTE] preload snapshots: {preload}", flush=True)

    # 決定要跑的日期清單
    if args.backfill > 0:
        dates = _list_recent_trading_dates(args.backfill)
        if not dates:
            print(
                "[PRECOMPUTE] daily_prices 表空,backfill 沒日期可跑",
                flush=True,
            )
            return 1
        print(
            f"[PRECOMPUTE] backfill {len(dates)} 天: "
            f"{dates[-1]} ~ {dates[0]}",
            flush=True,
        )
    elif args.date:
        dates = [args.date]
    else:
        latest = db.get_latest_trading_date()
        if not latest:
            from datetime import date as _date
            latest = _date.today().isoformat()
            print(
                f"[PRECOMPUTE] 警告:SQLite 無歷史,用 today={latest}",
                flush=True,
            )
        dates = [latest]

    grand_total = 0
    for d in dates:
        results = precompute_for_date(d)
        grand_total += sum(results.values())

    # dump CSV 給 nightly workflow git push(雲端容器 preload 用)
    # backfill 模式也只 dump 一次 — CSV 包整個 daily_picks 表所有日期
    if grand_total > 0:
        dump_daily_picks_csv()

    print(
        f"[PRECOMPUTE] DONE total_rows={grand_total} dates={len(dates)}",
        flush=True,
    )
    return 0 if grand_total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
