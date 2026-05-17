"""scripts/backfill_financials.py 單元測試。

scripts/ 不是 package,用 importlib 載入。Mock fetch_quarterly_financials 驗
skip-existing + idempotent + dump CSV。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "backfill_financials.py"
)
_spec = importlib.util.spec_from_file_location("backfill_financials", _SCRIPT)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """tmp DB,init schema。"""
    db_file = tmp_path / "fin.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    return db_file


def _seed_financials(rows: list[tuple[str, str]]) -> None:
    """rows: list of (stock_id, period). period_type 一律 'quarterly'。"""
    payload = [
        {
            "stock_id": sid, "period_type": "quarterly", "period": p,
            "revenue": None, "revenue_yoy": None,
            "eps": 1.0, "roe": 10.0,
        }
        for (sid, p) in rows
    ]
    db.upsert_financials(payload)


def _seed_stocks_with_history(sids: list[str]) -> None:
    """讓 pure_stock_universe(min_history=20) 撈得到這些 sid。"""
    # stocks 表 + daily_prices 表都要有,且 daily_prices ≥ 20 天
    db.upsert_stocks([
        {"stock_id": s, "name": f"name_{s}", "market": "TW"}
        for s in sids
    ])
    rows = []
    for sid in sids:
        for d in range(25):
            rows.append({
                "stock_id": sid,
                "date": f"2026-04-{(d + 1):02d}",
                "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
                "volume": 1000,
            })
    with db.get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO daily_prices
               (stock_id, date, open, high, low, close, volume)
               VALUES (:stock_id, :date, :open, :high, :low, :close, :volume)""",
            rows,
        )


def test_stocks_with_financials_basic(tmp_db):
    """有 quarterly 的 sid 才回。monthly_revenue 不算。"""
    _seed_financials([("2330", "2026-Q1"), ("2317", "2025-Q4")])
    # 加一筆 monthly_revenue 不該被當「有 quarterly」
    db.upsert_financials([{
        "stock_id": "9999", "period_type": "monthly_revenue",
        "period": "2026-04", "revenue": 100.0,
        "revenue_yoy": None, "eps": None, "roe": None,
    }])

    have = bf.stocks_with_financials()
    assert have == {"2330", "2317"}
    assert "9999" not in have


def test_backfill_one_ok(tmp_db):
    """fetch_fn 回非空 DF → ok=True, status='ok'。"""
    fake_df = pd.DataFrame([
        {"stock_id": "2330", "period_type": "quarterly", "period": "2026-Q1",
         "revenue": None, "revenue_yoy": None, "eps": 9.5, "roe": 25.0},
    ])
    ok, status = bf.backfill_one(
        "2330", "2021-01-01", "2026-05-16",
        fetch_fn=lambda sid, s, e: fake_df,
    )
    assert ok and status == "ok"


def test_backfill_one_empty(tmp_db):
    """fetch_fn 回空 DF → ok=False, status='empty'(非錯誤,ETF / 受益憑證 常見)。"""
    ok, status = bf.backfill_one(
        "0050", "2021-01-01", "2026-05-16",
        fetch_fn=lambda sid, s, e: pd.DataFrame(),
    )
    assert not ok and status == "empty"


def test_backfill_one_error_swallowed(tmp_db):
    """fetch_fn 拋例外 → ok=False, status='error',不傳到上層。"""
    def raises(sid, s, e):
        raise RuntimeError("FinMind quota exhausted")

    ok, status = bf.backfill_one(
        "2330", "2021-01-01", "2026-05-16", fetch_fn=raises,
    )
    assert not ok and status == "error"


def test_main_skips_already_filled(tmp_db, monkeypatch, capsys):
    """已在 financials 表的 sid 不會打 fetch_quarterly_financials。"""
    _seed_stocks_with_history(["2330", "2317", "1101"])
    # 2330 / 2317 已有 financials,只有 1101 該被打
    _seed_financials([("2330", "2026-Q1"), ("2317", "2025-Q4")])

    calls: list[str] = []

    def fake_fetch(sid, s, e):
        calls.append(sid)
        return pd.DataFrame([{
            "stock_id": sid, "period_type": "quarterly", "period": "2026-Q1",
            "revenue": None, "revenue_yoy": None, "eps": 1.0, "roe": 5.0,
        }])

    monkeypatch.setattr(
        "src.data_fetcher.fetch_quarterly_financials", fake_fetch,
    )

    rc = bf.main(["--sleep", "0", "--progress-every", "1"])
    assert rc == 0
    assert calls == ["1101"]


