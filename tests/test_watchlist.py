"""watchlist CRUD 測試。"""
from __future__ import annotations

import pytest

from src import config, database as db


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "wl.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db.init_db()
    return db_file


def test_add_to_watchlist_creates_row(tmp_db):
    db.add_to_watchlist("2330", note="長線觀察")
    items = db.get_watchlist()
    assert len(items) == 1
    assert items[0]["stock_id"] == "2330"
    assert items[0]["note"] == "長線觀察"
    assert items[0]["added_at"]  # 自動帶 timestamp


def test_add_to_watchlist_idempotent_keeps_added_at(tmp_db):
    """重複 add 不該插重複,且 added_at 不變,只更新 note。"""
    db.add_to_watchlist("2330", note="第一次")
    first = db.get_watchlist()[0]
    db.add_to_watchlist("2330", note="第二次")
    second = db.get_watchlist()
    assert len(second) == 1
    assert second[0]["note"] == "第二次"
    assert second[0]["added_at"] == first["added_at"]  # 沒變


def test_is_in_watchlist(tmp_db):
    assert db.is_in_watchlist("2330") is False
    db.add_to_watchlist("2330")
    assert db.is_in_watchlist("2330") is True


def test_remove_from_watchlist(tmp_db):
    db.add_to_watchlist("2330")
    assert db.remove_from_watchlist("2330") is True
    assert db.is_in_watchlist("2330") is False
    # 第二次 remove 該回 False
    assert db.remove_from_watchlist("2330") is False


def test_get_watchlist_sorted_by_added_at_desc(tmp_db):
    """較晚加入的排前面。"""
    import time
    db.add_to_watchlist("2330")
    time.sleep(1.05)  # 確保 timestamp 不同(timespec=seconds)
    db.add_to_watchlist("2454")
    items = db.get_watchlist()
    assert [it["stock_id"] for it in items] == ["2454", "2330"]


def test_remove_unknown_stock_returns_false(tmp_db):
    """移除沒在清單的股票該回 False,不該炸。"""
    assert db.remove_from_watchlist("9999") is False


def test_add_without_note_uses_none(tmp_db):
    db.add_to_watchlist("2330")
    items = db.get_watchlist()
    assert items[0]["note"] is None


# === added_at 保留:雲端容器 reboot → load_from_csv 應還原原時間戳 ===

def test_add_to_watchlist_uses_provided_added_at(tmp_db):
    """傳入 added_at 應寫入 DB,不被 _now_iso() 覆寫。"""
    custom_ts = "2023-01-15T08:30:00+00:00"
    db.add_to_watchlist("2330", added_at=custom_ts)
    items = db.get_watchlist()
    assert items[0]["added_at"] == custom_ts


def test_add_to_watchlist_default_added_at_is_now(tmp_db):
    """不傳 added_at 時仍走 _now_iso(),維持既有行為。"""
    db.add_to_watchlist("2330")
    items = db.get_watchlist()
    # _now_iso() 帶秒級 ISO 8601,會帶 'T' 跟時區
    assert "T" in items[0]["added_at"]
    assert items[0]["added_at"].endswith("+00:00")


def test_load_from_csv_preserves_added_at(tmp_db, tmp_path, monkeypatch):
    """load_from_csv 應保留 CSV 內的 added_at,不被 _now_iso() 覆寫。

    回歸測試:雲端容器重啟 → load_from_csv → SQLite 的 added_at 應等於 CSV
    原值,不應該變成「啟動時間」(造成下次 dump 出假變更)。
    """
    from src import watchlist_snapshot
    csv_file = tmp_path / "watchlist.csv"
    csv_file.write_text(
        "stock_id,added_at,note\n"
        "2330,2023-01-15T08:30:00+00:00,初次關注\n"
        "3680,2024-06-10T12:00:00+00:00,\n",
        encoding="utf-8",
    )
    # 把 helper 的 WATCHLIST_CSV 指到 tmp 檔
    monkeypatch.setattr(watchlist_snapshot, "WATCHLIST_CSV", csv_file)
    # 繞過「DB 必須在 PROJECT_ROOT 底下」的 guard (tests 用 tmp_path)
    monkeypatch.setattr(
        watchlist_snapshot, "_db_inside_project", lambda _: True,
    )

    n = watchlist_snapshot.load_from_csv()
    assert n == 2
    items = {it["stock_id"]: it for it in db.get_watchlist()}
    assert items["2330"]["added_at"] == "2023-01-15T08:30:00+00:00"
    assert items["2330"]["note"] == "初次關注"
    assert items["3680"]["added_at"] == "2024-06-10T12:00:00+00:00"
    assert items["3680"]["note"] is None


