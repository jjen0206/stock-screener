"""M2 Phase 2:per_strategy v1 → v3 OOB-based A/B + 個別 rollback。

每個 strategy 比較 new meta(v3 16-feat)vs .v1.bak meta(v1 11-feat):
- 都 trained → 比 OOB,new < old - tolerance → rollback 該 strategy
- v1 trained / v3 fallback → rollback(v3 樣本不足倒退)
- v1 fallback / v3 trained → 留 v3(改善)
- 都 fallback → no-op

OOB tolerance:0.02(跟 short_pick 同口徑)。

Output:per_strategy_ab_summary.json + per-strategy ROLLBACK / KEEP log。

Exit code 0(永遠成功;rollback 也算 graceful)。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.strategies import ALL_STRATEGIES  # noqa: E402

OOB_TOLERANCE = 0.02
PER_STRATEGY_DIR = _ROOT / "models" / "per_strategy"
SUMMARY_PATH = PER_STRATEGY_DIR / "ab_summary.json"


def _read_meta(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    rows: list[dict] = []
    rollback_count = 0
    keep_count = 0

    for strategy in ALL_STRATEGIES.keys():
        pkl = PER_STRATEGY_DIR / f"{strategy}.pkl"
        meta = PER_STRATEGY_DIR / f"{strategy}.meta.json"
        pkl_bak = PER_STRATEGY_DIR / f"{strategy}.pkl.v1.bak"
        meta_bak = PER_STRATEGY_DIR / f"{strategy}.meta.json.v1.bak"

        new_meta = _read_meta(meta)
        old_meta = _read_meta(meta_bak)

        new_status = (new_meta or {}).get("status", "missing")
        old_status = (old_meta or {}).get("status", "missing")
        new_oob = (new_meta or {}).get("oob_score")
        old_oob = (old_meta or {}).get("oob_score")
        new_samples = (new_meta or {}).get("samples", 0)
        old_samples = (old_meta or {}).get("samples", 0)

        decision = "keep"
        reason = ""

        if new_status == "trained" and old_status == "trained":
            diff = (new_oob or 0.0) - (old_oob or 0.0)
            if diff >= -OOB_TOLERANCE:
                decision = "keep"
                reason = (
                    f"OOB {old_oob:.4f} -> {new_oob:.4f} "
                    f"(Δ={diff:+.4f}) within tolerance"
                )
                keep_count += 1
            else:
                decision = "rollback"
                reason = (
                    f"OOB {old_oob:.4f} -> {new_oob:.4f} "
                    f"(Δ={diff:+.4f}) > -{OOB_TOLERANCE}"
                )
                rollback_count += 1
        elif new_status == "fallback" and old_status == "trained":
            decision = "rollback"
            reason = (
                f"v3 fallback (samples {new_samples} < threshold),"
                f"v1 was trained — keep v1"
            )
            rollback_count += 1
        elif new_status == "trained" and old_status in ("fallback", "missing"):
            decision = "keep"
            reason = f"v3 newly trained (samples {new_samples}); v1 was {old_status}"
            keep_count += 1
        elif new_status == "fallback" and old_status in ("fallback", "missing"):
            decision = "keep"
            reason = "both fallback — no-op"
            keep_count += 1
        else:
            decision = "keep"
            reason = f"unknown combo (new={new_status} / old={old_status})"
            keep_count += 1

        # Execute rollback
        if decision == "rollback":
            if pkl_bak.exists():
                shutil.copy2(pkl_bak, pkl)
            elif pkl.exists():
                # v1 was fallback (no pkl) and v3 we want to drop too
                pkl.unlink()
            if meta_bak.exists():
                shutil.copy2(meta_bak, meta)
            elif meta.exists():
                meta.unlink()

        rows.append({
            "strategy": strategy,
            "v1_status": old_status,
            "v1_oob": old_oob,
            "v1_samples": old_samples,
            "v3_status": new_status,
            "v3_oob": new_oob,
            "v3_samples": new_samples,
            "decision": decision,
            "reason": reason,
        })

        old_oob_str = "n/a" if old_oob is None else f"{old_oob:.3f}"
        new_oob_str = "n/a" if new_oob is None else f"{new_oob:.3f}"
        line = (
            f"[AB-PS] {strategy:<24s} "
            f"v1={old_status:<8s}({old_oob_str:>5}) "
            f"v3={new_status:<8s}({new_oob_str:>5}) "
            f"-> {decision.upper():<8s} {reason}"
        )
        print(line, flush=True)

    SUMMARY_PATH.write_text(
        json.dumps({
            "tolerance": OOB_TOLERANCE,
            "rollback_count": rollback_count,
            "keep_count": keep_count,
            "rows": rows,
        }, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        f"[AB-PS] === DONE === keep={keep_count}, rollback={rollback_count} "
        f"(summary -> {SUMMARY_PATH})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
