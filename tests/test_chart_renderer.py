"""src/chart_renderer.py 單元測試。

涵蓋:
- render_candlestick_chart:
  - 空 DB → 帶「找不到」annotation 的 fallback 圖
  - 基本 OHLC trace 存在
  - 指標 multiselect 動態切 subplot row count
  - 主圖只有 MA / Bollinger,RSI/MACD/KD/Stoch 才開副圖
- compute_bollinger / compute_kd / compute_stoch:
  - 邊界:資料不足 → NaN
  - upper > mid > lower
  - KD 9-3-3 + Stoch 14-3-3 都回 0–100 範圍
- mark_pick_dates:
  - 命中日期變成 annotation(⭐)
  - 範圍外的 pick 不標
  - dedup by date(同日多策略只標一次)
- mark_position_levels:
  - open position → 三條 hline(進場 / 停損 / 停利)
  - 多筆 open(加碼)→ 多倍 hline
  - 無 open → no hline
- mark_pattern_signals:
  - candlestick_patterns 不存在 → graceful return(不炸)
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pytest

from src import config, database as db
from src.chart_renderer import (
    compute_bollinger,
    compute_kd,
    compute_stoch,
    mark_pattern_signals,
    mark_pick_dates,
    mark_position_levels,
    render_candlestick_chart,
)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "chart_test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    return db_file


def _seed_prices(sid: str, n: int = 150, base: float = 100.0) -> None:
    """灌 n 天連續日線(從 today 倒數),價格小幅波動讓所有指標都有值。"""
    today = date.today()
    rows = []
    for i in range(n):
        d = (today - timedelta(days=n - 1 - i)).isoformat()
        # 走勢:緩升 + 小幅 sine 波(讓 KD / RSI 不會一直 100)
        close = base + i * 0.3 + 2.0 * np.sin(i * 0.4)
        rows.append({
            "stock_id": sid, "date": d,
            "open": close - 0.2, "high": close + 0.5,
            "low": close - 0.5, "close": close,
            "volume": 10000 + i * 100,
        })
    db.upsert_daily_prices(rows)


# ============================================================================
# render_candlestick_chart
# ============================================================================

def test_render_empty_returns_fallback_figure(tmp_db):
    """空 DB → 帶「找不到」annotation 的 placeholder 圖,不炸。"""
    fig = render_candlestick_chart("9999", days=60)
    assert isinstance(fig, go.Figure)
    # 應該有一個 fallback annotation 提醒沒資料
    ann_texts = [a.text for a in (fig.layout.annotations or [])]
    assert any("找不到" in (t or "") for t in ann_texts), (
        f"應有「找不到」fallback annotation,實際 annotations={ann_texts}"
    )


def test_render_has_candlestick_trace(tmp_db):
    _seed_prices("2330", n=150)
    fig = render_candlestick_chart("2330", days=60,
                                    indicators=["MA20", "MA60", "Volume"])
    # 至少有一個 Candlestick trace
    types = [type(tr).__name__ for tr in fig.data]
    assert "Candlestick" in types, f"應有 Candlestick trace,實際={types}"


def test_render_dynamic_subplot_rows_for_indicators(tmp_db):
    """指標 multiselect 動態切 subplot row 數 — 只選 Volume → 2 row;
    Volume+RSI+MACD → 4 row。"""
    _seed_prices("2330", n=150)
    # 只主圖 + Volume
    fig1 = render_candlestick_chart(
        "2330", days=60, indicators=["MA20", "Volume"],
    )
    # 5 row:主 + Volume + RSI + MACD + KD
    fig2 = render_candlestick_chart(
        "2330", days=60,
        indicators=["MA20", "Volume", "RSI", "MACD", "KD"],
    )
    # plotly subplots 透過 yaxis 數 row:row N 對應 yaxis{N}
    def _row_count(fig):
        # 走 layout.yaxis / yaxis2 / yaxis3 ...
        layout_dict = fig.layout.to_plotly_json()
        return sum(1 for k in layout_dict.keys() if k.startswith("yaxis"))
    assert _row_count(fig1) == 2
    assert _row_count(fig2) == 5


def test_render_main_only_indicators_keep_single_row(tmp_db):
    """只選 MA / Bollinger 主圖類指標 → 仍是 1 row(無副圖)。"""
    _seed_prices("2330", n=150)
    fig = render_candlestick_chart(
        "2330", days=60, indicators=["MA20", "MA60", "Bollinger"],
    )
    layout_dict = fig.layout.to_plotly_json()
    row_count = sum(1 for k in layout_dict.keys() if k.startswith("yaxis"))
    assert row_count == 1, f"主圖類指標應 1 row,實際 {row_count}"


def test_render_accepts_caller_supplied_df(tmp_db):
    """傳入預 load 的 df(避免重複 query)— 應跑過不打 DB。"""
    _seed_prices("2330", n=150)
    df = db.get_stock_kline_with_indicators("2330", days=60)
    assert not df.empty
    # 即使把 db 砍掉,只要 df 帶進去就不會 fail
    fig = render_candlestick_chart(
        "9999",  # 故意給不存在的 sid;有 df 應該不重撈
        days=60, indicators=["MA20", "Volume"],
        df=df,
    )
    types = [type(tr).__name__ for tr in fig.data]
    assert "Candlestick" in types


# ============================================================================
# 指標 helper
# ============================================================================

def test_compute_bollinger_orders_upper_mid_lower():
    closes = pd.Series([100 + i * 0.5 for i in range(40)])
    bb = compute_bollinger(closes, period=20, num_std=2.0)
    last = bb.iloc[-1]
    assert last["upper"] > last["mid"] > last["lower"]
    # std > 0 → upper > lower
    assert last["upper"] - last["lower"] > 0


def test_compute_bollinger_insufficient_data_returns_nan():
    closes = pd.Series([100.0, 101.0, 102.0])
    bb = compute_bollinger(closes, period=20)
    # period > rows → 全 NaN
    assert bb["upper"].isna().all()
    assert bb["mid"].isna().all()
    assert bb["lower"].isna().all()


def test_compute_kd_in_range_0_100():
    df = pd.DataFrame({
        "high": [100 + i * 0.3 for i in range(30)],
        "low": [99 + i * 0.3 for i in range(30)],
        "close": [99.5 + i * 0.3 for i in range(30)],
    })
    kd_df = compute_kd(df, period=9)
    valid = kd_df.dropna()
    assert (valid["K"] >= 0).all() and (valid["K"] <= 100).all()
    assert (valid["D"] >= 0).all() and (valid["D"] <= 100).all()


def test_compute_stoch_in_range_0_100():
    df = pd.DataFrame({
        "high": [100 + i * 0.3 + np.sin(i) for i in range(40)],
        "low": [99 + i * 0.3 + np.sin(i) for i in range(40)],
        "close": [99.5 + i * 0.3 + np.sin(i) for i in range(40)],
    })
    st_df = compute_stoch(df, period=14)
    valid = st_df.dropna()
    assert (valid["K"] >= 0).all() and (valid["K"] <= 100).all()
    assert (valid["D"] >= 0).all() and (valid["D"] <= 100).all()


def test_compute_stoch_insufficient_data_returns_nan():
    df = pd.DataFrame({
        "high": [100, 101, 102],
        "low": [99, 100, 101],
        "close": [99.5, 100.5, 101.5],
    })
    st_df = compute_stoch(df, period=14)
    assert st_df["K"].isna().all()


# ============================================================================
# mark_pick_dates
# ============================================================================

def test_mark_pick_dates_adds_star_annotations(tmp_db):
    _seed_prices("2330", n=120)
    # 灌 daily_picks — 在 chart 範圍內的 2 個日期
    today = date.today()
    in_range_d1 = (today - timedelta(days=10)).isoformat()
    in_range_d2 = (today - timedelta(days=20)).isoformat()
    out_of_range = (today - timedelta(days=500)).isoformat()  # 範圍外

    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO daily_picks (trade_date, universe, strategy, sid, "
            "score, rank, params_hash, payload, ml_prob, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (in_range_d1, "TW", "volume_kd", "2330",
                 80.0, 1, "h1", "{}", 0.7, "x"),
                # 同日多策略 — 應 dedup 只標一次
                (in_range_d1, "TW", "macd_cross", "2330",
                 75.0, 2, "h1", "{}", 0.6, "x"),
                (in_range_d2, "TW", "volume_kd", "2330",
                 78.0, 1, "h1", "{}", 0.65, "x"),
                (out_of_range, "TW", "volume_kd", "2330",
                 60.0, 1, "h1", "{}", 0.5, "x"),
            ],
        )

    fig = render_candlestick_chart(
        "2330", days=60, indicators=["MA20", "Volume"],
    )
    n_before = len(fig.layout.annotations or [])
    fig = mark_pick_dates(fig, "2330")
    n_after = len(fig.layout.annotations or [])

    # 應加 2 個 ⭐(dedup + 範圍外排除)
    star_anns = [
        a for a in (fig.layout.annotations or [])
        if (a.text or "").strip() == "⭐"
    ]
    assert len(star_anns) == 2, (
        f"應 2 個 ⭐(dedup + 範圍外排除),實際 {len(star_anns)}"
    )
    assert n_after > n_before


def test_mark_pick_dates_empty_no_op(tmp_db):
    """無 pick → 函式不炸,annotation 不變。"""
    _seed_prices("2330", n=60)
    fig = render_candlestick_chart("2330", days=60, indicators=["MA20"])
    n_before = len(fig.layout.annotations or [])
    fig = mark_pick_dates(fig, "2330")
    n_after = len(fig.layout.annotations or [])
    assert n_after == n_before


# ============================================================================
# mark_position_levels
# ============================================================================

def test_mark_position_levels_adds_three_hlines(tmp_db):
    """單筆 open position → 進場 / 停損 / 停利 三條 hline。"""
    _seed_prices("2330", n=120)
    db.add_position(
        stock_id="2330",
        entry_date=date.today().isoformat(),
        entry_price=100.0,
        shares=1000,
        stop_loss=95.0,
        take_profit=110.0,
        notes="test",
    )
    fig = render_candlestick_chart(
        "2330", days=60, indicators=["MA20", "Volume"],
    )
    fig = mark_position_levels(fig, "2330")
    # plotly add_hline 寫成 shape;檢查 shape 數至少 3
    shapes = fig.layout.shapes or []
    assert len(shapes) >= 3, f"應 ≥3 條 hline,實際 {len(shapes)}"


def test_mark_position_levels_other_sid_no_op(tmp_db):
    """另一檔的 open position → 不該標到 query 的 sid 上。"""
    _seed_prices("2330", n=60)
    db.add_position(
        stock_id="2454",  # 別檔
        entry_date=date.today().isoformat(),
        entry_price=200.0,
        shares=1000,
        stop_loss=190.0,
        take_profit=220.0,
    )
    fig = render_candlestick_chart("2330", days=60, indicators=["MA20"])
    shapes_before = len(fig.layout.shapes or [])
    fig = mark_position_levels(fig, "2330")
    shapes_after = len(fig.layout.shapes or [])
    assert shapes_after == shapes_before, "別檔 position 不該影響本檔 chart"


def test_mark_position_levels_no_open_no_op(tmp_db):
    _seed_prices("2330", n=60)
    fig = render_candlestick_chart("2330", days=60, indicators=["MA20"])
    shapes_before = len(fig.layout.shapes or [])
    fig = mark_position_levels(fig, "2330")
    shapes_after = len(fig.layout.shapes or [])
    assert shapes_after == shapes_before


# ============================================================================
# mark_pattern_signals (graceful import)
# ============================================================================

def test_mark_pattern_signals_graceful_when_module_missing(tmp_db):
    """candlestick_patterns 模組還沒 merge → try/except 直接 return fig 不炸。"""
    _seed_prices("2330", n=60)
    fig = render_candlestick_chart("2330", days=60, indicators=["MA20"])
    # 即使模組不存在 / API 名稱對不上,都該回原 fig
    fig2 = mark_pattern_signals(fig, "2330", days=60)
    assert fig2 is fig or isinstance(fig2, go.Figure)
