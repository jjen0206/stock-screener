"""src/backtester.py 單元測試。

策略:
- mock screen_short() 回傳預設好的入選結果,直接測 backtest 邏輯本身
- daily_prices 表灌真實假資料給 _find_sell 用
- 不打網路、不依賴指標計算正確
"""
from __future__ import annotations

import pandas as pd
import pytest

from src import backtester as bt
from src import config, database as db


# === 共用 fixture ===

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "bt.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def _seed_prices(stock_id: str, dates_prices: list[tuple[str, float]]) -> None:
    """灌假 daily_prices(只放 close 欄位,其餘 None)。"""
    db.upsert_stocks([{"stock_id": stock_id, "name": stock_id, "market": "TW"}])
    db.upsert_daily_prices([
        {"stock_id": stock_id, "date": d, "open": p, "high": p, "low": p,
         "close": p, "volume": 1000, "trading_money": None,
         "trading_turnover": None, "spread": None}
        for d, p in dates_prices
    ])


def _mock_screen(monkeypatch, results_by_date: dict[str, list[dict]]) -> None:
    """讓 backtester.screen_short 回傳預設好的入選結果。

    results_by_date[d] = [{stock_id, name, close}, ...] (其他欄位省略)
    """
    def fake(date, params=None, stock_ids=None):
        rows = results_by_date.get(date, [])
        return pd.DataFrame(
            rows,
            columns=["stock_id", "name", "close", "volume", "ma_volume_5",
                     "k", "d", "inst_total_3d", "matched_at"],
        )
    monkeypatch.setattr(bt, "screen_short", fake)


# 5 個交易日的範本
_DATES = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]


# === 主流程 ===

def test_backtest_basic_buy_and_sell(tmp_db, monkeypatch):
    """基本流程:第 1 日買、持 2 天賣,報酬正確算出。"""
    # 兩檔都有 5 天連續資料
    _seed_prices("A", list(zip(_DATES, [100.0, 102.0, 105.0, 110.0, 108.0])))
    _seed_prices("B", list(zip(_DATES, [50.0, 51.0, 49.0, 53.0, 55.0])))

    # 第一天 A 入選 (close=100)
    _mock_screen(monkeypatch, {
        "2024-01-02": [{"stock_id": "A", "name": "A股", "close": 100.0}],
    })

    result = bt.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=2,
        universe=[("A", "A股"), ("B", "B股")],
    )

    trades = result["trades"]
    assert len(trades) == 1
    t = trades.iloc[0]
    assert t["buy_date"] == "2024-01-02"
    assert t["stock_id"] == "A"
    assert t["buy_price"] == 100.0
    # 持 2 個交易日,賣出在第 3 個交易日 (2024-01-04, close=105)
    assert t["sell_date"] == "2024-01-04"
    assert t["sell_price"] == 105.0
    assert t["return_pct"] == pytest.approx(5.0)


def test_backtest_no_signal_no_trade(tmp_db, monkeypatch):
    """無入選日就不該產生交易。"""
    _seed_prices("A", list(zip(_DATES, [100.0, 101.0, 102.0, 103.0, 104.0])))
    _mock_screen(monkeypatch, {})  # 全部交易日都沒入選

    result = bt.backtest_short(
        "2024-01-01", "2024-01-31",
        universe=[("A", "A股")],
    )
    assert result["trades"].empty
    assert result["summary"]["trades"] == 0


def test_backtest_truncate_at_end(tmp_db, monkeypatch):
    """期間結尾入選但後面沒足夠交易日 → 強制取最後一筆當平倉,不丟掉。"""
    _seed_prices("A", list(zip(_DATES, [100.0, 105.0, 110.0, 115.0, 120.0])))

    # 倒數第二天入選,持 hold_days=10 (但後面只剩 1 天)
    _mock_screen(monkeypatch, {
        "2024-01-05": [{"stock_id": "A", "name": "A股", "close": 115.0}],
    })

    result = bt.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=10,
        universe=[("A", "A股")],
    )
    trades = result["trades"]
    assert len(trades) == 1
    # 強制取最後一筆 (2024-01-08, close=120)
    assert trades.iloc[0]["sell_date"] == "2024-01-08"
    assert trades.iloc[0]["sell_price"] == 120.0


def test_backtest_no_future_data_skipped(tmp_db, monkeypatch):
    """買進日是最後一天 → 沒下一個交易日 → 該筆不計入。"""
    _seed_prices("A", list(zip(_DATES, [100.0, 105.0, 110.0, 115.0, 120.0])))

    _mock_screen(monkeypatch, {
        "2024-01-08": [{"stock_id": "A", "name": "A股", "close": 120.0}],
    })

    result = bt.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=5,
        universe=[("A", "A股")],
    )
    assert result["trades"].empty


def test_backtest_skips_holiday_gaps(tmp_db, monkeypatch):
    """個股停牌(資料缺日)→ 自動取下一個有資料的交易日當平倉日。"""
    # A 股 2024-01-04 停牌(資料缺)
    _seed_prices("A", [
        ("2024-01-02", 100.0),
        ("2024-01-03", 102.0),
        # 2024-01-04 缺
        ("2024-01-05", 110.0),
        ("2024-01-08", 115.0),
    ])
    # B 股第 4 日有資料,讓 trading_days 仍包含 2024-01-04
    _seed_prices("B", [(d, 50.0) for d in _DATES])

    _mock_screen(monkeypatch, {
        "2024-01-02": [{"stock_id": "A", "name": "A股", "close": 100.0}],
    })

    result = bt.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=2,
        universe=[("A", "A股"), ("B", "B股")],
    )
    # 持 2 天但 2024-01-04 停牌 → 跳到 2024-01-05 (110.0)
    t = result["trades"].iloc[0]
    assert t["sell_date"] == "2024-01-05"
    assert t["sell_price"] == 110.0


