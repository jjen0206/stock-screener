"""scripts/morning_brief.py 測試 — 盤前快訊功能。

涵蓋:
  - 推播格式正確(警示 / news / diff / sentiment 區段)
  - 「無變動」情境 → 推極簡訊
  - 警示比對正確(昨晚 picks sid 在 stock_warnings 表)
  - news 關鍵字 filter(IMPORTANT_NEWS_KEYWORDS)
  - kill-switch MORNING_BRIEF_ENABLED=false → exit 0 不推
  - schema 對齊 production(db.init_db())
  - diff_picks: added / removed / reranked
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts import morning_brief as mb
from src import config, database as db


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """獨立 SQLite + schema init,跟 production 對齊。"""
    db_file = tmp_path / "mb_test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    monkeypatch.setenv("MORNING_BRIEF_ENABLED", "true")
    monkeypatch.setenv("WARNING_ANNOTATE_ENABLED", "true")
    yield db_file
    db._reset_path_cache()  # type: ignore[attr-defined]


# ============================================================================
# Kill-switch
# ============================================================================

def test_killswitch_disabled_skips_run(monkeypatch, capsys):
    """MORNING_BRIEF_ENABLED=false → main() exit 0 不推。"""
    monkeypatch.setenv("MORNING_BRIEF_ENABLED", "false")
    rc = mb.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "kill-switch" in out or "不推播" in out


def test_killswitch_enabled_default_true(monkeypatch):
    """環境變數未設 → 預設 on(_is_enabled 回 True)。"""
    monkeypatch.delenv("MORNING_BRIEF_ENABLED", raising=False)
    assert mb._is_enabled() is True


def test_killswitch_various_off_values(monkeypatch):
    for val in ("false", "0", "no", "off", "FALSE", "False", ""):
        monkeypatch.setenv("MORNING_BRIEF_ENABLED", val)
        assert mb._is_enabled() is False, f"value {val!r} 應視為 off"


# ============================================================================
# news 關鍵字 filter
# ============================================================================

def test_is_important_news_subject_hits_keywords():
    assert mb.is_important_news_subject("公司違約交割說明") is True
    assert mb.is_important_news_subject("董事會通過下修財測") is True
    assert mb.is_important_news_subject("產品召回公告") is True
    assert mb.is_important_news_subject("子公司重大訊息更正") is True
    assert mb.is_important_news_subject("裁員 200 人") is True


def test_is_important_news_subject_misses_normal():
    assert mb.is_important_news_subject("公告股東會召開日期") is False
    assert mb.is_important_news_subject("公告本公司新任董事長") is False
    assert mb.is_important_news_subject(None) is False
    assert mb.is_important_news_subject("") is False


def test_find_recent_picks_news_filters_by_sid_and_time(tmp_db):
    """news 表內 sid 對得上 + fetched_at < 12h + subject 含關鍵字 → 入選。"""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old_iso = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).isoformat(timespec="seconds")

    with db.get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO news (sid, company_name, publish_date, publish_time,
                              subject, article_no, url_hash, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                # 命中:sid 對 + 近 12h + 關鍵字
                ("2330", "台積電", "2026-05-17", "031500",
                 "美國設廠進度違約延後", "第14款", "hash_hit", now_iso),
                # 不命中:sid 不在 picks
                ("9999", "其他", "2026-05-17", "031500",
                 "公司違約交割", "第14款", "hash_other_sid", now_iso),
                # 不命中:24h 太舊
                ("2330", "台積電", "2026-05-16", "031500",
                 "重大事件", "第14款", "hash_too_old", old_iso),
                # 不命中:無關鍵字
                ("2330", "台積電", "2026-05-17", "031500",
                 "公告股東會",  "第8款", "hash_normal", now_iso),
            ],
        )

    out = mb.find_recent_picks_news(picks_sids=["2330"], hours=12)
    assert len(out) == 1
    assert out[0]["sid"] == "2330"
    assert "違約" in out[0]["subject"]


# ============================================================================
# 警示比對:昨晚 picks sid 是否命中 active warning
# ============================================================================

def test_find_newly_warned_picks_hits(tmp_db):
    """昨晚 picks 含 9999;9999 凌晨被列違約 → 應該抓到。"""
    db.upsert_stock_warnings([
        {
            "stock_id": "9999", "warning_type": "default_settlement",
            "announced_date": "2026-05-17", "effective_to": None,
            "reason": "違約交割",
        },
    ])
    yesterday_picks = [
        {"sid": "2330", "name": "台積電", "rank": 1},
        {"sid": "9999", "name": "違約檔", "rank": 2},
    ]
    out = mb.find_newly_warned_picks(yesterday_picks, as_of="2026-05-17")
    assert len(out) == 1
    assert out[0]["sid"] == "9999"
    assert "default_settlement" in out[0]["warning_types"]
    assert "違約交割" in out[0]["warning_labels"]


def test_find_newly_warned_picks_empty_when_no_warnings(tmp_db):
    yesterday_picks = [{"sid": "2330", "name": "台積電", "rank": 1}]
    out = mb.find_newly_warned_picks(yesterday_picks, as_of="2026-05-17")
    assert out == []


def test_find_newly_warned_picks_skips_expired(tmp_db):
    """effective_to 已過期 → 不該抓到。"""
    db.upsert_stock_warnings([
        {
            "stock_id": "9999", "warning_type": "attention",
            "announced_date": "2026-05-01", "effective_to": "2026-05-10",
        },
    ])
    yesterday_picks = [{"sid": "9999", "name": "已下警示", "rank": 1}]
    out = mb.find_newly_warned_picks(yesterday_picks, as_of="2026-05-17")
    assert out == []


# ============================================================================
# diff_picks: added / removed / reranked
# ============================================================================

def test_diff_picks_detects_added():
    y = [{"sid": "2330", "name": "台積電", "rank": 1}]
    t = [
        {"sid": "2330", "name": "台積電", "rank": 1},
        {"sid": "6223", "name": "旺矽", "rank": 2},
    ]
    d = mb.diff_picks(y, t)
    assert len(d["added"]) == 1
    assert d["added"][0]["sid"] == "6223"
    assert d["removed"] == []
    assert d["reranked"] == []


def test_diff_picks_detects_removed():
    y = [
        {"sid": "2330", "name": "台積電", "rank": 1},
        {"sid": "2317", "name": "鴻海", "rank": 2},
    ]
    t = [{"sid": "2330", "name": "台積電", "rank": 1}]
    d = mb.diff_picks(y, t)
    assert d["added"] == []
    assert len(d["removed"]) == 1
    assert d["removed"][0]["sid"] == "2317"


def test_diff_picks_detects_reranked():
    y = [
        {"sid": "2330", "name": "台積電", "rank": 1},
        {"sid": "2344", "name": "華邦電", "rank": 2},
    ]
    t = [
        {"sid": "2344", "name": "華邦電", "rank": 1},
        {"sid": "2330", "name": "台積電", "rank": 2},
    ]
    d = mb.diff_picks(y, t)
    assert d["added"] == []
    assert d["removed"] == []
    assert len(d["reranked"]) == 2
    by_sid = {r["sid"]: r for r in d["reranked"]}
    assert by_sid["2344"]["old_rank"] == 2
    assert by_sid["2344"]["new_rank"] == 1


def test_diff_picks_handles_empty_both_sides():
    d = mb.diff_picks([], [])
    assert d == {"added": [], "removed": [], "reranked": []}


# ============================================================================
# has_any_change 邏輯
# ============================================================================

def test_has_any_change_false_when_all_empty():
    sentiment = {"sentiment": "neutral"}
    diff = {"added": [], "removed": [], "reranked": []}
    assert mb.has_any_change([], [], diff, sentiment) is False


def test_has_any_change_true_on_warning():
    sentiment = {"sentiment": "neutral"}
    diff = {"added": [], "removed": [], "reranked": []}
    assert mb.has_any_change(
        [{"sid": "x"}], [], diff, sentiment,
    ) is True


def test_has_any_change_true_on_bearish():
    sentiment = {"sentiment": "bearish"}
    diff = {"added": [], "removed": [], "reranked": []}
    assert mb.has_any_change([], [], diff, sentiment) is True


def test_has_any_change_false_on_bullish():
    """美股小漲不該被當成「變動」吵主公早上。"""
    sentiment = {"sentiment": "bullish"}
    diff = {"added": [], "removed": [], "reranked": []}
    assert mb.has_any_change([], [], diff, sentiment) is False


# ============================================================================
# format_brief_message:無變動 + 完整 + telegram/discord
# ============================================================================

def test_format_no_change_telegram():
    msg = mb.format_brief_message(
        today_iso="2026-05-17",
        newly_warned=[], important_news=[],
        pick_diff={"added": [], "removed": [], "reranked": []},
        sentiment={"sentiment": "neutral", "indices": {}, "caption": ""},
        channel="telegram",
    )
    assert "盤前快訊" in msg
    assert "2026-05-17" in msg
    assert "✅" in msg
    assert "盤前無重大變動" in msg
    assert "9:30" in msg
    # 不該含警示 / news 區段
    assert "警示更新" not in msg
    assert "重大 news" not in msg


def test_format_no_change_discord():
    msg = mb.format_brief_message(
        today_iso="2026-05-17",
        newly_warned=[], important_news=[],
        pick_diff={"added": [], "removed": [], "reranked": []},
        sentiment={"sentiment": "neutral", "indices": {}, "caption": ""},
        channel="discord",
    )
    assert "**盤前快訊 2026-05-17**" in msg
    assert "盤前無重大變動" in msg


def test_format_full_message_telegram_includes_all_sections():
    msg = mb.format_brief_message(
        today_iso="2026-05-17",
        newly_warned=[{
            "sid": "9999", "name": "違約檔", "rank": 1,
            "warning_types": ["default_settlement"],
            "warning_labels": ["違約交割"],
        }],
        important_news=[{
            "sid": "2330", "company_name": "台積電",
            "publish_date": "2026-05-17", "publish_time": "031500",
            "subject": "美國設廠進度延後",
        }],
        pick_diff={
            "added": [{"sid": "6223", "name": "旺矽", "rank": 2, "ml_prob": 0.6}],
            "removed": [{"sid": "2317", "name": "鴻海"}],
            "reranked": [{
                "sid": "2344", "name": "華邦電",
                "old_rank": 2, "new_rank": 1,
            }],
        },
        sentiment={
            "indices": {"^DJI": {"name": "道瓊", "pct": -0.003}},
            "avg_pct": -0.003,
            "sentiment": "neutral",
            "caption": "🟡 中性,可正常進場但留意盤中量縮",
        },
        channel="telegram",
    )
    # header
    assert "🌅" in msg
    assert "盤前快訊 2026-05-17" in msg
    # 警示
    assert "警示更新" in msg
    assert "9999" in msg
    assert "違約交割" in msg
    # news
    assert "重大 news" in msg
    assert "2330" in msg
    assert "美國設廠" in msg
    # diff
    assert "推薦變動" in msg
    assert "新增" in msg
    assert "6223" in msg
    assert "移除" in msg
    assert "2317" in msg
    assert "排序" in msg
    assert "2344" in msg
    # sentiment
    assert "大盤情緒" in msg
    assert "道瓊" in msg
    # footer
    assert "9:30" in msg


def test_format_full_message_discord():
    msg = mb.format_brief_message(
        today_iso="2026-05-17",
        newly_warned=[{
            "sid": "9999", "name": "違約檔",
            "warning_labels": ["違約交割"],
        }],
        important_news=[],
        pick_diff={"added": [], "removed": [], "reranked": []},
        sentiment={"indices": {}, "sentiment": "bearish",
                   "caption": "🚨 大盤恐開低,軍師建議暫不追多"},
        channel="discord",
    )
    # Discord 用 ** bold
    assert "**盤前快訊 2026-05-17**" in msg
    assert "**警示更新" in msg
    assert "9999" in msg
    assert "🚨" in msg


# ============================================================================
# fetch_us_market_sentiment:yfinance 不可用 graceful
# ============================================================================

def test_fetch_us_market_sentiment_graceful_when_yfinance_missing(monkeypatch):
    """yfinance import 失敗 → 回 empty sentiment(unknown)。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "yfinance":
            raise ImportError("simulated missing yfinance")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out = mb.fetch_us_market_sentiment()
    assert out["sentiment"] == "unknown"
    assert out["indices"] == {}
    assert out["avg_pct"] is None
    assert "(美股資料不可用" in out["caption"]


