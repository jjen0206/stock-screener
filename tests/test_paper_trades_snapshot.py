"""src/paper_trades_snapshot.py 單元測試。

Pattern 跟 test_e2e_smoke.py 內 portfolio_snapshot 那段對齊。涵蓋:
  - dump → load round-trip 資料一致
  - dump 在 tmp DB(不在 PROJECT_ROOT)時 silent skip 回 -1
  - load 在表已有資料時 skip(避免覆蓋本機新加的)
  - safe_boot_load remote / fallback 路徑
  - add_paper_trade / bulk_add_paper_trades / evaluate_active_trades
    觸發 dump(實際路徑為 tmp_path,而非 repo)
"""
from __future__ import annotations

import json

import pytest

from src import config, database as db, paper_trading as pt, paper_trades_snapshot


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """獨立 SQLite + 確保不開 GH push thread(沒設 GITHUB_PAT)。"""
    db_file = tmp_path / "paper.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    yield tmp_path
    db._reset_path_cache()


def _seed_prices(sid: str, rows: list[dict]) -> None:
    with db.get_conn() as conn:
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO daily_prices "
                "(stock_id, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, r["date"], r.get("open", r["close"]),
                 r["high"], r["low"], r["close"],
                 r.get("volume", 1000)),
            )


def test_dump_then_load_roundtrip(tmp_db):
    """paper_trades 表 → CSV → 清表 → 從 CSV load 回來,核心欄位一致。"""
    pt.add_paper_trade(
        sid="2330", name="台積電",
        entry_date="2026-05-04", entry_price=600.0,
        matched_strategies=["ma_alignment", "macd_golden"],
        ml_prob=0.72,
    )
    pt.add_paper_trade(
        sid="2317", name="鴻海",
        entry_date="2026-05-04", entry_price=200.0,
        matched_strategies=["macd_golden"],
        ml_prob=0.65,
    )

    n = paper_trades_snapshot.dump_to_csv(snapshot_dir=tmp_db)
    assert n == 2
    csv_path = tmp_db / "paper_trades.csv"
    assert csv_path.exists()

    # 清空表
    with db.get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")
    assert _count_rows() == 0

    n_loaded = paper_trades_snapshot.load_from_csv(snapshot_dir=tmp_db)
    assert n_loaded == 2

    rows = _all_rows()
    by_sid = {r["sid"]: r for r in rows}
    assert by_sid["2330"]["entry_price"] == pytest.approx(600.0)
    assert by_sid["2330"]["target_price"] == pytest.approx(630.0)
    assert by_sid["2330"]["stop_price"] == pytest.approx(582.0)
    assert by_sid["2330"]["status"] == "active"
    assert by_sid["2330"]["ml_prob"] == pytest.approx(0.72)
    matched = json.loads(by_sid["2330"]["matched_strategies"])
    assert "ma_alignment" in matched and "macd_golden" in matched

    assert by_sid["2317"]["entry_price"] == pytest.approx(200.0)
    assert by_sid["2317"]["ml_prob"] == pytest.approx(0.65)


def test_dump_silent_skip_outside_project(tmp_db):
    """tmp DB(不在 PROJECT_ROOT 內)→ dump_to_csv() 預設 snapshot_dir=None
    時應 silent skip 回 -1,避免污染 repo paper_trades.csv。
    """
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)
    n = paper_trades_snapshot.dump_to_csv()  # 預設 snapshot_dir=None
    assert n == -1


def test_load_skip_when_table_not_empty(tmp_db):
    """paper_trades 表已有資料 → load_from_csv 整個 skip(避免覆蓋本機新加的)。"""
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)
    paper_trades_snapshot.dump_to_csv(snapshot_dir=tmp_db)
    pt.add_paper_trade("2317", "鴻海", "2026-05-04", 200.0)
    assert _count_rows() == 2

    n = paper_trades_snapshot.load_from_csv(snapshot_dir=tmp_db)
    assert n == 0  # 表已有資料 → 跳過
    assert _count_rows() == 2


