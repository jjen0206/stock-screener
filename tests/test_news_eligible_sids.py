"""src/news_fetcher.py 6 類個股 eligible_sids 過濾 + tag 顯示測試。

主公拍板(2026-05-08):重訊只推 6 類聯集
  ⭐ 關注 / 📋 短線 picks / 💎 長線 picks /
  🚀 漲停 / 💥 跌停反轉 / 🔥 熱門
sid 不在任一個 set → filter 掉,在多個 set → 多個 tag(固定優先級順序)。
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from src import config, database as db
from src import news_fetcher as nf
from src import notifier


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "elig.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    yield tmp_path
    db._reset_path_cache()


# === compute_news_tags(unit-level)===

def test_compute_news_tags_only_watchlist():
    groups = {
        "watchlist": {"2330"},
        "short_picks": set(),
        "long_picks": set(),
        "limit_up": set(),
        "limit_down_after_up": set(),
        "hot": set(),
    }
    assert nf.compute_news_tags("2330", groups) == ["⭐ 關注"]


def test_compute_news_tags_three_groups_in_priority_order():
    """sid 同時在 watchlist + short_picks + limit_up → 3 tag 按固定優先級。"""
    groups = {
        "watchlist": {"2330"},
        "short_picks": {"2330"},
        "long_picks": set(),
        "limit_up": {"2330"},
        "limit_down_after_up": set(),
        "hot": set(),
    }
    tags = nf.compute_news_tags("2330", groups)
    assert tags == ["⭐ 關注", "📋 短線", "🚀 漲停"], (
        f"tag 應按 watchlist → 短線 → 漲停 順序,實際 {tags}"
    )


def test_compute_news_tags_all_six_groups():
    """sid 命中全部 6 類 → 6 tag 完整順序。"""
    sid = "9999"
    groups = {k: {sid} for k in (
        "watchlist", "short_picks", "long_picks",
        "limit_up", "limit_down_after_up", "hot",
    )}
    tags = nf.compute_news_tags(sid, groups)
    assert tags == [
        "⭐ 關注", "📋 短線", "💎 長線",
        "🚀 漲停", "💥 跌停反轉", "🔥 熱門",
    ]


def test_compute_news_tags_not_in_any_returns_empty():
    groups = {k: set() for k in (
        "watchlist", "short_picks", "long_picks",
        "limit_up", "limit_down_after_up", "hot",
    )}
    assert nf.compute_news_tags("2330", groups) == []


def test_compute_news_tags_empty_inputs():
    assert nf.compute_news_tags("", {}) == []
    assert nf.compute_news_tags("2330", {}) == []
    assert nf.compute_news_tags(None, {"watchlist": {"2330"}}) == []


# === get_eligible_news_sids 整合(各 fetch 走真實 SQL)===

def test_eligible_sids_union_of_six_sources(tmp_db, monkeypatch):
    """6 類 fetch 各回不同 sid,聯集應含全部不重複。"""
    # mock 6 個底層 fetcher 直接回 set,不依賴真實 SQL
    monkeypatch.setattr(
        nf, "get_watchlist_sids", lambda db_path=None: {"2330"},
    )
    monkeypatch.setattr(
        nf, "get_short_picks_sids",
        lambda trade_date=None, db_path=None: {"2317"},
    )
    monkeypatch.setattr(
        nf, "get_long_picks_sids", lambda db_path=None: {"1101"},
    )

    # limit_movers 走 DataFrame import,各自 mock
    df_up = pd.DataFrame([{"編號": "2454", "名稱": "聯發科"}])
    df_down = pd.DataFrame([{"編號": "5678", "名稱": "X"}])
    df_hot = pd.DataFrame([{"編號": "8888", "名稱": "Y"}])
    with (
        patch("src.limit_movers.get_limit_up", return_value=df_up),
        patch("src.limit_movers.get_limit_down_after_up", return_value=df_down),
        patch("src.limit_movers.get_hot_stocks", return_value=df_hot),
    ):
        groups = nf.get_eligible_news_sids()

    assert groups["watchlist"] == {"2330"}
    assert groups["short_picks"] == {"2317"}
    assert groups["long_picks"] == {"1101"}
    assert groups["limit_up"] == {"2454"}
    assert groups["limit_down_after_up"] == {"5678"}
    assert groups["hot"] == {"8888"}
    assert groups["all"] == {
        "2330", "2317", "1101", "2454", "5678", "8888",
    }


def test_eligible_sids_all_empty_when_db_empty(tmp_db):
    """剛建 DB 沒任何資料 → 6 類 set 全空 + all 空 set。"""
    groups = nf.get_eligible_news_sids()
    for key in (
        "watchlist", "short_picks", "long_picks",
        "limit_up", "limit_down_after_up", "hot", "all",
    ):
        assert groups[key] == set(), f"{key} 應為空 set,實際 {groups[key]}"


# === list_unsent_important_news filter 整合 ===

def _seed_news(sid: str, article: str, time_hms: str = "100000") -> None:
    """灌一筆未推 news。"""
    rows = nf.normalize_twse_news([
        {
            "公司代號": sid, "公司名稱": f"S{sid}",
            "發言日期": "1150508", "發言時間": time_hms,
            "主旨 ": f"news for {sid}", "符合條款": article,
        },
    ])
    nf.upsert_news(rows)


def test_list_unsent_filters_out_news_not_in_eligible(tmp_db, monkeypatch):
    """sid 不在 6 類聯集任一個 → 整筆 filter 掉(不出現在 unsent 結果)。"""
    _seed_news("2330", "第10款")  # in watchlist 內
    _seed_news("9999", "第10款")  # 不在任何 set → 應被 filter

    monkeypatch.setattr(
        nf, "get_watchlist_sids", lambda db_path=None: {"2330"},
    )
    # 其他 5 類全空
    monkeypatch.setattr(
        nf, "get_short_picks_sids",
        lambda trade_date=None, db_path=None: set(),
    )
    monkeypatch.setattr(
        nf, "get_long_picks_sids", lambda db_path=None: set(),
    )
    with (
        patch("src.limit_movers.get_limit_up", return_value=pd.DataFrame()),
        patch("src.limit_movers.get_limit_down_after_up",
              return_value=pd.DataFrame()),
        patch("src.limit_movers.get_hot_stocks", return_value=pd.DataFrame()),
    ):
        unsent = nf.list_unsent_important_news(channel="telegram")
    sids = [r["sid"] for r in unsent]
    assert sids == ["2330"], (
        f"應只剩 2330(watchlist),9999 應被 filter 掉,實際 {sids}"
    )


def test_list_unsent_attaches_tags(tmp_db, monkeypatch):
    """通過 filter 的 news 應 enrich tags 欄(供 format_news_block 顯示)。"""
    _seed_news("2330", "第10款")

    monkeypatch.setattr(
        nf, "get_watchlist_sids", lambda db_path=None: {"2330"},
    )
    monkeypatch.setattr(
        nf, "get_short_picks_sids",
        lambda trade_date=None, db_path=None: {"2330"},
    )
    monkeypatch.setattr(
        nf, "get_long_picks_sids", lambda db_path=None: set(),
    )
    df_up = pd.DataFrame([{"編號": "2330", "名稱": "X"}])
    with (
        patch("src.limit_movers.get_limit_up", return_value=df_up),
        patch("src.limit_movers.get_limit_down_after_up",
              return_value=pd.DataFrame()),
        patch("src.limit_movers.get_hot_stocks", return_value=pd.DataFrame()),
    ):
        unsent = nf.list_unsent_important_news(channel="telegram")
    assert len(unsent) == 1
    assert unsent[0]["tags"] == ["⭐ 關注", "📋 短線", "🚀 漲停"]


def test_list_unsent_all_filtered_when_eligible_empty(tmp_db, monkeypatch):
    """6 類都空 → 所有 news 都被 filter 掉(系統剛啟動的 edge case)。"""
    _seed_news("2330", "第10款")
    _seed_news("2317", "第30款")

    # 全部 6 類都空
    monkeypatch.setattr(
        nf, "get_watchlist_sids", lambda db_path=None: set(),
    )
    monkeypatch.setattr(
        nf, "get_short_picks_sids",
        lambda trade_date=None, db_path=None: set(),
    )
    monkeypatch.setattr(
        nf, "get_long_picks_sids", lambda db_path=None: set(),
    )
    with (
        patch("src.limit_movers.get_limit_up", return_value=pd.DataFrame()),
        patch("src.limit_movers.get_limit_down_after_up",
              return_value=pd.DataFrame()),
        patch("src.limit_movers.get_hot_stocks", return_value=pd.DataFrame()),
    ):
        unsent = nf.list_unsent_important_news(channel="telegram")
    assert unsent == []


# === format_news_block 顯示 tags ===

def test_format_news_block_renders_tags_header():
    """tags 欄有值時,header 應包含 [⭐ 關注 · 📋 短線] 格式。"""
    news = {
        "sid": "2330", "company_name": "台積電",
        "publish_date": "2026-05-08", "publish_time": "143015",
        "subject": "重大訊息", "article_no": "第10款",
        "tags": ["⭐ 關注", "📋 短線", "🚀 漲停"],
    }
    block = notifier.format_news_block(news, channel="telegram")
    # 第一行應含 sid + tags
    first_line = block.splitlines()[0]
    assert "台積電 (2330)" in first_line
    assert "[⭐ 關注 · 📋 短線 · 🚀 漲停]" in first_line, (
        f"header 應含 tag list,實際:\n{first_line}"
    )
    assert "14:30" in first_line


def test_format_news_block_no_tags_renders_normally():
    """沒 tags 欄(舊 caller / filter 不出 tag)→ 不影響原有排版。"""
    news = {
        "sid": "2330", "company_name": "台積電",
        "publish_date": "2026-05-08", "publish_time": "143015",
        "subject": "重大訊息", "article_no": "第10款",
        # 無 tags
    }
    block = notifier.format_news_block(news, channel="telegram")
    first_line = block.splitlines()[0]
    assert "台積電 (2330)" in first_line
    assert "[" not in first_line, "無 tags 不該有 [..] 區塊"


# === get_short_picks_sids vs get_today_picks_sids 差異 ===

def test_get_latest_picks_sids_returns_max_trade_date(tmp_db):
    """DB 同時有 5/06 + 5/07 picks,get_latest_picks_sids 應只回 5/07
    (MAX(trade_date)),不管系統時鐘是哪天。
    """
    with db.get_conn() as conn:
        # 5/06 picks: A 命中 2 策略
        conn.execute(
            "INSERT INTO daily_picks (trade_date, universe, strategy, sid, "
            "params_hash, computed_at) "
            "VALUES ('2026-05-06', 'pure_stock', 'macd_golden', 'OLD_A', "
            "'default_v1', '2026-05-06')"
        )
        conn.execute(
            "INSERT INTO daily_picks (trade_date, universe, strategy, sid, "
            "params_hash, computed_at) "
            "VALUES ('2026-05-06', 'pure_stock', 'volume_kd', 'OLD_A', "
            "'default_v1', '2026-05-06')"
        )
        # 5/07 picks: B 命中 2 策略
        conn.execute(
            "INSERT INTO daily_picks (trade_date, universe, strategy, sid, "
            "params_hash, computed_at) "
            "VALUES ('2026-05-07', 'pure_stock', 'macd_golden', 'NEW_B', "
            "'default_v1', '2026-05-07')"
        )
        conn.execute(
            "INSERT INTO daily_picks (trade_date, universe, strategy, sid, "
            "params_hash, computed_at) "
            "VALUES ('2026-05-07', 'pure_stock', 'volume_kd', 'NEW_B', "
            "'default_v1', '2026-05-07')"
        )

    sids = nf.get_latest_picks_sids()
    assert sids == {"NEW_B"}, (
        f"應只回 5/07 那批 NEW_B(MAX trade_date),實際 {sids}"
    )


def test_get_today_picks_sids_alias_uses_latest(tmp_db):
    """主公拍板 (2026-05-08): get_today_picks_sids 改成 get_latest_picks_sids
    的 alias,行為對齊 latest(舊 caller 自動受惠,不用改 source)。
    """
    # 系統時鐘是 2026-05-08 (今天),DB 只有 5/07 picks
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_picks (trade_date, universe, strategy, sid, "
            "params_hash, computed_at) "
            "VALUES ('2026-05-07', 'pure_stock', 'macd_golden', 'X', "
            "'default_v1', '2026-05-07')"
        )
        conn.execute(
            "INSERT INTO daily_picks (trade_date, universe, strategy, sid, "
            "params_hash, computed_at) "
            "VALUES ('2026-05-07', 'pure_stock', 'volume_kd', 'X', "
            "'default_v1', '2026-05-07')"
        )

    # 舊 alias 應拿到 5/07 X(舊邏輯綁 today=5/08 會回空)
    today_sids = nf.get_today_picks_sids()
    assert today_sids == {"X"}, (
        f"alias 應回 latest=5/07 的 X,實際 {today_sids}"
    )


def test_eligible_sids_uses_latest_picks_when_today_empty(tmp_db, monkeypatch):
    """today 沒 picks 但昨日有 → eligible 仍含昨日 picks(latest 語意)。"""
    # DB 只有昨日 (5/07) picks,沒 today
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_picks (trade_date, universe, strategy, sid, "
            "params_hash, computed_at) "
            "VALUES ('2026-05-07', 'pure_stock', 'macd_golden', 'YESTERDAY', "
            "'default_v1', '2026-05-07')"
        )

    # 其他 5 類全空(專注驗 picks fallback 到 latest)
    monkeypatch.setattr(
        nf, "get_watchlist_sids", lambda db_path=None: set(),
    )
    monkeypatch.setattr(
        nf, "get_long_picks_sids", lambda db_path=None: set(),
    )
    with (
        patch("src.limit_movers.get_limit_up", return_value=pd.DataFrame()),
        patch("src.limit_movers.get_limit_down_after_up",
              return_value=pd.DataFrame()),
        patch("src.limit_movers.get_hot_stocks", return_value=pd.DataFrame()),
    ):
        groups = nf.get_eligible_news_sids()

    assert groups["short_picks"] == {"YESTERDAY"}, (
        f"short_picks 應 fallback 到 latest=5/07 撈到 YESTERDAY,"
        f"實際 {groups['short_picks']}"
    )
    assert "YESTERDAY" in groups["all"]


def test_get_short_picks_threshold_1_includes_all(tmp_db):
    """get_short_picks_sids(min_strategies=1)應比 get_today_picks_sids
    (min_strategies=2) 更寬鬆 — 命中 1 策略即合格。
    """
    from datetime import date as _date
    today = _date.today().isoformat()
    with db.get_conn() as conn:
        # sid A 命中 2 策略,sid B 命中 1 策略
        conn.execute(
            "INSERT INTO daily_picks "
            "(trade_date, universe, strategy, sid, params_hash, computed_at) "
            "VALUES (?, 'pure_stock', 'macd_golden', 'A', 'default_v1', '2026-05-08')",
            (today,),
        )
        conn.execute(
            "INSERT INTO daily_picks "
            "(trade_date, universe, strategy, sid, params_hash, computed_at) "
            "VALUES (?, 'pure_stock', 'volume_kd', 'A', 'default_v1', '2026-05-08')",
            (today,),
        )
        conn.execute(
            "INSERT INTO daily_picks "
            "(trade_date, universe, strategy, sid, params_hash, computed_at) "
            "VALUES (?, 'pure_stock', 'macd_golden', 'B', 'default_v1', '2026-05-08')",
            (today,),
        )

    short_sids = nf.get_short_picks_sids()
    today_sids = nf.get_today_picks_sids()  # min_strategies=2 預設
    assert "A" in short_sids and "B" in short_sids, (
        f"短線 picks (≥1 策略) 應含 A B,實際 {short_sids}"
    )
    assert "A" in today_sids and "B" not in today_sids, (
        f"嚴格 picks (≥2 策略) 只含 A,實際 {today_sids}"
    )
