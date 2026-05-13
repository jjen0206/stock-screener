"""Regression:TDCC opendata 合計列偵測修正(2026-05 baseline bug)。

Bug context:
  - 2026-05-08 第一次跑 baseline 後,每檔的 holders_1000up_count ≈ total_holders / 2,
    holders_pct ≈ 0.50,跨所有 3971 sids,明顯異常。
  - 2330 backfill 4/17 (qryStock 路徑) = 1502,但 baseline 5/8 (opendata 路徑) = 2,555,289。
  - root cause:`_LEVEL_TOTAL = 99` hardcode 不命中實際 CSV。TDCC opendata 把「合計列」
    寫在 level == max(level)(實測 2026-05 為 17)。原本程式以為 level 17 是普通級距、
    level 99 才是合計,結果合計列被當成「大戶級距」加進 holders_1000up,total 那邊
    fallback sum 也把合計列加進去 → 等於把 sum 算了兩倍。

修法:per-sid 用 max(level) 偵測合計列,不再 hardcode。

這支測試守住:
  1. 對「實際 TDCC opendata 格式」(1..17,17 是合計)抽出正確的 1000up / total。
  2. holders_pct 永遠 < 1.0(沒有跨市場全 ≈ 0.50 異常)。
  3. 至少覆蓋:正常股(2330)、ETF(0050)、稀疏股(只有部分 level)、無資料股
     (全 0)、其他大型股(2317)。
"""
from __future__ import annotations

import pytest

from scripts import fetch_shareholder_concentration as fetcher


# 仿照實際 TDCC opendata 格式:每檔 level 1..17,17 是合計(count = 1..16 加總)。
# 2330(台積電)實測 5/8 數字嵌進去當主要 case。
_REAL_TDCC_FIXTURE_CSV = """資料日期,證券代號,持股分級,人數,股數,占集保庫存比例%
20260508,2330,1,2046777,245296112,0.94
20260508,2330,2,411783,787602103,3.03
20260508,2330,3,48610,348925804,1.34
20260508,2330,4,16216,199201762,0.76
20260508,2330,5,7591,133719241,0.51
20260508,2330,6,7390,181066250,0.69
20260508,2330,7,3494,121069445,0.46
20260508,2330,8,2009,90592736,0.34
20260508,2330,9,3969,277311260,1.06
20260508,2330,10,2004,280445485,1.08
20260508,2330,11,1321,369402589,1.42
20260508,2330,12,550,269656735,1.03
20260508,2330,13,349,241733654,0.93
20260508,2330,14,216,192910048,0.74
20260508,2330,15,1504,22193600297,85.58
20260508,2330,16,2,9000,0.00
20260508,2330,17,2553783,25932524521,100.00
20260508,0050,1,1500000,30000000,0.05
20260508,0050,2,800000,20000000,0.03
20260508,0050,14,500,1000000,0.00
20260508,0050,15,200,500000000,0.20
20260508,0050,16,38,1000000000,0.40
20260508,0050,17,2814720,1551000000,100.00
20260508,2317,1,800000,15000000,0.03
20260508,2317,2,250000,30000000,0.06
20260508,2317,14,1000,20000000,0.04
20260508,2317,15,800,5000000000,9.50
20260508,2317,16,0,0,0.00
20260508,2317,17,1105364,5065000000,100.00
20260508,000218,1,0,0,0.00
20260508,000218,14,0,0,0.00
20260508,000218,15,1,422278902,100.00
20260508,000218,16,0,0,0.00
20260508,000218,17,1,422278902,100.00
20260508,9999,1,0,0,0.00
20260508,9999,17,0,0,0.00
"""


@pytest.fixture
def parsed_rows():
    df = fetcher.parse_tdcc_csv(_REAL_TDCC_FIXTURE_CSV)
    rows = fetcher.aggregate_to_rows(df)
    return {r["sid"]: r for r in rows}


# ============================================================================
# 主 case:2330(qryStock 4/17 = 1502,3 週後 1506 合理 delta)
# ============================================================================

def test_2330_holders_1000up_in_expected_range(parsed_rows):
    """2330 千張大戶 = level 15 + level 16 = 1504 + 2 = 1506。

    qryStock backfill 2026-04-17 = 1502;baseline 2026-05-08 應該 ≈ 1500-1600
    (3 週內變動 ± 一點)。若這裡爆出 2,555,289 就是合計列又被算進去了。
    """
    r = parsed_rows["2330"]
    assert 1500 <= r["holders_1000up_count"] <= 1600, (
        f"2330 holders_1000up 應 ≈ 1502(對齊 qryStock backfill),實際 "
        f"{r['holders_1000up_count']} — 合計列偵測壞了?"
    )
    # 精確 case:1504 + 2 = 1506(fixture 數字)
    assert r["holders_1000up_count"] == 1506