def test_load_from_string_roundtrip(tmp_db):
    """dump_to_string → 清表 → load_from_string 還原。"""
    pt.add_paper_trade(
        sid="2330", name="台積電", entry_date="2026-05-04", entry_price=600.0,
        matched_strategies=["ma_alignment"], ml_prob=0.72,
    )
    csv_text = paper_trades_snapshot.dump_to_string()
    assert "2330" in csv_text and "ma_alignment" in csv_text

    with db.get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")
    n = paper_trades_snapshot.load_from_string(csv_text)
    assert n == 1
    rows = _all_rows()
    assert rows[0]["sid"] == "2330"
    assert rows[0]["status"] == "active"


def test_load_from_string_empty_string_returns_zero(tmp_db):
    assert paper_trades_snapshot.load_from_string("") == 0
    assert paper_trades_snapshot.load_from_string("   \n") == 0


def test_safe_boot_load_remote_path(tmp_db, monkeypatch):
    """fetch 回 csv text → load_from_string 灌進 SQLite,result='remote'。"""
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)
    csv_text = paper_trades_snapshot.dump_to_string()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")

    from src import github_sync
    monkeypatch.setattr(
        github_sync, "fetch_paper_trades_from_github", lambda: csv_text,
    )

    result = paper_trades_snapshot.safe_boot_load()
    assert result == "remote"
    assert _count_rows() == 1


def test_safe_boot_load_fallback_no_remote(tmp_db, monkeypatch):
    """fetch 回 None → fallback 本機 load_from_csv,result='fallback-no-remote'。"""
    from src import github_sync
    monkeypatch.setattr(
        github_sync, "fetch_paper_trades_from_github", lambda: None,
    )
    result = paper_trades_snapshot.safe_boot_load()
    assert result == "fallback-no-remote"


def test_safe_boot_load_fetch_exception(tmp_db, monkeypatch):
    """fetch 拋例外 → safe_boot_load 不 raise,走 fallback。"""
    from src import github_sync

    def _raise() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(github_sync, "fetch_paper_trades_from_github", _raise)
    result = paper_trades_snapshot.safe_boot_load()
    assert result == "fallback-fetch-exception"


def test_add_paper_trade_dumps_csv(tmp_db, monkeypatch):
    """add_paper_trade 成功後應呼叫 _dump_paper_trades_snapshot。

    tmp DB 路徑下預設 dump_to_csv 會 silent skip(回 -1),但 helper 仍應被
    呼叫一次 — 用 monkeypatch 攔截 helper 驗證。
    """
    calls: list[object] = []
    real = db._dump_paper_trades_snapshot

    def _spy(db_path):
        calls.append(db_path)
        return real(db_path)

    monkeypatch.setattr(db, "_dump_paper_trades_snapshot", _spy)
    new_id = pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)
    assert new_id is not None
    assert len(calls) == 1, "add_paper_trade 應觸發 dump 一次"


def test_add_paper_trade_duplicate_does_not_dump(tmp_db, monkeypatch):
    """重複 add(UNIQUE 衝突)→ 不該觸發 dump。"""
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)

    calls: list[object] = []

    def _spy(db_path):
        calls.append(db_path)

    monkeypatch.setattr(db, "_dump_paper_trades_snapshot", _spy)
    dup = pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)
    assert dup is None
    assert len(calls) == 0, "重複 add 不該觸發 dump"


def test_bulk_add_dumps_only_once(tmp_db, monkeypatch):
    """bulk_add_paper_trades 應只 dump+push 一次,不是 N 次(避免 GH spam)。"""
    calls: list[object] = []

    def _spy(db_path):
        calls.append(db_path)

    monkeypatch.setattr(db, "_dump_paper_trades_snapshot", _spy)

    rows = [
        {"stock_id": "2330", "name": "台積電", "close": 600.0},
        {"stock_id": "2317", "name": "鴻海", "close": 200.0},
        {"stock_id": "1101", "name": "台泥", "close": 50.0},
    ]
    result = pt.bulk_add_paper_trades(rows, entry_date="2026-05-04")
    assert result["added"] == 3
    assert len(calls) == 1, f"bulk_add 3 筆只該 dump 1 次,實際 {len(calls)}"


def test_bulk_add_no_successes_does_not_dump(tmp_db, monkeypatch):
    """bulk_add 全 invalid / 全 dup → added=0 → 不該 dump。"""
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)

    calls: list[object] = []

    def _spy(db_path):
        calls.append(db_path)

    monkeypatch.setattr(db, "_dump_paper_trades_snapshot", _spy)
    result = pt.bulk_add_paper_trades(
        [{"stock_id": "2330", "close": 600.0}],  # dup → skipped
        entry_date="2026-05-04",
    )
    assert result == {"added": 0, "skipped": 1, "errors": 0}
    assert len(calls) == 0, "added=0 不該觸發 dump"


