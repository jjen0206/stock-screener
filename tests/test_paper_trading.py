"""src/paper_trading.py 單元測試 + e2e page render 測試。"""
from __future__ import annotations

import pytest

from src import config, database as db, paper_trading as pt


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "paper.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


def _seed_prices(sid: str, rows: list[dict]) -> None:
    """寫進 daily_prices 給 evaluate 用。"""
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


# === schema ===

def test_paper_trades_table_schema_exists(tmp_db):
    """init_db 後 paper_trades 表 + 必要欄位 + UNIQUE 約束在。"""
    with db.get_conn() as conn:
        names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "paper_trades" in names
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(paper_trades)"
            ).fetchall()
        }
        for required in (
            "id", "sid", "name", "entry_date", "entry_price",
            "matched_strategies", "ml_prob", "target_price", "stop_price",
            "hold_days", "expected_exit_date", "actual_exit_date",
            "actual_exit_price", "status", "return_pct", "notes",
            "created_at", "updated_at",
        ):
            assert required in cols, f"missing col {required}"


# === add_paper_trade ===

def test_add_paper_trade_creates_row(tmp_db):
    """正常 add → 寫入 row,target/stop/expected_exit 自動算。"""
    _seed_prices("2330", [
        {"date": "2026-05-04", "high": 600, "low": 595, "close": 600},
        {"date": "2026-05-05", "high": 610, "low": 600, "close": 605},
        {"date": "2026-05-06", "high": 615, "low": 605, "close": 610},
        {"date": "2026-05-07", "high": 618, "low": 610, "close": 615},
        {"date": "2026-05-08", "high": 620, "low": 612, "close": 618},
        {"date": "2026-05-09", "high": 625, "low": 615, "close": 620},
    ])
    new_id = pt.add_paper_trade(
        sid="2330", name="台積電",
        entry_date="2026-05-04", entry_price=600.0,
        matched_strategies=["ma_alignment", "macd_golden"],
        ml_prob=0.72,
    )
    assert new_id is not None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE id=?", (new_id,)
        ).fetchone()
    assert row["sid"] == "2330"
    assert row["entry_price"] == 600.0
    assert row["target_price"] == pytest.approx(630.0)  # 600 × 1.05
    assert row["stop_price"] == pytest.approx(582.0)    # 600 × 0.97
    assert row["hold_days"] == 5
    assert row["status"] == "active"
    assert row["expected_exit_date"] == "2026-05-09"  # 5 個交易日後
    assert row["ml_prob"] == pytest.approx(0.72)


def test_add_paper_trade_unique_constraint_same_sid_same_date(tmp_db):
    """同 sid 同 entry_date 第二次 add → 回 None(冪等),不寫第二筆。"""
    new_id = pt.add_paper_trade(
        sid="2330", name="台積電",
        entry_date="2026-05-04", entry_price=600.0,
    )
    assert new_id is not None
    dup = pt.add_paper_trade(
        sid="2330", name="台積電",
        entry_date="2026-05-04", entry_price=605.0,  # 不同價也擋
    )
    assert dup is None
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM paper_trades WHERE sid='2330'"
        ).fetchone()["c"]
    assert n == 1


def test_add_paper_trade_rejects_invalid_inputs(tmp_db):
    with pytest.raises(ValueError):
        pt.add_paper_trade("2330", None, "2026-05-04", entry_price=0)
    with pytest.raises(ValueError):
        pt.add_paper_trade("2330", None, "2026-05-04", entry_price=100, hold_days=0)
    with pytest.raises(ValueError):
        pt.add_paper_trade("2330", None, "2026-05-04", entry_price=100, target_pct=0)


def test_bulk_add_inserts_all_pending(tmp_db):
    """3 張全新 picks → 全寫入,added=3 / skipped=0 / errors=0。"""
    rows = [
        {"stock_id": "2330", "name": "台積電", "close": 600.0,
         "matched_strategies": ["ma_alignment"], "ml_prob": 0.72},
        {"stock_id": "2317", "name": "鴻海", "close": 200.0,
         "matched_strategies": ["macd_golden"], "ml_prob": 0.65},
        {"stock_id": "1101", "name": "台泥", "close": 50.0,
         "matched_strategies": [], "ml_prob": None},
    ]
    result = pt.bulk_add_paper_trades(rows, entry_date="2026-05-04")
    assert result == {"added": 3, "skipped": 0, "errors": 0}
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM paper_trades WHERE entry_date='2026-05-04'"
        ).fetchone()["c"]
    assert n == 3