def test_2330_total_holders_not_doubled(parsed_rows):
    """2330 總股東數 = 合計列(level 17)= 2,553,783,不該被 doubled。

    bug 出現時 total = 5,107,568(2,553,783 × 2),因為 fallback sum 把合計列
    自己也加進去。
    """
    r = parsed_rows["2330"]
    assert r["total_holders"] == 2553783, (
        f"2330 total_holders 應 = 2,553,783(level 17 合計列),實際 "
        f"{r['total_holders']} — 合計列被加總兩次?"
    )


def test_2330_pct_is_small_not_half(parsed_rows):
    """2330 大戶占比 ≈ 0.06%,絕對不該是 0.50 的全市場異常。"""
    r = parsed_rows["2330"]
    assert r["holders_pct"] is not None
    assert r["holders_pct"] < 0.01, (
        f"2330 holders_pct 應 < 1%,實際 {r['holders_pct']} — bug 復發?"
    )


# ============================================================================
# ETF / 中型股 / 稀疏 / 無資料 case
# ============================================================================

def test_0050_etf(parsed_rows):
    """0050 ETF:千張戶 = 200 + 38 = 238,total = level 17 = 2,814,720。"""
    r = parsed_rows["0050"]
    assert r["holders_1000up_count"] == 238
    assert r["total_holders"] == 2814720
    assert r["holders_pct"] is not None and r["holders_pct"] < 0.01


def test_2317_hon_hai(parsed_rows):
    """2317 鴻海:千張戶 = 800 + 0 = 800,total = 1,105,364。"""
    r = parsed_rows["2317"]
    assert r["holders_1000up_count"] == 800
    assert r["total_holders"] == 1105364
    assert r["holders_pct"] is not None and r["holders_pct"] < 0.01


def test_sparse_sid_only_total_row_holder(parsed_rows):
    """000218(稀疏 / 公司債券類):全部股東 1 人都在 level 15,合計 1。

    level 1-14 全 0,level 15 = 1,level 16 = 0,level 17 = 1(合計)。
    parser 應抽出 1000up = 1(只算 level 15,排除 level 17 合計),total = 1。
    """
    r = parsed_rows["000218"]
    assert r["holders_1000up_count"] == 1
    assert r["total_holders"] == 1
    # 占比為 100% 是合理的(只有一個大戶,占全部股東),但不該因 bug 算成 0.5
    assert r["holders_pct"] == 1.0


def test_no_data_sid_skipped(parsed_rows):
    """9999(假停止交易股,全 0)→ total_holders = 0 → 該被 skip,不寫入。"""
    assert "9999" not in parsed_rows, (
        "全 0 / 無資料股應 skip,避免 division-by-zero 或假 row 污染"
    )


# ============================================================================
# 跨 sid 不變式:沒有全市場 pct ≈ 0.5 異常
# ============================================================================

def test_no_global_pct_anomaly(parsed_rows):
    """全部抽出來的 sids 不該有 pct ≈ 0.50 大量出現(bug 簽名)。

    bug 出現時跨 ~3971 sids 全部 pct ≈ 0.5;這裡只要任何一檔 pct 在 [0.45, 0.55]
    範圍就拉警報。
    """
    suspicious = [
        sid for sid, r in parsed_rows.items()
        if r["holders_pct"] is not None and 0.45 < r["holders_pct"] < 0.55
    ]
    assert not suspicious, (
        f"出現 pct ≈ 0.5 異常 sids:{suspicious} — bug 復發,合計列又被加進大戶?"
    )


# ============================================================================
# 對舊 fixture(level 99 = 合計)的相容守護
# ============================================================================

def test_backward_compatible_with_level_99_as_total():
    """老 fixture 用 level 99 當合計仍能正確 parse(max-level 偵測法 forward+backward
    compatible)。
    """
    legacy_csv = """資料日期,證券代號,持股分級,人數,股數,占集保庫存比例%
20260508,2330,1,200000,30000000,0.10
20260508,2330,15,1200,500000000,0.20
20260508,2330,16,800,2000000000,0.30
20260508,2330,17,50,500000000,0.10
20260508,2330,99,304050,3100030000,1.00
"""
    df = fetcher.parse_tdcc_csv(legacy_csv)
    rows = fetcher.aggregate_to_rows(df)
    by_sid = {r["sid"]: r for r in rows}
    # max(level) = 99 → 合計;千張 = 15 + 16 + 17 = 1200+800+50 = 2050
    assert by_sid["2330"]["holders_1000up_count"] == 2050
    assert by_sid["2330"]["total_holders"] == 304050
