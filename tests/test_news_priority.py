"""src/news_fetcher.py 排序加權邏輯單元測試。

主公拍板 5 條新加權規則 (2026-05-08):
  +800  paper_trades 表內(主公正在試)
  +200  發布在交易時段 09:00-13:30
  +200  個股當日 |ret_1d| ≥ 5%
  +300  鉅額交易款本身(第 53 / 55 款)
  +300  現價 ≥ 法人共識目標(觸目標價)

加上原有 +1000 watchlist / +500 picks(保留不動)。

5 條新規則 + 白名單砍/加 + 整體排序 sanity 各一個 test case。
"""
from __future__ import annotations

import pytest

from src import config, database as db
from src import news_fetcher as nf


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "news.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    yield tmp_path
    db._reset_path_cache()


def _seed_paper_trade(sid: str, status: str = "active") -> None:
    """灌一筆 paper_trade。"""
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades
              (sid, name, entry_date, entry_price,
               target_price, stop_price, status, created_at)
            VALUES (?, '', '2026-05-07', 100.0, 105.0, 95.0, ?, '2026-05-07')
            """,
            (sid, status),
        )


def _seed_prices(sid: str, dates_closes: list[tuple[str, float]]) -> None:
    """灌 daily_prices,(date, close) tuples。"""
    with db.get_conn() as conn:
        for d, c in dates_closes:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_prices
                  (stock_id, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, 1000)
                """,
                (sid, d, c, c, c, c),
            )