def test_backtest_summary_stats_arithmetic(tmp_db, monkeypatch):
    """三筆已知交易,統計值對拍。"""
    # 第 1 天買 A → 持 1 天賣
    # 第 2 天買 B → 持 1 天賣
    # 第 3 天買 A → 持 1 天賣
    _seed_prices("A", list(zip(_DATES, [100.0, 110.0, 100.0, 90.0, 100.0])))  # +10%, ?, -10%
    _seed_prices("B", list(zip(_DATES, [50.0, 50.0, 55.0, 55.0, 55.0])))      # ?, +10%, ?

    _mock_screen(monkeypatch, {
        "2024-01-02": [{"stock_id": "A", "name": "A股", "close": 100.0}],   # 買 100, 隔天 110 → +10%
        "2024-01-03": [{"stock_id": "B", "name": "B股", "close": 50.0}],    # 買 50, 隔天 55 → +10%
        "2024-01-04": [{"stock_id": "A", "name": "A股", "close": 100.0}],   # 買 100, 隔天 90 → -10%
    })

    result = bt.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=1,
        universe=[("A", "A股"), ("B", "B股")],
    )
    s = result["summary"]
    assert s["trades"] == 3
    assert s["win_rate"] == pytest.approx(2 / 3 * 100)  # 2 勝
    assert s["avg_return"] == pytest.approx((10 + 10 - 10) / 3)  # ≈ 3.33
    # 複利: 1.10 × 1.10 × 0.90 - 1 = 0.089
    assert s["total_return"] == pytest.approx(8.9, abs=0.01)
    assert s["max_win"] == pytest.approx(10.0)
    assert s["max_loss"] == pytest.approx(-10.0)


def test_backtest_progress_callback(tmp_db, monkeypatch):
    """callback 該被叫一次/交易日。"""
    _seed_prices("A", list(zip(_DATES, [100.0] * 5)))
    _mock_screen(monkeypatch, {})

    calls = []

    def cb(idx, total, d):
        calls.append((idx, total, d))

    bt.backtest_short(
        "2024-01-01", "2024-01-31",
        universe=[("A", "A股")],
        on_progress=cb,
    )
    assert len(calls) == 5  # 5 個交易日
    assert calls[0][2] == "2024-01-02"
    assert calls[-1][2] == "2024-01-08"


def test_backtest_empty_universe(tmp_db):
    """空 universe → 直接回空結果,不該炸。"""
    result = bt.backtest_short(
        "2024-01-01", "2024-01-31", universe=[],
    )
    assert result["trades"].empty
    assert result["summary"]["trades"] == 0
    assert result["equity_curve"].empty


def test_backtest_no_data_in_period(tmp_db, monkeypatch):
    """universe 有股號但 daily_prices 無該區間資料 → 空結果。"""
    _mock_screen(monkeypatch, {})
    result = bt.backtest_short(
        "2024-01-01", "2024-01-31",
        universe=[("A", "A股")],
    )
    assert result["trades"].empty


def test_equity_curve_compounds_correctly(tmp_db, monkeypatch):
    """累積報酬曲線:在 sell_date 之後該日值應反映複利。"""
    _seed_prices("A", list(zip(_DATES, [100.0, 100.0, 110.0, 110.0, 121.0])))

    # 第 1 天買 A,第 3 天賣(報酬 +10%)
    # 第 3 天買 A,第 5 天賣(報酬 +10%)
    _mock_screen(monkeypatch, {
        "2024-01-02": [{"stock_id": "A", "name": "A股", "close": 100.0}],
        "2024-01-04": [{"stock_id": "A", "name": "A股", "close": 110.0}],
    })

    result = bt.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=2,
        universe=[("A", "A股")],
    )
    curve = result["equity_curve"]
    # 第 1 天 (2024-01-02): 還沒結算
    assert curve["2024-01-02"] == pytest.approx(0.0)
    # 2024-01-04: 第一筆結算 → +10%
    assert curve["2024-01-04"] == pytest.approx(10.0)
    # 2024-01-08: 第二筆結算 → 1.1 × 1.1 - 1 = 0.21 → 21%
    assert curve["2024-01-08"] == pytest.approx(21.0)


def test_backtest_callback_error_does_not_stop(tmp_db, monkeypatch):
    """callback 自己 raise 不該影響回測流程。"""
    _seed_prices("A", list(zip(_DATES, [100.0, 110.0, 100.0, 90.0, 100.0])))
    _mock_screen(monkeypatch, {
        "2024-01-02": [{"stock_id": "A", "name": "A股", "close": 100.0}],
    })

    def bad_cb(*args):
        raise RuntimeError("boom")

    result = bt.backtest_short(
        "2024-01-01", "2024-01-31", hold_days=1,
        universe=[("A", "A股")],
        on_progress=bad_cb,
    )
    # 流程沒中斷,仍產生 1 筆交易
    assert len(result["trades"]) == 1
