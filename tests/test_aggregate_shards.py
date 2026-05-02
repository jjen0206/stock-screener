"""scripts/aggregate_shards.py 測試。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db


_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "aggregate_shards.py"
)
_spec = importlib.util.spec_from_file_location("aggregate_shards", _SCRIPT)
agg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agg)


@pytest.fixture
def tmp_setup(monkeypatch, tmp_path):
    db_file = tmp_path / "agg.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    snapshot_dir = tmp_path / "twse_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agg, "SNAPSHOT_DIR", snapshot_dir)
    db.init_db()
    return snapshot_dir


def _write_shard_csv(
    snapshot_dir: Path, name: str, rows: list[dict],
) -> None:
    cols = [
        "stock_id", "date", "open", "high", "low", "close",
        "volume", "trading_money", "trading_turnover", "spread",
    ]
    pd.DataFrame(rows, columns=cols).to_csv(
        snapshot_dir / name, index=False,
    )


def _make_row(sid: str, day: int, close: float = 100.0) -> dict:
    return {
        "stock_id": sid, "date": f"2026-04-{day:02d}",
        "open": close, "high": close + 1, "low": close - 1, "close": close,
        "volume": 1000, "trading_money": None,
        "trading_turnover": None, "spread": 0.0,
    }


def test_aggregate_daily_prices_merges_all_shards(tmp_setup):
    """4 個 shard 各寫 1 檔資料 → 合併後 4 檔都在。"""
    for k in range(4):
        _write_shard_csv(
            tmp_setup, f"daily_prices_shard_{k}.csv",
            [_make_row(f"S{k:03d}", 25 + k)],
        )

    shard_total, merged_n = agg.aggregate_daily_prices(total_shards=4)
    assert shard_total == 4
    assert merged_n == 4
    df = pd.read_csv(tmp_setup / "daily_prices.csv", dtype={"stock_id": str})
    assert sorted(df["stock_id"].tolist()) == ["S000", "S001", "S002", "S003"]


def test_aggregate_daily_prices_dedups_with_existing(tmp_setup):
    """既有 daily_prices.csv 有 S000 舊資料,shard 0 也有 S000 同日新資料 →
    shard 的覆蓋既有(keep='last')。
    """
    _write_shard_csv(
        tmp_setup, "daily_prices.csv",
        [_make_row("S000", 25, close=100.0)],
    )
    _write_shard_csv(
        tmp_setup, "daily_prices_shard_0.csv",
        [_make_row("S000", 25, close=999.0)],  # shard 的新值
    )
    # 其他 3 shard 空
    for k in [1, 2, 3]:
        _write_shard_csv(tmp_setup, f"daily_prices_shard_{k}.csv", [])

    agg.aggregate_daily_prices(total_shards=4)
    df = pd.read_csv(tmp_setup / "daily_prices.csv", dtype={"stock_id": str})
    assert len(df) == 1
    # shard 的新值該勝出
    assert df.iloc[0]["close"] == 999.0


def test_aggregate_daily_prices_handles_empty_shards(tmp_setup):
    """全部 shard csv 都空 + 既有也空 → 寫空 csv 不炸。"""
    for k in range(4):
        _write_shard_csv(tmp_setup, f"daily_prices_shard_{k}.csv", [])
    shard_total, merged_n = agg.aggregate_daily_prices(total_shards=4)
    assert shard_total == 0
    assert merged_n == 0
    out = tmp_setup / "daily_prices.csv"
    assert out.exists()


def test_aggregate_daily_prices_missing_shard_csv_skipped(tmp_setup):
    """部分 shard csv 不存在(該 shard fail 沒 commit)→ 仍合其餘的。"""
    # 只有 shard 0, 2 存在
    _write_shard_csv(
        tmp_setup, "daily_prices_shard_0.csv", [_make_row("S000", 25)],
    )
    _write_shard_csv(
        tmp_setup, "daily_prices_shard_2.csv", [_make_row("S002", 26)],
    )
    shard_total, merged_n = agg.aggregate_daily_prices(total_shards=4)
    assert merged_n == 2
    df = pd.read_csv(tmp_setup / "daily_prices.csv", dtype={"stock_id": str})
    assert sorted(df["stock_id"].tolist()) == ["S000", "S002"]


def test_aggregate_institutional_merges(tmp_setup):
    """institutional 同樣的 merge 邏輯。"""
    inst_cols = [
        "stock_id", "date", "foreign_buy_sell", "trust_buy_sell",
        "dealer_buy_sell", "total_buy_sell",
    ]
    pd.DataFrame([
        {"stock_id": "S000", "date": "2026-04-25",
         "foreign_buy_sell": 100, "trust_buy_sell": 50,
         "dealer_buy_sell": 10, "total_buy_sell": 160},
    ], columns=inst_cols).to_csv(
        tmp_setup / "institutional_shard_0.csv", index=False,
    )
    pd.DataFrame([
        {"stock_id": "S001", "date": "2026-04-26",
         "foreign_buy_sell": -50, "trust_buy_sell": 0,
         "dealer_buy_sell": 5, "total_buy_sell": -45},
    ], columns=inst_cols).to_csv(
        tmp_setup / "institutional_shard_1.csv", index=False,
    )

    shard_total, merged_n = agg.aggregate_institutional(total_shards=2)
    assert shard_total == 2
    assert merged_n == 2
    df = pd.read_csv(
        tmp_setup / "institutional.csv", dtype={"stock_id": str},
    )
    assert sorted(df["stock_id"].tolist()) == ["S000", "S001"]


def test_dump_stocks_csv_from_sqlite(tmp_setup):
    """dump_stocks_csv 從 SQLite 拉,排序穩定。"""
    db.upsert_stocks([
        {"stock_id": "2454", "name": "聯發科",
         "market": "TW", "industry": "半導體"},
        {"stock_id": "2330", "name": "台積電",
         "market": "TW", "industry": "半導體"},
    ])
    n = agg.dump_stocks_csv()
    assert n == 2
    df = pd.read_csv(tmp_setup / "stocks.csv", dtype={"stock_id": str})
    # 該按 stock_id 排序
    assert df["stock_id"].tolist() == ["2330", "2454"]


def test_aggregate_last_backfill_sums_shard_stats(tmp_setup):
    """8 shard 各自寫 last_backfill_shard_K.txt → aggregate 加總。"""
    for k in range(4):
        (tmp_setup / f"last_backfill_shard_{k}.txt").write_text(
            f"backfilled_at=2026-05-02T10:0{k}:00\n"
            f"git_sha=abc{k}\n"
            f"run_id=12345\n"
            f"shard={k}\n"
            f"total_shards=4\n"
            f"days_requested=90\n"
            f"todo=100\n"
            f"price_ok=80\n"
            f"price_fail=20\n"
            f"inst_ok=10\n"
            f"inst_fail=2\n"
            f"elapsed_min={20 + k * 5}\n",
            encoding="utf-8",
        )

    totals = agg.aggregate_last_backfill(total_shards=4)
    assert totals["todo"] == 400
    assert totals["price_ok"] == 320
    assert totals["price_fail"] == 80
    assert totals["inst_ok"] == 40
    assert totals["inst_fail"] == 8

    out = (tmp_setup / "last_backfill.txt").read_text(encoding="utf-8")
    assert "shards_completed=4/4" in out
    assert "price_ok=320" in out
    assert "price_success_rate_pct=80.0" in out
    # elapsed 取最大值(瓶頸 shard)
    assert "elapsed_min_max=35.0" in out


def test_aggregate_last_backfill_partial_shards(tmp_setup):
    """部分 shard fail 沒寫 txt → aggregate 仍處理現有的。"""
    (tmp_setup / "last_backfill_shard_0.txt").write_text(
        "todo=100\nprice_ok=50\nprice_fail=50\ninst_ok=0\ninst_fail=0\n"
        "elapsed_min=10.0\n",
        encoding="utf-8",
    )
    totals = agg.aggregate_last_backfill(total_shards=4)
    assert totals["todo"] == 100
    out = (tmp_setup / "last_backfill.txt").read_text(encoding="utf-8")
    assert "shards_completed=1/4" in out


def test_cleanup_shard_files_removes_all(tmp_setup):
    """清理該刪掉所有 shard csv / txt。"""
    for k in range(4):
        (tmp_setup / f"daily_prices_shard_{k}.csv").write_text("a\n")
        (tmp_setup / f"institutional_shard_{k}.csv").write_text("a\n")
        (tmp_setup / f"last_backfill_shard_{k}.txt").write_text("a\n")
    n = agg.cleanup_shard_files(total_shards=4)
    assert n == 12  # 4 × 3
    for k in range(4):
        assert not (tmp_setup / f"daily_prices_shard_{k}.csv").exists()
        assert not (tmp_setup / f"institutional_shard_{k}.csv").exists()
        assert not (tmp_setup / f"last_backfill_shard_{k}.txt").exists()


def test_main_end_to_end(tmp_setup, monkeypatch):
    """完整跑一遍 main():4 shard csv → daily_prices.csv + cleanup。"""
    db.upsert_stocks([
        {"stock_id": "S000", "name": "X", "market": "TW"},
    ])
    for k in range(4):
        _write_shard_csv(
            tmp_setup, f"daily_prices_shard_{k}.csv",
            [_make_row(f"S{k:03d}", 25 + k)],
        )
        # institutional 用空 shard csv
        pd.DataFrame(columns=[
            "stock_id", "date", "foreign_buy_sell", "trust_buy_sell",
            "dealer_buy_sell", "total_buy_sell",
        ]).to_csv(
            tmp_setup / f"institutional_shard_{k}.csv", index=False,
        )
        (tmp_setup / f"last_backfill_shard_{k}.txt").write_text(
            "todo=10\nprice_ok=8\nprice_fail=2\ninst_ok=0\ninst_fail=0\n"
            "elapsed_min=5.0\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        "sys.argv", ["aggregate_shards.py", "--total-shards", "4"],
    )
    code = agg.main()
    assert code == 0
    # 合併後該有完整 daily_prices.csv
    df = pd.read_csv(tmp_setup / "daily_prices.csv", dtype={"stock_id": str})
    assert len(df) == 4
    # last_backfill.txt 有
    assert (tmp_setup / "last_backfill.txt").exists()
    # shard 暫存檔該被清掉
    for k in range(4):
        assert not (tmp_setup / f"daily_prices_shard_{k}.csv").exists()
        assert not (tmp_setup / f"last_backfill_shard_{k}.txt").exists()


def test_main_fails_when_no_shard_csv_exists(tmp_setup, monkeypatch):
    """8 個 shard 都沒寫 csv(全 fail)→ aggregate exit 1。"""
    monkeypatch.setattr(
        "sys.argv", ["aggregate_shards.py", "--total-shards", "4"],
    )
    code = agg.main()
    assert code == 1


def test_main_invalid_total_shards(tmp_setup, monkeypatch):
    """--total-shards 0 該 exit 2。"""
    monkeypatch.setattr(
        "sys.argv", ["aggregate_shards.py", "--total-shards", "0"],
    )
    code = agg.main()
    assert code == 2
