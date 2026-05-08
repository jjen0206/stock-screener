"""src/analyst_target_alerts.py 三項主公拍板功能單元測試。

(1) 異動推播:|Δ%| ≥ 5% AND eligible AND 同日同方向防重複
(2) 觸目標價推播:close ≥ target × 100% AND eligible AND 7 日冷卻
(3) picks 推播 Δ 標示:format_pick_block 加 prev → now 變動箭頭
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from src import (
    analyst_target_alerts as ata,
    analyst_targets as at,
    config,
    database as db,
    notifier,
    strategies,
)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "alerts.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    yield tmp_path
    db._reset_path_cache()


def _seed_close(sid: str, date_str: str, close: float) -> None:
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_prices
              (stock_id, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, 1000)
            """,
            (sid, date_str, close, close, close, close),
        )


def _seed_target(
    sid: str, target_mean: float, num_analysts: int = 10,
    source: str = "yfinance",
) -> None:
    """直接 INSERT 一筆 analyst_targets(不走 upsert,避免測 upsert 副作用)。"""
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO analyst_targets
              (stock_id, target_mean, num_analysts, source, fetched_at)
            VALUES (?, ?, ?, ?, '2026-05-07T00:00:00+00:00')
            """,
            (sid, target_mean, num_analysts, source),
        )


def _make_eligible_groups(*, all_sids: set[str], watchlist: set[str] = None):
    """測試用:組 minimal eligible_groups dict。"""
    watchlist = watchlist or set()
    return {
        "watchlist": watchlist, "short_picks": set(), "long_picks": set(),
        "limit_up": set(), "limit_down_after_up": set(), "hot": set(),
        "all": set(all_sids),
    }


# === Item 1: 異動推播 ===

def test_should_alert_change_above_5pct():
    """|Δ%| ≥ 5% → 觸發。"""
    eligible, direction, delta = ata.should_alert_change("2330", 1000.0, 1060.0)
    assert eligible is True
    assert direction == "up"
    assert delta == pytest.approx(0.06, abs=0.001)


def test_should_alert_change_below_5pct_skipped():
    """|Δ%| < 5% → skip。"""
    eligible, _, delta = ata.should_alert_change("2330", 1000.0, 1040.0)
    assert eligible is False
    assert delta == pytest.approx(0.04, abs=0.001)


def test_should_alert_change_down_direction():
    """新值低於舊值 → direction='down'。"""
    eligible, direction, delta = ata.should_alert_change("2330", 1000.0, 940.0)
    assert eligible is True
    assert direction == "down"
    assert delta < 0


def test_should_alert_change_no_old_value():
    """old=None(沒紀錄)→ 不觸發(沒比較基準)。"""
    eligible, _, _ = ata.should_alert_change("2330", None, 1000.0)
    assert eligible is False


def test_notify_target_changes_filters_non_eligible(tmp_db):
    """sid 不在 6 類聯集 → filter 掉(不推)。"""
    eligible_groups = _make_eligible_groups(all_sids={"2330"})  # 9999 不在
    with patch(
        "src.news_fetcher.get_eligible_news_sids",
        return_value=eligible_groups,
    ):
        changes = [
            {"sid": "2330", "old_target_mean": 1000.0,
             "new_target_mean": 1100.0, "num_analysts": 5},
            {"sid": "9999", "old_target_mean": 100.0,
             "new_target_mean": 110.0, "num_analysts": 1},
        ]
        # mock send fns 強制 token 視為缺(不真送),只驗 filter logic
        with patch.object(config, "TELEGRAM_BOT_TOKEN", ""), \
             patch.object(config, "DISCORD_WEBHOOK_URL", ""):
            result = ata.notify_target_changes(changes)
    # 只有 2330 通過 filter(9999 不在 eligible_all)
    assert result["n_eligible"] == 1


def test_notify_target_changes_dedup_same_day_direction(tmp_db):
    """同 sid 同日同方向已推過 → 第二次 skip。"""
    today = ata._today_iso()
    ata.record_change_alert(
        sid="2330", alert_date=today, direction="up",
        old_target=1000.0, new_target=1060.0,
        sent_telegram=True, sent_discord=False,
    )
    eligible_groups = _make_eligible_groups(all_sids={"2330"})
    with patch(
        "src.news_fetcher.get_eligible_news_sids",
        return_value=eligible_groups,
    ):
        changes = [
            {"sid": "2330", "old_target_mean": 1000.0,
             "new_target_mean": 1080.0, "num_analysts": 5},
        ]
        with patch.object(config, "TELEGRAM_BOT_TOKEN", ""), \
             patch.object(config, "DISCORD_WEBHOOK_URL", ""):
            result = ata.notify_target_changes(changes)
    # 已推過 → eligible 0
    assert result["n_eligible"] == 0


def test_format_target_change_block_up():
    change = {
        "sid": "2330", "name": "台積電",
        "old": 1180.0, "new": 1240.0,
        "direction": "up", "delta_pct": 0.0508,
        "num_analysts": 28, "tags": ["⭐ 關注"],
        "close": 1150.0,
    }
    block = ata.format_target_change_block(change, channel="telegram")
    assert "📈" in block
    assert "台積電 (2330)" in block
    assert "[⭐ 關注]" in block
    assert "法人調升目標" in block
    assert "1180" in block and "1240" in block
    assert "+5.1%" in block
    assert "券商 28 家" in block
    assert "現價 1150" in block


def test_format_target_change_block_down():
    change = {
        "sid": "2330", "name": "台積電",
        "old": 1180.0, "new": 1100.0,
        "direction": "down", "delta_pct": -0.0678,
        "num_analysts": 28, "tags": [],
        "close": 1150.0,
    }
    block = ata.format_target_change_block(change, channel="telegram")
    assert "📉" in block
    assert "法人調降目標" in block
    assert "-6.8%" in block


# === Item 2: 觸目標價 ===

def test_find_hit_candidates_at_target(tmp_db):
    """close = target × 100% → 入候選。"""
    _seed_close("HIT", "2026-05-07", 100.0)
    _seed_target("HIT", 100.0)
    candidates = ata.find_hit_candidates()
    sids = {c["sid"] for c in candidates}
    assert "HIT" in sids


def test_find_hit_candidates_below_target_excluded(tmp_db):
    """close = 99 < target=100 → 不入。"""
    _seed_close("LOW", "2026-05-07", 99.0)
    _seed_target("LOW", 100.0)
    candidates = ata.find_hit_candidates()
    sids = {c["sid"] for c in candidates}
    assert "LOW" not in sids


def test_was_hit_within_cooldown(tmp_db):
    """7 日內推過 → True;9 日前推過 → False。"""
    from datetime import date as _date, timedelta
    today = _date.today().isoformat()
    nine_days_ago = (_date.today() - timedelta(days=9)).isoformat()

    ata.record_hit(
        sid="A", hit_date=today, close=100.0, target_consensus=100.0,
        sent_telegram=True, sent_discord=False,
    )
    ata.record_hit(
        sid="B", hit_date=nine_days_ago, close=100.0, target_consensus=100.0,
        sent_telegram=True, sent_discord=False,
    )

    assert ata.was_hit_within_cooldown("A") is True, (
        "A 今天剛推 → 應在 7 日冷卻內"
    )
    assert ata.was_hit_within_cooldown("B") is False, (
        "B 9 日前推 → 應已過冷卻"
    )
    assert ata.was_hit_within_cooldown("C") is False, "C 從未推過"


def test_notify_target_hits_filters_eligible(tmp_db):
    """6 類聯集 filter:sid in eligible 才推。"""
    _seed_close("HIT_W", "2026-05-07", 105.0)
    _seed_target("HIT_W", 100.0)
    _seed_close("HIT_NONE", "2026-05-07", 105.0)
    _seed_target("HIT_NONE", 100.0)
    # HIT_W 在 watchlist,HIT_NONE 不在任一類
    eligible_groups = _make_eligible_groups(
        all_sids={"HIT_W"}, watchlist={"HIT_W"},
    )
    with patch(
        "src.news_fetcher.get_eligible_news_sids",
        return_value=eligible_groups,
    ):
        with patch.object(config, "TELEGRAM_BOT_TOKEN", ""), \
             patch.object(config, "DISCORD_WEBHOOK_URL", ""):
            result = ata.notify_target_hits()
    assert result["n_candidates"] == 2  # 兩檔 close ≥ target
    assert result["n_eligible"] == 1    # 只 HIT_W 在 eligible


def test_format_target_hit_block():
    hit = {
        "sid": "2330", "name": "台積電",
        "close": 1245.0, "target_consensus": 1240.0,
        "num_analysts": 28, "fetched_at": "2026-05-07T03:55:07+00:00",
        "tags": ["⭐ 關注"],
    }
    block = ata.format_target_hit_block(hit, channel="telegram")
    assert "🎯" in block
    assert "台積電 (2330)" in block
    assert "[⭐ 關注]" in block
    assert "觸法人共識目標" in block
    assert "現價 1245" in block
    assert "共識目標 1240" in block
    assert "+0.4%" in block  # (1245-1240)/1240 = 0.40%
    assert "券商 28 家" in block
    assert "2026-05-07" in block


# === Item 3: picks 推播 Δ 標示 ===

def test_format_pick_block_includes_delta_when_change_above_1pct():
    """analyst_target_prev_mean 跟 mean 變動 ≥ 1% → 顯「(↑ +5.1%)」。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 1150.0, "matched_strategies": ["x"], "matched_labels": ["X"],
        "ml_prob": 0.7,
        "analyst_target_mean": 1240.0,
        "analyst_target_prev_mean": 1180.0,  # +5.08%
        "analyst_num": 28,
    }
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "📊 共識目標" in block
    assert "1240" in block
    # delta (1240-1180)/1180 = 5.08% → "↑ +5.1%"
    assert "↑" in block
    assert "+5.1%" in block


