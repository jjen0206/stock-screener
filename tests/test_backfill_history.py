"""scripts/backfill_history.py 單元測試。

scripts/ 不是 package,用 importlib 載入。mock fetch_daily_price /
fetch_institutional / get_full_universe,用 tmp DB + tmp snapshot dir。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from src import config, database as db


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_history.py"
_spec = importlib.util.spec_from_file_location("backfill_history", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


@pytest.fixture
def tmp_setup(monkeypatch, tmp_path):
    """tmp DB + tmp snapshot dir。"""
    db_file = tmp_path / "backfill.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    snapshot_dir = tmp_path / "twse_snapshot"
    monkeypatch.setattr(backfill, "SNAPSHOT_DIR", snapshot_dir)
    db.init_db()
    return tmp_path


def test_backfill_skips_stocks_with_enough_history(tmp_setup, monkeypatch):
    """daily_prices 已有 >= min-existing 天的個股該跳過。"""
    # 預灌一檔 90 天歷史,另一檔 0 天
    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積", "market": "TW"},
        {"stock_id": "2454", "name": "聯發", "market": "TW"},
    ])
    db.upsert_daily_prices([
        {"stock_id": "2330", "date": f"2026-01-{i:02d}",
         "open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000,
         "trading_money": None, "trading_turnover": None, "spread": 0.0}
        for i in range(1, 31)  # 30 天
    ])
    monkeypatch.setattr(backfill, "get_full_universe", lambda: ["2330", "2454"])
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])

    fetch_calls = []
    monkeypatch.setattr(
        backfill, "fetch_daily_price",
        lambda sid, s, e: fetch_calls.append(sid),
    )
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_history.py", "--days", "10", "--min-existing", "20"],
    )

    code = backfill.main()
    assert code == 0
    # 2330 已有 30 天 >= 20 該跳過,只該抓 2454
    assert fetch_calls == ["2454"]


def test_backfill_dumps_csv_to_snapshot(tmp_setup, monkeypatch):
    """完成後該 dump daily_prices.csv / institutional.csv / stocks.csv。"""
    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積", "market": "TW"},
    ])
    monkeypatch.setattr(backfill, "get_full_universe", lambda: ["2330"])
    monkeypatch.setattr(backfill, "TW_TOP_50", [("2330", "台積")])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])

    def fake_fetch_price(sid, s, e):
        # 模擬 fetch 後資料寫入 DB
        db.upsert_daily_prices([{
            "stock_id": sid, "date": "2026-04-28",
            "open": 1000, "high": 1010, "low": 990, "close": 1000, "volume": 5000,
            "trading_money": None, "trading_turnover": None, "spread": 0.0,
        }])

    monkeypatch.setattr(backfill, "fetch_daily_price", fake_fetch_price)
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_history.py", "--days", "10", "--no-institutional"],
    )

    code = backfill.main()
    assert code == 0
    snapshot = tmp_setup / "twse_snapshot"
    assert (snapshot / "daily_prices.csv").exists()
    assert (snapshot / "institutional.csv").exists()
    assert (snapshot / "stocks.csv").exists()
    assert (snapshot / "last_backfill.txt").exists()

    # CSV 內容該包含剛灌入的資料
    df = pd.read_csv(snapshot / "daily_prices.csv", dtype={"stock_id": str})
    assert "2330" in df["stock_id"].tolist()


def test_backfill_returns_one_when_mostly_failing(tmp_setup, monkeypatch):
    """成功率 < 10% 才該 exit 1(token 過期 / API 故障警示)。"""
    db.upsert_stocks([
        {"stock_id": s, "name": "X", "market": "TW"}
        for s in ["A", "B", "C", "D"]
    ])
    monkeypatch.setattr(
        backfill, "get_full_universe", lambda: ["A", "B", "C", "D"],
    )
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])

    def always_fail(sid, s, e):
        raise RuntimeError(f"FinMind 429 for {sid}")

    monkeypatch.setattr(backfill, "fetch_daily_price", always_fail)
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv", ["backfill_history.py", "--days", "10"],
    )

    code = backfill.main()
    assert code == 1


def test_backfill_dumps_and_preloads_watchlist(tmp_setup, monkeypatch):
    """watchlist.csv 該被 preload 進 SQLite + dump 出來不 clobber。"""
    snapshot = tmp_setup / "twse_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    # 預先放一份 watchlist.csv 在 snapshot dir(模擬 repo 既有狀態)
    pd.DataFrame([
        {"stock_id": "2330", "added_at": "2026-04-01T00:00:00", "note": "台積"},
        {"stock_id": "2454", "added_at": "2026-04-02T00:00:00", "note": None},
    ]).to_csv(snapshot / "watchlist.csv", index=False)

    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積", "market": "TW"},
    ])
    monkeypatch.setattr(backfill, "get_full_universe", lambda: ["2330"])
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])
    monkeypatch.setattr(backfill, "fetch_daily_price", lambda *a: None)
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_history.py", "--days", "10", "--no-institutional"],
    )

    code = backfill.main()
    assert code == 0
    # CSV 該被 preserve(2 筆都還在,不被 empty SQLite clobber)
    df = pd.read_csv(snapshot / "watchlist.csv", dtype={"stock_id": str})
    assert sorted(df["stock_id"].tolist()) == ["2330", "2454"]
    # 同時 SQLite 也該被 preload(get_watchlist 該回 2 筆)
    items = db.get_watchlist()
    assert sorted(it["stock_id"] for it in items) == ["2330", "2454"]


# === preload daily_prices / institutional CSV(cross-run checkpoint) ===


def test_preload_daily_prices_csv_loads_into_sqlite(tmp_setup):
    """有 daily_prices.csv → 該 upsert 進 SQLite,行數 / 股票數要對。"""
    snapshot = tmp_setup / "twse_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    rows = []
    for sid in ["2330", "2454"]:
        for i in range(5):
            rows.append({
                "stock_id": sid, "date": f"2026-04-{20+i:02d}",
                "open": 100, "high": 101, "low": 99, "close": 100,
                "volume": 1000, "trading_money": None,
                "trading_turnover": None, "spread": 0.0,
            })
    pd.DataFrame(rows).to_csv(snapshot / "daily_prices.csv", index=False)

    backfill._preload_daily_prices_csv()

    with db.get_conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
        stocks = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT stock_id FROM daily_prices"
            )
        }
    assert cnt == 10
    assert stocks == {"2330", "2454"}


def test_preload_daily_prices_csv_silent_when_missing(tmp_setup):
    """沒 CSV 時不該 raise(本機 dev 路徑)。"""
    # snapshot dir 不存在
    backfill._preload_daily_prices_csv()
    # 也包含 dir 存在但 csv 缺
    snapshot = tmp_setup / "twse_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    backfill._preload_daily_prices_csv()


def test_preload_institutional_csv_loads_into_sqlite(tmp_setup):
    """institutional.csv 同樣該被讀回。"""
    snapshot = tmp_setup / "twse_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {
            "stock_id": "2330", "date": "2026-04-25",
            "foreign": 1000, "investment_trust": 200, "dealer": -50,
        },
        {
            "stock_id": "2330", "date": "2026-04-26",
            "foreign": -300, "investment_trust": 0, "dealer": 100,
        },
    ]).to_csv(snapshot / "institutional.csv", index=False)

    backfill._preload_institutional_csv()

    with db.get_conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM institutional").fetchone()[0]
    assert cnt == 2


def test_preload_institutional_csv_silent_when_missing(tmp_setup):
    """沒 institutional.csv 也不炸。"""
    backfill._preload_institutional_csv()


def test_main_skips_preloaded_stocks_via_csv(tmp_setup, monkeypatch):
    """整合測試:把已有 CSV 當 checkpoint,main() 該 skip 已達閾值的個股。"""
    snapshot = tmp_setup / "twse_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    # 預寫 daily_prices.csv 給 2330(35 天 >= min-existing 30)
    rows = []
    for i in range(35):
        rows.append({
            "stock_id": "2330", "date": f"2026-03-{i+1:02d}",
            "open": 100, "high": 101, "low": 99, "close": 100,
            "volume": 1000, "trading_money": None,
            "trading_turnover": None, "spread": 0.0,
        })
    pd.DataFrame(rows).to_csv(snapshot / "daily_prices.csv", index=False)

    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積", "market": "TW"},
        {"stock_id": "2454", "name": "聯發", "market": "TW"},
    ])
    monkeypatch.setattr(backfill, "get_full_universe", lambda: ["2330", "2454"])
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])

    fetch_calls = []
    monkeypatch.setattr(
        backfill, "fetch_daily_price",
        lambda sid, s, e: fetch_calls.append(sid),
    )
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_history.py", "--days", "10", "--min-existing", "30",
         "--no-institutional"],
    )

    code = backfill.main()
    assert code == 0
    # 2330 從 CSV preload 進來 35 天 >= 30,該跳過;只該抓 2454
    assert fetch_calls == ["2454"]


# === Shard 模式 ===


def test_shard_filter_distributes_evenly():
    """sorted(universe)[shard::total_shards] 該均勻分桶。"""
    universe = [f"S{i:04d}" for i in range(80)]
    buckets = [
        backfill._shard_filter(universe, k, 8) for k in range(8)
    ]
    # 每個 shard 各 10 檔
    for b in buckets:
        assert len(b) == 10
    # 8 shard 加總 = 完整 universe
    all_seen = sorted(s for b in buckets for s in b)
    assert all_seen == sorted(universe)
    # 不重複
    seen_set = set()
    for b in buckets:
        for s in b:
            assert s not in seen_set, f"{s} 重複"
            seen_set.add(s)


def test_shard_filter_uneven_division():
    """universe 不能整除時,前幾個 shard 多 1 檔。"""
    universe = [f"S{i:03d}" for i in range(10)]  # 10 檔切 3 shard
    b0 = backfill._shard_filter(universe, 0, 3)
    b1 = backfill._shard_filter(universe, 1, 3)
    b2 = backfill._shard_filter(universe, 2, 3)
    assert len(b0) + len(b1) + len(b2) == 10
    # stride=3:S000,S003,S006,S009 / S001,S004,S007 / S002,S005,S008
    assert b0 == ["S000", "S003", "S006", "S009"]
    assert b1 == ["S001", "S004", "S007"]
    assert b2 == ["S002", "S005", "S008"]


def test_shard_mode_only_processes_own_shard(tmp_setup, monkeypatch):
    """--shard 1 --total-shards 4 → 只跑自己 shard 內的個股。"""
    sids = [f"S{i:03d}" for i in range(8)]  # 8 檔
    db.upsert_stocks([
        {"stock_id": s, "name": "X", "market": "TW"} for s in sids
    ])
    monkeypatch.setattr(backfill, "get_full_universe", lambda: sids)
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])

    fetched: list[str] = []
    monkeypatch.setattr(
        backfill, "fetch_daily_price",
        lambda sid, s, e: fetched.append(sid),
    )
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "backfill_history.py",
            "--days", "10",
            "--min-existing", "1",  # 全當待補(空 DB → existing=0 < 1)
            "--shard", "1",
            "--total-shards", "4",
            "--no-institutional",
        ],
    )

    code = backfill.main()
    assert code == 0
    # sorted(sids)[1::4] = S001, S005
    assert fetched == ["S001", "S005"]


def test_shard_mode_dumps_shard_csv_only(tmp_setup, monkeypatch):
    """shard 模式只寫 daily_prices_shard_K.csv,不寫 daily_prices.csv 整份。"""
    sids = ["S000", "S001", "S002", "S003"]
    db.upsert_stocks([
        {"stock_id": s, "name": "X", "market": "TW"} for s in sids
    ])
    monkeypatch.setattr(backfill, "get_full_universe", lambda: sids)
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])

    def fake_fetch_price(sid, s, e):
        db.upsert_daily_prices([{
            "stock_id": sid, "date": "2026-04-28",
            "open": 100, "high": 101, "low": 99, "close": 100,
            "volume": 1000, "trading_money": None,
            "trading_turnover": None, "spread": 0.0,
        }])

    monkeypatch.setattr(backfill, "fetch_daily_price", fake_fetch_price)
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "backfill_history.py",
            "--days", "10",
            "--min-existing", "1",
            "--shard", "0",
            "--total-shards", "2",
            "--no-institutional",
        ],
    )

    code = backfill.main()
    assert code == 0
    snapshot = tmp_setup / "twse_snapshot"
    # shard 模式 → 該寫 daily_prices_shard_0.csv 跟 last_backfill_shard_0.txt
    assert (snapshot / "daily_prices_shard_0.csv").exists()
    assert (snapshot / "institutional_shard_0.csv").exists()
    assert (snapshot / "last_backfill_shard_0.txt").exists()
    # 不該寫整份 daily_prices.csv / stocks.csv / watchlist.csv(由 aggregator 處理)
    assert not (snapshot / "daily_prices.csv").exists()
    assert not (snapshot / "stocks.csv").exists()
    assert not (snapshot / "last_backfill.txt").exists()
    # shard csv 只該含此 shard 的個股(S000, S002)— 不含 S001, S003
    df = pd.read_csv(
        snapshot / "daily_prices_shard_0.csv", dtype={"stock_id": str},
    )
    assert sorted(df["stock_id"].unique().tolist()) == ["S000", "S002"]


def test_shard_mode_validates_shard_args(tmp_setup, monkeypatch):
    """只給 --shard 沒給 --total-shards 該 exit 2。"""
    monkeypatch.setattr(backfill, "get_full_universe", lambda: ["S000"])
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_history.py", "--days", "10", "--shard", "0"],
    )
    code = backfill.main()
    assert code == 2


def test_shard_mode_rejects_out_of_range_shard(tmp_setup, monkeypatch):
    """--shard 5 --total-shards 4 該 exit 2。"""
    monkeypatch.setattr(backfill, "get_full_universe", lambda: ["S000"])
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])
    monkeypatch.setattr(
        "sys.argv",
        [
            "backfill_history.py", "--days", "10",
            "--shard", "5", "--total-shards", "4",
        ],
    )
    code = backfill.main()
    assert code == 2


def test_shard_mode_writes_empty_csvs_when_nothing_to_do(tmp_setup, monkeypatch):
    """shard 內沒待補(全 checkpoint 命中)時,仍寫空 shard csv 讓 aggregator 一致。"""
    sids = ["S000", "S001"]
    db.upsert_stocks([
        {"stock_id": s, "name": "X", "market": "TW"} for s in sids
    ])
    # 預灌 S000 滿足 min-existing
    db.upsert_daily_prices([
        {"stock_id": "S000", "date": f"2026-01-{i:02d}",
         "open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000,
         "trading_money": None, "trading_turnover": None, "spread": 0.0}
        for i in range(1, 35)
    ])
    monkeypatch.setattr(backfill, "get_full_universe", lambda: sids)
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])
    monkeypatch.setattr(backfill, "fetch_daily_price", lambda *a: None)
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "backfill_history.py",
            "--days", "10",
            "--min-existing", "30",
            "--shard", "0",  # 只跑 S000(已滿)
            "--total-shards", "2",
            "--no-institutional",
        ],
    )

    code = backfill.main()
    assert code == 0
    snapshot = tmp_setup / "twse_snapshot"
    # 仍該有 shard csv(空)
    assert (snapshot / "daily_prices_shard_0.csv").exists()
    df = pd.read_csv(
        snapshot / "daily_prices_shard_0.csv", dtype={"stock_id": str},
    )
    assert df.empty


# === 原有測試延續 ===


def test_backfill_returns_zero_when_partial_success(tmp_setup, monkeypatch):
    """成功率 10-50% 偏低但仍 commit(避免「1305 檔成果被丟」的 bug)。"""
    sids = [f"S{i:02d}" for i in range(10)]
    db.upsert_stocks([
        {"stock_id": s, "name": "X", "market": "TW"} for s in sids
    ])
    monkeypatch.setattr(backfill, "get_full_universe", lambda: sids)
    monkeypatch.setattr(backfill, "TW_TOP_50", [])
    monkeypatch.setattr(backfill, "load_watchlist", lambda: [])

    # 10 檔中 3 檔成功(30% — 落在 10-50% 區間)
    def fake_fetch_price(sid, s, e):
        if sid in {"S00", "S01", "S02"}:
            db.upsert_daily_prices([{
                "stock_id": sid, "date": "2026-04-28",
                "open": 100, "high": 101, "low": 99, "close": 100,
                "volume": 1000, "trading_money": None,
                "trading_turnover": None, "spread": 0.0,
            }])
            return
        raise RuntimeError(f"FinMind 429 for {sid}")

    monkeypatch.setattr(backfill, "fetch_daily_price", fake_fetch_price)
    monkeypatch.setattr(backfill, "fetch_institutional", lambda *a: None)
    monkeypatch.setattr(
        "sys.argv", ["backfill_history.py", "--days", "10"],
    )

    code = backfill.main()
    assert code == 0  # 部分成功仍 exit 0,讓 workflow commit CSV
    # CSV 該有 dump 出來
    snapshot = tmp_setup / "twse_snapshot"
    df = pd.read_csv(snapshot / "daily_prices.csv", dtype={"stock_id": str})
    assert len(df) >= 3  # 3 檔成功的資料
