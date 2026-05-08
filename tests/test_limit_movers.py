"""src/limit_movers.py 三個 fetcher 單元測試。

3 組情境:
- 熱門股:trading_money 排序正確
- 漲停股:閾值 +9.95%(不到 / 剛好 / 超過)邊界對
- 跌停反轉:當日跌停 + 前 5 日內有漲停 → 入選;只有當日跌停沒前期漲停 → 不入選
"""
from __future__ import annotations

import pytest

from src import config, database as db
from src import limit_movers as lm


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "lm.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    yield tmp_path
    db._reset_path_cache()


def _seed_stocks(rows: list[dict]) -> None:
    """rows: list of {stock_id, name}. 走 db.upsert_stocks 自動補 updated_at。"""
    db.upsert_stocks([
        {"stock_id": r["stock_id"], "name": r.get("name", "")}
        for r in rows
    ])


def _seed_prices(prices: list[dict]) -> None:
    """prices: list of {stock_id, date, close, volume?, trading_money?}."""
    with db.get_conn() as conn:
        for r in prices:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_prices
                  (stock_id, date, open, high, low, close, volume, trading_money)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["stock_id"], r["date"],
                    r.get("open", r["close"]),
                    r.get("high", r["close"]),
                    r.get("low", r["close"]),
                    r["close"],
                    r.get("volume", 1000),
                    r.get("trading_money", r["close"] * r.get("volume", 1000)),
                ),
            )


# === 熱門股 ===

def test_get_hot_stocks_orders_by_trading_money_desc(tmp_db):
    _seed_stocks([
        {"stock_id": "2330", "name": "台積電"},
        {"stock_id": "2317", "name": "鴻海"},
        {"stock_id": "1101", "name": "台泥"},
    ])
    _seed_prices([
        # 前一日(算 ret 用)
        {"stock_id": "2330", "date": "2026-05-06", "close": 1000, "trading_money": 1e9},
        {"stock_id": "2317", "date": "2026-05-06", "close": 200,  "trading_money": 5e8},
        {"stock_id": "1101", "date": "2026-05-06", "close": 50,   "trading_money": 1e8},
        # 當日:trading_money 排序 2317 > 2330 > 1101
        {"stock_id": "2330", "date": "2026-05-07", "close": 1010, "trading_money": 2e9},
        {"stock_id": "2317", "date": "2026-05-07", "close": 210,  "trading_money": 5e9},  # top
        {"stock_id": "1101", "date": "2026-05-07", "close": 51,   "trading_money": 5e7},
    ])
    df = lm.get_hot_stocks(n=10)
    assert len(df) == 3
    # 排序:2317 (5e9) > 2330 (2e9) > 1101 (5e7)
    assert list(df["編號"]) == ["2317", "2330", "1101"]
    # 名稱對齊
    assert df.iloc[0]["名稱"] == "鴻海"
    # 漲幅算對
    row_2317 = df[df["編號"] == "2317"].iloc[0]
    assert row_2317["漲幅"] == pytest.approx(5.0, abs=0.01)  # (210-200)/200 = 5%
    # 成交金額(億):5e9 / 1e8 = 50 億
    assert row_2317["成交金額(億)"] == pytest.approx(50.0, abs=0.01)


def test_get_hot_stocks_n_limit(tmp_db):
    _seed_stocks([{"stock_id": f"100{i}", "name": f"S{i}"} for i in range(5)])
    _seed_prices([
        {"stock_id": f"100{i}", "date": "2026-05-07",
         "close": 100, "trading_money": (5 - i) * 1e8}
        for i in range(5)
    ])
    df = lm.get_hot_stocks(n=3)
    assert len(df) == 3
    # 1000(5e8) > 1001(4e8) > 1002(3e8)
    assert list(df["編號"]) == ["1000", "1001", "1002"]


