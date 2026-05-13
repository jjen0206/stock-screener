"""探測 TDCC 集保股權分散表能否 backfill 過去幾週。

不動 fetcher 本體,只跑各種 URL / 參數組合看哪些回應有效資料。

⚠️ 用途: 一次性 reference / 手動探測腳本。
    - 留作後續 fetcher / 文件參考。
    - **不要排進 daily / cron**,也不要納入 CI。
    - 想再跑驗證 TDCC 端有沒有改格式時,手動執行即可。
"""
from __future__ import annotations

import sys
import urllib3
import requests
from datetime import date, timedelta

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
HEADERS = {"User-Agent": UA}
TIMEOUT = 30


def probe(url: str, label: str) -> dict:
    """打一次 URL,回 status / size / 前 200 char。"""
    try:
        r = requests.get(url, timeout=TIMEOUT, verify=False, headers=HEADERS,
                         allow_redirects=True)
        body = r.text
        return {
            "label": label,
            "url": url,
            "status": r.status_code,
            "size": len(body),
            "ctype": r.headers.get("Content-Type", ""),
            "head": body[:200].replace("\n", "\\n"),
            "final_url": r.url,
        }
    except Exception as e:
        return {"label": label, "url": url, "error": str(e)[:200]}


def fmt(res: dict) -> str:
    if "error" in res:
        return f"[{res['label']}] ERROR: {res['error']}\n  url={res['url']}"
    return (
        f"[{res['label']}] status={res['status']} size={res['size']} "
        f"ctype={res['ctype']}\n  url={res['url']}\n  final={res['final_url']}\n"
        f"  head={res['head']!r}"
    )


def main():
    # 過去幾個週五的日期(TDCC 週五公告)
    today = date(2026, 5, 13)  # 主公給的當前日期
    # 找最近幾個週五
    fridays = []
    d = today
    while d.weekday() != 4:  # 4 = Friday
        d -= timedelta(days=1)
    for i in range(6):
        fridays.append(d - timedelta(weeks=i))

    print(f"# 探測日期 (週五): {[f.isoformat() for f in fridays[:3]]} ...")
    print(f"# 當前日期: {today}")
    print()

    base = "https://opendata.tdcc.com.tw/getOD.ashx"
    probes = []

    # 1. 不帶參數(基準 = 最新一週)
    probes.append((f"{base}?id=1-5", "baseline-1-5"))

    # 2. 試 date / qDate / DATE / queryDate 等常見參數名
    target = fridays[1]  # 上週五 = 2026-05-08
    target_old = fridays[3]  # 4 週前
    target_str_dash = target.isoformat()
    target_str = target.strftime("%Y%m%d")
    target_str_old = target_old.strftime("%Y%m%d")

    for param in ["date", "qDate", "DATE", "queryDate", "weekDate", "data_date"]:
        probes.append((f"{base}?id=1-5&{param}={target_str}",
                       f"1-5+{param}={target_str}"))
    probes.append((f"{base}?id=1-5&date={target_str_dash}",
                   f"1-5+date={target_str_dash}"))
    # 也試 4 週前 (driver: 看是否 silent 回最新還是真的歷史)
    probes.append((f"{base}?id=1-5&qDate={target_str_old}",
                   f"1-5+qDate={target_str_old}-old"))

    # 3. 試其他 id (TDCC opendata 列出的編號)
    # 1-1, 1-2, ... 看有沒有歷史變體
    for tid in ["1-1", "1-2", "1-3", "1-4", "1-6", "1-7", "1-8"]:
        probes.append((f"{base}?id={tid}", f"id={tid}"))

    print("=" * 60)
    print("PROBES:")
    print("=" * 60)
    for url, label in probes:
        res = probe(url, label)
        print(fmt(res))
        print()


if __name__ == "__main__":
    main()