def test_format_pick_block_skips_delta_when_change_below_1pct():
    """變動 < 1% → 不顯 Δ(避免雜訊)。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 1150.0, "matched_strategies": ["x"], "matched_labels": ["X"],
        "analyst_target_mean": 1240.0,
        "analyst_target_prev_mean": 1235.0,  # +0.4%,小於 1%
        "analyst_num": 28,
    }
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "1240" in block
    # 不該有箭頭 / Δ% — 找看看「↑」「↓」沒出現
    assert "↑" not in block, f"Δ < 1% 不該顯,實際:\n{block}"
    assert "↓" not in block


def test_format_pick_block_no_delta_when_no_prev():
    """沒 previous_target_mean → 不顯 Δ。"""
    pick = {
        "rank": 1, "sid": "2330", "name": "台積電",
        "close": 1150.0, "matched_strategies": ["x"], "matched_labels": ["X"],
        "analyst_target_mean": 1240.0,
        # 沒 analyst_target_prev_mean
        "analyst_num": 28,
    }
    block = notifier.format_pick_block(pick, channel="telegram")
    assert "1240" in block
    assert "↑" not in block and "↓" not in block


def test_enrich_with_analyst_target_includes_prev_mean(tmp_db):
    """strategies.enrich_with_analyst_target 應加 analyst_target_prev_mean 欄。"""
    # upsert 一次 = 沒 prev,upsert 第二次 = 有 prev
    at.upsert_analyst_target("2330", {
        "target_mean": 1180.0, "num_analysts": 28, "source": "yfinance",
    })
    at.upsert_analyst_target("2330", {
        "target_mean": 1240.0, "num_analysts": 28, "source": "yfinance",
    })
    df = pd.DataFrame([{"stock_id": "2330", "name": "台積電"}])
    out = strategies.enrich_with_analyst_target(df)
    assert "analyst_target_prev_mean" in out.columns
    row = out.iloc[0]
    assert row["analyst_target_mean"] == pytest.approx(1240.0)
    assert row["analyst_target_prev_mean"] == pytest.approx(1180.0)


# === upsert_analyst_target 跟 previous_target_mean ===

def test_upsert_analyst_target_returns_change_info(tmp_db):
    """upsert 第一次 → old=None;第二次 → old=第一次的 mean。"""
    info1 = at.upsert_analyst_target("2330", {
        "target_mean": 1000.0, "num_analysts": 10, "source": "yfinance",
    })
    assert info1 is not None
    assert info1["old_target_mean"] is None
    assert info1["new_target_mean"] == pytest.approx(1000.0)

    info2 = at.upsert_analyst_target("2330", {
        "target_mean": 1100.0, "num_analysts": 12, "source": "yfinance",
    })
    assert info2 is not None
    assert info2["old_target_mean"] == pytest.approx(1000.0)
    assert info2["new_target_mean"] == pytest.approx(1100.0)


def test_upsert_writes_previous_target_mean_column(tmp_db):
    """previous_target_mean 欄應在第二次 upsert 後等於第一次的值。"""
    at.upsert_analyst_target("2330", {
        "target_mean": 1000.0, "num_analysts": 10, "source": "yfinance",
    })
    at.upsert_analyst_target("2330", {
        "target_mean": 1100.0, "num_analysts": 10, "source": "yfinance",
    })
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT target_mean, previous_target_mean FROM analyst_targets "
            "WHERE stock_id=? AND source='yfinance'",
            ("2330",),
        ).fetchone()
    assert row["target_mean"] == pytest.approx(1100.0)
    assert row["previous_target_mean"] == pytest.approx(1000.0)