def test_bulk_add_skips_already_tracked(tmp_db):
    """同 sid 同 entry_date 已存在 → 算 skipped,其他新的照常 added。"""
    pt.add_paper_trade("2330", "台積電", "2026-05-04", 600.0)
    rows = [
        {"stock_id": "2330", "name": "台積電", "close": 605.0},  # 重複 → skip
        {"stock_id": "2317", "name": "鴻海", "close": 200.0},   # 新 → add
        {"stock_id": "1101", "name": "台泥", "close": 50.0},     # 新 → add
    ]
    result = pt.bulk_add_paper_trades(rows, entry_date="2026-05-04")
    assert result == {"added": 2, "skipped": 1, "errors": 0}
    with db.get_conn() as conn:
        sids = {
            r["sid"] for r in conn.execute(
                "SELECT sid FROM paper_trades WHERE entry_date='2026-05-04'"
            ).fetchall()
        }
    assert sids == {"2330", "2317", "1101"}


def test_bulk_add_counts_invalid_rows_as_errors(tmp_db):
    """invalid rows(沒 sid / close 0 / close NaN)→ errors,不影響其他。"""
    rows = [
        {"stock_id": "", "close": 100.0},        # 空 sid → error
        {"stock_id": "2330", "close": 0},        # close 0 → error
        {"stock_id": "2317", "close": float("nan")},  # NaN → error
        {"stock_id": "1101", "close": 50.0},     # OK → add
    ]
    result = pt.bulk_add_paper_trades(rows, entry_date="2026-05-04")
    assert result["added"] == 1
    assert result["errors"] == 3
    assert result["skipped"] == 0


def test_already_tracked_returns_true_after_add(tmp_db):
    pt.add_paper_trade("2330", None, "2026-05-04", entry_price=600.0)
    assert pt.already_tracked("2330", "2026-05-04") is True
    assert pt.already_tracked("2330", "2026-05-05") is False
    assert pt.already_tracked("2317", "2026-05-04") is False


# === evaluate_active_trades ===

def test_evaluate_active_trade_target_hit_returns_win(tmp_db):
    """day 2 high 觸 target → status='win',return=+5%,exit on day 2。"""
    _seed_prices("2330", [
        {"date": "2026-05-04", "high": 600, "low": 595, "close": 600},
        {"date": "2026-05-05", "high": 605, "low": 595, "close": 602},  # 沒觸
        {"date": "2026-05-06", "high": 632, "low": 600, "close": 625},  # 觸 630
        {"date": "2026-05-07", "high": 640, "low": 620, "close": 638},
        {"date": "2026-05-08", "high": 645, "low": 630, "close": 642},
        {"date": "2026-05-09", "high": 650, "low": 635, "close": 645},
    ])
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", entry_price=600.0)
    n = pt.evaluate_active_trades()
    assert n == 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status, return_pct, actual_exit_date, actual_exit_price "
            "FROM paper_trades WHERE sid='2330'"
        ).fetchone()
    assert row["status"] == "win"
    assert row["return_pct"] == pytest.approx(0.05)
    assert row["actual_exit_date"] == "2026-05-06"
    assert row["actual_exit_price"] == pytest.approx(630.0)


def test_evaluate_active_trade_stop_hit_returns_lose(tmp_db):
    """day 1 low 觸 stop → status='lose',return=-3%,exit on day 1。"""
    _seed_prices("2330", [
        {"date": "2026-05-04", "high": 600, "low": 595, "close": 600},
        {"date": "2026-05-05", "high": 600, "low": 580, "close": 585},  # 觸 stop 582
        {"date": "2026-05-06", "high": 590, "low": 575, "close": 580},
        {"date": "2026-05-07", "high": 585, "low": 570, "close": 575},
        {"date": "2026-05-08", "high": 590, "low": 575, "close": 580},
        {"date": "2026-05-09", "high": 595, "low": 580, "close": 585},
    ])
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", entry_price=600.0)
    n = pt.evaluate_active_trades()
    assert n == 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status, return_pct, actual_exit_date "
            "FROM paper_trades WHERE sid='2330'"
        ).fetchone()
    assert row["status"] == "lose"
    assert row["return_pct"] == pytest.approx(-0.03)
    assert row["actual_exit_date"] == "2026-05-05"


