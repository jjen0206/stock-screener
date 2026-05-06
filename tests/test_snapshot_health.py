"""src/snapshot_health.py 單元測試。

純 read-only 模組,不打網路 / 不寫 SQLite — 直接用 tmp_path 灌假 CSV。
"""
from __future__ import annotations

import pandas as pd
import pytest

from src import snapshot_health as sh


@pytest.fixture
def tmp_snapshot(tmp_path):
    """tmp_path / 'twse_snapshot' 目錄,給各 test 灌 CSV 用。"""
    d = tmp_path / "twse_snapshot"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_csv_with_date(path, dates: list[str], date_col: str = "date") -> None:
    """寫一個有 date_col 欄的 CSV,給 _read_max_date 跟 get_snapshot_health 用。"""
    df = pd.DataFrame({date_col: dates, "x": list(range(len(dates)))})
    df.to_csv(path, index=False)


# === _read_max_date ===

def test_read_max_date_returns_max_and_count(tmp_snapshot):
    p = tmp_snapshot / "daily_prices.csv"
    _write_csv_with_date(p, ["2026-04-30", "2026-05-01", "2026-05-02"])
    max_d, n = sh._read_max_date(p, "date")
    assert max_d == "2026-05-02"
    assert n == 3


def test_read_max_date_returns_none_for_missing_file(tmp_snapshot):
    p = tmp_snapshot / "nonexistent.csv"
    max_d, n = sh._read_max_date(p, "date")
    assert max_d is None and n == 0


# === _compute_days_lag ===

def test_compute_days_lag_iso_date():
    """ISO YYYY-MM-DD 直接相減。"""
    assert sh._compute_days_lag("2026-05-01", today_iso="2026-05-06") == 5


def test_compute_days_lag_quarterly_period():
    """quarterly '2024-Q4' → 該季最後一天 2024-12-31。"""
    lag = sh._compute_days_lag("2024-Q4", today_iso="2025-01-31")
    assert lag == 31


def test_compute_days_lag_monthly_period():
    """monthly '2026-01' → 該月 28 號(保守)。"""
    lag = sh._compute_days_lag("2026-01", today_iso="2026-02-28")
    assert lag == 31


def test_compute_days_lag_invalid_returns_none():
    assert sh._compute_days_lag("garbage") is None
    assert sh._compute_days_lag(None) is None


# === get_snapshot_health ===

def test_snapshot_health_reads_all_csvs(tmp_snapshot):
    """灌幾個 CSV → get_snapshot_health 該全 covered(_EXPECTED 內的全列出)。

    沒灌的 CSV → status='missing'。
    """
    _write_csv_with_date(
        tmp_snapshot / "daily_prices.csv",
        ["2026-05-04", "2026-05-05"], "date",
    )
    rows = sh.get_snapshot_health(
        snapshot_dir=tmp_snapshot, today_iso="2026-05-06",
    )
    # 全 _EXPECTED 都該出現(無論是否存在)
    tables = {r["table"] for r in rows}
    assert tables == set(sh._EXPECTED.keys())
    # daily_prices 有灌 → exists True
    dp = next(r for r in rows if r["table"] == "daily_prices")
    assert dp["exists"] is True
    assert dp["last_date"] == "2026-05-05"
    assert dp["row_count"] == 2
    # 沒灌的 → missing
    others = [r for r in rows if r["table"] != "daily_prices"]
    assert all(r["status"] == "missing" for r in others)


def test_snapshot_health_marks_recent_as_ok(tmp_snapshot):
    """daily_prices 1 天前 → ok(warn 線 = 2 天)。"""
    _write_csv_with_date(
        tmp_snapshot / "daily_prices.csv", ["2026-05-05"], "date",
    )
    rows = sh.get_snapshot_health(
        snapshot_dir=tmp_snapshot, today_iso="2026-05-06",
    )
    dp = next(r for r in rows if r["table"] == "daily_prices")
    assert dp["status"] == "ok"
    assert dp["days_lag"] == 1


def test_snapshot_health_marks_old_data_as_error(tmp_snapshot):
    """daily_prices 落後 10 天(超 error 線 5)→ status='error'。"""
    _write_csv_with_date(
        tmp_snapshot / "daily_prices.csv", ["2026-04-26"], "date",
    )
    rows = sh.get_snapshot_health(
        snapshot_dir=tmp_snapshot, today_iso="2026-05-06",
    )
    dp = next(r for r in rows if r["table"] == "daily_prices")
    assert dp["status"] == "error"
    assert dp["days_lag"] == 10
    assert "落後 10 天" in dp["note"]


def test_snapshot_health_quarterly_uses_period_column(tmp_snapshot):
    """financials_quarterly 用 period 欄(YYYY-QN 字串)。"""
    df = pd.DataFrame({
        "stock_id": ["2330"],
        "period": ["2024-Q4"],
        "revenue": [1.0e8],
    })
    df.to_csv(tmp_snapshot / "financials_quarterly.csv", index=False)
    rows = sh.get_snapshot_health(
        snapshot_dir=tmp_snapshot, today_iso="2025-02-28",
    )
    fq = next(r for r in rows if r["table"] == "financials_quarterly")
    assert fq["last_date"] == "2024-Q4"
    assert fq["days_lag"] == 59  # 2025-02-28 - 2024-12-31


# === overall_status ===

def test_overall_status_error_takes_priority():
    rows = [
        {"status": "ok"}, {"status": "warn"}, {"status": "error"},
    ]
    assert sh.overall_status(rows) == "error"


def test_overall_status_warn_when_no_error():
    rows = [{"status": "ok"}, {"status": "warn"}, {"status": "ok"}]
    assert sh.overall_status(rows) == "warn"


def test_overall_status_ok_when_all_ok():
    rows = [{"status": "ok"}, {"status": "ok"}]
    assert sh.overall_status(rows) == "ok"


def test_overall_status_missing_treated_as_warn():
    """missing → 視為 warn(部分 CSV 還沒首次 backfill)。"""
    rows = [{"status": "ok"}, {"status": "missing"}]
    assert sh.overall_status(rows) == "warn"
