"""compute_entry_range 單元測試(U3 進場價區間建議)。

驗收:
  - 正常 case:close + ATR + BB 算出 (low, high),low < close
  - BB_lower 比 ATR_floor 更低 → 採 BB_lower(min 邏輯)
  - 資料 < 20 天 → None
  - close 無效(None / 0 / NaN)→ None
  - 多檔 batch 各自獨立(不互相污染)

策略:塞 production schema 假資料,用 db.upsert_daily_prices 走真寫入路徑。
"""
from __future__ import annotations

import math

import pytest

from src import config, database as db
from src.notifier import compute_entry_range


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "entry_range.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    yield tmp_path
    db._reset_path_cache()


def _seed_steady_prices(sid: str, close: float, n_days: int = 25) -> None:
    """塞 n_days 天「穩態」價格 — close 固定 ± 微擾,讓 ATR / BB 算得出。

    用日期 2026-04-15..2026-05-09 範圍(25 個交易日)。
    """
    rows = []
    base_date = 20260415  # YYYYMMDD,方便遞增不跨月驗算
    for i in range(n_days):
        # 微擾 ±1.0,讓 high/low/close 有變化(ATR/BB 都需 spread)
        offset = (-1.0 if i % 2 else 1.0) * 0.5
        c = close + offset
        rows.append({
            "stock_id": sid,
            "date": f"2026-04-{15 + i:02d}" if (15 + i) <= 30
                    else f"2026-05-{15 + i - 30:02d}",
            "open": c,
            "high": c + 1.0,
            "low": c - 1.0,
            "close": c,
            "volume": 1000,
            "trading_money": c * 1000,
            "trading_turnover": 100,
            "spread": 0,
        })
    db.upsert_daily_prices(rows)


def _seed_widening_prices(sid: str, close_final: float, n_days: int = 25) -> None:
    """塞「BB 通道寬」的歷史 — close 變化大讓 std 大、BB_lower 遠低於 close。

    波動 ±5,讓 BB_lower 比 ATR_floor 還低。
    """
    rows = []
    for i in range(n_days):
        # 大幅振盪
        offset = (-5.0 if i % 2 else 5.0)
        c = close_final + offset
        rows.append({
            "stock_id": sid,
            "date": f"2026-04-{15 + i:02d}" if (15 + i) <= 30
                    else f"2026-05-{15 + i - 30:02d}",
            "open": c,
            "high": c + 1.5,
            "low": c - 1.5,
            "close": c,
            "volume": 1000,
            "trading_money": c * 1000,
            "trading_turnover": 100,
            "spread": 0,
        })
    # 最後一筆 = close_final 作為 anchor(讓 caller 知道「當前 close」應該多少)
    rows[-1]["close"] = close_final
    rows[-1]["open"] = close_final
    rows[-1]["high"] = close_final + 1.5
    rows[-1]["low"] = close_final - 1.5
    db.upsert_daily_prices(rows)


def test_normal_case_returns_low_below_close(tmp_db):
    """正常 case:close=1000,ATR ≈ 2 → low ≈ 999,upper = close。"""
    _seed_steady_prices("2330", close=1000.0, n_days=25)
    with db.get_conn() as conn:
        result = compute_entry_range("2330", 1000.0, conn)
    assert result is not None
    low, high = result
    assert high == pytest.approx(1000.0, abs=0.01)
    assert 0 < low < high  # 下緣必 < 上緣
    # 穩態微擾 ±0.5 → ATR ≈ 2(high-low=2 + 微擾),0.5×ATR ≈ 1 → low ≈ 999
    assert low <= 1000.0 - 0.5  # 至少有 0.5 折扣


def test_bb_lower_dominates_when_volatile(tmp_db):
    """高波動 → BB_lower 遠低於 close,min() 會選 BB_lower(更低)。"""
    _seed_widening_prices("9999", close_final=100.0, n_days=25)
    with db.get_conn() as conn:
        result = compute_entry_range("9999", 100.0, conn)
    assert result is not None
    low, high = result
    # BB_lower 應該至少 < close - 5(因為波動 ±5,std 大)
    # ATR_floor = close - 0.5×ATR ≈ close - 0.5×(7 左右) ≈ close - 3.5
    # min(close-3.5, BB_lower≈close-8) → BB_lower 勝出
    assert low < high - 5.0  # 確認 BB_lower 拉開比 ATR_floor 更多


def test_insufficient_history_returns_none(tmp_db):
    """歷史 < 20 天 → None。"""
    _seed_steady_prices("1234", close=500.0, n_days=10)
    with db.get_conn() as conn:
        result = compute_entry_range("1234", 500.0, conn)
    assert result is None


def test_close_none_returns_none(tmp_db):
    """close=None → None(不打 DB 也安全)。"""
    _seed_steady_prices("2330", close=1000.0, n_days=25)
    with db.get_conn() as conn:
        assert compute_entry_range("2330", None, conn) is None
        assert compute_entry_range("2330", 0.0, conn) is None
        assert compute_entry_range("2330", float("nan"), conn) is None


def test_unknown_sid_returns_none(tmp_db):
    """sid 在 DB 沒資料 → None(不存在的股票)。"""
    with db.get_conn() as conn:
        assert compute_entry_range("0000", 100.0, conn) is None


def test_multiple_sids_independent(tmp_db):
    """多檔 batch 各自獨立(不互相污染 ATR/BB cache)。"""
    _seed_steady_prices("1101", close=50.0, n_days=25)
    _seed_steady_prices("2330", close=1000.0, n_days=25)
    with db.get_conn() as conn:
        r1 = compute_entry_range("1101", 50.0, conn)
        r2 = compute_entry_range("2330", 1000.0, conn)
    assert r1 is not None and r2 is not None
    # 兩檔規模差 20×,low / high 應該對應股價尺度
    assert r1[1] == pytest.approx(50.0, abs=0.01)
    assert r2[1] == pytest.approx(1000.0, abs=0.01)
    assert r1[0] < r1[1]
    assert r2[0] < r2[1]
    # 不互相污染(2330 的 low 不會 <= 100)
    assert r2[0] > 100