def test_evaluate_active_trade_timeout_close_above_returns_timeout_win(tmp_db):
    """5 天結束都沒觸,close > entry → timeout_win + return=(close-entry)/entry。"""
    _seed_prices("2330", [
        {"date": "2026-05-04", "high": 600, "low": 595, "close": 600},
        {"date": "2026-05-05", "high": 615, "low": 598, "close": 610},  # 沒觸 630 / 582
        {"date": "2026-05-06", "high": 620, "low": 605, "close": 615},
        {"date": "2026-05-07", "high": 625, "low": 610, "close": 620},
        {"date": "2026-05-08", "high": 622, "low": 612, "close": 615},
        {"date": "2026-05-09", "high": 615, "low": 605, "close": 612},  # final = 612
    ])
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", entry_price=600.0)
    pt.evaluate_active_trades()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status, return_pct FROM paper_trades WHERE sid='2330'"
        ).fetchone()
    assert row["status"] == "timeout_win"
    # final close 612 vs entry 600 → +2%
    assert row["return_pct"] == pytest.approx(0.02)


def test_evaluate_active_trade_timeout_close_below_returns_timeout_lose(tmp_db):
    """5 天沒觸,close < entry → timeout_lose + return 負。"""
    _seed_prices("2330", [
        {"date": "2026-05-04", "high": 600, "low": 595, "close": 600},
        {"date": "2026-05-05", "high": 605, "low": 588, "close": 595},
        {"date": "2026-05-06", "high": 600, "low": 585, "close": 590},
        {"date": "2026-05-07", "high": 595, "low": 583, "close": 588},
        {"date": "2026-05-08", "high": 590, "low": 583, "close": 585},
        {"date": "2026-05-09", "high": 588, "low": 583, "close": 585},  # final = 585
    ])
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", entry_price=600.0)
    pt.evaluate_active_trades()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status, return_pct FROM paper_trades WHERE sid='2330'"
        ).fetchone()
    assert row["status"] == "timeout_lose"
    # final close 585 vs entry 600 → -2.5%
    assert row["return_pct"] == pytest.approx(-0.025)


def test_evaluate_active_trade_insufficient_future_data_keeps_active(tmp_db):
    """資料不足 5 天 → status 仍 active,evaluate 不算。"""
    _seed_prices("2330", [
        {"date": "2026-05-04", "high": 600, "low": 595, "close": 600},
        {"date": "2026-05-05", "high": 605, "low": 595, "close": 602},
        {"date": "2026-05-06", "high": 610, "low": 600, "close": 605},
    ])
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", entry_price=600.0)
    n = pt.evaluate_active_trades()
    assert n == 0
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM paper_trades WHERE sid='2330'"
        ).fetchone()
    assert row["status"] == "active"


def test_evaluate_same_day_both_hit_treats_as_lose(tmp_db):
    """同日 high 觸 target + low 觸 stop → 保守 lose(intra-day path 不可知)。"""
    _seed_prices("2330", [
        {"date": "2026-05-04", "high": 600, "low": 595, "close": 600},
        {"date": "2026-05-05", "high": 632, "low": 580, "close": 600},  # 同日兩邊都觸
        {"date": "2026-05-06", "high": 610, "low": 600, "close": 605},
        {"date": "2026-05-07", "high": 615, "low": 605, "close": 610},
        {"date": "2026-05-08", "high": 620, "low": 610, "close": 615},
        {"date": "2026-05-09", "high": 625, "low": 615, "close": 620},
    ])
    pt.add_paper_trade("2330", "TSMC", "2026-05-04", entry_price=600.0)
    pt.evaluate_active_trades()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status, return_pct FROM paper_trades WHERE sid='2330'"
        ).fetchone()
    assert row["status"] == "lose"
    assert row["return_pct"] == pytest.approx(-0.03)


# === list / stats ===

def test_list_active_trades_returns_only_active(tmp_db):
    pt.add_paper_trade("2330", "A", "2026-05-04", 600.0)
    pt.add_paper_trade("2317", "B", "2026-05-04", 200.0)
    # 強制把第二筆改成 win
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE paper_trades SET status='win', return_pct=0.05 "
            "WHERE sid='2317'"
        )
    df = pt.list_active_trades()
    assert len(df) == 1
    assert df.iloc[0]["sid"] == "2330"