def test_fetch_us_market_sentiment_classifies_bearish(monkeypatch):
    """三大指數平均跌 ≥ 1% → bearish + 🚨 caption。"""
    # 直接 stub fetch_us_market_sentiment 的 yfinance 路徑:
    # 自 mock yfinance.Ticker.history 回固定 DataFrame。
    import pandas as pd
    import sys

    class FakeTicker:
        def __init__(self, sym):
            self.sym = sym

        def history(self, period="5d"):
            # 上 100,今 98 → -2%(全部一致 → 平均 -2%)
            return pd.DataFrame({"Close": [100.0, 98.0]})

    fake_yf = type(sys)("yfinance")
    fake_yf.Ticker = FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    out = mb.fetch_us_market_sentiment()
    assert out["sentiment"] == "bearish"
    assert out["avg_pct"] < mb.SENTIMENT_BEARISH_THRESHOLD
    assert "🚨" in out["caption"]
    assert len(out["indices"]) == len(mb.US_INDEX_TICKERS)


# ============================================================================
# run_morning_brief 整合(輕量)— 不真送、跳 refetch、無昨晚 picks
# ============================================================================

def test_run_morning_brief_dry_run_no_picks(tmp_db, monkeypatch, capsys):
    """空 DB + skip_refetch=True + dry_run=True → 應跑完不炸,印極簡訊。"""
    # 確保 sentiment 不會因為網路出狀況拖時間
    monkeypatch.setattr(mb, "fetch_us_market_sentiment", lambda: {
        "indices": {}, "avg_pct": None, "sentiment": "neutral",
        "caption": "(stub)",
    })
    result = mb.run_morning_brief(
        target_date=None, dry_run=True,
        send_telegram=True, send_discord=True,
        skip_refetch=True,
    )
    assert result["has_change"] is False
    assert "盤前無重大變動" in result["tg_msg"]
    assert "盤前無重大變動" in result["dc_msg"]
    out = capsys.readouterr().out
    assert "Telegram" in out
    assert "Discord" in out


def test_run_morning_brief_does_not_call_send_when_no_secrets(
    tmp_db, monkeypatch,
):
    """無 TELEGRAM_BOT_TOKEN / DISCORD_WEBHOOK_URL → 不該 call send。"""
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(mb, "fetch_us_market_sentiment", lambda: {
        "indices": {}, "avg_pct": None, "sentiment": "neutral",
        "caption": "(stub)",
    })

    # spies — 若被 call 就 raise(這 test 是想驗證「沒 secrets 就不會 call」)
    def boom(*a, **kw):  # noqa: ANN001
        raise AssertionError("send_telegram_message 不該被 call(沒 token)")

    import src.notifier as _nm
    monkeypatch.setattr(_nm, "send_telegram_message", boom)

    result = mb.run_morning_brief(
        target_date=None, dry_run=False,
        send_telegram=True, send_discord=True,
        skip_refetch=True,
    )
    # 沒 secrets → 兩通道都 False(但不 raise)
    assert result["pushed_telegram"] is False
    assert result["pushed_discord"] is False