# === load_from_string:雲端 boot 從 watchlist-sync 分支拉到的 CSV 字串入庫 ===


def test_load_from_string_inserts_rows(tmp_db):
    """合法 CSV 字串應全數寫入,added_at 保留。"""
    from src import watchlist_snapshot
    csv_text = (
        "stock_id,added_at,note\n"
        "2330,2025-09-01T03:00:00+00:00,test note\n"
        "3680,2025-10-02T04:00:00+00:00,\n"
    )
    n = watchlist_snapshot.load_from_string(csv_text)
    assert n == 2
    items = {it["stock_id"]: it for it in db.get_watchlist()}
    assert items["2330"]["added_at"] == "2025-09-01T03:00:00+00:00"
    assert items["2330"]["note"] == "test note"
    assert items["3680"]["added_at"] == "2025-10-02T04:00:00+00:00"
    assert items["3680"]["note"] is None


def test_load_from_string_empty_returns_zero(tmp_db):
    """空字串 / 只有 header → 0 筆。"""
    from src import watchlist_snapshot
    assert watchlist_snapshot.load_from_string("") == 0
    assert watchlist_snapshot.load_from_string("   \n  ") == 0
    assert watchlist_snapshot.load_from_string(
        "stock_id,added_at,note\n"
    ) == 0


def test_load_from_string_idempotent_with_existing_rows(tmp_db):
    """既有 watchlist + 同一 stock_id 的 CSV 進來,added_at 保留。"""
    from src import watchlist_snapshot
    db.add_to_watchlist("2330", note="本機已有", added_at="2024-01-01T00:00:00+00:00")
    csv_text = (
        "stock_id,added_at,note\n"
        "2330,2099-12-31T23:59:59+00:00,新 note\n"
    )
    watchlist_snapshot.load_from_string(csv_text)
    items = db.get_watchlist()
    # added_at 維持本機原值(由 ON CONFLICT 保留),note 更新為 CSV 版本
    assert items[0]["added_at"] == "2024-01-01T00:00:00+00:00"
    assert items[0]["note"] == "新 note"


def test_load_from_string_malformed_returns_zero(tmp_db):
    """parse 失敗也只回 0,不 raise。"""
    from src import watchlist_snapshot
    # pandas 對純非 CSV 字串容錯極大,故用 binary noise 確保 raise
    n = watchlist_snapshot.load_from_string("\x00\x01\x02")
    assert n == 0


# === UI 整合測試:個股查詢 toggle 第二次新增不該失敗 ===
# 主公回報 bug:同 session 改 stock_id 後 toggle 失效;root cause = button key 固定。
# 修法:button key 包含當下 stock_id (key=f"star_toggle_{sid}");每次 render 重查 DB。

def test_query_page_toggle_first_add(monkeypatch, tmp_path):
    """情境 1:預設 2330,按 toggle → 加入 watchlist。"""
    from streamlit.testing.v1 import AppTest
    from src import config as cfg
    cfg.DATABASE_PATH = str(tmp_path / "ux1.db")
    db.init_db()

    at = AppTest.from_file("app.py").run(timeout=30)
    # 切到「🔍 個股」(預設首頁是 Dashboard,需手動切)
    at.session_state["nav_segmented"] = "🔍 個股"
    at.session_state["active_page"] = "🔍 個股"
    at.run(timeout=30)
    add_btn = next(
        (b for b in at.button if "2330" in b.label and "加入" in b.label),
        None,
    )
    assert add_btn is not None
    add_btn.click().run(timeout=30)
    assert db.is_in_watchlist("2330")