def test_get_hot_stocks_empty_db(tmp_db):
    df = lm.get_hot_stocks(n=10)
    assert df.empty
    assert set(df.columns) >= {
        "編號", "名稱", "目前股價", "漲幅", "成交金額(億)",
    }


# === 漲停股 ===

def test_get_limit_up_threshold_exact(tmp_db):
    """ret = +9.95% 剛好觸發,< +9.95% 不入選。"""
    _seed_stocks([
        {"stock_id": "0001", "name": "剛好漲停"},
        {"stock_id": "0002", "name": "差一點"},
        {"stock_id": "0003", "name": "正常漲"},
    ])
    _seed_prices([
        # prev 100 → today X 看 ret
        {"stock_id": "0001", "date": "2026-05-06", "close": 100},
        {"stock_id": "0002", "date": "2026-05-06", "close": 100},
        {"stock_id": "0003", "date": "2026-05-06", "close": 100},
        {"stock_id": "0001", "date": "2026-05-07", "close": 109.95},  # +9.95% 邊界
        {"stock_id": "0002", "date": "2026-05-07", "close": 109.0},   # +9.0% 不到
        {"stock_id": "0003", "date": "2026-05-07", "close": 105.0},   # +5%
    ])
    df = lm.get_limit_up()
    sids = set(df["編號"])
    assert sids == {"0001"}, f"應只有 0001 漲停,實際 {sids}"


def test_get_limit_up_orders_by_chg_desc(tmp_db):
    _seed_stocks([
        {"stock_id": "A", "name": "A"},
        {"stock_id": "B", "name": "B"},
    ])
    _seed_prices([
        {"stock_id": "A", "date": "2026-05-06", "close": 100},
        {"stock_id": "B", "date": "2026-05-06", "close": 100},
        {"stock_id": "A", "date": "2026-05-07", "close": 110},     # +10%
        {"stock_id": "B", "date": "2026-05-07", "close": 110.5},   # +10.5%
    ])
    df = lm.get_limit_up()
    assert list(df["編號"]) == ["B", "A"]


def test_get_limit_up_no_prev_date_returns_empty(tmp_db):
    _seed_stocks([{"stock_id": "0001", "name": "X"}])
    _seed_prices([{"stock_id": "0001", "date": "2026-05-07", "close": 100}])
    df = lm.get_limit_up()
    assert df.empty


# === 跌停反轉股(逃命波)===

def test_get_limit_down_after_up_with_prior_limit_up(tmp_db):
    """當日跌停 -10% + 前 5 日內有 +10% 漲停 → 入選。"""
    _seed_stocks([{"stock_id": "9999", "name": "飆完反轉"}])
    # 5/01 close=100, 5/02 close=110(+10% 漲停), 5/03~5/06 close=110,
    # 5/07 close=99(從 5/06 110 跌 -10%)
    _seed_prices([
        {"stock_id": "9999", "date": "2026-05-01", "close": 100},
        {"stock_id": "9999", "date": "2026-05-02", "close": 110},   # +10% 漲停
        {"stock_id": "9999", "date": "2026-05-03", "close": 110},
        {"stock_id": "9999", "date": "2026-05-04", "close": 110},
        {"stock_id": "9999", "date": "2026-05-05", "close": 110},
        {"stock_id": "9999", "date": "2026-05-06", "close": 110},
        {"stock_id": "9999", "date": "2026-05-07", "close": 99},    # -10%(從 110)
    ])
    df = lm.get_limit_down_after_up(window=5)
    assert len(df) == 1
    assert df.iloc[0]["編號"] == "9999"
    # 前 5 日漲停日 應含 2026-05-02
    assert "2026-05-02" in df.iloc[0]["前 N 日漲停日"]


