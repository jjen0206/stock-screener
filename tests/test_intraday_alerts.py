"""scripts/intraday_alerts.py 單元測試(production schema fixture)。

涵蓋:
  - 跌破停損觸發 → push + 寫 dedup
  - 同日重複條件不重推(第二次 run skip)
  - 沒命中(price 在區間中)→ 不推
  - entry_zone / breakout 各自獨立觸發
  - 訊息格式對齊主公規格
"""
from __future__ import annotations

import sys
from datetime import date as _date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts import intraday_alerts  # noqa: E402
from src import database as db  # noqa: E402

# tmp_db fixture 共用 tests/conftest.py


def _seed_active_trade(
    sid: str = "2330", name: str = "台積電",
    entry: float = 1000.0,
    target: float = 1050.0,  # +5%
    stop: float = 970.0,     # -3%
    current_stop: float | None = None,
) -> None:
    """直接 INSERT paper_trade(避開 add_paper_trade 算 expected_exit 抓 daily_prices)。"""
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO paper_trades "
            "(sid, name, entry_date, entry_price, target_price, stop_price, "
            " current_stop, hold_days, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                sid, name, "2026-05-14", entry, target, stop,
                current_stop if current_stop is not None else stop,
                5, "active", "2026-05-14T00:00:00Z",
            ),
        )
        conn.commit()


def _patch_pushers(monkeypatch, tg_calls: list, dc_calls: list):
    """攔截 Telegram / Discord 推播 — 不真打網路。"""
    def _fake_tg(text, **kwargs):
        tg_calls.append(text)
        return True

    def _fake_dc(content, **kwargs):
        dc_calls.append(content)
        return True

    monkeypatch.setattr(intraday_alerts, "send_telegram_message", _fake_tg)
    monkeypatch.setattr(intraday_alerts, "send_discord_message", _fake_dc)


def test_stop_loss_triggers_alert(tmp_db, monkeypatch):
    """current=950 < current_stop=970 → push 跌破停損 + 寫 dedup。"""
    _seed_active_trade(entry=1000, target=1050, stop=970, current_stop=970)
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)

    # 偽造 intraday → current=950(跌破停損 970)
    monkeypatch.setattr(
        intraday_alerts, "get_intraday_quote",
        lambda sids: {"2330": {
            "current": 950.0, "prev_close": 970.0,
            "change_pct": -2.06, "volume": 1000,
        }},
    )

    stats = intraday_alerts.run(use_intraday=True)
    assert stats["n_pushed"] == 1
    assert stats["n_skipped_dedup"] == 0
    assert len(tg_calls) == 1
    assert len(dc_calls) == 1
    assert "⛔" in tg_calls[0]
    assert "跌破停損" in tg_calls[0]
    assert "2330" in tg_calls[0]
    assert "970.00" in tg_calls[0]
    assert "950.00" in tg_calls[0]

    # alert_dedup 表有寫入
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alert_dedup WHERE sid='2330'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["alert_type"] == "stop_loss"
    assert rows[0]["alert_date"] == _date.today().isoformat()


def test_same_day_no_repeat_push(tmp_db, monkeypatch):
    """同一 sid + alert_type + 同日 → 第二次 run skip(不重推)。"""
    _seed_active_trade(entry=1000, target=1050, stop=970, current_stop=970)
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)
    monkeypatch.setattr(
        intraday_alerts, "get_intraday_quote",
        lambda sids: {"2330": {
            "current": 950.0, "prev_close": 970.0,
            "change_pct": -2.06, "volume": None,
        }},
    )

    # 第一次推 → 寫 dedup
    intraday_alerts.run(use_intraday=True)
    assert len(tg_calls) == 1

    # 第二次 → 命中 dedup,skip
    stats2 = intraday_alerts.run(use_intraday=True)
    assert stats2["n_skipped_dedup"] >= 1
    assert stats2["n_pushed"] == 0
    assert len(tg_calls) == 1  # 沒新增 push


