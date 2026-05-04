"""scripts/precompute_strategies.py 測試 — mock run_all_strategies + 驗 daily_picks 寫入。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 讓 import scripts.precompute_strategies 找得到
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "precompute.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


def _seed_minimal_universe(monkeypatch):
    """灌幾檔股 + 假設 stocks_with_min_history 回 2 檔,讓 3 個 universe 都有 sids。"""
    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積電", "market": "TW"},
        {"stock_id": "2317", "name": "鴻海", "market": "TW"},
    ])
    monkeypatch.setattr(
        db, "stocks_with_min_history", lambda min_history=20: ["2330", "2317"],
    )
    # preload_snapshots 不用真跑(tmp DB 沒 CSV),no-op
    monkeypatch.setattr(db, "preload_snapshots", lambda: {})


def test_precompute_runs_all_universes_and_writes_daily_picks(
    tmp_db, monkeypatch,
):
    """precompute_for_date 跑 3 個 universe,每個都 dump 進 daily_picks。"""
    from scripts import precompute_strategies as pcs

    _seed_minimal_universe(monkeypatch)

    fake_agg = {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD"],
            "details": {
                "volume_kd": {
                    "stock_id": "2330", "name": "台積電", "close": 600.0,
                },
            },
        },
    }

    call_log: list[tuple] = []

    def _spy(date, enabled=None, params=None, stock_ids=None):
        call_log.append((date, tuple(stock_ids or [])))
        return fake_agg

    monkeypatch.setattr(pcs, "run_all_strategies", _spy)

    results = pcs.precompute_for_date("2026-05-04")

    # 3 個 universe 各被 dump 1 筆(只一個 strategy 命中)
    assert sorted(results.keys()) == ["pure_stock", "top_50", "with_etf"]
    assert results["pure_stock"] == 1
    assert results["with_etf"] == 1
    assert results["top_50"] == 1

    # daily_picks 表內 3 universe × 1 row = 3 rows
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM daily_picks WHERE trade_date=?",
            ("2026-05-04",),
        ).fetchone()["c"]
    assert n == 3

    # run_all_strategies 該被叫 3 次(各 universe 一次)
    assert len(call_log) == 3
    # 都同一個 date
    assert all(d == "2026-05-04" for d, _ in call_log)


def test_precompute_clears_old_data_for_same_date(tmp_db, monkeypatch):
    """rerun 同一天 → 先清舊 + 重寫,沒遺留。"""
    from scripts import precompute_strategies as pcs

    _seed_minimal_universe(monkeypatch)

    # 第一次 run:有 2 檔
    agg_first = {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD"],
            "details": {"volume_kd": {"stock_id": "2330", "close": 600.0}},
        },
        "2317": {
            "name": "鴻海",
            "signals": ["量價KD"],
            "details": {"volume_kd": {"stock_id": "2317", "close": 200.0}},
        },
    }
    monkeypatch.setattr(
        pcs, "run_all_strategies",
        lambda *a, **kw: agg_first,
    )
    pcs.precompute_for_date("2026-05-04")

    with db.get_conn() as conn:
        first_count = conn.execute(
            "SELECT COUNT(*) c FROM daily_picks WHERE trade_date=?",
            ("2026-05-04",),
        ).fetchone()["c"]
    assert first_count == 6  # 3 universe × 2 sid

    # 第二次 run:只 1 檔(模擬資料變動,2317 從 universe 消失)
    agg_second = {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD"],
            "details": {"volume_kd": {"stock_id": "2330", "close": 999.0}},
        },
    }
    monkeypatch.setattr(
        pcs, "run_all_strategies",
        lambda *a, **kw: agg_second,
    )
    pcs.precompute_for_date("2026-05-04")

    with db.get_conn() as conn:
        second_count = conn.execute(
            "SELECT COUNT(*) c FROM daily_picks WHERE trade_date=?",
            ("2026-05-04",),
        ).fetchone()["c"]
    # 3 universe × 1 sid = 3,且 2330 close 應為 999(被覆蓋)
    assert second_count == 3
    loaded = db.load_daily_picks("2026-05-04", "pure_stock")
    assert loaded is not None
    assert "2317" not in loaded  # 舊資料清掉了
    assert loaded["2330"]["details"]["volume_kd"]["close"] == 999.0