def test_get_limit_down_after_up_no_prior_limit_up_excluded(tmp_db):
    """當日跌停但前 5 日沒漲停 → 不入選(普通跌停不算逃命波)。"""
    _seed_stocks([{"stock_id": "9998", "name": "正常跌停"}])
    _seed_prices([
        {"stock_id": "9998", "date": "2026-05-01", "close": 100},
        {"stock_id": "9998", "date": "2026-05-02", "close": 102},   # +2% 不算漲停
        {"stock_id": "9998", "date": "2026-05-03", "close": 103},
        {"stock_id": "9998", "date": "2026-05-04", "close": 103},
        {"stock_id": "9998", "date": "2026-05-05", "close": 103},
        {"stock_id": "9998", "date": "2026-05-06", "close": 103},
        {"stock_id": "9998", "date": "2026-05-07", "close": 92.7},  # -10% 跌停
    ])
    df = lm.get_limit_down_after_up(window=5)
    assert df.empty, f"沒前期漲停不該入選,實際: {df.to_dict('records')}"


def test_get_limit_down_after_up_today_not_limit_down_excluded(tmp_db):
    """當日只是跌很多但沒到 -9.95%,不算跌停。"""
    _seed_stocks([{"stock_id": "9997", "name": "X"}])
    _seed_prices([
        {"stock_id": "9997", "date": "2026-05-01", "close": 100},
        {"stock_id": "9997", "date": "2026-05-02", "close": 110},   # +10% 漲停過
        {"stock_id": "9997", "date": "2026-05-06", "close": 110},
        {"stock_id": "9997", "date": "2026-05-07", "close": 100},   # -9.09% 不到跌停
    ])
    df = lm.get_limit_down_after_up(window=5)
    assert df.empty


def test_get_limit_down_after_up_window_boundary_inside(tmp_db):
    """漲停日落在 window 內 → 入選。

    window=5 表示「前 5 個交易日內」,即 5/06, 5/05, 5/04, 5/03, 5/02
    (從 target_date=5/07 倒推 5 天)。5/02 的 ret 用 5/01 close 當 base 算。
    """
    _seed_stocks([{"stock_id": "5005", "name": "在內"}])
    # 5/01 close=100, 5/02 close=110(+10%, 在 window=5 內), 5/03~5/06=110, 5/07=99
    _seed_prices([
        {"stock_id": "5005", "date": "2026-05-01", "close": 100},
        {"stock_id": "5005", "date": "2026-05-02", "close": 110},   # +10% 漲停 IN window
        {"stock_id": "5005", "date": "2026-05-03", "close": 110},
        {"stock_id": "5005", "date": "2026-05-04", "close": 110},
        {"stock_id": "5005", "date": "2026-05-05", "close": 110},
        {"stock_id": "5005", "date": "2026-05-06", "close": 110},
        {"stock_id": "5005", "date": "2026-05-07", "close": 99},    # -10% 跌停
    ])
    df = lm.get_limit_down_after_up(window=5)
    assert len(df) == 1, "5/02 漲停在 window=5 內應入選"
    assert "2026-05-02" in df.iloc[0]["前 N 日漲停日"]


def test_get_limit_down_after_up_window_outside(tmp_db):
    """漲停日落在 window 外 → 不入選。"""
    _seed_stocks([{"stock_id": "5006", "name": "在外"}])
    # 5/02 漲停在 window=3 外(5/07 看前 3 天 = 5/06,5/05,5/04)
    _seed_prices([
        {"stock_id": "5006", "date": "2026-05-01", "close": 100},
        {"stock_id": "5006", "date": "2026-05-02", "close": 110},   # +10% 漲停 (5 天前)
        {"stock_id": "5006", "date": "2026-05-03", "close": 110},
        {"stock_id": "5006", "date": "2026-05-04", "close": 110},
        {"stock_id": "5006", "date": "2026-05-05", "close": 110},
        {"stock_id": "5006", "date": "2026-05-06", "close": 110},
        {"stock_id": "5006", "date": "2026-05-07", "close": 99},    # -10% 跌停
    ])
    df = lm.get_limit_down_after_up(window=3)
    assert df.empty, "5/02 漲停在 window=3 外不該入選"
