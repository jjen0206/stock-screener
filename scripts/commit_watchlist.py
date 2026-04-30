"""一鍵把本機 SQLite watchlist 同步進 repo:dump → git add/commit/push。

使用前提:本機 SQLite 必須是「想要的最終狀態」(沒 ☆ 就空、有 ☆ 就有)。
腳本不做 merge —— 直接以 SQLite 為準覆寫 CSV。

如果是新 clone 後第一次用,先讓 SQLite 跟 repo 同步:
    python -c "from src import watchlist_snapshot; watchlist_snapshot.load_from_csv()"
或開一次 streamlit (`streamlit run app.py`),boot 時會自動 load CSV 進 SQLite。

Usage:
    python scripts/commit_watchlist.py             # dump → 有變動就 add/commit/push
    python scripts/commit_watchlist.py --dry-run   # 只 dump,顯示 diff,不 commit/push
    python scripts/commit_watchlist.py -m "..."    # 自訂 commit 訊息

Exit code:
    0 = 成功(無變更也算成功)
    1 = dump silent skip / git 失敗
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src import watchlist_snapshot  # noqa: E402

DEFAULT_MSG = "chore(watchlist): sync watchlist.csv from local SQLite"
CSV_REL = "data/twse_snapshot/watchlist.csv"


def _run_git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """跑 git;check=True 時失敗 print stderr 並 exit 1。"""
    result = subprocess.run(
        ["git", *args], cwd=_ROOT, capture_output=True, text=True
    )
    if check and result.returncode != 0:
        print(f"[ERROR] git {' '.join(args)} 失敗 (exit {result.returncode}):")
        msg = result.stderr.strip() or result.stdout.strip()
        if msg:
            print(msg)
        sys.exit(1)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="只 dump,顯示 diff,不執行 commit/push",
    )
    ap.add_argument(
        "-m", "--message", default=DEFAULT_MSG,
        help=f"commit 訊息 (預設: {DEFAULT_MSG!r})",
    )
    args = ap.parse_args(argv)

    # 1. dump SQLite → CSV
    db.init_db()
    n = watchlist_snapshot.dump_to_csv()
    if n < 0:
        print(
            "[ERROR] dump 被 silent skip。可能原因:\n"
            "  - data/twse_snapshot/ 資料夾不存在\n"
            "  - DATABASE_PATH 不在 PROJECT_ROOT 底下 (此 script 必須在 repo 內跑)"
        )
        return 1
    print(f"[INFO] dump 完成,SQLite watchlist 共 {n} 筆 → {CSV_REL}")

    # 2. 檢查 working tree vs HEAD 是否有變動
    diff_check = _run_git(
        ["diff", "--exit-code", "--quiet", "HEAD", "--", CSV_REL],
        check=False,
    )
    if diff_check.returncode == 0:
        print("[INFO] watchlist.csv 沒變更,無需 commit")
        return 0

    # 印 diff stat 給使用者看大概改了多少
    stat = _run_git(["diff", "--stat", "HEAD", "--", CSV_REL])
    print("[INFO] 偵測到變更:")
    print(stat.stdout.rstrip() or "(stat 為空)")

    if args.dry_run:
        print("\n--- diff (HEAD vs working tree) ---")
        full = _run_git(["diff", "HEAD", "--", CSV_REL])
        print(full.stdout.rstrip())
        print("\n[INFO] --dry-run,不執行 commit/push")
        return 0

    # 3. add → commit (--only 確保只 commit 此檔,不誤帶其他 staged 內容) → push
    _run_git(["add", CSV_REL])
    _run_git(["commit", "--only", CSV_REL, "-m", args.message])
    _run_git(["push"])

    log = _run_git(["log", "--oneline", "-1"])
    print(f"[OK] 已 push: {log.stdout.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
