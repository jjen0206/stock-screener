"""探 TDCC qryStock POST 機制:能否抓單檔歷史週?response 格式如何?

⚠️ 用途: 一次性 reference / 手動探測腳本。
    - 留作 shareholder_concentration backfill fetcher 設計依據。
    - **不要排進 daily / cron**,也不要納入 CI。
    - 想再跑驗證 POST/token/SqlMethod 行為時手動執行。
"""
from __future__ import annotations
import re
import sys
import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
SESS = requests.Session()
SESS.headers.update({"User-Agent": UA})
SESS.verify = False

URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"


def get_token() -> tuple[str, str]:
    r = SESS.get(URL, timeout=30)
    r.raise_for_status()
    html = r.text
    tok = re.search(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"', html).group(1)
    fir = re.search(r'name="firDate" value="([^"]+)"', html).group(1)
    return tok, fir


def query(scaDate: str, stockNo: str) -> dict:
    tok, fir = get_token()
    payload = {
        "SYNCHRONIZER_TOKEN": tok,
        "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock",
        "method": "submit",
        "firDate": fir,
        "scaDate": scaDate,
        "sqlMethod": "StockNo",
        "stockNo": stockNo,
        "stockName": "",
    }
    r = SESS.post(URL, data=payload, timeout=30)
    body = r.text
    # 找表格 row 數量 + 是否有「合計」+ 「無資料」訊息
    has_table = "<table" in body
    no_data = "查無資料" in body or "無資料" in body
    # TDCC 通常用 <table class="table"> 顯示 15 級距 + 合計
    table_rows = len(re.findall(r"<tr[^>]*>", body))
    # 找出第一個合計行的數字 (人數)
    holders_levels = re.findall(
        r"<td[^>]*>(\d+)</td>\s*<td[^>]*>([\d,]+)</td>\s*<td[^>]*>([\d,]+)</td>",
        body
    )
    return {
        "status": r.status_code,
        "size": len(body),
        "has_table": has_table,
        "no_data": no_data,
        "tr_count": table_rows,
        "first_5_rows": holders_levels[:5],
        "last_3_rows": holders_levels[-3:] if holders_levels else [],
    }


def main():
    # 試 2 個歷史週 + 一個未來日 (control)
    cases = [
        ("20260508", "2330", "tsmc-latest"),
        ("20250516", "2330", "tsmc-1yr-ago"),
        ("20251017", "2330", "tsmc-mid"),
        ("20260508", "2454", "mtk-latest"),
    ]
    for scaDate, stockNo, label in cases:
        print(f"--- {label} (scaDate={scaDate}, stockNo={stockNo}) ---")
        try:
            res = query(scaDate, stockNo)
            for k, v in res.items():
                print(f"  {k}: {v}")
        except Exception as e:
            print(f"  ERROR: {e}")
        print()


if __name__ == "__main__":
    main()