def test_evaluate_active_trades_dumps_when_settled(tmp_db, monkeypatch):
    """evaluate 後有 trade 結算 → 應觸發 dump 反映新 status / current_stop。"""
    _seed_prices("2330", [
        {"date": "2026-05-04", "high": 600, "low": 595, "close": 600},
        {"date": "2026-05-05", "high": 605, "low": 595, "close": 602},
        {"date": "2026-05-06", "high": 632, "low": 600, "close": 625},  # 觸 target 630
        {"date": "2026-05-07", "high": 640, "low": 620, "close": 638},
        {"date": "2026-05-08", "high": 645, "low": 630, "close": 642},
        {"date": "2026-05-09", "high": 650, "low": 635, "close": 645},
    ])
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)

    calls: list[object] = []

    def _spy(db_path):
        calls.append(db_path)

    monkeypatch.setattr(db, "_dump_paper_trades_snapshot", _spy)
    n = pt.evaluate_active_trades()
    assert n == 1
    assert len(calls) == 1, "結算後應 dump 一次反映新 status"


def test_evaluate_active_trades_no_settle_no_dump(tmp_db, monkeypatch):
    """evaluate 沒有任何 trade 結算 → 不該 dump。"""
    _seed_prices("2330", [
        {"date": "2026-05-04", "high": 600, "low": 595, "close": 600},
        {"date": "2026-05-05", "high": 605, "low": 595, "close": 602},
        # 只有 2 天,hold_days=5 不夠 → 不結算
    ])
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)

    calls: list[object] = []

    def _spy(db_path):
        calls.append(db_path)

    monkeypatch.setattr(db, "_dump_paper_trades_snapshot", _spy)
    n = pt.evaluate_active_trades()
    assert n == 0
    assert len(calls) == 0, "n_updated=0 不該觸發 dump"


def test_load_does_not_dump_back(tmp_db, monkeypatch):
    """load_from_csv 期間 _LOAD_IN_PROGRESS=True → dump_to_csv 應 silent skip,
    避免 N 筆 row preload 觸發 N 次寫檔。
    """
    # 先準備 csv
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)
    pt.add_paper_trade("2317", "鴻海", "2026-05-04", 200.0)
    paper_trades_snapshot.dump_to_csv(snapshot_dir=tmp_db)

    with db.get_conn() as conn:
        conn.execute("DELETE FROM paper_trades")

    write_calls: list = []

    # 觀察 dump_to_csv 在 _LOAD_IN_PROGRESS 期間是否實際寫檔(改 to_csv 為 spy)
    import pandas as pd
    real_to_csv_method = pd.DataFrame.to_csv

    def _spy_to_csv(self, *a, **kw):
        write_calls.append(a)
        return real_to_csv_method(self, *a, **kw)

    monkeypatch.setattr(pd.DataFrame, "to_csv", _spy_to_csv)
    n = paper_trades_snapshot.load_from_csv(snapshot_dir=tmp_db)
    assert n == 2
    # load 過程中可能有 dump 嘗試,但因 _LOAD_IN_PROGRESS 應全部 silent skip
    # _spy_to_csv 只在真的寫檔時被觸發 — load_from_csv 本身不會寫,所以 0 次
    assert len(write_calls) == 0, (
        f"load_from_csv 期間不該觸發 to_csv 寫檔,實際 {len(write_calls)} 次"
    )


def test_dump_to_string_format_includes_all_columns(tmp_db):
    """dump_to_string 字串應含所有 schema 欄位,給 GitHub push 用。"""
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", 600.0)
    csv_text = paper_trades_snapshot.dump_to_string()
    header = csv_text.splitlines()[0]
    for col in (
        "sid", "name", "entry_date", "entry_price",
        "target_price", "stop_price", "current_stop", "trailing_level",
        "status", "matched_strategies", "ml_prob",
    ):
        assert col in header, f"header 漏 {col}"


def _all_rows() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_trades ORDER BY entry_date, sid"
        ).fetchall()
    return [dict(r) for r in rows]


def _count_rows() -> int:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM paper_trades"
        ).fetchone()[0]
