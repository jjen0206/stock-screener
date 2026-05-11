"""Stage 1 e2e:千張大戶 TDCC pipeline 守住 boot wiring + fetcher 端到端。

主公拍板的「加 helper 不夠,必須 wire 進 boot path 並 spy 守住」教訓 →
這層測試:
  1. SCHEMA 真的建出 shareholder_concentration 表(boot path 必經)
  2. fetcher 跑完(用 fixture CSV 灌假資料,不打真網)資料可從 SQLite query 回來
  3. preload_snapshots 真的會讀 shareholder_concentration.csv 並 upsert 進 SQLite
  4. dump_shareholder_concentration_csv 真的會把 SQLite 內容寫回 CSV

不打 TDCC 真網路 — 用 monkeypatch 替換 fetch_tdcc_csv_text。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import fetch_shareholder_concentration as fetcher
from src import config, database as db


_FIXTURE_CSV = """資料日期,證券代號,持股分級,人數,股數,占集保庫存比例%
20260508,2330,1,200000,30000000,0.10
20260508,2330,2,100000,40000000,0.20
20260508,2330,14,2000,30000000,0.10
20260508,2330,15,1200,500000000,0.20
20260508,2330,16,800,2000000000,0.30
20260508,2330,17,50,500000000,0.10
20260508,2330,99,304050,3100030000,1.00
20260508,2454,1,80000,10000000,0.05
20260508,2454,2,50000,20000000,0.10
20260508,2454,15,500,300000000,0.40
20260508,2454,16,200,500000000,0.30
20260508,2454,17,20,400000000,0.15
20260508,2454,99,130720,1230010000,1.00
"""


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """乾淨 tmp DB,避免污染本機 cache.db + 觸發 GH push thread。"""
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "sc.db"))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    yield tmp_path
    db._reset_path_cache()  # type: ignore[attr-defined]


# ============================================================================
# Schema 守住:boot path 必經
# ============================================================================

def test_init_db_creates_shareholder_concentration_table(tmp_db):
    """db.init_db() 必須建出 shareholder_concentration 表(boot path 第一步)。

    沒這層 schema,fetcher upsert / notifier enrich / app render 全會炸。
    """
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='shareholder_concentration'"
        ).fetchone()
    assert row is not None, (
        "shareholder_concentration 表沒建出來,boot path 必經這張表"
    )

    # 欄位齊全(防漏欄)
    with db.get_conn() as conn:
        cols = {
            r["name"]
            for r in conn.execute(
                "PRAGMA table_info(shareholder_concentration)"
            ).fetchall()
        }
    expected = {
        "sid", "week_end", "holders_1000up_count", "total_holders",
        "holders_pct", "holders_delta_w", "fetched_at",
    }
    assert expected.issubset(cols), (
        f"shareholder_concentration 欄位漏:expected {expected}, actual {cols}"
    )


# ============================================================================
# Fetcher 端到端:CSV → parse → upsert → query 回來
# ============================================================================

def test_fetcher_run_with_fixture_csv_writes_rows_queryable(tmp_db, monkeypatch):
    """fetcher.run() 跑完(用 fixture CSV,不打網)資料可從 SQLite query 出來。"""
    # 不寫真 CSV(tmp_db 不在 PROJECT_ROOT,dump silent skip 回 -1)
    summary = fetcher.run(csv_text=_FIXTURE_CSV, dump_csv=False)

    assert summary["rows_written"] == 2, (
        f"fixture 含 2330 + 2454 兩檔,實際寫入 {summary['rows_written']}"
    )
    assert summary["week_end"] == "2026-05-08"

    # 真的能 query 出來
    row_2330 = db.get_latest_shareholder_concentration("2330")
    assert row_2330 is not None
    # 千張戶 = level 15-17 加總 = 1200 + 800 + 50 = 2050
    assert row_2330["holders_1000up_count"] == 2050
    # 總股東 = level 99(合計列）= 304050
    assert row_2330["total_holders"] == 304050
    # 占比 ≈ 2050 / 304050 ≈ 0.00674
    assert row_2330["holders_pct"] is not None
    assert 0.005 < row_2330["holders_pct"] < 0.010
    # 第一次抓 → 沒上週基準 → delta_w 為 None
    assert row_2330["holders_delta_w"] is None
    assert row_2330["week_end"] == "2026-05-08"

    row_2454 = db.get_latest_shareholder_concentration("2454")
    assert row_2454 is not None
    # 千張戶 = 500 + 200 + 20 = 720
    assert row_2454["holders_1000up_count"] == 720
    assert row_2454["total_holders"] == 130720


def test_fetcher_run_second_week_computes_delta_w(tmp_db, monkeypatch):
    """第二次抓(新週次)應算出 holders_delta_w = 本週 - 上週。"""
    # 第一週:2026-05-08,2330 千張戶 2050
    fetcher.run(csv_text=_FIXTURE_CSV, dump_csv=False)

    # 第二週:2026-05-15,2330 千張戶從 2050 → 2080(+30)
    week2_csv = """資料日期,證券代號,持股分級,人數,股數,占集保庫存比例%
