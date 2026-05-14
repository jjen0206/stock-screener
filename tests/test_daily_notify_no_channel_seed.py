"""Regression test:`scripts/daily_notify.py --no-telegram --no-discord` 仍須
auto-seed paper_trades(別因兩通道都關就 early return 跳過)。

Bug history(2026-05):當 GitHub Actions 把 push secrets 關掉測試跑時,
`notify_top_picks(send_telegram=False, send_discord=False)` 回 `{}`,
daily_notify.main() line 110 撞到 `if not results: return 0` 早退,完全略過
line 129+ 的 `pt.auto_seed_from_picks(...)` → paper_trades 沒寫今日 row →
下游 GH Actions inline backtest step 沒東西可算。本 test 守住:
- main() 跑完仍呼叫 auto_seed_from_picks
- paper_trades 真的多一筆 row
- exit code == 0(被使用者明確關掉,不是失敗)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import daily_notify  # noqa: E402
from src import config, database as db, notifier as notifier_mod  # noqa: E402
from src import paper_trading as pt  # noqa: E402


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """production schema init_db()(同 test_paper_trading.tmp_db)。"""
    db_file = tmp_path / "paper.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


def _fake_pick(sid: str, name: str, close: float) -> dict:
    """compute_top_picks 回傳的 pick dict 縮影(只給 auto_seed_from_picks 用的欄位)。"""
    return {
        "sid": sid,
        "name": name,
        "close": close,
        "matched_strategies": ["ma_alignment", "macd_golden"],
        "ml_prob": 0.72,
    }


def test_no_channel_still_auto_seeds_paper_trades(
    tmp_db, monkeypatch, capsys,
):
    """--no-telegram --no-discord → main() exit 0 且 paper_trades 多一筆 row。"""
    # 1) CLI args:date 固定 + 兩通道都關
    target_date = "2026-05-14"
    monkeypatch.setattr(sys, "argv", [
        "daily_notify.py",
        "--date", target_date,
        "--no-telegram",
        "--no-discord",
    ])

    # 2) preload_snapshots 別碰 disk(test 用 tmp_path 空 DB)
    monkeypatch.setattr(db, "preload_snapshots", lambda *a, **k: {})

    # 3) notify_top_picks 模擬「兩通道都關」回 {}
    #    (不 mock real notify path,而是直接讓它回 {} 符合 production behaviour)
    notify_calls: list[dict] = []

    def _fake_notify(**kwargs):
        notify_calls.append(kwargs)
        # send_telegram=False & send_discord=False → 模擬 notifier 回 {}
        return {}

    monkeypatch.setattr(daily_notify, "notify_top_picks", _fake_notify)

    # 4) compute_top_picks 餵兩筆 picks(daily_notify 推播後 reuse 同一隻抓 picks)
    picks = [
        _fake_pick("2330", "台積電", 600.0),
        _fake_pick("2317", "鴻海", 200.0),
    ]
    monkeypatch.setattr(daily_notify, "compute_top_picks", lambda **kw: picks)

    # 5) spy auto_seed_from_picks(確認真的被 call,且接到正確 entry_date)
    auto_seed_calls: list[tuple] = []
    real_auto_seed = pt.auto_seed_from_picks

    def _spy_auto_seed(picks_arg, entry_date, **kw):
        auto_seed_calls.append((list(picks_arg or []), entry_date))
        return real_auto_seed(picks_arg, entry_date=entry_date, **kw)

    monkeypatch.setattr(daily_notify.pt, "auto_seed_from_picks", _spy_auto_seed)

    # 6) 跑 main()
    exit_code = daily_notify.main()

    # 7) Assertions
    # exit 0:兩通道被使用者明確關掉,不是失敗
    assert exit_code == 0, f"exit code 該為 0,實際 {exit_code}"

    # notify_top_picks 有被 call(send_telegram=False, send_discord=False)
    assert len(notify_calls) == 1
    assert notify_calls[0]["send_telegram"] is False
    assert notify_calls[0]["send_discord"] is False

    # auto_seed_from_picks 被 call,entry_date 對齊 target_date
    assert len(auto_seed_calls) == 1, (
        "auto_seed_from_picks 沒被 call — early return bug 還在"
    )
    _, seed_entry_date = auto_seed_calls[0]
    assert seed_entry_date == target_date

    # paper_trades 真的有 row 寫入(production schema,經 add_paper_trade 全 pipeline)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT sid, entry_date, entry_price FROM paper_trades "
            "WHERE entry_date=? ORDER BY sid",
            (target_date,),
        ).fetchall()
    assert len(rows) == 2, f"paper_trades 該有 2 筆,實際 {len(rows)}"
    sids = sorted(r["sid"] for r in rows)
    assert sids == ["2317", "2330"]

    # stdout 有 auto-seed 訊息(用於 GH Actions log 排查)
    captured = capsys.readouterr()
    assert "paper_trades auto-seed" in captured.out
    assert "added=2" in captured.out


def test_dry_run_skips_auto_seed_even_when_no_channel(
    tmp_db, monkeypatch, capsys,
):
    """dry-run + --no-telegram --no-discord → 不寫 paper_trades(避免本機污染)。"""
    target_date = "2026-05-14"
    monkeypatch.setattr(sys, "argv", [
        "daily_notify.py",
        "--date", target_date,
        "--no-telegram",
        "--no-discord",
        "--dry-run",
    ])
    monkeypatch.setattr(db, "preload_snapshots", lambda *a, **k: {})
    monkeypatch.setattr(daily_notify, "notify_top_picks", lambda **kw: {})

    seed_called = {"v": False}

    def _spy(*a, **kw):
        seed_called["v"] = True
        return {"added": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr(daily_notify.pt, "auto_seed_from_picks", _spy)

    exit_code = daily_notify.main()
    assert exit_code == 0
    assert seed_called["v"] is False, "dry-run 不該跑 auto-seed"

    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM paper_trades WHERE entry_date=?",
            (target_date,),
        ).fetchone()["c"]
    assert n == 0

    captured = capsys.readouterr()
    assert "dry-run: 跳過 paper_trades auto-seed" in captured.out
