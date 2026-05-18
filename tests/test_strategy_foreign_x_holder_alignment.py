"""src/strategies.py::screen_foreign_x_holder_alignment 單元測試。

涵蓋:
- 沒有 shareholder_concentration 資料 → 0 picks
- 全條件符合(單檔池)→ 1 pick,score 含 foreign_z=threshold + holder_z
- 外資 5d 累計 <= 0 → 不入選
- 千張戶本週 delta_w <= 0 → 不入選
- 千張戶突破未達 4w mean + 0.5σ → 不入選
- 千張戶歷史不足 4 週 → 不入選
- 60 日均量 < 1000 張 → 不入選
- 多檔池:foreign z-score 過濾(小者 z<1.0 被攔)
- registry 已 wire(ALL_STRATEGIES / LABELS / CATEGORY / RR_PARAMS / NATURE)
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _td

import pytest

from src import config, consensus, database as db
from src import strategies as strat


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "fx_holder.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def _seed_stocks(sids: list[str]) -> None:
    db.upsert_stocks([
        {"stock_id": s, "name": f"股{s}", "market": "TW"}
        for s in sids
    ])


def _seed_daily_prices(
    sid: str,
    start: str,
    end: str,
    volume: int = 2_000_000,  # 預設 2000 張(> 1000 張門檻)
    close: float = 100.0,
) -> None:
    """灌日 K 線(工作日):volume 用「股」單位(1 張 = 1000 股)。"""
    rows = []
    d = _date.fromisoformat(start)
    end_d = _date.fromisoformat(end)
    while d <= end_d:
        if d.weekday() < 5:
            ds = d.isoformat()
            rows.append({
                "stock_id": sid, "date": ds,
                "open": close, "high": close, "low": close, "close": close,
                "volume": int(volume),
                "trading_money": None, "trading_turnover": None, "spread": None,
            })
        d += _td(days=1)
    db.upsert_daily_prices(rows)


def _seed_foreign(
    sid: str, dates_and_foreign: list[tuple[str, int]],
) -> None:
    """[(date, foreign_net), ...] — trust/dealer = 0。"""
    db.upsert_institutional([
        {
            "stock_id": sid, "date": d,
            "foreign_buy_sell": f, "trust_buy_sell": 0,
            "dealer_buy_sell": 0, "total_buy_sell": f,
        }
        for d, f in dates_and_foreign
    ])


def _seed_shareholder(
    sid: str, weeks_and_deltas: list[tuple[str, int]],
) -> None:
    """[(week_end, holders_delta_w), ...]"""
    db.upsert_shareholder_concentration([
        {
            "sid": sid, "week_end": wk,
            "holders_1000up_count": 100 + d,
            "total_holders": 10000,
            "holders_pct": 0.01,
            "holders_delta_w": d,
        }
        for wk, d in weeks_and_deltas
    ])


def _last_5_weekdays(as_of: str) -> list[str]:
    out: list[str] = []
    d = _date.fromisoformat(as_of)
    while len(out) < 5:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= _td(days=1)
    return out


def _last_5_weeks(as_of: str) -> list[str]:
    """as_of 之前(含)的最近 5 個週五(ASC,最舊在前)。"""
    out: list[str] = []
    d = _date.fromisoformat(as_of)
    while len(out) < 5:
        if d.weekday() == 4:  # Friday
            out.append(d.isoformat())
        d -= _td(days=1)
    return list(reversed(out))  # ASC


# === case 1:完全沒 shareholder 資料 → 0 picks ===

def test_fx_holder_no_data(tmp_db):
    sid = "1201"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    days = _last_5_weekdays("2026-05-15")
    _seed_foreign(sid, [(d, 100_000) for d in days])
    # 不灌 shareholder_concentration

    df = strat.screen_foreign_x_holder_alignment(
        "2026-05-15", stock_ids=[sid]
    )
    assert df.empty


# === case 2:全條件符合(單檔池) → 1 pick ===

def test_fx_holder_full_match_single_pool(tmp_db):
    """單檔池,池太小 → foreign_z = threshold(1.0)中性過門檻。

    千張戶 4 週 prior = [10,10,10,10] → mu=10, sigma=0, threshold = 10 + 0.5*0 = 10。
    本週 delta_w = 50 > 10 → 過。sigma_h=0 → holder_z = sigma_thr(0.5)。
    score = (1.0 * 0.5) + (0.5 * 0.5) = 0.75。
    """
    sid = "1202"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15", volume=2_000_000)
    days = _last_5_weekdays("2026-05-15")
    _seed_foreign(sid, [(d, 200_000) for d in days])  # 5d sum = 1M > 0
    weeks = _last_5_weeks("2026-05-15")
    # 前 4 週 delta_w=10,本週 delta_w=50(突破 mean+0.5σ since σ=0)
    deltas = [(weeks[i], 10) for i in range(4)] + [(weeks[4], 50)]
    _seed_shareholder(sid, deltas)

    df = strat.screen_foreign_x_holder_alignment(
        "2026-05-15", stock_ids=[sid]
    )
    assert len(df) == 1, df
    row = df.iloc[0]
    assert row["stock_id"] == sid
    assert row["foreign_5d_sum"] == 1_000_000
    assert row["foreign_zscore"] == pytest.approx(1.0)
    assert row["holders_delta_w"] == 50
    assert row["holder_zscore"] == pytest.approx(0.5)
    assert row["score"] == pytest.approx(0.75)
    assert row["week_end"] == weeks[4]


# === case 3:外資 5d 累計 <= 0 → 不入選 ===

def test_fx_holder_foreign_sum_nonpositive(tmp_db):
    sid = "1203"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    days = _last_5_weekdays("2026-05-15")
    # 3 天買 + 2 天賣 = sum 0
    nets = [
        (days[0], 100_000),
        (days[1], 100_000),
        (days[2], 100_000),
        (days[3], -150_000),
        (days[4], -150_000),
    ]
    _seed_foreign(sid, nets)
    weeks = _last_5_weeks("2026-05-15")
    deltas = [(weeks[i], 10) for i in range(4)] + [(weeks[4], 50)]
    _seed_shareholder(sid, deltas)

    df = strat.screen_foreign_x_holder_alignment(
        "2026-05-15", stock_ids=[sid]
    )
    assert df.empty


# === case 4:千張戶本週 delta_w <= 0 → 不入選 ===

def test_fx_holder_delta_nonpositive(tmp_db):
    sid = "1204"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    days = _last_5_weekdays("2026-05-15")
    _seed_foreign(sid, [(d, 100_000) for d in days])
    weeks = _last_5_weeks("2026-05-15")
    # 本週 delta_w = -5(千張戶減少 → 大戶出貨,不算)
    deltas = [(weeks[i], 10) for i in range(4)] + [(weeks[4], -5)]
    _seed_shareholder(sid, deltas)

    df = strat.screen_foreign_x_holder_alignment(
        "2026-05-15", stock_ids=[sid]
    )
    assert df.empty


# === case 5:千張戶突破未達 4w mean + 0.5σ → 不入選 ===

def test_fx_holder_delta_below_threshold(tmp_db):
    """前 4 週 delta=[10,12,14,16],mu=13, sigma≈2.58,threshold=13+0.5*2.58≈14.29。
    本週 delta_w = 14 → 不過(< 14.29)。
    """
    sid = "1205"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    days = _last_5_weekdays("2026-05-15")
    _seed_foreign(sid, [(d, 100_000) for d in days])
    weeks = _last_5_weeks("2026-05-15")
    deltas = [
        (weeks[0], 10), (weeks[1], 12), (weeks[2], 14), (weeks[3], 16),
        (weeks[4], 14),  # 本週低於 threshold ~14.29
    ]
    _seed_shareholder(sid, deltas)

    df = strat.screen_foreign_x_holder_alignment(
        "2026-05-15", stock_ids=[sid]
    )
    assert df.empty


# === case 6:千張戶歷史不足 4 週 → 不入選 ===

def test_fx_holder_insufficient_history(tmp_db):
    sid = "1206"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    days = _last_5_weekdays("2026-05-15")
    _seed_foreign(sid, [(d, 100_000) for d in days])
    weeks = _last_5_weeks("2026-05-15")
    # 只有 2 週歷史 + 本週 → 不足 4 週
    deltas = [(weeks[2], 10), (weeks[3], 10), (weeks[4], 100)]
    _seed_shareholder(sid, deltas)

    df = strat.screen_foreign_x_holder_alignment(
        "2026-05-15", stock_ids=[sid]
    )
    assert df.empty


# === case 7:60 日均量不足 → 不入選 ===

def test_fx_holder_low_liquidity(tmp_db):
    sid = "1207"
    _seed_stocks([sid])
    # volume = 500_000 股 = 500 張 < 1000 張
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15", volume=500_000)
    days = _last_5_weekdays("2026-05-15")
    _seed_foreign(sid, [(d, 100_000) for d in days])
    weeks = _last_5_weeks("2026-05-15")
    deltas = [(weeks[i], 10) for i in range(4)] + [(weeks[4], 50)]
    _seed_shareholder(sid, deltas)

    df = strat.screen_foreign_x_holder_alignment(
        "2026-05-15", stock_ids=[sid]
    )
    assert df.empty


# === case 8:多檔池 — foreign z-score 過濾 ===

def test_fx_holder_multi_pool_zscore_filter(tmp_db):
    """2 檔池,foreign 5d sum 一大一小。
    大者 sum=2_000_000,小者 sum=100_000。
    mu = 1_050_000, sigma ≈ 1_343_503(ddof=1)。
    大者 z = (2_000_000 - 1_050_000) / 1_343_503 ≈ 0.707 < 1.0 → 不過!

    為了讓大者過 z>=1.0,需要更大差距。讓我用 sum=5_000_000 vs sum=100_000:
    mu = 2_550_000, sigma ≈ 3_464_823(ddof=1)
    大者 z = (5M - 2.55M) / 3.46M ≈ 0.707 → 還是 ~0.707(2 檔池 z 上限 ≈ 0.707)

    結論:2 檔池的 z-score 數學上限是 sqrt((n-1)/n) ≈ 0.707。所以不可能
    用 2 檔池測 z>=1.0 過濾(會兩個都被擋)。改用 3 檔池:
    sums = [10M, 1M, 1M] → mu=4M, sigma(ddof=1)=sqrt((36+9+9)*1e12/2)=5.196M
    大者 z = (10M - 4M) / 5.196M ≈ 1.155 ≥ 1.0 → 過
    小者 z = (1M - 4M) / 5.196M ≈ -0.577 < 1.0 → 不過
    """
    sid_big, sid_small_a, sid_small_b = "1208", "1209", "1210"
    _seed_stocks([sid_big, sid_small_a, sid_small_b])
    for sid in (sid_big, sid_small_a, sid_small_b):
        _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    days = _last_5_weekdays("2026-05-15")
    _seed_foreign(sid_big, [(d, 2_000_000) for d in days])  # 5d sum = 10M
    _seed_foreign(sid_small_a, [(d, 200_000) for d in days])  # 5d sum = 1M
    _seed_foreign(sid_small_b, [(d, 200_000) for d in days])  # 5d sum = 1M
    weeks = _last_5_weeks("2026-05-15")
    deltas = [(weeks[i], 10) for i in range(4)] + [(weeks[4], 50)]
    for sid in (sid_big, sid_small_a, sid_small_b):
        _seed_shareholder(sid, deltas)

    df = strat.screen_foreign_x_holder_alignment(
        "2026-05-15", stock_ids=[sid_big, sid_small_a, sid_small_b]
    )
    # 大者過 z>=1.0,小者兩個都不過
    assert len(df) == 1
    assert df.iloc[0]["stock_id"] == sid_big
    assert df.iloc[0]["foreign_zscore"] >= 1.0


# === case 9:score clip 上限 ===

def test_fx_holder_score_clip_upper(tmp_db):
    """強信號 → score 應 clip 到 score_clip_max=2.0。

    手動傳 score_clip_max=1.5,讓本應 1.875 的 score 被 clip 到 1.5。
    """
    sid = "1211"
    _seed_stocks([sid])
    _seed_daily_prices(sid, "2026-01-01", "2026-05-15")
    days = _last_5_weekdays("2026-05-15")
    _seed_foreign(sid, [(d, 500_000) for d in days])
    weeks = _last_5_weeks("2026-05-15")
    # 前 4 週 delta=[10,10,10,10],本週 50:sigma=0 → holder_z = 0.5(sigma_thr fallback)
    # 單檔池 → foreign_z = 1.0
    # raw score = 0.5 + 0.25 = 0.75。不會超過 clip。改用顯式高 holder_z:
    # 改前期 [5,5,5,5],本週 50 → sigma=0,但 delta>5+0.5*0=5 ✓
    # 還是 holder_z = 0.5(sigma=0 fallback)
    # → raw=0.75。OK 改用 sigma>0 的歷史:
    # 前 4 週 [5,10,15,20],本週 200。mu=12.5, sigma≈6.45。threshold=12.5+0.5*6.45≈15.73
    # holder_z = (200-12.5)/6.45 ≈ 29.06
    # raw score = 1.0*0.5 + 29.06*0.5 ≈ 15.03 → clip score_clip_max
    deltas = [
        (weeks[0], 5), (weeks[1], 10), (weeks[2], 15), (weeks[3], 20),
        (weeks[4], 200),
    ]
    _seed_shareholder(sid, deltas)

    df = strat.screen_foreign_x_holder_alignment(
        "2026-05-15", stock_ids=[sid],
        params={"score_clip_max": 1.5},
    )
    assert len(df) == 1
    assert df.iloc[0]["score"] == pytest.approx(1.5)  # clip 到設定的 max


# === case 10:registry wired ===

def test_fx_holder_in_registry():
    key = "foreign_x_holder_alignment"
    assert key in strat.ALL_STRATEGIES
    assert strat.STRATEGY_LABELS[key] == "外資千張共振"
    assert strat.STRATEGY_CATEGORY[key] == "籌碼"
    target, stop, hold = strat.STRATEGY_RR_PARAMS[key]
    assert target == 0.04 and stop == 0.03
    assert hold == 10  # spec:hold 10
    assert consensus.STRATEGY_NATURE[key] == "neutral"
