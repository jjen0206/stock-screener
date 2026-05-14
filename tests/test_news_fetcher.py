"""src/news_fetcher.py 單元測試 + scripts/news_notify.py 整合測試。

不打 TWSE 真網路 — mock fetch_twse_news_raw / requests.post。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from src import config, database as db, news_fetcher, notifier


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "news.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    # 防 preload_snapshots 從 data/twse_snapshot/news.csv(workflow auto-commit
    # 進 repo)灌真實資料污染 tmp DB → 測試行為跟現實 news.csv 內容耦合
    monkeypatch.setattr(db, "preload_snapshots", lambda *a, **kw: {})
    db.init_db()
    yield db_file
    db._reset_path_cache()


# === schema ===

def test_news_table_schema(tmp_db):
    """init_db 後 news 表 + UNIQUE(url_hash) + 必要欄位都在。"""
    with db.get_conn() as conn:
        names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "news" in names
        cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(news)"
            ).fetchall()
        }
    for required in (
        "id", "sid", "company_name", "publish_date", "publish_time",
        "subject", "article_no", "description", "fact_date",
        "url_hash", "sent_telegram", "sent_discord", "fetched_at",
    ):
        assert required in cols, f"missing col {required}"


# === _roc_to_iso ===

def test_roc_to_iso_7_digit():
    assert news_fetcher._roc_to_iso("1150505") == "2026-05-05"
    assert news_fetcher._roc_to_iso("1140101") == "2025-01-01"


def test_roc_to_iso_invalid():
    assert news_fetcher._roc_to_iso(None) is None
    assert news_fetcher._roc_to_iso("") is None
    assert news_fetcher._roc_to_iso("abc") is None
    assert news_fetcher._roc_to_iso("12345") is None  # 太短


# === normalize_twse_news ===

def test_normalize_handles_subject_with_trailing_space_key():
    """TWSE 真實 key '主旨 ' 帶尾空格;normalize 該抓得到。"""
    raw = [{
        "出表日期": "1150505",
        "發言日期": "1150504",
        "發言時間": "70003",
        "公司代號": "2330",
        "公司名稱": "台積電",
        "主旨 ": "公告本公司董事會決議...",  # ← 尾有空格的 key
        "符合條款": "第10款",
        "事實發生日": "1150504",
        "說明": "1.事實發生日:民國115年05月04日...",
    }]
    rows = news_fetcher.normalize_twse_news(raw)
    assert len(rows) == 1
    r = rows[0]
    assert r["sid"] == "2330"
    assert r["company_name"] == "台積電"
    assert r["publish_date"] == "2026-05-04"
    assert r["subject"] == "公告本公司董事會決議..."
    assert r["article_no"] == "第10款"
    assert r["fact_date"] == "2026-05-04"
    assert r["url_hash"]  # 非空


def test_normalize_skips_rows_with_no_sid_or_subject():
    raw = [
        {"公司代號": "", "主旨 ": "x", "發言日期": "1150504"},
        {"公司代號": "2330", "主旨 ": "", "發言日期": "1150504"},
        {"公司代號": "2330", "主旨 ": "ok", "發言日期": "1150504"},
    ]
    rows = news_fetcher.normalize_twse_news(raw)
    assert len(rows) == 1
    assert rows[0]["sid"] == "2330"


# === url_hash dedup ===

def test_url_hash_same_for_same_content():
    h1 = news_fetcher._compute_url_hash(
        "2330", "2026-05-04", "公告本公司...",
    )
    h2 = news_fetcher._compute_url_hash(
        "2330", "2026-05-04", "公告本公司...",
    )
    assert h1 == h2


def test_url_hash_different_for_different_content():
    h1 = news_fetcher._compute_url_hash("2330", "2026-05-04", "A")
    h2 = news_fetcher._compute_url_hash("2330", "2026-05-04", "B")
    h3 = news_fetcher._compute_url_hash("2454", "2026-05-04", "A")
    assert len({h1, h2, h3}) == 3


# === upsert_news (dedup) ===

def test_upsert_news_dedup_by_url_hash(tmp_db):
    rows = news_fetcher.normalize_twse_news([
        {
            "公司代號": "2330", "公司名稱": "台積電",
            "發言日期": "1150504", "發言時間": "70003",
            "主旨 ": "公告A", "符合條款": "第10款",
        },
    ])
    inserted, skipped = news_fetcher.upsert_news(rows)
    assert inserted == 1 and skipped == 0

    # 同樣 raw 再 upsert 一次 → 該 skip
    inserted, skipped = news_fetcher.upsert_news(rows)
    assert inserted == 0 and skipped == 1

    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM news").fetchone()["c"]
    assert n == 1


# === list_unsent + whitelist filter ===

def _mock_eligible_sids_open(monkeypatch, sids: set[str]) -> None:
    """測試用 helper:mock get_eligible_news_sids 把指定 sid 全放進 watchlist
    set,讓主公 2026-05-08 加的 6 類 filter 等同 no-op,讓舊條款 / mark_sent
    測試專注驗證自身邏輯。
    """
    groups = {
        "watchlist": set(sids),
        "short_picks": set(),
        "long_picks": set(),
        "limit_up": set(),
        "limit_down_after_up": set(),
        "hot": set(),
        "all": set(sids),
    }
    monkeypatch.setattr(
        news_fetcher, "get_eligible_news_sids",
        lambda trade_date=None, db_path=None: groups,
    )


def test_list_unsent_filters_by_whitelist(tmp_db, monkeypatch):
    """條款不在白名單 → 不在 unsent list 裡。"""
    _mock_eligible_sids_open(monkeypatch, {"2330", "2454", "1101"})
    rows = news_fetcher.normalize_twse_news([
        {
            "公司代號": "2330", "公司名稱": "台積",
            "發言日期": "1150504", "主旨 ": "重要訴訟", "符合條款": "第10款",
        },
        {
            "公司代號": "2454", "公司名稱": "聯發",
            "發言日期": "1150504", "主旨 ": "財報會議預告", "符合條款": "第31款",
        },
        {
            "公司代號": "1101", "公司名稱": "台泥",
            "發言日期": "1150504", "主旨 ": "改名", "符合條款": "第51款",
        },
    ])
    news_fetcher.upsert_news(rows)

    unsent = news_fetcher.list_unsent_important_news(channel="telegram")
    sids = sorted(r["sid"] for r in unsent)
    assert sids == ["2330"], (
        f"預期只 2330 (第10款 在白名單),實際: {sids}"
    )


def test_mark_news_sent_updates_only_target_channel(tmp_db, monkeypatch):
    _mock_eligible_sids_open(monkeypatch, {"2330"})
    rows = news_fetcher.normalize_twse_news([
        {
            "公司代號": "2330", "公司名稱": "台積",
            "發言日期": "1150504", "主旨 ": "ok", "符合條款": "第10款",
        },
    ])
    news_fetcher.upsert_news(rows)

    # 取 id
    with db.get_conn() as conn:
        id_ = conn.execute("SELECT id FROM news LIMIT 1").fetchone()["id"]

    n = news_fetcher.mark_news_sent([id_], channel="telegram")
    assert n == 1
    # telegram 標 1,discord 仍 0
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT sent_telegram, sent_discord FROM news WHERE id=?",
            (id_,),
        ).fetchone()
    assert r["sent_telegram"] == 1 and r["sent_discord"] == 0

    # discord 也應該還能撈到該則
    unsent = news_fetcher.list_unsent_important_news(channel="discord")
    assert len(unsent) == 1


# === format_news_block ===

def _make_news_dict() -> dict:
    return {
        "sid": "2330", "company_name": "台積電",
        "publish_date": "2026-05-04", "publish_time": "143005",
        "subject": "公告本公司董事會決議盈餘分配",
        "article_no": "第10款",
        "description": "1.事實發生日...",
    }


def test_format_news_block_telegram_includes_fields():
    news = _make_news_dict()
    block = notifier.format_news_block(news, channel="telegram")
    assert "台積電 (2330)" in block
    assert "14:30" in block
    assert "第10款" in block
    assert "盈餘分配" in block
    # Telegram = HTML bold (842b196: switched from Markdown to HTML parse_mode)
    assert "<b>台積電 (2330)</b>" in block


def test_format_news_block_discord_uses_double_asterisk():
    news = _make_news_dict()
    block = notifier.format_news_block(news, channel="discord")
    assert "**台積電 (2330)**" in block


def test_format_news_block_handles_5_digit_time():
    """時間 70003(早上 7 點)— 5 碼 → padded 070003 → 07:00。"""
    news = _make_news_dict()
    news["publish_time"] = "70003"
    block = notifier.format_news_block(news, channel="telegram")
    assert "07:00" in block


def test_format_news_block_truncates_long_subject():
    news = _make_news_dict()
    news["subject"] = "超長主旨" * 100
    block = notifier.format_news_block(news, channel="telegram")
    # 截斷到 200,加 "..."
    assert "..." in block
    assert len(block) < 1000  # 不會炸開


# === format_news_message ===

def test_format_news_message_includes_separator_and_count():
    news_list = [_make_news_dict() for _ in range(3)]
    msg = notifier.format_news_message(news_list, channel="telegram")
    assert "重大訊息" in msg
    assert "━━━━━━━━━━━━━━━━" in msg
    assert "本輪推送 3 則" in msg
    assert "TWSE 公開資訊" in msg


def test_format_news_message_empty_returns_empty_string():
    """空 list → 空 string;caller 自己判斷不送。"""
    assert notifier.format_news_message([], channel="telegram") == ""


# === priority sort ===

def _make_news_row(sid: str, article: str, time_str: str = "100000") -> dict:
    """組單筆 normalize 過的 news row(供 upsert / sort 測試共用)。"""
    return {
        "公司代號": sid, "公司名稱": f"C{sid}",
        "發言日期": "1150504", "發言時間": time_str,
        "主旨 ": f"news for {sid} {article}",
        "符合條款": article,
    }


def test_priority_score_watchlist_first():
    """watchlist 命中 → +1000,壓過任何條款分數。"""
    n_w = {"sid": "2330", "article_no": "第4款"}    # watchlist 但低條款
    n_3 = {"sid": "9999", "article_no": "第30款"}   # 第30款 base 100,但不在 watchlist
    s_w = news_fetcher._priority_score(n_w, watchlist_sids={"2330"})
    s_3 = news_fetcher._priority_score(n_3, watchlist_sids={"2330"})
    assert s_w > s_3, (
        f"watchlist (第4款 base 70 + 1000 = 1070) 應 > 第30款 base 100,"
        f"得 {s_w} vs {s_3}"
    )


def test_priority_score_picks_second():
    """picks 命中 (+500) > 高條款 (100);但 watchlist (+1000) > picks。"""
    n_p = {"sid": "2454", "article_no": "第12款"}   # picks 但很低條款 (50)
    n_h = {"sid": "9999", "article_no": "第30款"}   # 第30款 base 100
    n_w = {"sid": "1234", "article_no": "第12款"}   # watchlist 命中
    s_p = news_fetcher._priority_score(n_p, picks_sids={"2454"})
    s_h = news_fetcher._priority_score(n_h, picks_sids={"2454"})
    s_w = news_fetcher._priority_score(
        n_w, watchlist_sids={"1234"}, picks_sids={"2454"},
    )
    assert s_p > s_h, f"picks (50+500=550) 應 > 第30款 (100),得 {s_p} vs {s_h}"
    assert s_w > s_p, f"watchlist (50+1000=1050) 應 > picks (550),得 {s_w} vs {s_p}"


def test_priority_high_article_first():
    """都不在 watchlist / picks → 純條款 base 比較,第30款 > 第3款 > 第12款。"""
    n30 = {"sid": "1", "article_no": "第30款"}
    n3 = {"sid": "2", "article_no": "第3款"}
    n12 = {"sid": "3", "article_no": "第12款"}
    s30 = news_fetcher._priority_score(n30)
    s3 = news_fetcher._priority_score(n3)
    s12 = news_fetcher._priority_score(n12)
    assert s30 == 100 and s3 == 85 and s12 == 50
    assert s30 > s3 > s12


def test_load_unsent_returns_sorted_by_priority(tmp_db, monkeypatch):
    """list_unsent_important_news 回 list 順序:watchlist 第一 → picks 第二 →
    高條款第三 → 低條款最後(排序 by score DESC)。
    """
    # 4 筆 unsent:不同 sid 不同條款(改用新白名單條款,publish_time 都用盤後
    # 14:00 排除 trading_hours 加分干擾)
    rows = news_fetcher.normalize_twse_news([
        _make_news_row("1111", "第12款", "140000"),  # 低條款 50
        _make_news_row("2222", "第30款", "140100"),  # 高條款 100
        _make_news_row("3333", "第4款",  "140200"),  # 中條款 70(picks)
        _make_news_row("4444", "第12款", "140300"),  # 低條款 50(watchlist)
    ])
    news_fetcher.upsert_news(rows)

    # mock watchlist + picks(其他 ctx fetcher 走真實 SQLite 都回空 set)
    monkeypatch.setattr(
        news_fetcher, "get_watchlist_sids", lambda db_path=None: {"4444"},
    )
    monkeypatch.setattr(
        news_fetcher, "get_today_picks_sids", lambda db_path=None: {"3333"},
    )
    # mock eligible_sids 把 4 個 sid 都納入(沒納入會被 2026-05-08 加的 sid filter
    # 砍掉 — 此 test 焦點是 priority sort,不是 sid filter)
    _mock_eligible_sids_open(monkeypatch, {"1111", "2222", "3333", "4444"})

    unsent = news_fetcher.list_unsent_important_news(channel="telegram")
    sids = [r["sid"] for r in unsent]
    assert sids == ["4444", "3333", "2222", "1111"], (
        f"預期 watchlist 4444 (1050) → picks 3333 (570) → 第30款 2222 (100) → "
        f"第12款 1111 (50),實際: {sids}"
    )


# === e2e: news_notify dry-run ===

_NOTIFY_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "news_notify.py"
)
_n_spec = importlib.util.spec_from_file_location(
    "news_notify", _NOTIFY_SCRIPT,
)
news_notify_mod = importlib.util.module_from_spec(_n_spec)
_n_spec.loader.exec_module(news_notify_mod)


def test_news_notify_dry_run_prints_to_stdout(tmp_db, monkeypatch, capsys):
    """dry-run 不打網路、不打 channel,只 print。"""
    fake_raw = [
        {
            "出表日期": "1150505", "發言日期": "1150504",
            "發言時間": "143005", "公司代號": "2330",
            "公司名稱": "台積電",
            "主旨 ": "重大訴訟",
            "符合條款": "第10款",
            "事實發生日": "1150504",
            "說明": "...",
        },
    ]
    monkeypatch.setattr(
        news_fetcher, "fetch_twse_news_raw", lambda timeout=15: fake_raw,
    )
    # 也 patch news_notify_mod 的 import 後綁定的 reference(間接 import)
    monkeypatch.setattr(
        news_notify_mod, "fetch_and_store_news",
        lambda db_path=None: (
            news_fetcher.normalize_twse_news(fake_raw),
            *news_fetcher.upsert_news(news_fetcher.normalize_twse_news(fake_raw)),
        )[:3],
    )
    # 把 fake news 的 sid 納入 eligible_sids 讓主公 2026-05-08 加的 sid filter
    # 不擋住此 dry-run e2e test
    _mock_eligible_sids_open(monkeypatch, {"2330"})
    # 防漏網,確認 requests.post 不會被叫
    with patch("requests.post") as m_post:
        monkeypatch.setattr(
            "sys.argv",
            ["news_notify.py", "--dry-run", "--batch-size", "5"],
        )
        code = news_notify_mod.main()

    assert code == 0
    assert m_post.call_count == 0  # dry-run 不打網路
    out = capsys.readouterr().out
    assert "TELEGRAM (dry-run)" in out
    assert "DISCORD (dry-run)" in out
    assert "2330" in out  # 公司代號出現在訊息裡
    assert "台積電" in out


def test_news_notify_skips_when_no_unsent(tmp_db, monkeypatch, capsys):
    """fetch 後沒任何 unsent 重要新聞(條款全不在白名單)→ 不送。"""
    fake_raw = [
        {
            "出表日期": "1150505", "發言日期": "1150504",
            "發言時間": "143005", "公司代號": "2454",
            "公司名稱": "聯發",
            "主旨 ": "財報會議預告",
            "符合條款": "第31款",  # ← 不在白名單
            "事實發生日": "1150504",
            "說明": "",
        },
    ]
    monkeypatch.setattr(
        news_fetcher, "fetch_twse_news_raw", lambda timeout=15: fake_raw,
    )
    monkeypatch.setattr(
        news_notify_mod, "fetch_and_store_news",
        lambda db_path=None: (
            news_fetcher.normalize_twse_news(fake_raw),
            *news_fetcher.upsert_news(news_fetcher.normalize_twse_news(fake_raw)),
        )[:3],
    )
    with patch("requests.post") as m_post:
        monkeypatch.setattr(
            "sys.argv", ["news_notify.py", "--dry-run"],
        )
        news_notify_mod.main()

    out = capsys.readouterr().out
    assert "無 unsent 重要新聞" in out
    assert m_post.call_count == 0


def test_news_notify_no_push_flags_keep_fetch_skip_channels(
    tmp_db, monkeypatch, capsys,
):
    """暫停推播模式 — workflow 帶 --no-telegram --no-discord 上去後:
    - news 仍正常 fetch 進 DB(供 ML 訓練 + Streamlit「新聞」頁顯示)
    - Telegram + Discord 都 0 push call(requests.post 不被叫)
    """
    fake_raw = [
        {
            "出表日期": "1150505", "發言日期": "1150504",
            "發言時間": "143005", "公司代號": "2330",
            "公司名稱": "台積電",
            "主旨 ": "重大訴訟",
            "符合條款": "第10款",
            "事實發生日": "1150504",
            "說明": "...",
        },
    ]
    monkeypatch.setattr(
        news_fetcher, "fetch_twse_news_raw", lambda timeout=15: fake_raw,
    )
    monkeypatch.setattr(
        news_notify_mod, "fetch_and_store_news",
        lambda db_path=None: (
            news_fetcher.normalize_twse_news(fake_raw),
            *news_fetcher.upsert_news(news_fetcher.normalize_twse_news(fake_raw)),
        )[:3],
    )
    _mock_eligible_sids_open(monkeypatch, {"2330"})

    # 模擬 prod env 有 token / webhook — 若 push 沒被 flag 擋,真送會被觸發
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://fake.example/hook")

    with patch("requests.post") as m_post:
        monkeypatch.setattr(
            "sys.argv",
            [
                "news_notify.py",
                "--no-telegram", "--no-discord", "--batch-size", "5",
            ],
        )
        code = news_notify_mod.main()

    assert code == 0
    # 兩個 channel 都被 flag skip,push 應為 0 call
    assert m_post.call_count == 0

    # fetch 仍正常進 DB
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT sid, subject FROM news WHERE sid='2330'"
        ).fetchall()
    assert len(rows) >= 1, "暫停推播後 news 仍應 fetch 進 DB"
    assert "重大訴訟" in (rows[0]["subject"] or "")
