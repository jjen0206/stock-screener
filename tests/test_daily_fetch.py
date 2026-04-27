"""scripts/daily_fetch.py 單元測試。

scripts/ 不是 package,用 importlib 載入。然後 monkeypatch 該 module 內
的 fetch_daily_price / fetch_institutional / TW_TOP_50 來避免打網路與全跑 50 檔。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src import config, database as db


# 動態載 scripts/daily_fetch.py 為 module
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "daily_fetch.py"
_spec = importlib.util.spec_from_file_location("daily_fetch", _SCRIPT)
daily_fetch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daily_fetch)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "fetch.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


@pytest.fixture
def mini_universe(monkeypatch):
    """縮小 universe 加速測試。"""
    monkeypatch.setattr(daily_fetch, "TW_TOP_50", [
        ("2330", "台積電"),
        ("2454", "聯發科"),
        ("2317", "鴻海"),
    ])


def test_run_calls_both_fetchers_per_stock(tmp_db, mini_universe, monkeypatch, capsys):
    """每檔該呼叫 daily_price + institutional 各 1 次。"""
    calls = {"price": [], "inst": []}

    monkeypatch.setattr(
        daily_fetch, "fetch_daily_price",
        lambda sid, s, e: calls["price"].append(sid),
    )
    monkeypatch.setattr(
        daily_fetch, "fetch_institutional",
        lambda sid, s, e: calls["inst"].append(sid),
    )

    summary = daily_fetch.run(days=7)
    captured = capsys.readouterr()

    assert calls["price"] == ["2330", "2454", "2317"]
    assert calls["inst"] == ["2330", "2454", "2317"]
    assert summary == {"price_ok": 3, "inst_ok": 3, "total": 3, "days": 7}
    # 每檔該印一行進度
    assert "[1/3] 2330" in captured.out
    assert "[3/3] 2317" in captured.out
    assert "done. price ok=3/3, inst ok=3/3" in captured.out


def test_run_continues_on_failure(tmp_db, mini_universe, monkeypatch, capsys):
    """單檔 raise 不中斷整批,summary 反映實際成功數。"""
    def fake_price(sid, s, e):
        if sid == "2454":
            raise RuntimeError("simulated FinMind 429")
    def fake_inst(sid, s, e):
        if sid == "2317":
            raise RuntimeError("simulated network")

    monkeypatch.setattr(daily_fetch, "fetch_daily_price", fake_price)
    monkeypatch.setattr(daily_fetch, "fetch_institutional", fake_inst)

    summary = daily_fetch.run(days=7)
    captured = capsys.readouterr()

    assert summary["price_ok"] == 2  # 2454 失敗
    assert summary["inst_ok"] == 2  # 2317 失敗
    assert summary["total"] == 3
    # 失敗該印 FAIL
    assert "2454" in captured.out and "FAIL" in captured.out
    assert "2317" in captured.out
    assert "done. price ok=2/3, inst ok=2/3" in captured.out


def test_run_seeds_stocks_table(tmp_db, mini_universe, monkeypatch):
    """run() 該把 universe 的個股灌進 stocks 表(讓 screen_short 能找到)。"""
    monkeypatch.setattr(daily_fetch, "fetch_daily_price", lambda *a: None)
    monkeypatch.setattr(daily_fetch, "fetch_institutional", lambda *a: None)

    daily_fetch.run(days=7)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id, name FROM stocks WHERE market='TW' ORDER BY stock_id"
        ).fetchall()
    sids = [r["stock_id"] for r in rows]
    assert "2330" in sids and "2454" in sids and "2317" in sids


def test_main_returns_zero_even_on_partial_failure(
    tmp_db, mini_universe, monkeypatch
):
    """exit code 0 即使部分失敗(GitHub Actions 不要因部分失敗整個紅)。"""
    def fake_price(sid, s, e):
        raise RuntimeError("all fail")
    monkeypatch.setattr(daily_fetch, "fetch_daily_price", fake_price)
    monkeypatch.setattr(daily_fetch, "fetch_institutional", lambda *a: None)
    # mock argv 給 argparse
    monkeypatch.setattr("sys.argv", ["daily_fetch.py", "--days", "7"])

    code = daily_fetch.main()
    assert code == 0
