"""Phase 2 觀察機制(2026-05-19):paper_trades 三欄落地 SQL 驗證。

把 P2-7 consensus_multiplier / P2-8 position_pct / conviction_score 從
Telegram 文字落到 SQL,30 天觀察期後可歸因「總體勝率上升來自誰」。

本檔守:
1. init_db 後 schema 確實有這 3 欄。
2. 對舊 cache.db(無這 3 欄)init_db 自動 ALTER ADD COLUMN。
3. add_paper_trade 傳這 3 個值 → 真的寫進 SQL,讀回正確。
4. bulk_add_paper_trades / auto_seed_from_picks 從 row dict 撈這 3 個值
   並落到 SQL。
5. 舊 snapshot CSV(沒這 3 欄)preload 不 crash,新欄寫 NULL(向後相容)。
6. 新 snapshot dump → load round-trip 三欄值保留。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db, paper_trading as pt  # noqa: E402
from src import paper_trades_snapshot as pts  # noqa: E402


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "p2cols.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


# === Schema ===

def test_schema_has_p2_columns_after_init_db(tmp_db):
    """init_db 後 paper_trades 必須有 consensus_multiplier / position_pct
    / conviction_score 三欄。"""
    with db.get_conn() as conn:
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(paper_trades)"
            ).fetchall()
        }
    for required in ("consensus_multiplier", "position_pct", "conviction_score"):
        assert required in cols, f"missing P2 col {required}"


# === Migration on legacy DB ===

def test_migration_alters_legacy_paper_trades(monkeypatch, tmp_path):
    """模擬 2026-05-18 前版本 cache.db:手刻 paper_trades 不含 P2 三欄,
    再跑 init_db → migration 應自動 ALTER ADD COLUMN 補齊三欄,既有 row 保留。"""
    db_file = tmp_path / "legacy.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()

    # 手刻舊 schema(包含 current_stop/trailing_level — 上個 migration 後產物,
    # 但不含 P2 三欄)
    import sqlite3
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            """
            CREATE TABLE paper_trades (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                sid                 TEXT NOT NULL,
                name                TEXT,
                entry_date          TEXT NOT NULL,
                entry_price         REAL NOT NULL CHECK(entry_price > 0),
                matched_strategies  TEXT,
                ml_prob             REAL,
                target_price        REAL NOT NULL,
                stop_price          REAL NOT NULL,
                current_stop        REAL,
                trailing_level      INTEGER NOT NULL DEFAULT 0,
                hold_days           INTEGER NOT NULL DEFAULT 5,
                expected_exit_date  TEXT,
                actual_exit_date    TEXT,
                actual_exit_price   REAL,
                status              TEXT NOT NULL,
                return_pct          REAL,
                notes               TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT,
                UNIQUE(sid, entry_date)
            )
            """
        )
        # 寫一筆舊 row,migration 後不該被擾動
        conn.execute(
            "INSERT INTO paper_trades "
            "(sid, name, entry_date, entry_price, matched_strategies, ml_prob, "
            " target_price, stop_price, current_stop, trailing_level, "
            " hold_days, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'active', ?)",
            ("2330", "台積電", "2026-05-10", 580.0, None, 0.6,
             609.0, 562.6, 562.6, 5, "2026-05-10T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    # 模擬 production boot:init_db() 跑 schema + migration
    db.init_db()

    with db.get_conn() as conn:
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(paper_trades)"
            ).fetchall()
        }
        # 三新欄都應已存在
        for c in ("consensus_multiplier", "position_pct", "conviction_score"):
            assert c in cols
        # 舊 row 保留 + 新欄 NULL
        row = conn.execute(
            "SELECT consensus_multiplier, position_pct, conviction_score "
            "FROM paper_trades WHERE sid=?",
            ("2330",),
        ).fetchone()
        assert row["consensus_multiplier"] is None
        assert row["position_pct"] is None
        assert row["conviction_score"] is None

    db._reset_path_cache()


def test_migration_is_idempotent(tmp_db):
    """重複跑 init_db 不該 crash(SQLite 沒 ADD COLUMN IF NOT EXISTS)。"""
    db.init_db()
    db.init_db()
    db.init_db()
    with db.get_conn() as conn:
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(paper_trades)"
            ).fetchall()
        }
    assert "consensus_multiplier" in cols


# === add_paper_trade writes the 3 values ===

def test_add_paper_trade_persists_p2_values(tmp_db):
    """add_paper_trade 傳這 3 個值 → SQL 寫入後讀回應一致。"""
    new_id = pt.add_paper_trade(
        sid="2330", name="台積電",
        entry_date="2026-05-19", entry_price=600.0,
        matched_strategies=["ma_alignment", "macd_golden"],
        ml_prob=0.72,
        consensus_multiplier=1.5,
        position_pct=0.035,
        conviction_score=0.78,
    )
    assert new_id is not None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT consensus_multiplier, position_pct, conviction_score "
            "FROM paper_trades WHERE id=?", (new_id,),
        ).fetchone()
    assert row["consensus_multiplier"] == pytest.approx(1.5)
    assert row["position_pct"] == pytest.approx(0.035)
    assert row["conviction_score"] == pytest.approx(0.78)


def test_add_paper_trade_defaults_p2_to_null(tmp_db):
    """不傳 P2 三參數 → SQL 應寫 NULL(向後相容,既有 caller 不爆)。"""
    new_id = pt.add_paper_trade(
        sid="2330", name="台積電",
        entry_date="2026-05-19", entry_price=600.0,
    )
    assert new_id is not None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT consensus_multiplier, position_pct, conviction_score "
            "FROM paper_trades WHERE id=?", (new_id,),
        ).fetchone()
    assert row["consensus_multiplier"] is None
    assert row["position_pct"] is None
    assert row["conviction_score"] is None


# === bulk_add_paper_trades / auto_seed_from_picks 串連 ===

def test_bulk_add_paper_trades_carries_p2_fields(tmp_db):
    """row dict 帶這 3 個值 → SQL 落地 OK。"""
    rows = [
        {
            "stock_id": "2330", "name": "台積電", "close": 600.0,
            "matched_strategies": ["ma_alignment"], "ml_prob": 0.72,
            "consensus_multiplier": 1.5,
            "position_pct": 0.10,
            "conviction_score": 0.81,
        },
        {
            "stock_id": "2317", "name": "鴻海", "close": 200.0,
            "matched_strategies": ["macd_golden"], "ml_prob": 0.65,
            "consensus_multiplier": 1.0,
            "position_pct": 0.04,
            "conviction_score": 0.45,
        },
    ]
    res = pt.bulk_add_paper_trades(rows, entry_date="2026-05-19")
    assert res["added"] == 2
    with db.get_conn() as conn:
        out = conn.execute(
            "SELECT sid, consensus_multiplier, position_pct, conviction_score "
            "FROM paper_trades ORDER BY sid"
        ).fetchall()
    assert len(out) == 2
    by_sid = {r["sid"]: r for r in out}
    assert by_sid["2330"]["consensus_multiplier"] == pytest.approx(1.5)
    assert by_sid["2330"]["position_pct"] == pytest.approx(0.10)
    assert by_sid["2330"]["conviction_score"] == pytest.approx(0.81)
    assert by_sid["2317"]["consensus_multiplier"] == pytest.approx(1.0)


def test_bulk_add_handles_missing_or_nan_p2(tmp_db):
    """row dict 缺 / NaN P2 欄位 → 不 crash,SQL 寫 NULL。"""
    rows = [
        # 全缺欄
        {"stock_id": "2330", "close": 600.0},
        # 顯式 None
        {"stock_id": "2317", "close": 200.0,
         "consensus_multiplier": None,
         "position_pct": None,
         "conviction_score": None},
        # NaN(實際從 pandas 拿到的常見狀況)
        {"stock_id": "1101", "close": 50.0,
         "consensus_multiplier": float("nan"),
         "position_pct": float("nan"),
         "conviction_score": float("nan")},
    ]
    res = pt.bulk_add_paper_trades(rows, entry_date="2026-05-19")
    assert res["added"] == 3
    with db.get_conn() as conn:
        out = conn.execute(
            "SELECT sid, consensus_multiplier, position_pct, conviction_score "
            "FROM paper_trades ORDER BY sid"
        ).fetchall()
    for r in out:
        assert r["consensus_multiplier"] is None
        assert r["position_pct"] is None
        assert r["conviction_score"] is None


def test_auto_seed_from_picks_carries_p2_fields(tmp_db):
    """auto_seed_from_picks 從 pick dict 撈 top-level 三欄(notifier enrich
    後的結構)→ SQL 落地。"""
    picks = [
        {
            "sid": "2330", "name": "台積電", "close": 600.0,
            "matched_strategies": ["ma_alignment", "macd_golden"],
            "ml_prob": 0.72,
            "consensus_multiplier": 1.5,
            "position_pct": 0.10,
            "conviction_score": 0.81,
        },
    ]
    res = pt.auto_seed_from_picks(picks, entry_date="2026-05-19")
    assert res["added"] == 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT consensus_multiplier, position_pct, conviction_score "
            "FROM paper_trades WHERE sid='2330'"
        ).fetchone()
    assert row["consensus_multiplier"] == pytest.approx(1.5)
    assert row["position_pct"] == pytest.approx(0.10)
    assert row["conviction_score"] == pytest.approx(0.81)


def test_auto_seed_falls_back_to_position_advice_dict(tmp_db):
    """pick top-level 沒 position_pct 但 position_advice dict 有 → 應 fallback
    撈進來(notify_top_picks 老路 / 沒呼叫 flatten 時)。"""
    picks = [
        {
            "sid": "2330", "name": "台積電", "close": 600.0,
            "matched_strategies": ["ma_alignment"],
            "ml_prob": 0.72,
            "consensus_multiplier": 1.3,
            # 沒 top-level position_pct
            "position_advice": {"position_pct": 0.075, "suggested_lots": 1},
            "conviction_score": 0.65,
        },
    ]
    res = pt.auto_seed_from_picks(picks, entry_date="2026-05-19")
    assert res["added"] == 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT consensus_multiplier, position_pct, conviction_score "
            "FROM paper_trades WHERE sid='2330'"
        ).fetchone()
    assert row["consensus_multiplier"] == pytest.approx(1.3)
    assert row["position_pct"] == pytest.approx(0.075)
    assert row["conviction_score"] == pytest.approx(0.65)


# === Snapshot dump / preload round-trip ===

def test_snapshot_dump_includes_p2_columns(tmp_db, tmp_path):
    """dump_to_csv 後的 CSV 欄位應含 P2 三欄,row 值正確。"""
    pt.add_paper_trade(
        sid="2330", name="台積電",
        entry_date="2026-05-19", entry_price=600.0,
        consensus_multiplier=1.5,
        position_pct=0.10,
        conviction_score=0.78,
    )
    out_dir = tmp_path / "snap"
    n = pts.dump_to_csv(snapshot_dir=out_dir)
    assert n == 1
    csv_path = out_dir / "paper_trades.csv"
    df = pd.read_csv(csv_path)
    for c in ("consensus_multiplier", "position_pct", "conviction_score"):
        assert c in df.columns
    r = df.iloc[0]
    assert r["consensus_multiplier"] == pytest.approx(1.5)
    assert r["position_pct"] == pytest.approx(0.10)
    assert r["conviction_score"] == pytest.approx(0.78)


def test_snapshot_load_old_csv_without_p2_columns(monkeypatch, tmp_path):
    """舊 snapshot CSV(沒 P2 三欄)preload → 不 crash,新欄落 NULL。"""
    db_file = tmp_path / "old_preload.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()

    snap_dir = tmp_path / "snap_old"
    snap_dir.mkdir()
    # 手刻舊 schema CSV(不含 P2 三欄,且 status='active')
    old_csv = snap_dir / "paper_trades.csv"
    old_csv.write_text(
        "id,sid,name,entry_date,entry_price,matched_strategies,ml_prob,"
        "target_price,stop_price,current_stop,trailing_level,hold_days,"
        "expected_exit_date,actual_exit_date,actual_exit_price,status,"
        "return_pct,notes,created_at,updated_at\n"
        "1,2330,台積電,2026-05-10,580.0,,0.6,609.0,562.6,562.6,0,5,"
        ",,,active,,,2026-05-10T00:00:00+00:00,\n",
        encoding="utf-8",
    )

    n = pts.load_from_csv(snapshot_dir=snap_dir)
    assert n >= 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT consensus_multiplier, position_pct, conviction_score "
            "FROM paper_trades WHERE sid=?",
            ("2330",),
        ).fetchone()
    assert row["consensus_multiplier"] is None
    assert row["position_pct"] is None
    assert row["conviction_score"] is None

    db._reset_path_cache()


def test_snapshot_round_trip_preserves_p2_values(monkeypatch, tmp_path):
    """新 CSV(含 P2 三欄)dump → 在另一空 DB 上 load → 三值仍對得上。"""
    # 第一個 DB:寫一筆 + dump CSV
    db_file1 = tmp_path / "src.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file1))
    db._reset_path_cache()
    db.init_db()
    pt.add_paper_trade(
        sid="2330", name="台積電",
        entry_date="2026-05-19", entry_price=600.0,
        consensus_multiplier=1.5,
        position_pct=0.10,
        conviction_score=0.78,
    )
    snap_dir = tmp_path / "snap_rt"
    n = pts.dump_to_csv(snapshot_dir=snap_dir)
    assert n == 1

    # 第二個 DB:全新 init,load CSV
    db._reset_path_cache()
    db_file2 = tmp_path / "dst.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file2))
    db.init_db()
    loaded = pts.load_from_csv(snapshot_dir=snap_dir)
    assert loaded >= 1

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT consensus_multiplier, position_pct, conviction_score "
            "FROM paper_trades WHERE sid=?",
            ("2330",),
        ).fetchone()
    assert row["consensus_multiplier"] == pytest.approx(1.5)
    assert row["position_pct"] == pytest.approx(0.10)
    assert row["conviction_score"] == pytest.approx(0.78)

    db._reset_path_cache()
