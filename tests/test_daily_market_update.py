"""scripts/daily_market_update.py 單元測試(原 weekly_market_update.py)。

測試 dump CSV 流程是否正確,**不打 TWSE 真網路**(mock update_long_term_data_free)。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db


_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "daily_market_update.py"
)
_spec = importlib.util.spec_from_file_location("daily_market_update", _SCRIPT)
weekly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(weekly)


@pytest.fixture
def tmp_env(monkeypatch, tmp_path):
    """獨立 SQLite + snapshot 目錄,測試不污染真實 cache。"""
    db_file = tmp_path / "weekly.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    snapshot_dir = tmp_path / "twse_snapshot"
    monkeypatch.setattr(weekly, "SNAPSHOT_DIR", snapshot_dir)
    db.init_db()
    return tmp_path


def _fake_update_success(stock_ids, on_progress=None):
    """模擬 TWSE 抓取成功。"""
    # 寫一些假資料進 SQLite,讓 dump 能拉到資料
    for sid in stock_ids[:2]:  # 前 2 檔
        db.upsert_daily_metrics([{
            "stock_id": sid, "date": "2026-04-25",
            "close": 100.0, "pe": 20.0, "pb": 2.0, "dividend_yield": 3.0,
        }])
        db.upsert_financials([{
            "stock_id": sid, "period_type": "quarterly",
            "period": "2025-Q4", "revenue": None, "revenue_yoy": None,
            "eps": 5.0, "roe": 10.0,
        }])
    return {
        "success_metrics": stock_ids[:2],
        "success_eps": stock_ids[:2],
        "failed": stock_ids[2:],
        "error": None,
    }


def _fake_update_all_fail(stock_ids, on_progress=None):
    return {
        "success_metrics": [],
        "success_eps": [],
        "failed": stock_ids,
        "error": RuntimeError("TWSE blocked"),
    }


def test_main_writes_6_csvs_on_success(tmp_env, monkeypatch):
    """從 4 → 6 CSV(2026-05-06 主公拍板把 monthly_revenue + dividend
    整合進 daily,砍掉冗余 backfill-financials.yml workflow)。
    """
    # mock 兩個新 fetcher 不打網路
    monkeypatch.setattr(
        weekly, "update_long_term_data_free", _fake_update_success,
    )
    monkeypatch.setattr(
        weekly, "fetch_monthly_revenue", lambda sid, s, e: None,
    )
    monkeypatch.setattr(
        weekly, "fetch_dividend", lambda sid, s, e: None,
    )
    code = weekly.main()
    assert code == 0
    snapshot = weekly.SNAPSHOT_DIR
    assert (snapshot / "daily_metrics.csv").exists()
    assert (snapshot / "financials_quarterly.csv").exists()
    assert (snapshot / "stocks.csv").exists()
    assert (snapshot / "daily_prices.csv").exists()
    # 守門:monthly_revenue.csv 必須被 dump(原來只在 backfill-revenue 週跑)
    assert (snapshot / "monthly_revenue.csv").exists(), (
        "monthly_revenue.csv 必須由 daily_market_update 每日 dump"
    )
    # 守門:dividend.csv 必須被 dump(原來只在 backfill-dividend 週跑)
    assert (snapshot / "dividend.csv").exists(), (
        "dividend.csv 必須由 daily_market_update 每日 dump"
    )


def test_main_calls_fetch_monthly_revenue_per_stock(tmp_env, monkeypatch):
    """守門:確認 daily_market_update 對 universe 每檔呼叫 fetch_monthly_revenue。"""
    monkeypatch.setattr(
        weekly, "update_long_term_data_free", _fake_update_success,
    )
    rev_calls: list[str] = []
    monkeypatch.setattr(
        weekly, "fetch_monthly_revenue",
        lambda sid, s, e: rev_calls.append(sid),
    )
    monkeypatch.setattr(
        weekly, "fetch_dividend", lambda sid, s, e: None,
    )
    weekly.main()
    # universe = TW_TOP_50 fallback(SQLite 空)→ 50 檔該被呼叫
    assert len(rev_calls) == 50, (
        f"期望 50 個 monthly_revenue 呼叫(TW_TOP_50),實際 {len(rev_calls)}"
    )


def test_main_calls_fetch_dividend_per_stock(tmp_env, monkeypatch):
    """守門:確認 daily_market_update 對 universe 每檔呼叫 fetch_dividend。"""
    monkeypatch.setattr(
        weekly, "update_long_term_data_free", _fake_update_success,
    )
    div_calls: list[str] = []
    monkeypatch.setattr(
        weekly, "fetch_monthly_revenue", lambda sid, s, e: None,
    )
    monkeypatch.setattr(
        weekly, "fetch_dividend",
        lambda sid, s, e: div_calls.append(sid),
    )
    weekly.main()
    assert len(div_calls) == 50, (
        f"期望 50 個 dividend 呼叫(TW_TOP_50),實際 {len(div_calls)}"
    )


def test_main_csvs_have_expected_data(tmp_env, monkeypatch):
    monkeypatch.setattr(
        weekly, "update_long_term_data_free", _fake_update_success,
    )
    weekly.main()
    snapshot = weekly.SNAPSHOT_DIR

    df = pd.read_csv(snapshot / "daily_metrics.csv")
    assert len(df) == 2  # mock 寫了前 2 檔
    assert "pe" in df.columns and "pb" in df.columns

    df = pd.read_csv(snapshot / "financials_quarterly.csv")
    assert len(df) == 2
    assert "roe" in df.columns and "eps" in df.columns

    df = pd.read_csv(snapshot / "stocks.csv")
    assert len(df) == 50  # TW_TOP_50 全部
    assert "industry" in df.columns


def test_main_returns_1_when_all_fail(tmp_env, monkeypatch, capsys):
    monkeypatch.setattr(
        weekly, "update_long_term_data_free", _fake_update_all_fail,
    )
    code = weekly.main()
    assert code == 1
    captured = capsys.readouterr()
    assert "全部 fail" in captured.out
    # CSV 不該被寫(因為沒成功)
    assert not (weekly.SNAPSHOT_DIR / "daily_metrics.csv").exists()
