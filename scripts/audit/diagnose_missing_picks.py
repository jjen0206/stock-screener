"""throwaway 診斷:對指定股票檢查為何沒出現在短線推薦。

對每個 sid 跑:
  1. 是否在 pure_stock_universe(min_history=20)
  2. daily_prices 涵蓋(rows / 最新日期 / MIN_HISTORY=45 是否夠)
  3. 11 個策略當日哪幾個 fire
  4. ML 過濾結果(routing strategy / threshold / ml_prob / pass-fail)

執行:
    python scripts/audit/diagnose_missing_picks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.ml_predictor import (  # noqa: E402
    MIN_HISTORY_DAYS, load_model, load_strategy_model, predict_for_strategy,
)
from src.strategies import (  # noqa: E402
    STRATEGY_LABELS, STRATEGY_ML_THRESHOLDS,
    run_all_strategies,
)
from src.universe import pure_stock_universe  # noqa: E402


SIDS_TO_DIAGNOSE = ["3680", "3711"]


def _stock_name(sid: str) -> str:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM stocks WHERE stock_id=?", (sid,),
        ).fetchone()
    return row["name"] if row and row["name"] else "(unknown)"


def _routing_strategy(matched: list[str]) -> str | None:
    """跟 app.py 的 _routing_strategy_for_pick 同邏輯:取最嚴格 threshold strategy。"""
    candidates = [
        (s, STRATEGY_ML_THRESHOLDS[s])
        for s in matched
        if STRATEGY_ML_THRESHOLDS.get(s) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda kv: kv[1])[0]


def _strictest_threshold(matched: list[str]) -> float | None:
    """跟 app.py 的 _per_strategy_threshold_for_pick 同邏輯。"""
    ths = [
        STRATEGY_ML_THRESHOLDS[s]
        for s in matched if STRATEGY_ML_THRESHOLDS.get(s) is not None
    ]
    return max(ths) if ths else None


def _check_data_coverage(sid: str, period_end: str) -> dict:
    """daily_prices rows / 最新日期 / 是否 >= MIN_HISTORY / 是否有 period_end 那天。"""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(date) AS latest, MIN(date) AS earliest "
            "FROM daily_prices WHERE stock_id=?",
            (sid,),
        ).fetchone()
        has_period_end = conn.execute(
            "SELECT 1 FROM daily_prices WHERE stock_id=? AND date=? LIMIT 1",
            (sid, period_end),
        ).fetchone() is not None
        inst_n = conn.execute(
            "SELECT COUNT(*) AS n FROM institutional WHERE stock_id=?",
            (sid,),
        ).fetchone()["n"]
        inst_recent = conn.execute(
            "SELECT MAX(date) AS latest FROM institutional WHERE stock_id=?",
            (sid,),
        ).fetchone()
    return {
        "n": row["n"] if row else 0,
        "latest": row["latest"] if row else None,
        "earliest": row["earliest"] if row else None,
        "has_period_end_data": has_period_end,
        "min_required": MIN_HISTORY_DAYS,
        "enough_for_ml": (row["n"] or 0) >= MIN_HISTORY_DAYS,
        "inst_n": inst_n,
        "inst_latest": inst_recent["latest"] if inst_recent else None,
    }


def diagnose(sid: str, period_end: str, universe: list[str], general_model) -> None:
    name = _stock_name(sid)
    print("\n" + "=" * 70, flush=True)
    print(f"📋 診斷 {sid} {name}", flush=True)
    print("=" * 70, flush=True)

    # 1. universe 檢查
    in_universe = sid in set(universe)
    print("\n[1] 是否在 default universe(pure_stock, ≥20 天歷史)?", flush=True)
    print(f"    → {'✅ 是' if in_universe else '❌ 否'}", flush=True)

    # 2. 資料涵蓋
    cov = _check_data_coverage(sid, period_end)
    print("\n[2] daily_prices 涵蓋:", flush=True)
    print(
        f"    → rows={cov['n']}, range={cov['earliest']} → {cov['latest']}, "
        f"min_required(ML)={cov['min_required']} "
        f"{'✅ 足' if cov['enough_for_ml'] else '❌ 不足'}",
        flush=True,
    )
    print(
        f"    → 有 {period_end} 當天的價格?"
        f"{'✅ 有' if cov['has_period_end_data'] else '❌ 沒有(latest='+ str(cov['latest']) +')'}",
        flush=True,
    )
    print(
        f"    → institutional rows={cov['inst_n']},latest={cov['inst_latest']}",
        flush=True,
    )

    if not in_universe:
        print(
            f"\n  ⛔ 結論:{sid} {name} 不在 universe → 短線頁直接看不到。",
            flush=True,
        )
        return

    # 致命檢查:沒 period_end 當天資料 → 9/11 策略要求 df.date.iloc[-1] == period_end
    if not cov["has_period_end_data"]:
        gap_days = "N/A"
        try:
            from datetime import date as _date
            d1 = _date.fromisoformat(period_end)
            d2 = _date.fromisoformat(cov["latest"]) if cov["latest"] else None
            if d2 is not None:
                gap_days = (d1 - d2).days
        except Exception:  # noqa: BLE001
            pass
        print(
            f"\n  ⛔ 主因找到了:{sid} {name} 在 SQLite 內最新一筆價格是 "
            f"{cov['latest']},距離 period_end({period_end})有 {gap_days} 天落差。",
            flush=True,
        )
        print(
            "     → 11 策略中 9 個要求 df.date.iloc[-1] == period_end "
            "(精確當日)否則直接 return None,所以全沒 fire。",
            flush=True,
        )
        print(
            f"     → 修法:跑 daily_fetch backfill 把 {sid} 的最新價格"
            f"補進 daily_prices(可能 FinMind 漏抓 / 沒抓最新一週)。",
            flush=True,
        )
        # 不 return,繼續跑診斷看 strategy 流程確認

    # 3. 11 策略哪幾個 fire(只跑 [sid] universe 加快速度)
    print(f"\n[3] 11 策略當日({period_end})fire 結果:", flush=True)
    agg = run_all_strategies(period_end, stock_ids=[sid])
    sid_info = agg.get(sid)
    if not sid_info:
        print("    → ❌ 沒任何策略 fire", flush=True)
        print(
            f"\n  ⛔ 結論:{sid} {name} 在 universe 但 11 策略全沒 fire → "
            "選股條件都不符合。",
            flush=True,
        )
        return

    matched = list((sid_info.get("details") or {}).keys())
    print(f"    → ✅ {len(matched)} 個策略 fire:", flush=True)
    for s in matched:
        print(f"       - {s} ({STRATEGY_LABELS.get(s, s)})", flush=True)

    # 4. ML filter:routing strategy + ml_prob + threshold
    print("\n[4] ML 過濾(高信心模式)診斷:", flush=True)
    routing = _routing_strategy(matched)
    threshold = _strictest_threshold(matched)

    if routing is None:
        print(
            f"    → 命中策略 [{', '.join(matched)}] **全不在** "
            f"STRATEGY_ML_THRESHOLDS dict",
            flush=True,
        )
        print(
            "    → 高信心模式不過濾 → 該 pick 應該會出現!",
            flush=True,
        )
        print(
            f"\n  ⚠️ 異常:{sid} {name} 應該會在短線頁顯示,可能是 UI Top N "
            f"截斷或其他原因(看一下短線頁 ranking)。",
            flush=True,
        )
        return

    # 跑 per-strategy model 預測
    sm = load_strategy_model(routing)
    sm_status = "trained pkl 存在" if sm is not None else "fallback 走通用模型"
    probs = predict_for_strategy(
        strategy_name=routing,
        stock_ids=[sid],
        target_date=period_end,
        fallback_model=general_model,
        strategy_model=sm,
    )
    ml_prob = probs.get(sid)

    print(
        f"    → routing strategy = {routing} "
        f"(strictest threshold {threshold:.2f},"
        f"{sm_status})",
        flush=True,
    )
    if ml_prob is None:
        print(
            "    → ml_prob = None ❌(features 不足或 predict 失敗)",
            flush=True,
        )
        print(
            f"    → ml_prob None + threshold {threshold:.2f} 存在 → "
            "**過濾掉**",
            flush=True,
        )
        print(
            f"\n  ⛔ 結論:{sid} {name} 觸發 {len(matched)} 個策略,但 "
            f"ML 模型算不出機率(features 不足或資料異常),"
            f"被 {routing} 門檻 ≥ {threshold:.2f} 過濾掉。",
            flush=True,
        )
    else:
        passed = ml_prob >= threshold
        verdict = "✅ 通過" if passed else "❌ 過濾"
        print(
            f"    → ml_prob = {ml_prob:.3f}(門檻 {threshold:.2f}) "
            f"{verdict}",
            flush=True,
        )
        # 同時印每個 fired strategy 各自門檻給對照
        print("    → 各策略門檻對照:", flush=True)
        for s in matched:
            t = STRATEGY_ML_THRESHOLDS.get(s)
            t_str = f"{t:.2f}" if t is not None else "(無)"
            print(f"       - {s}: threshold={t_str}", flush=True)
        if passed:
            print(
                f"\n  ✅ 結論:{sid} {name} 應該會出現在短線推薦。"
                f"若沒看到,可能是 UI Top N 截斷(預設只顯前幾名)。",
                flush=True,
            )
        else:
            print(
                f"\n  ⛔ 結論:{sid} {name} 觸發 {len(matched)} 個策略,"
                f"但 ML 機率 {ml_prob:.3f} **<** routing 門檻 "
                f"{threshold:.2f}({routing} {STRATEGY_LABELS.get(routing, routing)})"
                f",高信心模式被擋掉。\n"
                f"     關掉「高信心模式」toggle 就會看到這檔。",
                flush=True,
            )


def main() -> int:
    db.init_db()
    counts = db.preload_snapshots()
    if counts:
        print(f"[DIAG] preload: {counts}", flush=True)

    period_end = db.get_latest_trading_date()
    if not period_end:
        print("[DIAG] daily_prices 表空", flush=True)
        return 1
    print(f"[DIAG] period_end={period_end}", flush=True)

    universe = pure_stock_universe(min_history=20)
    print(f"[DIAG] universe size = {len(universe)}", flush=True)

    # 通用 model 給 fallback
    model_path = config.PROJECT_ROOT / "models" / "short_pick.pkl"
    general_model = load_model(model_path) if model_path.exists() else None
    if general_model is None:
        print("[DIAG] ⚠ general short_pick.pkl 載入失敗,fallback 路徑會回 None", flush=True)

    for sid in SIDS_TO_DIAGNOSE:
        diagnose(sid, period_end, universe, general_model)

    return 0


if __name__ == "__main__":
    sys.exit(main())
