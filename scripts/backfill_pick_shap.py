"""排程入口:對歷史 daily_picks 補算 SHAP top features,寫進 pick_shap_explanations。

背景
----
`pick_shap_explanations` 只有 daily_notify 走 `_enrich_picks_with_shap` 時即時算
+ 寫進 cache。但這要求那天有跑 daily_notify 推播;歷史 daily_picks(2026-04-30
~ 2026-05-04 那段)沒有對應 SHAP 解釋。

本 script 補歷史 SHAP — 對 daily_picks 內每筆 (sid, strategy) 補算對應的 SHAP
top-3 features,寫進 pick_shap_explanations(同 (pick_date, sid, strategy) 跳過,
idempotent)。

設計
----
跟 daily_notify `_enrich_picks_with_shap` 同 routing:per (sid, strategy):
  - 若 strategy ∈ STRATEGY_ML_THRESHOLDS → 用 per-strategy model
  - 否則 fallback general `models/short_pick.pkl`
  - feats = extract_features(sid, pick_date)
  - compute_pick_shap(sid, model, feats, top_k=3) → save_shap_explanation

CLI
---
::

    # default: 補近 90 天歷史
    python scripts/backfill_pick_shap.py

    # 指定區間
    python scripts/backfill_pick_shap.py --start 2026-04-30 --end 2026-05-14

    # 強制覆寫(默認跳過已存在的)
    python scripts/backfill_pick_shap.py --force

    # 限定前 N 筆 picks(debug)
    python scripts/backfill_pick_shap.py --limit 10

Exit code:
  0 = 成功(完成或無 todo)
  1 = 所有 picks 都失敗(model 缺 / shap 未裝 / extract_features 全 fail)
  2 = 參數錯誤
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = _ROOT / "data" / "twse_snapshot"


def _fetch_picks_to_backfill(
    start: str,
    end: str,
    force: bool,
    db_path: str | Path | None = None,
) -> list[tuple[str, str, str]]:
    """撈 [start, end] 範圍內 daily_picks (date, sid, strategy)。

    force=False → 跳掉 pick_shap_explanations 已有的 (date, sid, strategy)
    force=True → 全撈,讓 caller 重算覆寫

    回 list of (pick_date, sid, strategy) tuple(去重 + 穩定排序)。
    """
    with db.get_conn(db_path) as conn:
        if force:
            rows = conn.execute(
                """
                SELECT DISTINCT trade_date, sid, strategy
                FROM daily_picks
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date, sid, strategy
                """,
                (start, end),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT dp.trade_date, dp.sid, dp.strategy
                FROM daily_picks dp
                WHERE dp.trade_date BETWEEN ? AND ?
                  AND NOT EXISTS (
                      SELECT 1 FROM pick_shap_explanations pse
                      WHERE pse.pick_date = dp.trade_date
                        AND pse.sid = dp.sid
                        AND pse.strategy = dp.strategy
                  )
                ORDER BY dp.trade_date, dp.sid, dp.strategy
                """,
                (start, end),
            ).fetchall()
    return [(r["trade_date"], r["sid"], r["strategy"]) for r in rows]