def test_list_settled_trades_excludes_active(tmp_db):
    pt.add_paper_trade("2330", "A", "2026-05-04", 600.0)
    pt.add_paper_trade("2317", "B", "2026-05-04", 200.0)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE paper_trades SET status='win', return_pct=0.05 "
            "WHERE sid='2317'"
        )
    df = pt.list_settled_trades()
    assert len(df) == 1
    assert df.iloc[0]["sid"] == "2317"
    assert df.iloc[0]["status"] == "win"


def test_compute_stats_calculates_wr_avg_return_max_loss_streak(tmp_db):
    """5 筆 settled:3W / 2L 連敗,WR 60%,avg_return = mean,max_loss_streak=2。"""
    import json as _json
    statuses_returns = [
        ("2026-05-01", "win", 0.05, ["ma_alignment"]),
        ("2026-05-02", "lose", -0.03, ["ma_alignment", "macd_golden"]),
        ("2026-05-03", "lose", -0.03, ["macd_golden"]),
        ("2026-05-04", "win", 0.05, ["bias_convergence"]),
        ("2026-05-05", "timeout_win", 0.02, ["bias_convergence"]),
    ]
    with db.get_conn() as conn:
        for i, (date_, status, ret, matched) in enumerate(statuses_returns):
            conn.execute(
                "INSERT INTO paper_trades "
                "(sid, name, entry_date, entry_price, matched_strategies, "
                "target_price, stop_price, hold_days, status, return_pct, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"sid{i}", f"name{i}", date_, 100.0,
                 _json.dumps(matched), 105.0, 97.0, 5, status, ret,
                 "2026-05-05T00:00:00+00:00"),
            )
    df = pt.list_settled_trades()
    stats = pt.compute_stats(df)
    assert stats["n_settled"] == 5
    assert stats["n_wins"] == 3  # win + win + timeout_win
    assert stats["win_rate"] == pytest.approx(0.6)
    # avg = (0.05 + -0.03 + -0.03 + 0.05 + 0.02) / 5 = 0.012
    assert stats["avg_return"] == pytest.approx(0.012)
    # max_loss_streak:沿 entry_date 看 [W, L, L, W, W] → 連敗 2
    assert stats["max_loss_streak"] == 2
    # by_strategy:ma_alignment 出現 2 次(1 W 1 L),macd_golden 2 次(0 W 2 L),
    # bias_convergence 2 次(2 W)
    by = stats["by_strategy"]
    assert by["ma_alignment"]["n"] == 2
    assert by["ma_alignment"]["wins"] == 1
    assert by["ma_alignment"]["win_rate"] == pytest.approx(0.5)
    assert by["macd_golden"]["wins"] == 0
    assert by["bias_convergence"]["wins"] == 2
    assert by["bias_convergence"]["win_rate"] == pytest.approx(1.0)


def test_compute_stats_empty_df_returns_zeros(tmp_db):
    import pandas as _pd
    stats = pt.compute_stats(_pd.DataFrame())
    assert stats["n_settled"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["avg_return"] == 0.0
    assert stats["max_loss_streak"] == 0
    assert stats["by_strategy"] == {}


# === e2e page render ===

def test_paper_tracking_page_renders(tmp_db):
    """e2e:側邊 segmented 切「🧪 實測追蹤」頁,無 Python error。"""
    from streamlit.testing.v1 import AppTest

    # 先放一筆 active + 一筆 settled,涵蓋 Section 2 + Section 3 渲染
    pt.add_paper_trade("2330", "A", "2026-05-04", 600.0)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO paper_trades "
            "(sid, name, entry_date, entry_price, matched_strategies, "
            "target_price, stop_price, hold_days, status, return_pct, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2317", "鴻海", "2026-04-28", 200.0, '["ma_alignment"]',
             210.0, 194.0, 5, "win", 0.05, "2026-05-04T00:00:00+00:00"),
        )

    at = AppTest.from_file("app.py", default_timeout=60)
    # 預設模擬 sticky-submit OFF + 高信心模式 OFF(快避免 Section 1 跑 ML predict)
    at.session_state["high_confidence_mode"] = False
    at.session_state["active_page"] = "🧪 實測追蹤"
    at.run()

    # 不該有 Python exception
    assert not at.exception, f"page 爆 exception: {at.exception}"
