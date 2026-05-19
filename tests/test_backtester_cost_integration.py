"""backtester / backtest / strategy_backtest 與 backtest_costs 整合測試。

核心驗證:`apply_costs=True` vs `apply_costs=False` 對比,**net < gross**。
覆蓋全部三個 backtester 主入口:
- src.backtester.backtest_short
- src.backtest.simulate_outcome / backtest_strategy
- src.strategy_backtest.backtest_combination
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from src import backtester as bter  # noqa: E402
from src import database as db  # noqa: E402
from src import strategy_backtest as sb  # noqa: E402
from src.backtest import simulate_outcome  # noqa: E402
from src.backtest_costs import round_trip_cost_rate  # noqa: E402


# === simulate_outcome ===

def test_simulate_outcome_cost_reduces_win_return():
    """同樣觸 target +5% 路徑,apply_costs=True 的 return < False。"""
    df = pd.DataFrame([
        {"high": 106, "low": 100, "close": 105},
    ])
    _, gross = simulate_outcome(df, entry_price=100.0, apply_costs=False)
    _, net = simulate_outcome(df, entry_price=100.0, apply_costs=True)
    assert gross == pytest.approx(0.05)
    # net 應比 gross 少 ~0.6% (round_trip 0.585% + 雙邊滑價在價格內)
    assert net < gross
    assert (gross - net) > 0.005  # 至少差 0.5pp


def test_simulate_outcome_cost_makes_loss_deeper():
    """觸 stop -3% 路徑,apply_costs=True 讓虧損更深。"""
    df = pd.DataFrame([
        {"high": 102, "low": 96, "close": 97},
    ])
    _, gross = simulate_outcome(df, entry_price=100.0, apply_costs=False)
    _, net = simulate_outcome(df, entry_price=100.0, apply_costs=True)
    assert gross == pytest.approx(-0.03)
    assert net < gross  # 虧損更深(net 更負)


def test_simulate_outcome_cost_zero_gross_yields_negative_net():
    """gross 0% 平局,apply_costs=True 變負(純被成本咬)。"""
    df = pd.DataFrame([
        {"high": 101, "low": 99, "close": 100},  # 沒觸 target / stop
    ])
    outcome, net = simulate_outcome(df, entry_price=100.0, apply_costs=True)
    # 100→100 gross 0% 但扣完成本 (滑價 + 稅費) 變負
    assert net < 0
    # outcome 應為 lose(net < 0)
    assert outcome == "lose"


# === backtest_short ===

_DATES = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    from src import config
    db_file = tmp_path / "bt.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def _seed_prices(stock_id: str, dates_prices: list[tuple[str, float]]) -> None:
    db.upsert_stocks([{"stock_id": stock_id, "name": stock_id, "market": "TW"}])
    db.upsert_daily_prices([
        {"stock_id": stock_id, "date": d, "open": p, "high": p, "low": p,
         "close": p, "volume": 1000, "trading_money": None,
         "trading_turnover": None, "spread": None}
        for d, p in dates_prices
    ])


def _mock_screen(monkeypatch, results_by_date):
    def fake(date, params=None, stock_ids=None):
        rows = results_by_date.get(date, [])
        return pd.DataFrame(
            rows,
            columns=["stock_id", "name", "close", "volume", "ma_volume_5",
                     "k", "d", "inst_total_3d", "matched_at"],
        )
    monkeypatch.setattr(bter, "screen_short", fake)


def test_backtest_short_net_less_than_gross(tmp_db, monkeypatch):
    """同樣 trades,apply_costs=True 的 return_pct < False 的版本。"""
    _seed_prices("A", list(zip(_DATES, [100.0, 102.0, 105.0, 110.0, 108.0])))
    _mock_screen(monkeypatch, {
        "2024-01-02": [{"stock_id": "A", "name": "A股", "close": 100.0}],
    })

    res_gross = bter.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=2,
        universe=[("A", "A股")],
        apply_costs=False,
    )
    res_net = bter.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=2,
        universe=[("A", "A股")],
        apply_costs=True,
    )
    gross_ret = res_gross["trades"].iloc[0]["return_pct"]
    net_ret = res_net["trades"].iloc[0]["return_pct"]
    assert gross_ret == pytest.approx(5.0)
    assert net_ret < gross_ret
    # 預期差距:來回成本 0.585% + 滑價 ≈ 0.1% → 大約 0.685pp
    assert 0.5 < (gross_ret - net_ret) < 1.0, (
        f"預期差距 0.5-1.0pp,實際 {gross_ret - net_ret:.3f}pp"
    )


def test_backtest_short_summary_reflects_cost(tmp_db, monkeypatch):
    """總報酬 / 平均報酬都該被成本咬,跟 apply_costs=False 對比。"""
    _seed_prices("A", list(zip(_DATES, [100.0, 110.0, 100.0, 90.0, 100.0])))
    _mock_screen(monkeypatch, {
        "2024-01-02": [{"stock_id": "A", "name": "A股", "close": 100.0}],
        "2024-01-03": [{"stock_id": "A", "name": "A股", "close": 110.0}],
        "2024-01-04": [{"stock_id": "A", "name": "A股", "close": 100.0}],
    })

    gross = bter.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=1,
        universe=[("A", "A股")],
        apply_costs=False,
    )["summary"]
    net = bter.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=1,
        universe=[("A", "A股")],
        apply_costs=True,
    )["summary"]
    assert gross["trades"] == net["trades"]  # 同數筆 trade
    assert net["avg_return"] < gross["avg_return"]
    assert net["total_return"] < gross["total_return"]


def test_backtest_short_broker_fee_discount_reduces_cost(tmp_db, monkeypatch):
    """broker_fee_discount < 1.0 → 成本下降,net 介於 gross 與 full-cost net 之間。"""
    _seed_prices("A", list(zip(_DATES, [100.0, 102.0, 105.0, 110.0, 108.0])))
    _mock_screen(monkeypatch, {
        "2024-01-02": [{"stock_id": "A", "name": "A股", "close": 100.0}],
    })
    full_cost = bter.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=2,
        universe=[("A", "A股")],
        apply_costs=True, broker_fee_discount=1.0,
    )["trades"].iloc[0]["return_pct"]
    discount = bter.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=2,
        universe=[("A", "A股")],
        apply_costs=True, broker_fee_discount=0.28,
    )["trades"].iloc[0]["return_pct"]
    # 28 折手續費 → 成本更低,net 報酬更高
    assert discount > full_cost


# === strategy_backtest.backtest_combination ===

def _seed_daily_pick(sid: str, trade_date: str, strategy: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_picks ("
            "trade_date, universe, strategy, sid, params_hash, payload, "
            "computed_at) VALUES (?, 'top_50', ?, ?, 'default_v1', '{}', "
            "'2026-05-17T00:00:00Z')",
            (trade_date, strategy, sid),
        )
        conn.commit()


def _seed_price_series(sid: str, start_date_iso: str, prices: list[float]) -> None:
    from datetime import date as _date, timedelta
    d = _date.fromisoformat(start_date_iso)
    with db.get_conn() as conn:
        for px in prices:
            conn.execute(
                "INSERT OR REPLACE INTO daily_prices ("
                "stock_id, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, 1000)",
                (sid, d.isoformat(), px, px, px, px),
            )
            d += timedelta(days=1)
        conn.commit()


def test_backtest_combination_net_below_gross(tmp_db):
    """backtest_combination 套成本後 avg_return_pct < apply_costs=False。"""
    _seed_daily_pick("2330", "2026-05-01", "ma_alignment")
    _seed_price_series("2330", "2026-05-01", [100.0, 102.0, 105.0, 110.0])

    with db.get_conn() as conn:
        gross = sb.backtest_combination(
            conn, strategies=["ma_alignment"],
            start_date="2026-05-01", end_date="2026-05-31",
            holding_days=3, mode="union",
            apply_costs=False,
        )
        net = sb.backtest_combination(
            conn, strategies=["ma_alignment"],
            start_date="2026-05-01", end_date="2026-05-31",
            holding_days=3, mode="union",
            apply_costs=True,
        )
    assert gross["n_trades"] == net["n_trades"]
    assert gross["avg_return_pct"] == pytest.approx(10.0, rel=1e-3)
    assert net["avg_return_pct"] < gross["avg_return_pct"]
    # 差距:0.585% (cost) + ~0.1% (滑價) ≈ 0.685pp
    diff = gross["avg_return_pct"] - net["avg_return_pct"]
    assert 0.5 < diff < 1.0


def test_backtest_combination_costs_default_on(tmp_db):
    """不傳 apply_costs → 預設 True(主公規格 default)。"""
    _seed_daily_pick("2330", "2026-05-01", "ma_alignment")
    _seed_price_series("2330", "2026-05-01", [100.0, 102.0, 105.0, 110.0])

    with db.get_conn() as conn:
        default = sb.backtest_combination(
            conn, strategies=["ma_alignment"],
            start_date="2026-05-01", end_date="2026-05-31",
            holding_days=3, mode="union",
        )
    # default 應 < 10.0 gross(被成本咬)
    assert default["avg_return_pct"] < 10.0


# === backtest cost math sanity ===

def test_cost_math_is_consistent_across_modules(tmp_db, monkeypatch):
    """同一場景跑 backtest_short 跟 backtest_combination,套成本後差距量級該一致。

    這個 test 是 regression guard — 若日後有人在某 module 重複扣 cost
    或忘記扣,差距會擴大 / 縮小,test 直接 fail。
    """
    _seed_prices("X", list(zip(_DATES, [100.0, 102.0, 105.0, 110.0, 108.0])))
    _mock_screen(monkeypatch, {
        "2024-01-02": [{"stock_id": "X", "name": "X股", "close": 100.0}],
    })
    bt_diff = (
        bter.backtest_short(
            "2024-01-01", "2024-01-31", hold_days=2,
            universe=[("X", "X股")],
            apply_costs=False,
        )["trades"].iloc[0]["return_pct"]
        - bter.backtest_short(
            "2024-01-01", "2024-01-31", hold_days=2,
            universe=[("X", "X股")],
            apply_costs=True,
        )["trades"].iloc[0]["return_pct"]
    )

    _seed_daily_pick("X", "2024-01-02", "fake_s")
    with db.get_conn() as conn:
        sb_diff = (
            sb.backtest_combination(
                conn, strategies=["fake_s"],
                start_date="2024-01-02", end_date="2024-01-08",
                holding_days=2, mode="union",
                apply_costs=False,
            )["avg_return_pct"]
            - sb.backtest_combination(
                conn, strategies=["fake_s"],
                start_date="2024-01-02", end_date="2024-01-08",
                holding_days=2, mode="union",
                apply_costs=True,
            )["avg_return_pct"]
        )

    # 兩個 module 套成本後差距應在同量級(約 0.685pp)
    assert abs(bt_diff - sb_diff) < 0.1, (
        f"backtester vs strategy_backtest 成本扣除不一致 — "
        f"bter={bt_diff:.4f} vs sb={sb_diff:.4f}"
    )
    # 兩者都該介於合理區間(0.5 ~ 1.0 pp)
    cost_rt_pct = round_trip_cost_rate() * 100
    assert cost_rt_pct < bt_diff < cost_rt_pct + 0.5
    assert cost_rt_pct < sb_diff < cost_rt_pct + 0.5