20260515,2330,1,200000,30000000,0.10
20260515,2330,15,1230,500000000,0.20
20260515,2330,16,800,2000000000,0.30
20260515,2330,17,50,500000000,0.10
20260515,2330,99,304080,3100030000,1.00
"""
    summary2 = fetcher.run(csv_text=week2_csv, dump_csv=False)
    assert summary2["week_end"] == "2026-05-15"

    row = db.get_latest_shareholder_concentration("2330")
    assert row["week_end"] == "2026-05-15"
    assert row["holders_1000up_count"] == 2080
    assert row["holders_delta_w"] == 30, (
        f"delta_w 應 = 2080 - 2050 = 30,實際 {row['holders_delta_w']}"
    )


def test_fetcher_run_with_limit_caps_rows(tmp_db):
    """--limit N 只該寫前 N 檔(smoke 模式)。"""
    summary = fetcher.run(csv_text=_FIXTURE_CSV, limit=1, dump_csv=False)
    assert summary["rows_written"] == 1, (
        f"--limit 1 應只寫 1 筆,實際 {summary['rows_written']}"
    )


# ============================================================================
# Boot wiring 守住:preload_snapshots 必須讀 shareholder_concentration.csv
# ============================================================================

def test_preload_snapshots_loads_shareholder_concentration_csv(tmp_db):
    """preload_snapshots 必須認得 shareholder_concentration.csv 並 upsert 進 SQLite。

    主公的飛彈條款:加 helper 不夠,必須 wire 進 boot path 並 spy 守住 — 這層測試
    就是這個 spy。preload_snapshots 漏接 shareholder_concentration.csv →
    雲端容器重啟後 SQLite 內這張表永遠是空的,UI / Telegram 永遠 graceful skip
    顯不出來。
    """
    snap_dir = tmp_db / "snap"
    snap_dir.mkdir()
    csv_path = snap_dir / "shareholder_concentration.csv"
    csv_path.write_text(
        "sid,week_end,holders_1000up_count,total_holders,"
        "holders_pct,holders_delta_w,fetched_at\n"
        "2330,2026-05-08,2050,304050,0.006741,15,2026-05-09T02:00:00+00:00\n"
        "2454,2026-05-08,720,130720,0.005509,,2026-05-09T02:00:00+00:00\n",
        encoding="utf-8",
    )

    counts = db.preload_snapshots(snapshot_dir=snap_dir)

    assert counts.get("shareholder_concentration") == 2, (
        f"preload_snapshots 沒把 shareholder_concentration.csv 灌進來,"
        f"counts={counts}"
    )

    # 真的進到表內
    row = db.get_latest_shareholder_concentration("2330")
    assert row is not None
    assert row["holders_1000up_count"] == 2050
    assert row["total_holders"] == 304050
    assert row["holders_delta_w"] == 15

    # 第二檔的 delta 是空 → 灌進去應為 None,非 0
    row2 = db.get_latest_shareholder_concentration("2454")
    assert row2 is not None
    assert row2["holders_delta_w"] is None


def test_preload_snapshots_missing_csv_silent_skip(tmp_db):
    """shareholder_concentration.csv 不存在 → preload skip,不該炸。

    第一次部署 / fetcher 還沒跑過時的預期路徑。
    """
    snap_dir = tmp_db / "snap_empty"
    snap_dir.mkdir()

    counts = db.preload_snapshots(snapshot_dir=snap_dir)

    assert "shareholder_concentration" not in counts


# ============================================================================
# get_shareholder_concentration_for_sids 批量查
# ============================================================================

def test_get_shareholder_concentration_for_sids_returns_latest_per_sid(tmp_db):
    """批量 lookup 必須回每檔最新週次(給 notifier _select_top_picks enrich 用)。"""
    db.upsert_shareholder_concentration([
        {"sid": "2330", "week_end": "2026-05-01",
         "holders_1000up_count": 2000, "total_holders": 300000,
         "holders_pct": 0.00667, "holders_delta_w": None},
        {"sid": "2330", "week_end": "2026-05-08",
         "holders_1000up_count": 2050, "total_holders": 304050,
         "holders_pct": 0.00674, "holders_delta_w": 50},
        {"sid": "2454", "week_end": "2026-05-08",
         "holders_1000up_count": 720, "total_holders": 130720,
         "holders_pct": 0.00551, "holders_delta_w": None},
    ])
    out = db.get_shareholder_concentration_for_sids(["2330", "2454", "9999"])
    assert set(out.keys()) == {"2330", "2454"}
    # 2330 該回新的那筆
    assert out["2330"]["week_end"] == "2026-05-08"
    assert out["2330"]["holders_1000up_count"] == 2050
    assert out["2330"]["holders_delta_w"] == 50
    assert out["2454"]["holders_1000up_count"] == 720


# ============================================================================
# Parse 邊界:欄位缺失 / 空 CSV / 不同 date 格式
# ============================================================================

def test_parse_tdcc_csv_handles_empty_input():
    """空字串 / 空 DataFrame 不該炸,回空 df 讓 caller 走 0 入 path。"""
    df = fetcher.parse_tdcc_csv("")
    assert df.empty


def test_parse_tdcc_csv_handles_missing_required_columns():
    """欄位缺 → 回空 df + log warning(caller 走 0 入 path)。"""
    bad_csv = "資料日期,證券代號\n20260508,2330\n"
    df = fetcher.parse_tdcc_csv(bad_csv)
    assert df.empty


def test_normalize_date_variants():
    """容忍 YYYYMMDD / YYYY/MM/DD / 已 normalized。"""
    assert fetcher._normalize_date("20260508") == "2026-05-08"
    assert fetcher._normalize_date("2026/05/08") == "2026-05-08"
    assert fetcher._normalize_date("2026-05-08") == "2026-05-08"
    assert fetcher._normalize_date("") == ""