def _seed_analyst_target(sid: str, target_mean: float) -> None:
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO analyst_targets
              (stock_id, target_mean, num_analysts, source, fetched_at)
            VALUES (?, ?, 5, 'yfinance', '2026-05-07T00:00:00+00:00')
            """,
            (sid, target_mean),
        )


# === 白名單 set 變動驗收 ===

def test_whitelist_砍掉_6_7_8_14_39():
    """主公砍掉的 5 款不應在白名單內。"""
    for art in ("第6款", "第7款", "第8款", "第14款", "第39款"):
        assert art not in nf.IMPORTANT_ARTICLES, f"{art} 應已從白名單移除"


def test_whitelist_新增_1_4_19_53_55():
    """主公新加的 5 款應在白名單內。"""
    for art in ("第1款", "第4款", "第19款", "第53款", "第55款"):
        assert art in nf.IMPORTANT_ARTICLES, f"{art} 應已新加進白名單"


def test_whitelist_size_eq_17():
    """主公拍板 17 款(2026-05-08 三修:第 20 款 B 方案回補 + 主旨黑名單)。"""
    assert len(nf.IMPORTANT_ARTICLES) == 17


def test_article_20_in_whitelist_with_subject_blacklist():
    """第 20 款 B 方案:回補白名單 + ARTICLE_PRIORITY,但用主旨黑名單
    精準過濾「子公司+固定收益/公司債」這類雜訊;其他主旨放行。
    """
    assert "第20款" in nf.IMPORTANT_ARTICLES, "第 20 款應已回補"
    assert nf.ARTICLE_PRIORITY.get("第20款") == 80, "base score 80"
    # 第 20 款的黑名單規則必須存在
    assert "第20款" in nf.SUBJECT_BLACKLIST_BY_ARTICLE
    rules = nf.SUBJECT_BLACKLIST_BY_ARTICLE["第20款"]
    assert "子公司" in rules
    assert "固定收益" in rules["子公司"]


def test_article_priority_新加5款_base_score():
    """新加 5 款的 base score 應跟亮拍板的數值對齊。"""
    assert nf.ARTICLE_PRIORITY["第1款"] == 95
    assert nf.ARTICLE_PRIORITY["第4款"] == 70
    assert nf.ARTICLE_PRIORITY["第19款"] == 90
    assert nf.ARTICLE_PRIORITY["第53款"] == 85
    assert nf.ARTICLE_PRIORITY["第55款"] == 90


def test_article_priority_刪除款_不在表():
    """砍掉的 6/8/14 款 base score 不該還在 ARTICLE_PRIORITY。"""
    for art in ("第6款", "第8款", "第14款"):
        assert art not in nf.ARTICLE_PRIORITY, f"{art} 應已從 ARTICLE_PRIORITY 移除"


# === 5 條加權規則各一個 case ===

def test_bonus_paper_trades_plus800(tmp_db):
    """個股在 paper_trades 表 → +800。"""
    _seed_paper_trade("2330")
    ctx = nf.build_priority_context()
    news = {"sid": "2330", "article_no": "第10款", "publish_time": "140000"}
    base = nf.ARTICLE_PRIORITY["第10款"]  # 95
    expected = base + 800
    assert nf._priority_score(news, ctx=ctx) == expected, (
        f"paper_trades 應加 800,實際 {nf._priority_score(news, ctx=ctx)} vs 期望 {expected}"
    )


def test_bonus_trading_hours_plus200(tmp_db):
    """publish_time 在 09:00-13:30 → +200。"""
    ctx = nf.build_priority_context()
    in_hours = {"sid": "2330", "article_no": "第10款", "publish_time": "103015"}
    out_hours = {"sid": "2330", "article_no": "第10款", "publish_time": "140000"}
    base = nf.ARTICLE_PRIORITY["第10款"]
    assert nf._priority_score(in_hours, ctx=ctx) == base + 200
    assert nf._priority_score(out_hours, ctx=ctx) == base


def test_trading_hours_boundary():
    """09:00:00 / 13:30:00 邊界 → 算盤中;08:59 / 13:31 → 算盤外。"""
    assert nf._is_in_trading_hours("090000")  # 開盤鐘聲
    assert nf._is_in_trading_hours("133000")  # 收盤鐘聲
    assert not nf._is_in_trading_hours("085959")
    assert not nf._is_in_trading_hours("133001")
    # 5 碼 zfill
    assert nf._is_in_trading_hours("90000")  # 9:00:00
    assert not nf._is_in_trading_hours("")
    assert not nf._is_in_trading_hours(None)


def test_bonus_big_move_plus200(tmp_db):
    """個股當日 |ret_1d| ≥ 5% → +200。"""
    # 5/06 close=100 → 5/07 close=106(+6%)→ in big movers
    _seed_prices("9999", [("2026-05-06", 100.0), ("2026-05-07", 106.0)])
    # 5/06 close=100 → 5/07 close=103(+3%)→ NOT in big movers
    _seed_prices("8888", [("2026-05-06", 100.0), ("2026-05-07", 103.0)])
    ctx = nf.build_priority_context()
    base = nf.ARTICLE_PRIORITY["第10款"]
    n_big = {"sid": "9999", "article_no": "第10款", "publish_time": "140000"}
    n_small = {"sid": "8888", "article_no": "第10款", "publish_time": "140000"}
    assert nf._priority_score(n_big, ctx=ctx) == base + 200
    assert nf._priority_score(n_small, ctx=ctx) == base


def test_bonus_big_txn_article_plus300(tmp_db):
    """第 53 / 55 款本身 → +300(主力訊號)。第 10 款不加。"""
    ctx = nf.build_priority_context()
    n53 = {"sid": "X", "article_no": "第53款", "publish_time": "140000"}
    n55 = {"sid": "X", "article_no": "第55款", "publish_time": "140000"}
    n10 = {"sid": "X", "article_no": "第10款", "publish_time": "140000"}
    score53 = nf._priority_score(n53, ctx=ctx)
    score55 = nf._priority_score(n55, ctx=ctx)
    score10 = nf._priority_score(n10, ctx=ctx)
    # 第 53 款 base=85, +300 = 385
    assert score53 == nf.ARTICLE_PRIORITY["第53款"] + 300
    # 第 55 款 base=90, +300 = 390
    assert score55 == nf.ARTICLE_PRIORITY["第55款"] + 300
    # 第 10 款不加
    assert score10 == nf.ARTICLE_PRIORITY["第10款"]


def test_bonus_target_hit_plus300(tmp_db):
    """現價 ≥ 法人共識目標 → +300。"""
    # 目標價 100,現價 105 → 觸目標價
    _seed_prices("HOT", [("2026-05-06", 95.0), ("2026-05-07", 105.0)])
    _seed_analyst_target("HOT", 100.0)
    # 目標價 200,現價 150 → 沒觸
    _seed_prices("LOW", [("2026-05-06", 145.0), ("2026-05-07", 150.0)])
    _seed_analyst_target("LOW", 200.0)
    ctx = nf.build_priority_context()
    base = nf.ARTICLE_PRIORITY["第10款"]
    n_hit = {"sid": "HOT", "article_no": "第10款", "publish_time": "140000"}
    n_no = {"sid": "LOW", "article_no": "第10款", "publish_time": "140000"}
    # HOT 觸目標 +300,且漲幅 = (105-95)/95 = 10.5% ≥ 5% → +200
    score_hit = nf._priority_score(n_hit, ctx=ctx)
    score_no = nf._priority_score(n_no, ctx=ctx)
    assert score_hit == base + 300 + 200, (
        f"HOT 觸目標 + big_move 應 +500,實際 {score_hit} vs 期望 {base + 500}"
    )
    # LOW 沒觸,漲幅 = (150-145)/145 ≈ 3.4% < 5% → 都不加
    assert score_no == base


# === 整體排序 sanity ===

def test_priority_score_combines_all_bonuses(tmp_db):
    """全部 bonus 全中:watchlist + paper_trades + picks + big_move +
    target_hit + big_txn_article + trading_hours + base 第55款。
    Expected: 1000 + 800 + 500 + 200 + 300 + 300 + 200 + 90 = 3390
    """
    db.add_to_watchlist("2330")
    _seed_paper_trade("2330")
    _seed_prices("2330", [("2026-05-06", 100.0), ("2026-05-07", 110.0)])  # +10%
    _seed_analyst_target("2330", 100.0)  # 110 ≥ 100 觸目標
    # picks_sids 走 daily_picks,我們 mock get_today_picks_sids 直接給 ctx
    ctx = nf.build_priority_context()
    # 手動加 picks(單測不需打 daily_picks)
    ctx["picks_sids"] = {"2330"}

    news = {
        "sid": "2330",
        "article_no": "第55款",  # base 90 + 300 (big_txn)
        "publish_time": "100000",  # in trading hours +200
    }
    score = nf._priority_score(news, ctx=ctx)
    expected = 90 + 1000 + 800 + 500 + 200 + 300 + 300 + 200
    assert score == expected, (
        f"全部 bonus 應 sum 到 {expected},實際 {score}"
    )


def test_priority_score_backward_compat_no_ctx(tmp_db):
    """舊路徑:caller 傳 watchlist_sids / picks_sids 不走 ctx,新加權全 0。"""
    news = {"sid": "2330", "article_no": "第10款", "publish_time": "140000"}
    base = nf.ARTICLE_PRIORITY["第10款"]
    score = nf._priority_score(
        news,
        watchlist_sids={"2330"},
        picks_sids=set(),
    )
    # 只 watchlist +1000,沒 ctx → paper_trades / big_move / target_hit / big_txn /
    # trading_hours 全 0(big_txn 跟 trading_hours 是 news-property 不需 ctx,
    # 但 caller 走舊路徑時就以 ctx=None 為準,只算 sid_set bonuses 跟 article)
    # 第 10 款不在 _BIG_TXN_ARTICLES,publish_time 也不在 trading hours
    assert score == base + 1000


def test_get_paper_trades_sids_excludes_closed(tmp_db):
    """status != 'active' 的 paper_trade 不該入 set。"""
    _seed_paper_trade("2330", status="active")
    _seed_paper_trade("2317", status="win")
    _seed_paper_trade("1101", status="lose")
    sids = nf.get_paper_trades_sids()
    assert sids == {"2330"}


def test_get_big_movers_threshold(tmp_db):
    """5% threshold 的邊界:剛好 5% 算,差一點不算。"""
    # 5/06 close=100, 5/07 close=105 → ret = 5% 剛好
    _seed_prices("EQ5", [("2026-05-06", 100.0), ("2026-05-07", 105.0)])
    # 5/06 close=100, 5/07 close=104.9 → ret = 4.9% 不到
    _seed_prices("UNDER5", [("2026-05-06", 100.0), ("2026-05-07", 104.9)])
    # 5/06 close=100, 5/07 close=94 → ret = -6% 算大跌
    _seed_prices("DOWN6", [("2026-05-06", 100.0), ("2026-05-07", 94.0)])
    sids = nf.get_big_movers_sids(threshold=0.05)
    assert "EQ5" in sids
    assert "UNDER5" not in sids
    assert "DOWN6" in sids


def test_get_target_hit_sids(tmp_db):
    _seed_prices("HIT", [("2026-05-07", 105.0)])
    _seed_analyst_target("HIT", 100.0)
    _seed_prices("MISS", [("2026-05-07", 95.0)])
    _seed_analyst_target("MISS", 100.0)
    _seed_prices("EQ", [("2026-05-07", 100.0)])
    _seed_analyst_target("EQ", 100.0)
    # 沒目標價的不入
    _seed_prices("NOT_AT", [("2026-05-07", 200.0)])

    sids = nf.get_target_hit_sids()
    assert "HIT" in sids
    assert "EQ" in sids  # >= 算觸目標
    assert "MISS" not in sids
    assert "NOT_AT" not in sids


def test_build_priority_context_returns_all_keys(tmp_db):
    ctx = nf.build_priority_context()
    assert set(ctx.keys()) == {
        "watchlist_sids", "picks_sids", "paper_trades_sids",
        "big_movers_sids", "target_hit_sids",
    }
    for v in ctx.values():
        assert isinstance(v, set)
