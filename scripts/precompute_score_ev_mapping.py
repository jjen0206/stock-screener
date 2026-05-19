"""週 cron 入口:重算 score → EV 校準表,dump 到 data/twse_snapshot/score_to_ev.csv。

從 `daily_picks.csv` (ml_prob) JOIN `pick_outcomes.csv` (return_d5)
切 10 個 quantile bucket → 每 bucket avg return = 該 bucket EV。

策略獨立 vs 全市場合一:
  - 樣本 >= 100 的策略 → 算該策略自己的 mapping
  - 全市場(包括所有策略)合成的 mapping 永遠存(strategy=__global__)

CLI:
    python scripts/precompute_score_ev_mapping.py
    python scripts/precompute_score_ev_mapping.py --quiet

Exit code:
    0 = 成功生成 mapping
    1 = 資料不足 / picks 或 outcomes CSV 缺失
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 讓本檔從任何 cwd 執行都能 import src.*
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from src import config  # noqa: E402
from src.score_to_ev import (  # noqa: E402
    build_score_to_ev_mapping,
    dump_mapping_to_csv,
    invalidate_cache,
)

logger = logging.getLogger("precompute_score_ev")


def main() -> int:
    parser = argparse.ArgumentParser(description="Precompute score → EV mapping")
    parser.add_argument("--quiet", action="store_true", help="只印錯誤")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    snapshot_dir = config.PROJECT_ROOT / "data" / "twse_snapshot"
    picks_csv = snapshot_dir / "daily_picks.csv"
    outcomes_csv = snapshot_dir / "pick_outcomes.csv"

    if not picks_csv.exists():
        logger.error("daily_picks.csv 不存在:%s", picks_csv)
        return 1
    if not outcomes_csv.exists():
        logger.error("pick_outcomes.csv 不存在:%s", outcomes_csv)
        return 1

    picks = pd.read_csv(picks_csv, dtype={"sid": str})
    outcomes = pd.read_csv(outcomes_csv, dtype={"sid": str})
    logger.info("picks=%d rows, outcomes=%d rows", len(picks), len(outcomes))

    mapping = build_score_to_ev_mapping(picks, outcomes)
    if mapping.empty:
        logger.error("Mapping 空 — 樣本不足 (joined < 30 rows)")
        # 仍寫空 CSV(讓 loader 走 fallback,不會炸)
        out_path = dump_mapping_to_csv(mapping, snapshot_dir=snapshot_dir)
        logger.info("空 mapping 寫到:%s", out_path)
        return 1

    out_path = dump_mapping_to_csv(mapping, snapshot_dir=snapshot_dir)
    invalidate_cache()

    # 摘要
    n_strats = mapping["strategy"].nunique()
    total_samples = int(mapping["n_samples"].sum())
    logger.info(
        "Mapping 寫到 %s — %d 策略(含 __global__),%d 樣本",
        out_path, n_strats, total_samples,
    )

    # 印一行 sample for 觀察(global mapping head/tail)
    g = mapping[mapping["strategy"] == "__global__"]
    if not g.empty:
        for _, r in g.iterrows():
            logger.info(
                "  [__global__] score %.3f-%.3f → EV %+.2f%% (n=%d)",
                max(r["bucket_lo"], -1.0),
                min(r["bucket_hi"], 2.0),
                r["avg_ev"] * 100,
                int(r["n_samples"]),
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