def backfill_one(
    pick_date: str,
    sid: str,
    strategy: str,
    *,
    general_model,
    strategy_models: dict[str, object],
    strategy_ml_thresholds: dict[str, float],
    extract_features_fn,
    compute_pick_shap_fn,
    load_strategy_model_fn,
    db_path: str | Path | None = None,
) -> str:
    """補一筆 SHAP。

    Routing(同 _enrich_picks_with_shap):若 strategy 有 per-strategy model
    threshold → 用 per-strategy;否則 general。Model load 失敗 → general fallback;
    general 也沒 → skip。

    Returns one of:
      - "ok"        : SHAP 算成功,已 save_shap_explanation
      - "no_model"  : 兩個 model 都 None
      - "no_feats"  : extract_features 回 None / 拋例外
      - "no_shap"   : compute_pick_shap 回空 list(shap 沒裝 / shape 不認得)
    """
    chosen_model = None
    if strategy in strategy_ml_thresholds:
        if strategy not in strategy_models:
            strategy_models[strategy] = load_strategy_model_fn(strategy)
        chosen_model = strategy_models[strategy]
    model = chosen_model if chosen_model is not None else general_model
    strategy_key = strategy if chosen_model is not None else "general"

    if model is None:
        return "no_model"

    try:
        feats = extract_features_fn(sid, pick_date, db_path=db_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("[BACKFILL-SHAP] extract_features %s %s 失敗: %s",
                       sid, pick_date, e)
        return "no_feats"
    if not feats:
        return "no_feats"

    explanations = compute_pick_shap_fn(sid, model, feats, top_k=3)
    if not explanations:
        return "no_shap"

    db.save_shap_explanation(
        pick_date, sid, strategy_key, explanations, db_path=db_path,
    )
    return "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="補歷史 daily_picks 對應的 SHAP top-feature 解釋",
    )
    today = date.today()
    parser.add_argument(
        "--start", default=(today - timedelta(days=90)).isoformat(),
        help="起始日(ISO, 含)— 預設今天 -90 天",
    )
    parser.add_argument(
        "--end", default=today.isoformat(),
        help="終止日(ISO, 含)— 預設今天",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="強制覆寫(默認跳掉已存在的)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="只算前 N 筆(debug 用,0 = 全跑)",
    )
    parser.add_argument(
        "--progress-every", type=int, default=100,
        help="每 N 筆印一次進度(default 100)",
    )
    parser.add_argument(
        "--dump-csv", action="store_true",
        help="跑完後把 pick_shap_explanations 表 dump 進 "
             "data/twse_snapshot/pick_shap_explanations.csv",
    )
    args = parser.parse_args(argv)

    setup_file_logging("backfill_pick_shap")

    try:
        date.fromisoformat(args.start)
        date.fromisoformat(args.end)
    except ValueError as ex:
        print(f"❌ 日期格式錯誤: {ex}", file=sys.stderr, flush=True)
        return 2
    if args.start > args.end:
        print("❌ --start 必須 <= --end", file=sys.stderr, flush=True)
        return 2

    db.init_db()

    todo = _fetch_picks_to_backfill(args.start, args.end, args.force)
    if args.limit > 0:
        todo = todo[: args.limit]

    logger.info(
        "[BACKFILL-SHAP] 區間 %s ~ %s: 待補 %d 筆 (force=%s)",
        args.start, args.end, len(todo), args.force,
    )
    print(
        f"[BACKFILL-SHAP] 區間 {args.start} ~ {args.end}: 待補 {len(todo)} "
        f"筆 (force={args.force})",
        flush=True,
    )

    if not todo:
        print("[BACKFILL-SHAP] 無待補,直接結束", flush=True)
        if args.dump_csv:
            _dump_csv()
        return 0

    # Lazy import(讓參數錯誤 / 無 todo 時不用 import heavy modules)
    try:
        from src.ml_predictor import (
            extract_features, load_model, load_strategy_model,
        )
        from src.ml_shap import compute_pick_shap
        from src.strategies import STRATEGY_ML_THRESHOLDS
    except ImportError as e:
        print(f"❌ import 失敗: {e}", file=sys.stderr, flush=True)
        return 1

    general_path = Path(config.PROJECT_ROOT) / "models" / "short_pick.pkl"
    general_model = load_model(general_path) if general_path.exists() else None
    if general_model is None:
        print(
            "[BACKFILL-SHAP] ⚠ 通用 models/short_pick.pkl 缺,"
            "只有 per-strategy 模型有的策略才能算 SHAP",
            flush=True,
        )

    strategy_models: dict[str, object] = {}

    t0 = time.time()
    counts = {"ok": 0, "no_model": 0, "no_feats": 0, "no_shap": 0, "error": 0}

    for i, (pick_date, sid, strategy) in enumerate(todo, start=1):
        try:
            status = backfill_one(
                pick_date, sid, strategy,
                general_model=general_model,
                strategy_models=strategy_models,
                strategy_ml_thresholds=STRATEGY_ML_THRESHOLDS,
                extract_features_fn=extract_features,
                compute_pick_shap_fn=compute_pick_shap,
                load_strategy_model_fn=load_strategy_model,
            )
            counts[status] = counts.get(status, 0) + 1
        except Exception as e:  # noqa: BLE001
            counts["error"] = counts.get("error", 0) + 1
            if counts["error"] <= 5:
                logger.warning(
                    "[BACKFILL-SHAP] %s %s %s 例外: %s: %s",
                    pick_date, sid, strategy, type(e).__name__, e,
                )

        if i % args.progress_every == 0 or i == len(todo):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(todo) - i) / rate / 60 if rate > 0 else 0
            print(
                f"[BACKFILL-SHAP] 進度 {i}/{len(todo)} "
                f"ok={counts['ok']} no_model={counts['no_model']} "
                f"no_feats={counts['no_feats']} no_shap={counts['no_shap']} "
                f"err={counts['error']} ETA={eta:.1f}m",
                flush=True,
            )

    elapsed = time.time() - t0
    print(
        f"[BACKFILL-SHAP DONE] 耗時 {elapsed/60:.1f} 分鐘,"
        f"ok={counts['ok']} skipped(no_model/feats/shap)="
        f"{counts['no_model'] + counts['no_feats'] + counts['no_shap']} "
        f"err={counts['error']}",
        flush=True,
    )
    logger.info(
        "[BACKFILL-SHAP DONE] 耗時 %.1f 分鐘, %s",
        elapsed / 60, counts,
    )

    if args.dump_csv:
        _dump_csv()

    if counts["ok"] == 0 and counts["error"] >= len(todo):
        return 1
    return 0


def _dump_csv() -> int:
    """把 pick_shap_explanations 全表 dump 進 SNAPSHOT_DIR/pick_shap_explanations.csv。

    回 row count。空表 → 不寫檔,回 0。
    """
    import pandas as pd

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with db.get_conn() as conn:
        df = pd.read_sql(
            "SELECT pick_date, sid, strategy, top_features, generated_at "
            "FROM pick_shap_explanations "
            "ORDER BY pick_date DESC, sid, strategy",
            conn,
        )
    if df.empty:
        print(
            "[BACKFILL-SHAP] pick_shap_explanations 表空,不 dump CSV",
            flush=True,
        )
        return 0
    out = SNAPSHOT_DIR / "pick_shap_explanations.csv"
    df.to_csv(out, index=False)
    print(f"[BACKFILL-SHAP] 寫 {out.name}: {len(df)} 行", flush=True)
    return len(df)


if __name__ == "__main__":
    sys.exit(main())
