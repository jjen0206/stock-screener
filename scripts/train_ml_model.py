"""訓練短線勝率預測模型(RandomForestClassifier)→ 存 models/short_pick.pkl。

用法:
    python scripts/train_ml_model.py
    python scripts/train_ml_model.py --universe top50  # 預設,~ 60 檔
    python scripts/train_ml_model.py --universe full   # 全市場(慢,僅供 ad-hoc 用)
    python scripts/train_ml_model.py --output models/short_pick.pkl

輸出:
- 模型 pkl 進 models/short_pick.pkl
- console 印 accuracy / precision / recall / F1 + 整體 win rate
- exit 0 = 訓練成功;exit 1 = 資料太少 train 不起來
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src import ml_predictor  # noqa: E402
from src.universe import TW_TOP_50  # noqa: E402

DEFAULT_OUTPUT = _ROOT / "models" / "short_pick.pkl"


def main() -> int:
    p = argparse.ArgumentParser(description="訓練短線勝率預測模型")
    p.add_argument(
        "--universe", choices=["top50", "full", "watchlist"],
        default="top50",
        help="訓練 universe(預設 top50,~60 檔約 1-2 分鐘)",
    )
    p.add_argument(
        "--output", default=str(DEFAULT_OUTPUT),
        help=f"輸出 pkl 路徑(預設 {DEFAULT_OUTPUT})",
    )
    args = p.parse_args()

    db.init_db()
    # GitHub Actions / 本機 fresh container 都先 preload snapshot 確保 SQLite
    # 有歷史 daily_prices(否則 sample 不夠 train)
    counts = db.preload_snapshots()
    if counts:
        print(f"[TRAIN] preload snapshots: {counts}", flush=True)

    if args.universe == "top50":
        sids = [s for s, _ in TW_TOP_50]
    elif args.universe == "watchlist":
        items = db.get_watchlist()
        sids = [it["stock_id"] for it in items] or [s for s, _ in TW_TOP_50]
    else:  # full
        with db.get_conn() as conn:
            sids = [
                r["stock_id"] for r in conn.execute(
                    "SELECT DISTINCT stock_id FROM daily_prices "
                    "WHERE stock_id != 'TAIEX' "
                    "ORDER BY stock_id"
                ).fetchall()
            ]

    print(f"[TRAIN] universe = {len(sids)} 檔", flush=True)
    print("[TRAIN] 構造訓練資料(sliding window features + label)...", flush=True)
    t0 = time.time()
    X, y = ml_predictor.build_training_dataset(stock_ids=sids)
    elapsed = time.time() - t0
    print(
        f"[TRAIN] dataset 構造完成:{len(X)} samples × {len(X.columns)} features"
        f" / win rate {y.mean():.1%} / 耗時 {elapsed:.1f}s",
        flush=True,
    )

    if len(X) < 50:
        print(
            f"❌ 訓練資料太少({len(X)} samples),最少需 50 筆。"
            "可能原因:SQLite 內 daily_prices 歷史不足(需 60+ 天)。",
            flush=True,
        )
        return 1

    print("[TRAIN] 訓練 RandomForestClassifier...", flush=True)
    t0 = time.time()
    model, metrics = ml_predictor.train_short_pick_model(X, y)
    elapsed = time.time() - t0
    print(f"[TRAIN] 訓練完成,耗時 {elapsed:.1f}s", flush=True)
    print("[TRAIN] === 評估指標(8:2 train/test split) ===", flush=True)
    print(f"  整體 win rate: {metrics['win_rate_overall']:.1%}", flush=True)
    print(f"  訓練樣本:    {metrics['n_train']}", flush=True)
    print(f"  測試樣本:    {metrics['n_test']}", flush=True)
    print(f"  Accuracy:     {metrics['accuracy']:.3f}", flush=True)
    print(f"  Precision:    {metrics['precision']:.3f}", flush=True)
    print(f"  Recall:       {metrics['recall']:.3f}", flush=True)
    print(f"  F1:           {metrics['f1']:.3f}", flush=True)

    # 特徵重要度
    importances = sorted(
        zip(ml_predictor.FEATURE_NAMES, model.feature_importances_),
        key=lambda kv: -kv[1],
    )
    print("[TRAIN] === Feature importance(top 5) ===", flush=True)
    for name, imp in importances[:5]:
        print(f"  {name:<20} {imp:.3f}", flush=True)

    out_path = Path(args.output)
    ml_predictor.save_model(model, out_path)
    print(f"[TRAIN] 模型已存 {out_path}", flush=True)

    # dump sidecar metadata 給「⚙️ 系統」頁顯示
    meta_path = ml_predictor.dump_model_meta(
        out_path,
        metrics=metrics,
        feature_names=ml_predictor.FEATURE_NAMES,
    )
    print(f"[TRAIN] Metadata 已存 {meta_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