def test_no_hit_no_push(tmp_db, monkeypatch):
    """current 在 entry < current < target 區間 → 0 命中。"""
    _seed_active_trade(entry=1000, target=1050, stop=970, current_stop=970)
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)

    monkeypatch.setattr(
        intraday_alerts, "get_intraday_quote",
        lambda sids: {"2330": {
            "current": 1010.0,  # 1000 < 1010 < 1050,3 條件都不觸
            "prev_close": 1000.0, "change_pct": 1.0, "volume": None,
        }},
    )

    stats = intraday_alerts.run(use_intraday=True)
    assert stats["n_candidates"] == 0
    assert stats["n_pushed"] == 0
    assert len(tg_calls) == 0


def test_entry_zone_triggers_alert(tmp_db, monkeypatch):
    """current ≤ entry → push 進場時機(訊息不顯 change_pct)。"""
    _seed_active_trade(
        sid="2330", name="台積電",
        entry=832, target=873, stop=807, current_stop=807,
    )
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)
    monkeypatch.setattr(
        intraday_alerts, "get_intraday_quote",
        lambda sids: {"2330": {
            "current": 830.0, "prev_close": 832.0,
            "change_pct": -0.24, "volume": None,
        }},
    )

    stats = intraday_alerts.run(use_intraday=True)
    assert stats["n_pushed"] == 1
    assert "💰" in tg_calls[0]
    assert "進場時機" in tg_calls[0]
    assert "832.00" in tg_calls[0]
    assert "830.00" in tg_calls[0]
    # entry_zone 訊息不應有「%」(規格:不顯 change_pct)
    assert "%" not in tg_calls[0]


def test_breakout_triggers_alert(tmp_db, monkeypatch):
    """current > target → push 突破壓力。"""
    _seed_active_trade(entry=900, target=950, stop=873, current_stop=873)
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)
    monkeypatch.setattr(
        intraday_alerts, "get_intraday_quote",
        lambda sids: {"2330": {
            "current": 952.0, "prev_close": 940.0,
            "change_pct": 1.28, "volume": None,
        }},
    )

    stats = intraday_alerts.run(use_intraday=True)
    assert stats["n_pushed"] == 1
    assert "🚀" in tg_calls[0]
    assert "突破壓力" in tg_calls[0]
    assert "950.00" in tg_calls[0]
    assert "952.00" in tg_calls[0]
    assert "+1.28%" in tg_calls[0]


def test_dry_run_no_push_no_dedup(tmp_db, monkeypatch):
    """--dry-run → 不推不寫 dedup(但仍 count n_pushed 給 log)。"""
    _seed_active_trade(entry=1000, target=1050, stop=970, current_stop=970)
    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)
    monkeypatch.setattr(
        intraday_alerts, "get_intraday_quote",
        lambda sids: {"2330": {
            "current": 950.0, "prev_close": 970.0,
            "change_pct": -2.06, "volume": None,
        }},
    )

    intraday_alerts.run(dry_run=True, use_intraday=True)
    assert len(tg_calls) == 0
    assert len(dc_calls) == 0
    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM alert_dedup").fetchone()["c"]
    assert n == 0


def test_no_active_trades_short_circuit(tmp_db, monkeypatch):
    """無 active paper_trades → 不打 intraday API,n_active_trades=0。"""
    called: list[bool] = []

    def _spy(sids):
        called.append(True)
        return {}

    monkeypatch.setattr(intraday_alerts, "get_intraday_quote", _spy)
    stats = intraday_alerts.run(use_intraday=True)
    assert stats["n_active_trades"] == 0
    assert stats["n_pushed"] == 0
    assert called == []


def test_fallback_to_daily_close_when_intraday_fails(tmp_db, monkeypatch):
    """intraday 全部回 None → fallback 用 daily_prices 最新 close。"""
    _seed_active_trade(entry=1000, target=1050, stop=970, current_stop=970)

    # daily_prices 餵 close=965(會觸停損)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_prices (stock_id, date, close) VALUES (?,?,?)",
            ("2330", "2026-05-14", 965.0),
        )
        conn.commit()

    tg_calls: list[str] = []
    dc_calls: list[str] = []
    _patch_pushers(monkeypatch, tg_calls, dc_calls)
    monkeypatch.setattr(
        intraday_alerts, "get_intraday_quote",
        lambda sids: {"2330": None},  # 抓失敗
    )

    stats = intraday_alerts.run(use_intraday=True)
    assert stats["n_pushed"] == 1
    assert "跌破停損" in tg_calls[0]
