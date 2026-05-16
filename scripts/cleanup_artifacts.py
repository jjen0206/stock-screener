"""清理本地產出物:vacuum SQLite cache、刪過期 log、刪過期 model backup。

用途
----
這支腳本是**個人工具**,設計給主公在本機手動跑(或塞 cron),
**不會**自動跑在 GitHub Actions(避免 runner 上的 cache 被誤刪)。

預設 dry-run:只列出會動的檔案,不真刪。加 ``--execute`` 才動真的。

使用範例
--------
::

    # 看會刪哪些(預設 dry-run)
    python scripts/cleanup_artifacts.py

    # 真的執行
    python scripts/cleanup_artifacts.py --execute

    # 只 vacuum DB,不動 log/model
    python scripts/cleanup_artifacts.py --vacuum-only --execute

    # 改門檻
    python scripts/cleanup_artifacts.py --log-days 14 --model-days 60 --execute

清理規則
--------
1. **VACUUM ``data/cache.db``** — 釋放 SQLite delete 後遺留的 page space
2. **移除 ``logs/*.log``** 修改時間 > ``--log-days`` 天(預設 7)
3. **移除 ``models/**/*.bak``** 修改時間 > ``--model-days`` 天(預設 30)
   - 涵蓋 ``*.v1.bak`` / ``*.v2.bak`` / ``*.pre_retrain.bak`` / ``*.v3.candidate``
   - 一律保留**最新一份** backup(即使超齡也不刪),避免 retrain 失敗無 fallback
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_DB = REPO_ROOT / "data" / "cache.db"
LOGS_DIR = REPO_ROOT / "logs"
MODELS_DIR = REPO_ROOT / "models"

MODEL_BACKUP_PATTERNS = ["*.v1.bak", "*.v2.bak", "*.pre_retrain.bak", "*.v3.candidate"]


def fmt_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} TB"


def vacuum_cache_db(execute: bool) -> None:
    if not CACHE_DB.exists():
        print(f"[vacuum] {CACHE_DB} 不存在,跳過")
        return
    before = CACHE_DB.stat().st_size
    print(f"[vacuum] {CACHE_DB} 當前大小: {fmt_size(before)}")
    if not execute:
        print("[vacuum] (dry-run) 會 VACUUM,實際大小變化要 --execute 才知道")
        return
    conn = sqlite3.connect(CACHE_DB)
    try:
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    after = CACHE_DB.stat().st_size
    diff = before - after
    sign = "-" if diff > 0 else "+"
    print(
        f"[vacuum] 完成,新大小: {fmt_size(after)} "
        f"({sign}{fmt_size(abs(diff))})"
    )


def cleanup_logs(log_days: int, execute: bool) -> None:
    if not LOGS_DIR.exists():
        print(f"[logs] {LOGS_DIR} 不存在,跳過")
        return
    cutoff = time.time() - log_days * 86400
    candidates = [p for p in LOGS_DIR.rglob("*.log") if p.is_file() and p.stat().st_mtime < cutoff]
    if not candidates:
        print(f"[logs] {LOGS_DIR} 內無 > {log_days} 天的 .log")
        return
    total = sum(p.stat().st_size for p in candidates)
    print(f"[logs] 找到 {len(candidates)} 個 > {log_days} 天的 log (合計 {fmt_size(total)}):")
    for p in sorted(candidates):
        age = (time.time() - p.stat().st_mtime) / 86400
        print(f"  - {p.relative_to(REPO_ROOT)} ({age:.1f} 天前, {fmt_size(p.stat().st_size)})")
    if execute:
        for p in candidates:
            p.unlink()
        print(f"[logs] 已刪 {len(candidates)} 檔")
    else:
        print("[logs] (dry-run) 加 --execute 才真刪")


def cleanup_model_backups(model_days: int, execute: bool) -> None:
    if not MODELS_DIR.exists():
        print(f"[models] {MODELS_DIR} 不存在,跳過")
        return
    cutoff = time.time() - model_days * 86400
    # 找所有 backup 檔
    all_backups: list[Path] = []
    for pat in MODEL_BACKUP_PATTERNS:
        all_backups.extend(MODELS_DIR.rglob(pat))
    if not all_backups:
        print(f"[models] {MODELS_DIR} 內無 backup 檔 ({', '.join(MODEL_BACKUP_PATTERNS)})")
        return
    # 按 (主檔 stem) 分組:同一個 model 的多份 backup,保留 mtime 最新那份
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in all_backups:
        # 取掉 .bak / .candidate 後的 stem 當 group key(不含 .v1/.v2 區隔)
        # 例:short_pick.v1.bak / short_pick.v2.bak / short_pick.pre_retrain.bak 同組
        key = str(p.parent / p.name.split(".")[0])
        groups[key].append(p)
    candidates: list[Path] = []
    for key, files in groups.items():
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        latest = files[0]
        for f in files[1:]:
            if f.stat().st_mtime < cutoff:
                candidates.append(f)
        # latest 永遠保留(safety net)
        if latest in candidates:
            candidates.remove(latest)
    if not candidates:
        print(
            f"[models] 無 > {model_days} 天的 backup 可刪(每組最新一份永遠保留)"
        )
        return
    total = sum(p.stat().st_size for p in candidates)
    print(
        f"[models] 找到 {len(candidates)} 個 > {model_days} 天的 backup "
        f"(合計 {fmt_size(total)}):"
    )
    for p in sorted(candidates):
        age = (time.time() - p.stat().st_mtime) / 86400
        print(f"  - {p.relative_to(REPO_ROOT)} ({age:.1f} 天前, {fmt_size(p.stat().st_size)})")
    if execute:
        for p in candidates:
            p.unlink()
        print(f"[models] 已刪 {len(candidates)} 檔")
    else:
        print("[models] (dry-run) 加 --execute 才真刪")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真的執行(預設只 dry-run)",
    )
    parser.add_argument(
        "--log-days",
        type=int,
        default=7,
        help="保留近 N 天 log,預設 7",
    )
    parser.add_argument(
        "--model-days",
        type=int,
        default=30,
        help="保留近 N 天 model backup,預設 30(每組最新一份永遠保留)",
    )
    parser.add_argument(
        "--vacuum-only",
        action="store_true",
        help="只 VACUUM cache.db,不動 log/model",
    )
    parser.add_argument(
        "--no-vacuum",
        action="store_true",
        help="跳過 VACUUM cache.db",
    )
    args = parser.parse_args()

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"=== cleanup_artifacts.py [{mode}] ===\n")

    if not args.no_vacuum:
        vacuum_cache_db(args.execute)
        print()

    if not args.vacuum_only:
        cleanup_logs(args.log_days, args.execute)
        print()
        cleanup_model_backups(args.model_days, args.execute)

    if not args.execute:
        print("\n→ 加 --execute 才真的動")
    return 0


if __name__ == "__main__":
    sys.exit(main())