def test_query_page_toggle_second_add_after_changing_stock(monkeypatch, tmp_path):
    """情境 2(主公的 bug):加入 2330 後改成 2317,該能再加入 — 兩檔都在。"""
    from streamlit.testing.v1 import AppTest
    from src import config as cfg
    cfg.DATABASE_PATH = str(tmp_path / "ux2.db")
    db.init_db()

    at = AppTest.from_file("app.py").run(timeout=30)
    # 切到「🔍 個股」(預設首頁是 Dashboard,需手動切)
    at.session_state["nav_segmented"] = "🔍 個股"
    at.session_state["active_page"] = "🔍 個股"
    at.run(timeout=30)
    # 第一次加 2330
    btn = next(b for b in at.button if "2330" in b.label and "加入" in b.label)
    btn.click().run(timeout=30)
    # 改成 2317 第二次加
    at.text_input[0].set_value("2317").run(timeout=30)
    btn = next(b for b in at.button if "2317" in b.label and "加入" in b.label)
    btn.click().run(timeout=30)
    # 兩檔都該在(2330 不該被誤殺)
    assert db.is_in_watchlist("2317")
    assert db.is_in_watchlist("2330")


def test_backfill_watchlist_history_skips_full_cache(monkeypatch, tmp_path):
    """daily_prices 已 >= 15 筆的個股不該被 fetch。"""
    from src import config as cfg
    cfg.DATABASE_PATH = str(tmp_path / "bf.db")
    db.init_db()
    db.upsert_stocks([{"stock_id": "FULL", "name": "X", "market": "TW"}])
    # 灌 20 筆假 daily_prices
    db.upsert_daily_prices([
        {"stock_id": "FULL", "date": f"2024-01-{i:02d}", "open": 100,
         "high": 101, "low": 99, "close": 100, "volume": 1000,
         "trading_money": None, "trading_turnover": None, "spread": None}
        for i in range(1, 21)
    ])
    # 模擬 backfill helper(import 從 app.py)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "app", tmp_path.parent.parent.parent / "app.py"
    )
    # 直接用 backfill 邏輯:對 daily_prices < 15 的補
    fetch_calls = []
    monkeypatch.setattr(
        "src.data_fetcher.fetch_daily_price",
        lambda sid, s, e: fetch_calls.append(sid),
    )
    # 只 import 函式,不跑整個 app
    import sys
    sys.path.insert(0, str(tmp_path.parent.parent.parent))
    from app import _backfill_watchlist_history
    n = _backfill_watchlist_history(["FULL"], min_required=15)
    assert n == 0
    assert fetch_calls == []


def test_query_page_toggle_remove_existing(monkeypatch, tmp_path):
    """情境 3:對已關注的股票按 toggle → 取消關注。"""
    from streamlit.testing.v1 import AppTest
    from src import config as cfg
    cfg.DATABASE_PATH = str(tmp_path / "ux3.db")
    db.init_db()
    db.add_to_watchlist("2330")  # 預先加入

    at = AppTest.from_file("app.py").run(timeout=30)
    # 切到「🔍 個股」(預設首頁是 Dashboard,需手動切)
    at.session_state["nav_segmented"] = "🔍 個股"
    at.session_state["active_page"] = "🔍 個股"
    at.run(timeout=30)
    # 該顯示「已關注 2330」按鈕
    remove_btn = next(
        (b for b in at.button if "2330" in b.label and "已關注" in b.label),
        None,
    )
    assert remove_btn is not None, "已在 watchlist 該顯示移除 button"
    remove_btn.click().run(timeout=30)
    assert not db.is_in_watchlist("2330")
