"""第 20 款主旨黑名單(B 方案)單元測試 — 主公拍板 2026-05-08。

src/news_fetcher.py:is_blacklisted_subject() 限定特定款 + 主旨關鍵字組合 → skip
- 第 20 款「子公司+固定收益/公司債/短期票券/金融債」雜訊精準砍
- 第 20 款其他主旨(土地交易 / 設備採購 / 處分股票 / 子公司增資孫公司等)放行
- 別款(11、51 等)的「子公司+公司債」不受此 blacklist 影響
"""
from __future__ import annotations

import pytest

from src import config, database as db, news_fetcher as nf


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "blacklist.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    yield tmp_path
    db._reset_path_cache()


# === is_blacklisted_subject helper ===

def test_blacklist_article_20_subsidiary_fixed_income_skipped():
    """第 20 款「子公司+固定收益」→ True(會被砍)。"""
    assert nf.is_blacklisted_subject(
        "第20款",
        "本公司代子公司 TSMC Global Ltd. 公告取得固定收益證券",
    ) is True


def test_blacklist_article_20_subsidiary_corp_bond_skipped():
    """第 20 款「子公司+公司債」→ True(會被砍)。"""
    assert nf.is_blacklisted_subject(
        "第20款",
        "代子公司凱基證券公告取得凱基人壽保險115年度第二期無擔保累積次順位公司債",
    ) is True


def test_blacklist_article_20_subsidiary_short_term_note_skipped():
    """第 20 款「子公司+短期票券」→ True。"""
    assert nf.is_blacklisted_subject(
        "第20款", "本公司代子公司 X 公告取得短期票券",
    ) is True


def test_blacklist_article_20_subsidiary_financial_bond_skipped():
    """第 20 款「子公司+金融債」→ True。"""
    assert nf.is_blacklisted_subject(
        "第20款", "代子公司公告取得金融債券投資",
    ) is True


def test_passthrough_article_20_land_transaction():
    """第 20 款「土地交易」→ False(放行,有實質訊號)。"""
    assert nf.is_blacklisted_subject(
        "第20款", "公告本公司向關係人租賃取得不動產使用權資產",
    ) is False


def test_passthrough_article_20_equipment_purchase():
    """第 20 款「設備採購」→ False。"""
    assert nf.is_blacklisted_subject(
        "第20款", "公告本公司「新增高鐵列車組採購案地面設備(東芝)」採購案",
    ) is False


def test_passthrough_article_20_subsidiary_capital_increase_no_keyword():
    """第 20 款「子公司增資越南孫公司」→ False(沒固定收益關鍵字)。"""
    assert nf.is_blacklisted_subject(
        "第20款", "代重要子公司 AUDEN TECHNO (BVI) CORPORATION 公告增資越南孫公司",
    ) is False


def test_passthrough_article_20_subsidiary_disposal_stock():
    """第 20 款「代子公司處分聯發科普通股」→ False(沒固定收益關鍵字)。"""
    assert nf.is_blacklisted_subject(
        "第20款", "代子公司環電(股)公司公告處分聯發科普通股",
    ) is False


def test_blacklist_only_applies_to_article_20():
    """限定第 20 款才套用;第 11 款「子公司+公司債」(凱基金子公司發行公司債)
    這類對母公司是資本結構訊號,要保留 → False。"""
    assert nf.is_blacklisted_subject(
        "第11款",
        "元大金控代子公司元大證券補充說明公司債發行條件",
    ) is False


def test_blacklist_no_keyword_match_passthrough():
    """第 20 款「子公司」單獨不算雜訊(沒任一固定收益關鍵字)→ False。"""
    assert nf.is_blacklisted_subject(
        "第20款", "代子公司公告處分機器設備",
    ) is False


def test_blacklist_keyword_without_subsidiary_passthrough():
    """第 20 款「公司債」但沒「子公司」→ False(母公司直接持有公司債仍有訊號)。"""
    assert nf.is_blacklisted_subject(
        "第20款", "本公司公告取得 XX 公司債",
    ) is False


def test_blacklist_unknown_article_passthrough():
    """非第 20 款的訊息全放行(blacklist 限定 article_no='第20款')。"""
    assert nf.is_blacklisted_subject(
        "第10款", "本公司代子公司公告取得固定收益證券",
    ) is False


def test_blacklist_empty_inputs():
    """空字串 / None 安全 fallback → False。"""
    assert nf.is_blacklisted_subject("", "本公司代子公司公告取得固定收益證券") is False
    assert nf.is_blacklisted_subject("第20款", "") is False
    assert nf.is_blacklisted_subject(None, "test") is False  # type: ignore[arg-type]


# === Integration:list_unsent_important_news 過濾 ===

def _seed_news(rows: list[dict]) -> None:
    """灌一批 news 直接到 SQLite。"""
    with db.get_conn() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO news
                  (sid, company_name, publish_date, publish_time, subject,
                   article_no, description, fact_date, url_hash, fetched_at)
                VALUES (?, '', ?, '140000', ?, ?, '', '', ?, '2026-05-07T00:00:00')
                """,
                (
                    r["sid"], r.get("publish_date", "2026-05-07"),
                    r["subject"], r["article_no"],
                    f"hash_{r['sid']}_{r['article_no']}_{r['subject'][:20]}",
                ),
            )


def test_list_unsent_filters_article_20_subsidiary_fixed_income(
    tmp_db, monkeypatch,
):
    """第 20 款的「子公司+固定收益」訊息會被 list_unsent 過濾掉。
    其他第 20 款主旨保留。
    """
    # 把 watchlist 灌進去讓 sid 都過 6 類 eligible filter
    db.add_to_watchlist("2330")
    db.add_to_watchlist("3138")

    _seed_news([
        # 應 skip:子公司+固定收益
        {
            "sid": "2330",
            "subject": "本公司代子公司 TSMC Global Ltd. 公告取得固定收益證券",
            "article_no": "第20款",
        },
        # 應保留:第 20 款其他主旨
        {
            "sid": "3138",
            "subject": "代重要子公司 AUDEN TECHNO 公告增資越南孫公司",
            "article_no": "第20款",
        },
    ])

    unsent = nf.list_unsent_important_news(channel="telegram")
    sids = [u["sid"] for u in unsent]
    assert "2330" not in sids, "子公司+固定收益主旨應被 blacklist 砍掉"
    assert "3138" in sids, "子公司增資孫公司應放行"
