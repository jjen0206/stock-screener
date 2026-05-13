"""scripts/backfill_qrystock.py 單元測試。

不打真網,用 fixture HTML + fake session 注入。covers:
  - parse_qrystock_html: 抽 level=15 / level=17
  - load_theme_sids: union 多份 YAML
  - _sca_date_to_week_end: 格式
  - run_backfill: full flow + UPSERT 不重複 + 已存在跳過 + delta_w 算對
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src import config, database as db


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_qrystock.py"
_spec = importlib.util.spec_from_file_location("backfill_qrystock", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


# === fixture HTML(模 TDCC qryStock POST response 結構) ===

def _make_html(*, big_count: int, total_count: int) -> str:
    """產一個只含必要 tr 的最簡 fixture。

    結構:每列 5 td = [level, range, count, shares, pct]。
    """
    levels = [
        (1, "1-999", 1900000),
        (14, "800001-1000000", 200),
        (big_count, ),  # placeholder, 下面 override
    ]
    # 用具體 row 拼接,易讀
    rows_html = []
    rows_html.append(
        "<tr><td>1</td><td>1-999</td><td>1,900,000</td>"
        "<td>200,000,000</td><td>0.80</td></tr>"
    )
    rows_html.append(
        "<tr><td>14</td><td>800001-1000000</td><td>200</td>"
        "<td>180,000,000</td><td>0.70</td></tr>"
    )
    rows_html.append(
        f"<tr><td>15</td><td>1000001以上</td><td>{big_count:,}</td>"
        "<td>22,000,000,000</td><td>85.00</td></tr>"
    )
    rows_html.append(
        "<tr><td>16</td><td>差異數調整</td><td></td>"
        "<td>-1000</td><td>-0.00</td></tr>"
    )
    rows_html.append(
        f"<tr><td>17</td><td>合計</td><td>{total_count:,}</td>"
        "<td>25,000,000,000</td><td>100.00</td></tr>"
    )
    return "<html><body><table>" + "".join(rows_html) + "</table></body></html>"


def _make_form_html() -> str:
    """GET response 模:含 SYNCHRONIZER_TOKEN + firDate + scaDate options。"""
    return (
        '<html><body>'
        '<input name="SYNCHRONIZER_TOKEN" value="tok-abc">'
        '<input name="firDate" value="20260508">'
        '<select name="scaDate">'
        '<option value="20260508">2026/05/08</option>'
        '<option value="20260430">2026/04/30</option>'
        '<option value="20260424">2026/04/24</option>'
        '<option value="20260417">2026/04/17</option>'
        '</select>'
        '</body></html>'
    )


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        # _make_html 是 ASCII,big5 decode 安全
        self.content = text.encode("utf-8")
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """模 requests.Session,以 (method, scaDate, stockNo) routing 回 fixture。"""

    def __init__(self, post_responder):
        self.post_responder = post_responder
        self.headers = {}
        self.verify = False
        self.posts: list[dict] = []

    def get(self, url, timeout=None):
        return _FakeResponse(_make_form_html())

    def post(self, url, data=None, timeout=None):
        self.posts.append(dict(data or {}))
        body = self.post_responder(data or {})
        # backfill 會走 r.content.decode('big5'),這邊用 utf-8 模擬;
        # _make_html 是 ASCII,big5 decode utf-8 bytes 不會炸數字。
        return _FakeResponse(body)


# === parse_qrystock_html ===

def test_parse_qrystock_html_extracts_level_15_and_17():
    html = _make_html(big_count=1503, total_count=2464344)
    out = backfill.parse_qrystock_html(html)
    assert out == {
        "holders_1000up_count": 1503,
        "total_holders": 2464344,
    }


def test_parse_qrystock_html_no_table_returns_none():
    assert backfill.parse_qrystock_html("<html>no table</html>") is None
    assert backfill.parse_qrystock_html("") is None


def test_parse_qrystock_html_missing_total_returns_none():
    # 只有 15 沒有更後面的合計列 → None
    html = (
        "<html><table>"
        '<tr><td>15</td><td>1000001以上</td><td>500</td><td>1</td><td>1</td></tr>'
        "</table></html>"
    )
    assert backfill.parse_qrystock_html(html) is None


def test_parse_qrystock_html_16_level_structure_no_adjustment_row():
    """有些股(冷門或早期週)沒有 level=16 的『差異數調整』行,
    合計直接出現在 level=16。parser 必須處理這種變體。"""
    rows_html = []
    rows_html.append(
        "<tr><td>1</td><td>1-999</td><td>33,986</td>"
        "<td>8,466,700</td><td>0.47</td></tr>"
    )
    rows_html.append(
        "<tr><td>14</td><td>800001-1000000</td><td>13</td>"
        "<td>11,488,319</td><td>0.63</td></tr>"
    )
    rows_html.append(
        "<tr><td>15</td><td>1000001以上</td><td>78</td>"
        "<td>1,129,719,196</td><td>62.90</td></tr>"
    )
    # 注意:這裡 level=16 就是合計列,沒有差異數調整行
    rows_html.append(
        "<tr><td>16</td><td>合計</td><td>100,940</td>"
        "<td>1,795,960,668</td><td>100.00</td></tr>"
    )
    html = "<html><table>" + "".join(rows_html) + "</table></html>"
    out = backfill.parse_qrystock_html(html)
    assert out == {"holders_1000up_count": 78, "total_holders": 100940}


# === _sca_date_to_week_end ===

def test_sca_date_to_week_end_format():
    assert backfill._sca_date_to_week_end("20260508") == "2026-05-08"
    assert backfill._sca_date_to_week_end("20251017") == "2025-10-17"


# === load_theme_sids ===

def test_load_theme_sids_unions_multiple_yamls(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "sids:\n  - '2330'\n  - '2317'\n", encoding="utf-8",
    )
    (tmp_path / "b.yaml").write_text(
        "sids:\n  - '2330'\n  - 6669\n", encoding="utf-8",
    )
    sids = backfill.load_theme_sids(tmp_path)
    assert sids == ["2317", "2330", "6669"]


# === run_backfill 整合 ===

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "qry.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()


def test_run_backfill_writes_rows_and_computes_delta(tmp_db, tmp_path):
    # 兩檔 × 三週,模擬 count 每週 +10
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "x.yaml").write_text(
        "sids:\n  - '2330'\n  - '2317'\n", encoding="utf-8",
    )

    # 處理順序: 20260424 -> 20260430 -> 20260508 (從 fixture form scaDates 前 3 個取)
    counts_by_week_sid = {
        ("20260508", "2330"): 1500,  # newest
        ("20260430", "2330"): 1490,
        ("20260424", "2330"): 1480,  # oldest
        ("20260508", "2317"): 800,
        ("20260430", "2317"): 790,
        ("20260424", "2317"): 780,
    }

    def responder(data):
        key = (data["scaDate"], data["stockNo"])
        cnt = counts_by_week_sid[key]
        return _make_html(big_count=cnt, total_count=10000)

    fake_sess = _FakeSession(responder)
    summary = backfill.run_backfill(
        themes, weeks=3,
        rate_limit_secs=0.0, sleep_fn=lambda x: None,
        session_factory=lambda: fake_sess,
    )

    assert summary["ok"] == 6  # 2 檔 × 3 週
    assert summary["failed"] == 0
    assert summary["skipped"] == 0

    # 驗 DB: 每週都寫到 + delta_w 計算正確(從第 2 週起非 None)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT sid, week_end, holders_1000up_count, holders_delta_w "
            "FROM shareholder_concentration ORDER BY sid, week_end"
        ).fetchall()
    rows = [dict(r) for r in rows]
    assert len(rows) == 6

    # 2317: 780(oldest, delta=None) → 790(+10) → 800(+10)
    r2317 = [r for r in rows if r["sid"] == "2317"]
    assert r2317[0]["week_end"] == "2026-04-24"
    assert r2317[0]["holders_1000up_count"] == 780
    assert r2317[0]["holders_delta_w"] is None
    assert r2317[1]["holders_delta_w"] == 10
    assert r2317[2]["holders_delta_w"] == 10

    # 2330: 1480 → 1490(+10) → 1500(+10)
    r2330 = [r for r in rows if r["sid"] == "2330"]
    assert r2330[0]["holders_delta_w"] is None
    assert r2330[1]["holders_delta_w"] == 10
    assert r2330[2]["holders_delta_w"] == 10


def test_run_backfill_skips_existing_pairs(tmp_db, tmp_path):
    """DB 已有的 (sid, week_end) 不再 POST,直接 skip。"""
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "x.yaml").write_text("sids:\n  - '2330'\n", encoding="utf-8")

    # 預先塞一筆 — 2026-04-30 應該被 skip
    db.upsert_shareholder_concentration([{
        "sid": "2330",
        "week_end": "2026-04-30",
        "holders_1000up_count": 9999,
        "total_holders": 99999,
        "holders_pct": 0.1,
        "holders_delta_w": None,
    }])

    posted = []

    def responder(data):
        posted.append((data["scaDate"], data["stockNo"]))
        return _make_html(big_count=500, total_count=1000)

    fake_sess = _FakeSession(responder)
    summary = backfill.run_backfill(
        themes, weeks=2,
        rate_limit_secs=0.0, sleep_fn=lambda x: None,
        session_factory=lambda: fake_sess,
    )

    # 2 週中 1 週(20260430)已存在 → skip 1,POST 1
    assert summary["skipped"] == 1
    assert summary["ok"] == 1
    assert len(posted) == 1
    # 沒打 20260430 那一週
    assert all(scaDate != "20260430" for scaDate, _ in posted)


def test_run_backfill_upsert_no_duplicate(tmp_db, tmp_path):
    """連跑兩次 backfill 不會 duplicate row。"""
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "x.yaml").write_text("sids:\n  - '2330'\n", encoding="utf-8")

    def responder(_data):
        return _make_html(big_count=1500, total_count=2000)

    # 第一次:寫 1 週
    fake_sess = _FakeSession(responder)
    backfill.run_backfill(
        themes, weeks=1,
        rate_limit_secs=0.0, sleep_fn=lambda x: None,
        session_factory=lambda: fake_sess,
    )

    # 第二次:同樣 1 週(會被 skip)
    fake_sess2 = _FakeSession(responder)
    summary = backfill.run_backfill(
        themes, weeks=1,
        rate_limit_secs=0.0, sleep_fn=lambda x: None,
        session_factory=lambda: fake_sess2,
    )
    assert summary["skipped"] == 1
    assert summary["ok"] == 0

    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM shareholder_concentration WHERE sid='2330'"
        ).fetchone()["c"]
    assert n == 1


def test_run_backfill_skipped_week_seeds_next_delta(tmp_db, tmp_path):
    """已存在週 (skip) 仍把 count 灌進 prev_counts,讓下一週 delta_w 算對。"""
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "x.yaml").write_text("sids:\n  - '2330'\n", encoding="utf-8")

    # 預先塞 oldest 週 (2026-04-24 = scaDate 20260424, count=100)
    db.upsert_shareholder_concentration([{
        "sid": "2330",
        "week_end": "2026-04-24",
        "holders_1000up_count": 100,
        "total_holders": 10000,
        "holders_pct": 0.01,
        "holders_delta_w": None,
    }])

    # 後兩週 fetch 走 fake(weeks=3 從 fixture 取 20260508/20260430/20260424,
    # 處理順序 oldest first: 20260424 → 20260430 → 20260508)
    def responder(data):
        cnt = {"20260430": 150, "20260508": 200}[data["scaDate"]]
        return _make_html(big_count=cnt, total_count=10000)

    fake_sess = _FakeSession(responder)
    backfill.run_backfill(
        themes, weeks=3,
        rate_limit_secs=0.0, sleep_fn=lambda x: None,
        session_factory=lambda: fake_sess,
    )

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT week_end, holders_1000up_count, holders_delta_w "
            "FROM shareholder_concentration WHERE sid='2330' "
            "ORDER BY week_end"
        ).fetchall()
    rows = [dict(r) for r in rows]
    assert rows[0]["week_end"] == "2026-04-24"
    # 0430 應該 delta = 150 - 100 = 50 (因為 prev_counts seed 自 skip 那週)
    assert rows[1]["holders_delta_w"] == 50
    assert rows[2]["holders_delta_w"] == 50  # 200 - 150


def test_run_backfill_dry_run_does_not_write_db(tmp_db, tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "x.yaml").write_text("sids:\n  - '2330'\n", encoding="utf-8")

    def responder(_data):
        raise AssertionError("dry-run 不應該 POST")

    fake_sess = _FakeSession(responder)
    backfill.run_backfill(
        themes, weeks=2, dry_run=True,
        rate_limit_secs=0.0, sleep_fn=lambda x: None,
        session_factory=lambda: fake_sess,
    )
    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM shareholder_concentration"
        ).fetchone()["c"]
    assert n == 0