def test_main_force_overrides_skip(tmp_db, monkeypatch):
    """--force 一律打,即使已有資料。"""
    _seed_stocks_with_history(["2330", "1101"])
    _seed_financials([("2330", "2026-Q1")])

    calls: list[str] = []

    def fake_fetch(sid, s, e):
        calls.append(sid)
        return pd.DataFrame([{
            "stock_id": sid, "period_type": "quarterly", "period": "2026-Q1",
            "revenue": None, "revenue_yoy": None, "eps": 1.0, "roe": 5.0,
        }])

    monkeypatch.setattr(
        "src.data_fetcher.fetch_quarterly_financials", fake_fetch,
    )

    rc = bf.main(["--force", "--sleep", "0", "--progress-every", "1"])
    assert rc == 0
    # 兩個都被打(順序由 pure_stock_universe 決定)
    assert set(calls) == {"2330", "1101"}


def test_main_empty_universe_returns_1(tmp_db, monkeypatch, capsys):
    """pure_stock universe 空 → 印警告 + exit 1。"""
    # 不 seed stocks_with_history → universe 空
    rc = bf.main(["--sleep", "0"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "universe 為空" in out


def test_main_dump_csv(tmp_db, monkeypatch, tmp_path):
    """--dump-csv 寫進 SNAPSHOT_DIR/financials_quarterly.csv。"""
    monkeypatch.setattr(bf, "SNAPSHOT_DIR", tmp_path / "snap")
    _seed_stocks_with_history(["2330"])
    _seed_financials([("2330", "2026-Q1")])

    rc = bf.main(["--dump-csv", "--sleep", "0", "--progress-every", "1"])
    assert rc == 0
    out_csv = tmp_path / "snap" / "financials_quarterly.csv"
    assert out_csv.exists()
    df = pd.read_csv(out_csv, dtype={"stock_id": str})
    assert "2330" in df["stock_id"].values


def test_main_high_failure_returns_1(tmp_db, monkeypatch):
    """超過 25% 檔 fetch error → exit 1 (上限為 max(20, n//4))。
    使用 25 檔 → n//4 = 6, max(20, 6) = 20 → 觸發要 fail > 20。"""
    sids = [f"{1000 + i:04d}" for i in range(25)]
    _seed_stocks_with_history(sids)

    def fail(sid, s, e):
        raise RuntimeError("API down")

    monkeypatch.setattr(
        "src.data_fetcher.fetch_quarterly_financials", fail,
    )

    rc = bf.main(["--sleep", "0", "--progress-every", "100"])
    # 25 檔全 fail > max(20, 25//4=6)=20 → exit 1
    assert rc == 1


# === 402 fail-fast(quota 爆)===

def test_backfill_one_propagates_quota_error(tmp_db):
    """fetch_fn 拋 FinMindQuotaError → backfill_one 不該 swallow,該 raise 給上層。"""
    from src.data_fetcher import FinMindQuotaError

    def quota(sid, s, e):
        raise FinMindQuotaError("status=402 quota exhausted")

    with pytest.raises(FinMindQuotaError):
        bf.backfill_one("2330", "2021-01-01", "2026-05-16", fetch_fn=quota)


def test_main_quota_error_aborts_immediately(tmp_db, monkeypatch, capsys):
    """第 1 檔就撞 quota → 整批中斷,不繼續打剩下的檔,exit 1。"""
    from src.data_fetcher import FinMindQuotaError

    sids = [f"{1000 + i:04d}" for i in range(10)]
    _seed_stocks_with_history(sids)

    calls: list[str] = []

    def quota_first(sid, s, e):
        calls.append(sid)
        if len(calls) >= 3:  # 第 3 檔開始 quota 爆(模擬中途撞)
            raise FinMindQuotaError(
                "status=402 quota — 改日再跑或加 token"
            )
        return pd.DataFrame([{
            "stock_id": sid, "period_type": "quarterly", "period": "2026-Q1",
            "revenue": None, "revenue_yoy": None, "eps": 1.0, "roe": 5.0,
        }])

    monkeypatch.setattr(
        "src.data_fetcher.fetch_quarterly_financials", quota_first,
    )

    rc = bf.main(["--sleep", "0", "--progress-every", "100"])
    # 第 3 檔 raise → 第 4 ~ 10 檔不該被打
    assert len(calls) == 3
    assert rc == 1
    err = capsys.readouterr().err
    # 警告 message 該提到 quota
    assert "quota" in err.lower()


def test_with_retry_no_retry_on_quota_error(monkeypatch):
    """with_retry 對 no_retry_exceptions 該 fail-fast(0 retry / 0 sleep)。

    驗 long-backoff schedule 對 FinMindQuotaError 不生效 — 不會耗 60+120+300+...s
    sleep 卡住 batch。
    """
    import time as _time
    from src._retry import with_retry
    from src.data_fetcher import FinMindQuotaError

    sleep_calls: list[float] = []
    monkeypatch.setattr(_time, "sleep", lambda s: sleep_calls.append(s))

    call_count = {"n": 0}

    def attempt():
        call_count["n"] += 1
        raise FinMindQuotaError("status=402 quota")

    with pytest.raises(FinMindQuotaError):
        with_retry(
            attempt, delays=[60, 120, 300, 600, 900],
            label="test", no_retry_exceptions=(FinMindQuotaError,),
        )
    # 一次嘗試 → fail-fast → 不該 retry / 不該 sleep
    assert call_count["n"] == 1
    assert sleep_calls == []


# === Batch range / max-stocks ===

def test_main_batch_range_subsets_universe(tmp_db, monkeypatch):
    """--batch-start / --batch-end 該切 universe 視窗,只打範圍內的 sid。"""
    sids = [f"{1000 + i:04d}" for i in range(10)]
    _seed_stocks_with_history(sids)

    calls: list[str] = []

    def fake_fetch(sid, s, e):
        calls.append(sid)
        return pd.DataFrame([{
            "stock_id": sid, "period_type": "quarterly", "period": "2026-Q1",
            "revenue": None, "revenue_yoy": None, "eps": 1.0, "roe": 5.0,
        }])

    monkeypatch.setattr(
        "src.data_fetcher.fetch_quarterly_financials", fake_fetch,
    )

    rc = bf.main([
        "--batch-start", "2", "--batch-end", "5",
        "--sleep", "0", "--progress-every", "100",
    ])
    assert rc == 0
    # universe 排序 = 1000, 1001, ..., 1009;[2:5] = 1002, 1003, 1004
    assert set(calls) == {"1002", "1003", "1004"}


def test_main_max_stocks_caps_run(tmp_db, monkeypatch):
    """--max-stocks 限單次 run 上限,避免一次燒光 quota。"""
    sids = [f"{1000 + i:04d}" for i in range(20)]
    _seed_stocks_with_history(sids)

    calls: list[str] = []

    def fake_fetch(sid, s, e):
        calls.append(sid)
        return pd.DataFrame([{
            "stock_id": sid, "period_type": "quarterly", "period": "2026-Q1",
            "revenue": None, "revenue_yoy": None, "eps": 1.0, "roe": 5.0,
        }])

    monkeypatch.setattr(
        "src.data_fetcher.fetch_quarterly_financials", fake_fetch,
    )

    rc = bf.main([
        "--max-stocks", "5",
        "--sleep", "0", "--progress-every", "100",
    ])
    assert rc == 0
    # 20 待補但 max-stocks=5 → 只打 5 檔
    assert len(calls) == 5


def test_main_batch_start_beyond_universe_exits_0(tmp_db, monkeypatch, capsys):
    """--batch-start 超過 universe size → 沒事可做,exit 0(不算錯)。"""
    sids = [f"{1000 + i:04d}" for i in range(3)]
    _seed_stocks_with_history(sids)

    calls: list[str] = []

    def fake_fetch(sid, s, e):
        calls.append(sid)
        return pd.DataFrame()

    monkeypatch.setattr(
        "src.data_fetcher.fetch_quarterly_financials", fake_fetch,
    )

    rc = bf.main([
        "--batch-start", "100", "--batch-end", "200",
        "--sleep", "0",
    ])
    assert rc == 0
    assert calls == []
    assert "nothing to do" in capsys.readouterr().out


def test_main_batch_start_ge_end_exits_2(tmp_db, monkeypatch, capsys):
    """--batch-start >= --batch-end 該回 exit 2(參數錯)。"""
    sids = [f"{1000 + i:04d}" for i in range(5)]
    _seed_stocks_with_history(sids)

    rc = bf.main([
        "--batch-start", "3", "--batch-end", "3",
        "--sleep", "0",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "區間空" in err
