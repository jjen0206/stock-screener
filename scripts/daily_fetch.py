"""排程入口:抓 TW_TOP_50 過去 N 天 daily_price + institutional 進 SQLite cache。

GitHub Actions 用:在 `daily_notify.py` 之前跑,讓 SQLite 有資料可供選股。

使用範例:
    python scripts/daily_fetch.py
    python scripts/daily_fetch.py --days 60

Exit code:
    0 — 跑完(即使部分失敗,主流程仍應繼續到 daily_notify)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import database as db  # noqa: E402
from src.data_fetcher import fetch_daily_price, fetch_institutional  # noqa: E402
from src.universe import TW_TOP_50  # noqa: E402


def run(days: int = 90) -> dict:
    """主邏輯(可單獨呼叫,方便測試)。

    對 TW_TOP_50 每檔跑 fetch_daily_price + fetch_institutional,
    印進度到 stdout,**單檔失敗印 warning 不中斷**。

    回傳 {"price_ok": int, "inst_ok": int, "total": int, "days": int}
    """
    today = date.today()
    today_iso = today.isoformat()
    start_iso = (today - timedelta(days=days)).isoformat()

    print(
        f"[FETCH] 抓 {len(TW_TOP_50)} 檔過去 {days} 天 "
        f"({start_iso} ~ {today_iso})",
        flush=True,
    )

    db.init_db()
    db.upsert_stocks([
        {"stock_id": sid, "name": name, "market": "TW"}
        for sid, name in TW_TOP_50
    ])

    n = len(TW_TOP_50)
    price_ok = 0
    inst_ok = 0

    for i, (sid, name) in enumerate(TW_TOP_50, start=1):
        price_status = "FAIL"
        inst_status = "FAIL"
        try:
            fetch_daily_price(sid, start_iso, today_iso)
            price_ok += 1
            price_status = "OK"
        except Exception as e:  # noqa: BLE001 — 容錯
            price_status = f"FAIL({type(e).__name__})"
        try:
            fetch_institutional(sid, start_iso, today_iso)
            inst_ok += 1
            inst_status = "OK"
        except Exception as e:  # noqa: BLE001 — 容錯
            inst_status = f"FAIL({type(e).__name__})"
        print(
            f"[{i}/{n}] {sid} {name} ... price {price_status} | inst {inst_status}",
            flush=True,
        )

    print(
        f"\ndone. price ok={price_ok}/{n}, inst ok={inst_ok}/{n}",
        flush=True,
    )
    return {
        "price_ok": price_ok,
        "inst_ok": inst_ok,
        "total": n,
        "days": days,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="抓 TW_TOP_50 daily_price + institutional 進 SQLite",
    )
    p.add_argument(
        "--days", type=int, default=90,
        help="抓過去 N 天,預設 90(夠 KD/MA 計算)",
    )
    args = p.parse_args()
    run(days=args.days)
    # 即使部分失敗也回 0,主流程(daily_notify)仍應繼續
    return 0


if __name__ == "__main__":
    sys.exit(main())
