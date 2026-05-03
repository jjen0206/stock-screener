"""端到端 smoke 測試:用 streamlit.testing.v1.AppTest 驅動真的 UI。

加這層的原因:單元測試會讓 caller/callee 簽名漂移悄悄過關(callee 改了
kwarg、單測對 callee 直接呼叫所以 OK,但 caller 實際傳的 kwarg 已經對不上,
單測抓不到、push 上去 UI 一點就炸)。這層是「整支 app 真的 boot 起來、頁面
真的切過去、按鈕真的按下去」的最後防線。

驗證範圍:
- 8 頁皆能 boot,初始 render 0 exception
- 我的關注:批量加入 form 渲染正常 + textarea 真的填 + 按鈕真的按 → 0 exception
- 短線:run_all_strategies 用 mock 灌假結果(避免依賴 SQLite 歷史) →
  render_picks_cards 真的跑 → 0 exception(專抓 caller/callee kwarg 漂移)
- 回測:backtest_short 真的被 _page_backtest 呼叫,而且收到 enabled_strategies
  kwarg(UI 多選 ≠ 預設 volume_kd 才會帶,所以選兩套確保走聚合路徑)→ 0 exception
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest


APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
PAGE_KEYS = [
    "🏠 首頁", "🔥 短線", "💎 長線", "📈 回測",
    "🔍 個股", "⭐ 關注", "📊 大盤", "⚙️ 系統", "⚙️ 設定",
]


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """每個 e2e 測試用乾淨 tmp DB,避免污染本機 cache.db / 觸發 GH push。

    config.DATABASE_PATH 改 tmp_path,並清掉 src.database 內部 path cache。
    GITHUB_PAT 確保不存在(snapshot dump 才不會 spawn push thread)。
    sys.modules 把 app 移除,確保 AppTest 跑出全新 module。
    """
    from src import config, database as db

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "e2e.db"))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()

    # AppTest 的 script runner 會重新執行 app.py module body,但若 app 已在
    # sys.modules 內,from-import 會走 module 快取版本,patch 不到位。從 cache
    # 移除 → AppTest 載入時會走「全新 import」,所有 from-import 都會重綁。
    sys.modules.pop("app", None)

    yield tmp_path

    db._reset_path_cache()  # type: ignore[attr-defined]


def _new_at(page: str | None = None) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    if page is not None:
        at.session_state["active_page"] = page
    return at


def _exc_msgs(at: AppTest) -> list[str]:
    return [str(e.value)[:300] for e in at.exception]


# ============================================================================
# Boot smoke:每個頁面 default render 0 exception
# ============================================================================

@pytest.mark.parametrize("page", PAGE_KEYS)
def test_each_page_boots_without_exception(isolated_db, page):
    at = _new_at(page)
    at.run()
    assert not at.exception, (
        f"page={page!r} raised: {_exc_msgs(at)}"
    )


# ============================================================================
# 我的關注:批量加入 form 真的能用
# ============================================================================

def test_watchlist_bulk_add_form_renders_and_submits(isolated_db):
    """填 textarea + 按 wl_bulk_btn → 後端真的有寫進 DB,UI 不炸。

    輸入混合分隔(逗號 + 換行)+ 一個無效格式,測:
    - 正規化 + split 邏輯走得通
    - bulk_add_to_watchlist 簽名 / 回傳格式跟 _bulk_add_form 期望一致
      (這就是 cloud traceback 點名的地方,單測單獨呼叫 bulk_add 過,但 UI 真按
      下去才能驗證 caller 取的 key 跟 callee 回的 dict shape 對得上)
    """
    from src import database as db

    at = _new_at("⭐ 關注")
    at.run()
    assert not at.exception, _exc_msgs(at)

    ta = at.text_area(key="wl_bulk_textarea")
    assert ta is not None, "wl_bulk_textarea 沒渲染"

    # bad!! / 12 走 regex invalid 路徑;9999X 雖是 4digit+letter 也合法,別當
    # invalid 用(會誤殺 1101A 之類的權證)
    ta.set_value("2330, 2317\nbad!!\n00878\n12").run()
    assert not at.exception, f"填 textarea 後炸: {_exc_msgs(at)}"

    at.button(key="wl_bulk_btn").click().run()
    assert not at.exception, f"按批量加入後炸: {_exc_msgs(at)}"

    # 後端真的寫進去 — 確認 caller 沒因為 key 名漂移把結果默默丟掉
    items = db.get_watchlist()
    sids = {it["stock_id"] for it in items}
    assert sids == {"2330", "2317", "00878"}, f"實際 watchlist={sids}"


def test_watchlist_bulk_add_handles_all_invalid_input(isolated_db):
    """全部無效格式 → 不炸 + DB 沒新增。"""
    from src import database as db

    at = _new_at("⭐ 關注")
    at.run()
    at.text_area(key="wl_bulk_textarea").set_value("abc, xyz, !!").run()
    at.button(key="wl_bulk_btn").click().run()
    assert not at.exception, _exc_msgs(at)
    assert db.get_watchlist() == []


# ============================================================================
# 短線頁:跑選股 → render_picks_cards 真的渲染 mock 結果
# ============================================================================

def test_short_screen_renders_picks_without_exception(
    isolated_db, monkeypatch
):
    """mock run_all_strategies 灌 2 檔假結果 → 點「執行選股」→ render_picks_cards
    必須無痛跑完。專抓 ui_cards.render_picks_cards 簽名跟 _page_short caller
    傳的 kwargs (show_add_button / button_key_prefix) 漂移。
    """
    from src import database as db, strategies

    # TW_TOP_50 universe 路徑不需歷史 → upsert 一筆讓 stocks 表非空
    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])

    fake_agg = {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD", "多頭排列"],
            "details": {
                "volume_kd": {
                    "stock_id": "2330", "name": "台積電",
                    "close": 600.0, "atr14": 12.0,
                },
            },
        },
        "2317": {
            "name": "鴻海",
            "signals": ["量價KD"],
            "details": {
                "volume_kd": {
                    "stock_id": "2317", "name": "鴻海",
                    "close": 200.0, "atr14": 5.0,
                },
            },
        },
    }
    monkeypatch.setattr(
        strategies, "run_all_strategies",
        lambda *a, **kw: fake_agg,
    )

    # compute_target_prices 也 mock — 它會去查 daily_prices,tmp DB 沒
    def fake_targets(sids, **kw):
        return {
            sid: {
                "target_low": 610.0, "target_high": 620.0,
                "stop_loss": 580.0, "risk_reward": 1.5,
            }
            for sid in sids
        }
    monkeypatch.setattr(
        strategies, "compute_target_prices", fake_targets,
    )

    at = _new_at("🔥 短線")
    at.run()
    # universe 預設「🎯 純股票」需歷史,改用「快速:50 檔大型股」走 hardcoded 路徑
    sb = at.selectbox[0]
    sb.set_value("快速:50 檔大型股").run()
    assert not at.exception, f"切 universe 後炸: {_exc_msgs(at)}"

    # 找「執行選股」按鈕(沒有 key,用 label 找)
    submit = next(b for b in at.button if b.label == "執行選股")
    submit.click().run()
    assert not at.exception, (
        f"執行選股後炸(render_picks_cards 漂移?): {_exc_msgs(at)}"
    )


def test_short_star_button_adds_clicked_stock_id_not_another(
    isolated_db, monkeypatch
):
    """**Index-mismatch regression** — 點 picks 第一張卡的 ⭐,實際加進
    watchlist 的必須是第一張的 stock_id,不是任何其他張。

    生產上回報過:138 picks,點第一張(01002T)的 ☆,卻加進 3680。推測是
    rerun 之間 row state 漂移 + button key/closure 綁錯股號。

    此測試灌 3 檔特意 sort 後第一張為 01002T(數字 < 字母,且 01 < 36 < 23
    開頭),點該檔 ⭐ → assert watchlist 只含 01002T。
    """
    from src import database as db, strategies

    # universe 路徑用「快速:50 檔大型股」(免歷史),但要 upsert 假股號
    # 進 stocks 表免得後續查 name 失敗
    db.upsert_stocks([
        {"stock_id": "01002T", "name": "土銀國泰R1", "market": "TW"},
        {"stock_id": "3680", "name": "家登", "market": "TW"},
        {"stock_id": "2330", "name": "台積電", "market": "TW"},
    ])

    # 全部相同 信號數 → 排序由 stock_id asc 決定:01002T < 2330 < 3680
    fake_agg = {
        sid: {
            "name": name,
            "signals": ["量價KD"],
            "details": {"volume_kd": {
                "stock_id": sid, "name": name,
                "close": 100.0, "atr14": 5.0,
            }},
        }
        for sid, name in [
            ("01002T", "土銀國泰R1"),
            ("3680", "家登"),
            ("2330", "台積電"),
        ]
    }
    monkeypatch.setattr(
        strategies, "run_all_strategies",
        lambda *a, **kw: fake_agg,
    )

    at = _new_at("🔥 短線")
    at.run()
    sb = at.selectbox[0]
    sb.set_value("快速:50 檔大型股").run()
    submit = next(b for b in at.button if b.label == "執行選股")
    submit.click().run()
    assert not at.exception, _exc_msgs(at)

    # 確認 picks 真的渲染出來(短線頁卡片帶 button_key_prefix="short")
    star_keys = [b.key for b in at.button if b.key and b.key.startswith("short_add_")]
    assert "short_add_01002T" in star_keys, (
        f"01002T 的 ⭐ 沒被渲染出來,可見的 star keys = {star_keys}"
    )

    # 點第一張卡(01002T)的 ⭐
    at.button(key="short_add_01002T").click().run()
    assert not at.exception, _exc_msgs(at)

    items = db.get_watchlist()
    sids = [it["stock_id"] for it in items]
    assert sids == ["01002T"], (
        f"點 01002T 的 ⭐ 應只加 01002T,但 watchlist={sids} "
        f"(若含 3680 / 2330 = index 漂移 bug)"
    )


def test_short_star_button_middle_card_binds_correctly(
    isolated_db, monkeypatch
):
    """點 picks **中間**那張卡的 ⭐ → 加進 watchlist 的必須是中間那張。

    補位 first-card 測試的盲點:若實作剛好對第一張正確、其他張漂移,
    上面的 test 抓不到。中間張(2330)被點 → 只能加 2330。
    """
    from src import database as db, strategies

    db.upsert_stocks([
        {"stock_id": "01002T", "name": "土銀國泰R1", "market": "TW"},
        {"stock_id": "2330", "name": "台積電", "market": "TW"},
        {"stock_id": "3680", "name": "家登", "market": "TW"},
    ])
    fake_agg = {
        sid: {
            "name": name,
            "signals": ["量價KD"],
            "details": {"volume_kd": {
                "stock_id": sid, "name": name,
                "close": 100.0, "atr14": 5.0,
            }},
        }
        for sid, name in [
            ("01002T", "土銀國泰R1"),
            ("2330", "台積電"),
            ("3680", "家登"),
        ]
    }
    monkeypatch.setattr(
        strategies, "run_all_strategies",
        lambda *a, **kw: fake_agg,
    )

    at = _new_at("🔥 短線")
    at.run()
    at.selectbox[0].set_value("快速:50 檔大型股").run()
    next(b for b in at.button if b.label == "執行選股").click().run()
    assert not at.exception, _exc_msgs(at)

    at.button(key="short_add_2330").click().run()
    assert not at.exception, _exc_msgs(at)

    sids = [it["stock_id"] for it in db.get_watchlist()]
    assert sids == ["2330"], (
        f"點 2330(中間)的 ⭐ 應只加 2330,但 watchlist={sids}"
    )


# ============================================================================
# 回測頁:backtest_short 必須收到 enabled_strategies kwarg
# ============================================================================

def test_backtest_passes_enabled_strategies_kwarg(isolated_db, monkeypatch):
    """選兩套策略 → 點「執行回測」→ backtest_short 必須以 enabled_strategies kwarg
    被呼叫(use_multi 路徑)+ result render 不炸。

    這是 cloud traceback 點名的「TypeError: backtest_short() got an unexpected
    keyword argument 'enabled_strategies'」對應 e2e 守門。
    """
    from src import backtester

    spy = MagicMock(return_value={
        "summary": {
            "trades": 1, "win_rate": 100.0, "avg_return": 5.0,
            "total_return": 5.0, "annual_return": 12.0,
            "sharpe": 1.5, "max_drawdown": 0.0, "hold_days": 5,
        },
        "trades": pd.DataFrame([{
            "buy_date": "2026-01-01", "stock_id": "2330", "name": "台積電",
            "buy_price": 600.0, "sell_date": "2026-01-08",
            "sell_price": 630.0, "return_pct": 5.0,
        }]),
        "equity_curve": pd.Series([0.0, 5.0], index=["2026-01-01", "2026-01-08"]),
    })
    monkeypatch.setattr(backtester, "backtest_short", spy)

    at = _new_at("📈 回測")
    at.run()
    assert not at.exception, _exc_msgs(at)

    # 多選兩套策略 → use_multi=True → 走 enabled_strategies kwarg 路徑
    bt_strats = at.multiselect(key="bt_strategies")
    bt_strats.set_value(["volume_kd", "ma_alignment"]).run()
    assert not at.exception, _exc_msgs(at)

    submit = next(b for b in at.button if b.label == "執行回測")
    submit.click().run()
    assert not at.exception, (
        f"執行回測後炸(backtest_short 簽名漂移?): {_exc_msgs(at)}"
    )

    spy.assert_called_once()
    _, kwargs = spy.call_args
    assert "enabled_strategies" in kwargs, (
        f"backtest_short 沒收到 enabled_strategies kwarg: kwargs={kwargs!r}"
    )
    assert set(kwargs["enabled_strategies"]) == {"volume_kd", "ma_alignment"}


# ============================================================================
# 日期 default:週末 / 假日打開時不能踩到非交易日(會 0 picks)
# ============================================================================

def _seed_latest_trading_day(latest_iso: str) -> None:
    """灌一筆 daily_prices 讓 MAX(date) = latest_iso(供 default date 取用)。"""
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    db.upsert_daily_prices([{
        "stock_id": "2330", "date": latest_iso,
        "open": 600.0, "high": 605.0, "low": 595.0, "close": 600.0,
        "volume": 10000,
    }])


def test_short_default_date_uses_latest_trading_day(isolated_db):
    """短線頁「選股日期」default 不該是 date.today()(週末/假日 → 0 picks),
    而該是 daily_prices 內最新一筆日期。
    """
    _seed_latest_trading_day("2026-04-30")

    at = _new_at("🔥 短線")
    at.run()
    assert not at.exception, _exc_msgs(at)

    di = at.date_input[0]
    assert di.label == "選股日期", (
        f"預期短線頁第一個 date_input label = 『選股日期』, got {di.label!r}"
    )
    assert di.value == date(2026, 4, 30), (
        f"短線『選股日期』default 應 = max(daily_prices.date) = 2026-04-30, "
        f"實際 = {di.value}"
    )


def test_backtest_default_dates_use_latest_trading_day(isolated_db):
    """回測頁自訂模式的「回測結束」default 應 = 最新交易日,「回測起始」應 = 最新 - 180 天。"""
    _seed_latest_trading_day("2026-04-30")

    at = _new_at("📈 回測")
    at.run()
    assert not at.exception, _exc_msgs(at)

    # 切到「自訂」才會出 date_input(其他 preset 走 metric)
    at.radio(key="bt_period_preset").set_value("自訂").run()
    assert not at.exception, _exc_msgs(at)

    bt_start = at.date_input(key="bt_start")
    bt_end = at.date_input(key="bt_end")
    assert bt_end.value == date(2026, 4, 30), (
        f"『回測結束』default 應 = 2026-04-30, 實際 = {bt_end.value}"
    )
    # 起始 = 最新 - 180 天 = 2025-11-01
    assert bt_start.value == date(2025, 11, 1), (
        f"『回測起始』default 應 = latest - 180d = 2025-11-01, "
        f"實際 = {bt_start.value}"
    )


def test_get_default_screen_date_falls_back_to_today(monkeypatch):
    """daily_prices 查不到資料時 fallback date.today()(新部署 / SQL 失敗都不該炸)。
    helper-level 直接 patch — 走 e2e 會被 boot 時 snapshot CSV 灌進來干擾。
    """
    import app as app_mod

    monkeypatch.setattr(app_mod, "_get_latest_data_date", lambda: None)
    assert app_mod._get_default_screen_date() == date.today()

    # 壞日期 string(理論上 SQL 不會回,但保險)也要 fallback
    monkeypatch.setattr(app_mod, "_get_latest_data_date", lambda: "not-a-date")
    assert app_mod._get_default_screen_date() == date.today()


# ============================================================================
# 個股頁:三大法人籌碼明細表
# ============================================================================

def _read_inst_df(at):
    """從 AppTest 拉出 institutional table 的底層 pandas DataFrame。
    現在用 st.table 渲染(避開 dataframe canvas 寬度 bug),所以走 at.table。
    Styler 物件透過 .data 拿原 DataFrame。
    """
    val = at.table[0].value
    return val.data if hasattr(val, "data") else val


def test_institutional_table_renders_with_data(isolated_db):
    """灌 5 行 institutional → _render_institutional_table 渲染表格,不顯示
    fallback 訊息,且 5 欄(日期/外資/投信/自營商/合計)都齊。"""
    from src import database as db

    db.upsert_institutional([
        {
            "stock_id": "2330", "date": f"2026-04-{30 - i:02d}",
            # 注意 schema 單位「股」,UI 顯示時除 1000 → 張
            "foreign_buy_sell": (-21_000_000 if i % 2 == 0 else 5_000_000),
            "trust_buy_sell": 600_000 * (1 if i % 2 == 0 else -1),
            "dealer_buy_sell": 100_000,
            "total_buy_sell": -20_000_000 + i * 500_000,
        }
        for i in range(5)
    ])

    def _harness():
        import app
        app._render_institutional_table("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    # 有資料 → expander 出現,info fallback 不應出現
    assert at.expander, "預期 institutional table 包在 expander 裡, 但沒看到 expander"
    assert any(
        "三大法人買賣超" in (e.label or "") for e in at.expander
    ), f"預期 expander label 含『三大法人買賣超』, 實際: {[e.label for e in at.expander]}"
    assert not any(
        "無三大法人籌碼資料" in str(i.value) for i in at.info
    ), "有資料時不該顯示 fallback info"

    # 5 欄都在,順序也對
    inst_df = _read_inst_df(at)
    assert list(inst_df.columns) == ["日期", "外資", "投信", "自營商", "合計"], (
        f"欄位順序 / 名稱不對: {list(inst_df.columns)}"
    )

    # 合計 = 三者和(每一列)
    expected_total = inst_df["外資"] + inst_df["投信"] + inst_df["自營商"]
    assert (inst_df["合計"] == expected_total).all(), (
        f"合計欄應等於三者和\n外資={inst_df['外資'].tolist()}\n"
        f"投信={inst_df['投信'].tolist()}\n自營商={inst_df['自營商'].tolist()}\n"
        f"合計={inst_df['合計'].tolist()}\nexpected={expected_total.tolist()}"
    )


def test_institutional_table_fallback_when_no_data(isolated_db):
    """無資料時顯示 fallback info,不渲染 expander/table。"""
    def _harness():
        import app
        app._render_institutional_table("9999")  # 不存在的股號

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    assert any(
        "無三大法人籌碼資料" in str(i.value) for i in at.info
    ), f"預期顯示 fallback info, 實際 info={[str(i.value) for i in at.info]}"
    assert not at.expander, (
        f"無資料時不該有 expander, 但見到: {[e.label for e in at.expander]}"
    )


def test_institutional_table_total_when_sqlite_total_is_zero(isolated_db):
    """SQLite total_buy_sell=0 但三欄非 0 時(資料不一致 / 舊資料),合計欄
    應顯示三者和,不是 SQLite 的 0。"""
    from src import database as db

    # 三欄共 +1500 張(1,500,000 股),但故意把 total_buy_sell 塞 0
    db.upsert_institutional([{
        "stock_id": "2330", "date": "2026-04-30",
        "foreign_buy_sell": 1_000_000,
        "trust_buy_sell": 300_000,
        "dealer_buy_sell": 200_000,
        "total_buy_sell": 0,  # 故意不一致
    }])

    def _harness():
        import app
        app._render_institutional_table("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    inst_df = _read_inst_df(at)
    assert inst_df["合計"].iloc[0] == 1500, (
        f"合計應 = 1000+300+200 = 1500, 實際 = {inst_df['合計'].iloc[0]}"
    )


def test_institutional_table_total_when_sqlite_total_is_null(isolated_db):
    """SQLite total_buy_sell=NULL 時不該炸,合計仍應顯示三者和。"""
    from src import database as db

    # upsert_institutional 會把 None 轉 0,要塞真 NULL 必須走 raw SQL
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO institutional "
            "(stock_id, date, foreign_buy_sell, trust_buy_sell, "
            "dealer_buy_sell, total_buy_sell) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            ("2330", "2026-04-30", 800_000, 100_000, 100_000),
        )
        conn.commit()

    def _harness():
        import app
        app._render_institutional_table("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    inst_df = _read_inst_df(at)
    assert inst_df["合計"].iloc[0] == 1000, (
        f"合計應 = 800+100+100 = 1000(忽略 NULL 的 total_buy_sell), "
        f"實際 = {inst_df['合計'].iloc[0]}"
    )


# ============================================================================
# 個股頁:主力進出 5/10 日累計表
# ============================================================================

def _seed_inst_cumulative(n_days: int = 20, base_close: float = 100.0) -> None:
    """灌 n_days 筆 daily_prices + institutional(stock=2330)。
    每天 inst_total = (i+1) * 1000 股 = (i+1) 張(讓累計值好計算驗證)。
    每天 close 漲 1 元。
    """
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    prices = []
    insts = []
    # 從 day 1 (i=0) 到 day n_days(用 ISO 連續日,週末週日 SQLite 不在意)
    base_day = 1
    for i in range(n_days):
        d = f"2026-04-{base_day + i:02d}"
        prices.append({
            "stock_id": "2330", "date": d,
            "open": base_close + i, "high": base_close + i + 0.5,
            "low": base_close + i - 0.5, "close": base_close + i,
            "volume": 10000,
        })
        insts.append({
            "stock_id": "2330", "date": d,
            # i+1 張 = (i+1)*1000 股, 全進外資(其他 0),inst_total = i+1 張
            "foreign_buy_sell": (i + 1) * 1000,
            "trust_buy_sell": 0, "dealer_buy_sell": 0,
            "total_buy_sell": (i + 1) * 1000,
        })
    db.upsert_daily_prices(prices)
    db.upsert_institutional(insts)


def _read_cum_df(at):
    """個股頁第 2 個 table = 累計表(第 1 個是三大法人表)。
    helper 是被獨立呼叫的(harness 只跑一個 helper),所以 at.table[0] 就是它。
    """
    val = at.table[0].value
    return val.data if hasattr(val, "data") else val


def test_cumulative_table_renders_with_data(isolated_db):
    """灌 20 天 → 累計表有 5 欄 + 10 列(倒序)+ rolling sum 數值正確。"""
    _seed_inst_cumulative(n_days=20)

    def _harness():
        import app
        app._render_institutional_cumulative_table("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    assert at.expander, "累計表應包在 expander"
    assert any(
        "主力進出累計" in (e.label or "") for e in at.expander
    ), f"expander label 不對: {[e.label for e in at.expander]}"

    cum_df = _read_cum_df(at)
    assert list(cum_df.columns) == [
        "日期", "5 日累計", "10 日累計", "收盤價", "漲跌幅",
    ], f"欄序不對: {list(cum_df.columns)}"
    assert len(cum_df) == 10, f"預期 10 列, 實際 {len(cum_df)}"

    # 倒序:第 1 列 = day 20 (2026-04-20), 最後一列 = day 11 (2026-04-11)
    assert cum_df["日期"].iloc[0] == "2026-04-20"
    assert cum_df["日期"].iloc[-1] == "2026-04-11"

    # day 20 的 5 日累計 = (16+17+18+19+20) = 90 張
    # day 20 的 10 日累計 = sum(11..20) = 155 張
    assert cum_df["5 日累計"].iloc[0] == 90, (
        f"day 20 的 5 日累計應 = 16+17+18+19+20 = 90, "
        f"實際 = {cum_df['5 日累計'].iloc[0]}"
    )
    assert cum_df["10 日累計"].iloc[0] == 155, (
        f"day 20 的 10 日累計應 = sum(11..20) = 155, "
        f"實際 = {cum_df['10 日累計'].iloc[0]}"
    )

    # 收盤價 day 20 = 100 + 19 = 119(base 100, day 1 i=0 → close=100)
    assert abs(float(cum_df["收盤價"].iloc[0]) - 119.0) < 1e-6

    # 漲跌幅 day 20 = (119 - 118) / 118 * 100 ≈ 0.847%
    pct_day20 = float(cum_df["漲跌幅"].iloc[0])
    assert abs(pct_day20 - (1.0 / 118.0 * 100)) < 1e-6, (
        f"漲跌幅 day 20 應 ≈ 0.847%, 實際 = {pct_day20}"
    )


def test_cumulative_table_fallback_when_no_prices(isolated_db):
    """無 daily_prices 時顯示 fallback,不渲染 expander。"""
    def _harness():
        import app
        app._render_institutional_cumulative_table("9999")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    assert any(
        "無主力進出累計資料" in str(i.value) for i in at.info
    ), f"預期 fallback info, 實際: {[str(i.value) for i in at.info]}"
    assert not at.expander


def test_cumulative_table_fallback_when_institutional_all_null(isolated_db):
    """daily_prices 有但 institutional 完全沒(LEFT JOIN 三欄全 NULL)
    → 走 fallback,**不渲染**累計全 0 的誤導表格(例:01002T 等覆蓋不足個股)。
    """
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    db.upsert_daily_prices([
        {
            "stock_id": "2330", "date": f"2026-04-{1 + i:02d}",
            "open": 100.0 + i, "high": 100.5 + i, "low": 99.5 + i,
            "close": 100.0 + i, "volume": 10000,
        }
        for i in range(20)
    ])
    # 故意不灌 institutional

    def _harness():
        import app
        app._render_institutional_cumulative_table("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    assert any(
        "無主力進出累計資料" in str(i.value) for i in at.info
    ), f"預期 fallback info, 實際: {[str(i.value) for i in at.info]}"
    assert not at.expander, (
        f"institutional 全 NULL 時不該渲染累計表 expander, "
        f"但見到: {[e.label for e in at.expander]}"
    )
    assert not at.table, "institutional 全 NULL 時不該渲染任何 table"


# ============================================================================
# 個股頁:關鍵價位(壓力 / 回檔 / 支撐)
# ============================================================================

def _seed_daily_prices_only(n_days: int, base: float = 100.0) -> None:
    """灌 n_days 筆 daily_prices,close 在 base 附近震盪(讓 BB std > 0)。"""
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    rows = []
    for i in range(n_days):
        offset = (i % 5) - 2  # -2 -1 0 1 2 循環,close 震盪
        c = base + offset
        rows.append({
            "stock_id": "2330", "date": f"2026-04-{1 + i:02d}",
            "open": c - 0.5, "high": c + 0.5, "low": c - 1.0, "close": c,
            "volume": 10000,
        })
    db.upsert_daily_prices(rows)


def test_key_levels_renders_with_sufficient_history(isolated_db):
    """灌 25 天 → 三個區間都算出來並渲染(壓力 / 回檔 / 支撐)。"""
    _seed_daily_prices_only(n_days=25)

    def _harness():
        import app
        app._render_key_levels("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "壓力區" in md_text, f"期望渲染壓力區, 實際 markdown={md_text!r}"
    assert "回檔區" in md_text
    assert "支撐區" in md_text
    # 不該顯示 fallback info
    assert not any(
        "歷史不足" in str(i.value) for i in at.info
    ), f"資料夠時不該顯示 fallback, 實際 info={[str(i.value) for i in at.info]}"


def test_key_levels_fallback_when_history_insufficient(isolated_db):
    """灌 15 天(< 20)→ 顯示『歷史不足』fallback,不渲染區間。"""
    _seed_daily_prices_only(n_days=15)

    def _harness():
        import app
        app._render_key_levels("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    assert any(
        "歷史不足" in str(i.value) for i in at.info
    ), f"預期 fallback info, 實際: {[str(i.value) for i in at.info]}"
    md_text = "\n".join(m.value for m in at.markdown)
    assert "壓力區" not in md_text, "歷史不足時不該渲染壓力區"


def test_key_levels_fallback_when_no_data(isolated_db):
    """完全沒 daily_prices(不存在的股號)→ fallback 訊息含『目前 0 天』。"""
    def _harness():
        import app
        app._render_key_levels("9999")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    info_text = "\n".join(str(i.value) for i in at.info)
    assert "歷史不足" in info_text
    assert "0 天" in info_text, f"預期 fallback 標明 0 天, 實際: {info_text!r}"


# ============================================================================
# 個股頁:技術分析總覽(7 項 rule-based 文字解讀)
# ============================================================================

def _seed_trend_prices(direction: str, n_days: int = 70) -> None:
    """灌 n_days 天 OHLC,direction='up' 線性漲、'down' 線性跌。
    確保 MA5 < MA20 < MA60(漲時反過來)+ close 偏離中軌 → 趨勢明顯。
    """
    from datetime import date as _date, timedelta as _td
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    rows = []
    start = _date(2026, 1, 1)
    base = 100.0
    for i in range(n_days):
        d = start + _td(days=i)
        c = base + i if direction == "up" else base + (n_days - 1 - i)
        rows.append({
            "stock_id": "2330", "date": d.isoformat(),
            "open": c - 0.5, "high": c + 0.5, "low": c - 1.0, "close": c,
            "volume": 10000,
        })
    db.upsert_daily_prices(rows)


def test_technical_summary_renders_uptrend(isolated_db):
    """灌 70 天線性上漲 → 期望出現「多頭趨勢」「多頭排列」。"""
    _seed_trend_prices(direction="up", n_days=70)

    def _harness():
        import app
        app._render_technical_summary("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "技術分析總覽" in md_text
    assert "多頭趨勢" in md_text, f"線性漲應判多頭, 實際: {md_text!r}"
    assert "多頭排列" in md_text, f"線性漲應判多頭排列, 實際: {md_text!r}"
    # 不該顯示 fallback
    assert not any("歷史不足" in str(i.value) for i in at.info)


def test_technical_summary_renders_downtrend(isolated_db):
    """灌 70 天線性下跌 → 期望出現「空頭趨勢」「空頭排列」。"""
    _seed_trend_prices(direction="down", n_days=70)

    def _harness():
        import app
        app._render_technical_summary("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "空頭趨勢" in md_text, f"線性跌應判空頭, 實際: {md_text!r}"
    assert "空頭排列" in md_text


def test_technical_summary_fallback_when_history_insufficient(isolated_db):
    """灌 30 天(< 60)→ fallback「歷史不足」+ 不渲染總覽 markdown。"""
    _seed_trend_prices(direction="up", n_days=30)

    def _harness():
        import app
        app._render_technical_summary("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    info_text = "\n".join(str(i.value) for i in at.info)
    assert "歷史不足" in info_text
    md_text = "\n".join(m.value for m in at.markdown)
    assert "趨勢分析" not in md_text, "歷史不足時不該渲染總覽 markdown"


# ============================================================================
# 個股頁:操作建議區(短/中/長線進場目標停損 + 操作核心)
# ============================================================================

def test_action_suggestion_renders_uptrend(isolated_db):
    """灌 70 天線性漲 → 操作建議完整渲染:短/中/長線各區間 + 多頭操作核心。"""
    _seed_trend_prices(direction="up", n_days=70)

    def _harness():
        import app
        app._render_action_suggestion("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "操作建議" in md_text
    assert "短線" in md_text
    assert "中線" in md_text
    assert "長線" in md_text
    assert "進場區間" in md_text
    assert "目標" in md_text
    assert "停損" in md_text
    assert "風險報酬" in md_text

    # 操作核心走 st.warning,不在 markdown text
    warn_text = "\n".join(str(w.value) for w in at.warning)
    assert "操作核心" in warn_text
    assert "多頭趨勢" in warn_text, f"線性漲應判多頭, 實際: {warn_text!r}"

    # 不該顯示 fallback
    assert not any("歷史不足" in str(i.value) for i in at.info)


def test_action_suggestion_renders_downtrend(isolated_db):
    """灌 70 天線性跌 → 操作核心應含空頭描述。"""
    _seed_trend_prices(direction="down", n_days=70)

    def _harness():
        import app
        app._render_action_suggestion("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    warn_text = "\n".join(str(w.value) for w in at.warning)
    assert "空頭趨勢" in warn_text, f"線性跌應判空頭, 實際: {warn_text!r}"


def test_action_suggestion_fallback_when_history_insufficient(isolated_db):
    """歷史不足(30 天 < 60 天 MA60 門檻)→ fallback「歷史不足」+ 不渲染建議。"""
    _seed_trend_prices(direction="up", n_days=30)

    def _harness():
        import app
        app._render_action_suggestion("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    info_text = "\n".join(str(i.value) for i in at.info)
    assert "歷史不足" in info_text
    md_text = "\n".join(m.value for m in at.markdown)
    assert "短線" not in md_text, "歷史不足時不該渲染短線建議"


# ============================================================================
# 個股頁:多週期趨勢分析(日 K + 週 K 並排)
# ============================================================================

def test_multi_timeframe_renders_uptrend(isolated_db):
    """灌 200 天線性漲 → 日 K 跟週 K 都判多頭趨勢 + 多頭排列。"""
    _seed_trend_prices(direction="up", n_days=200)

    def _harness():
        import app
        app._render_multi_timeframe("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "多週期趨勢分析" in md_text
    assert "日 K" in md_text
    assert "週 K" in md_text
    # 線性漲 → 兩個週期都應判多頭
    assert md_text.count("多頭趨勢") >= 2, (
        f"線性漲應日 K + 週 K 都判多頭, 實際 markdown=\n{md_text}"
    )
    assert md_text.count("多頭排列") >= 2

    assert not any("歷史不足" in str(i.value) for i in at.info)


def test_multi_timeframe_renders_downtrend(isolated_db):
    """灌 200 天線性跌 → 日 K 跟週 K 都判空頭。"""
    _seed_trend_prices(direction="down", n_days=200)

    def _harness():
        import app
        app._render_multi_timeframe("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert md_text.count("空頭趨勢") >= 2, (
        f"線性跌應兩週期都判空頭, 實際 markdown=\n{md_text}"
    )


def test_multi_timeframe_fallback_when_history_insufficient(isolated_db):
    """灌 50 天(< 100 天門檻)→ fallback,不渲染雙週期區塊。"""
    _seed_trend_prices(direction="up", n_days=50)

    def _harness():
        import app
        app._render_multi_timeframe("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    info_text = "\n".join(str(i.value) for i in at.info)
    assert "歷史不足" in info_text
    md_text = "\n".join(m.value for m in at.markdown)
    assert "日 K" not in md_text, "歷史不足時不該渲染雙週期區塊"


# ============================================================================
# 個股頁:型態分析(W 底 / M 頭)
# ============================================================================

def _seed_pattern_prices(closes: list[float]) -> None:
    """灌 OHLC by close 序列,OHLC 自動由 close ± 0.5 推算。"""
    from datetime import date as _date, timedelta as _td
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    rows = []
    start = _date(2026, 1, 1)
    for i, c in enumerate(closes):
        d = start + _td(days=i)
        rows.append({
            "stock_id": "2330", "date": d.isoformat(),
            "open": c - 0.3, "high": c + 0.5, "low": c - 0.5, "close": c,
            "volume": 10000,
        })
    db.upsert_daily_prices(rows)


def _make_w_bottom_closes() -> list[float]:
    """造一個明顯 W 底序列(70 天):
    平盤(100)→ 大跌到 79(left low)→ 反彈到 96.5(mid high)→ 再跌到 78.5
    (right low,接近 left)→ 突破 96.5 上漲到 109.5。
    """
    closes = []
    for i in range(20):
        closes.append(100.0 + (i % 3) * 0.5 - 0.5)
    for i in range(6):  # 100 → 79
        closes.append(100.0 - (i + 1) * 3.5)
    for i in range(7):  # 79 → 96.5(中高)
        closes.append(79.0 + (i + 1) * 2.5)
    for i in range(6):  # 96.5 → 78.5(右低)
        closes.append(96.5 - (i + 1) * 3.0)
    for i in range(31):  # 78.5 → 109.5(突破中高 96.5)
        closes.append(78.5 + (i + 1) * 1.0)
    return closes


def _make_m_top_closes() -> list[float]:
    """M 頭鏡像:平盤(100)→ 漲到 121(left high)→ 回檔 103.5(mid low)→
    再漲到 121.5(right high)→ 跌破 103.5 到 90.5。
    """
    closes = []
    for i in range(20):
        closes.append(100.0 + (i % 3) * 0.5 - 0.5)
    for i in range(6):
        closes.append(100.0 + (i + 1) * 3.5)
    for i in range(7):
        closes.append(121.0 - (i + 1) * 2.5)
    for i in range(6):
        closes.append(103.5 + (i + 1) * 3.0)
    for i in range(31):
        closes.append(121.5 - (i + 1) * 1.0)
    return closes


def test_pattern_w_bottom_detected(isolated_db):
    """灌明顯 W 底序列 → W 底狀態應為已形成 / 形成中,評分 ≥ 50。"""
    _seed_pattern_prices(_make_w_bottom_closes())

    def _harness():
        import app
        app._render_pattern_analysis("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "型態分析" in md_text
    assert "W 底分析" in md_text
    # 明顯 W 底:兩低相似 + 大反彈 + 突破 → 預期已形成
    assert "已形成" in md_text or "形成中" in md_text, (
        f"明顯 W 底應判已形成 / 形成中, 實際:\n{md_text}"
    )
    # 應包含 W 底特徵欄位
    assert "左低" in md_text
    assert "中高" in md_text
    assert "右低" in md_text


def test_pattern_m_top_detected(isolated_db):
    """灌明顯 M 頭序列 → M 頭狀態應為已形成 / 形成中。"""
    _seed_pattern_prices(_make_m_top_closes())

    def _harness():
        import app
        app._render_pattern_analysis("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "M 頭分析" in md_text
    # M 頭區塊裡應該見到「已形成」或「形成中」(W 底區塊應該未抓到)
    # 簡單驗:整段 markdown 含「左高」「中低」「右高」欄位 → M 頭區塊有 detail
    assert "左高" in md_text, f"M 頭未渲染欄位, markdown=\n{md_text}"
    assert "中低" in md_text
    assert "右高" in md_text


def test_pattern_no_pattern_in_flat_data(isolated_db):
    """灌 70 天平盤(close 在 100 ± 1 微震盪)→ 兩種型態都未發現 / 未形成。"""
    closes = [100.0 + (i % 5) * 0.3 - 0.6 for i in range(70)]
    _seed_pattern_prices(closes)

    def _harness():
        import app
        app._render_pattern_analysis("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    # 平盤資料 → 不該判已形成(評分組件全低或 pivot 抓不到)
    assert "已形成" not in md_text, (
        f"平盤資料不該判已形成, markdown=\n{md_text}"
    )


def test_pattern_fallback_when_history_insufficient(isolated_db):
    """< 60 天 → fallback「歷史不足」,不渲染型態區塊。"""
    closes = [100.0 + i * 0.5 for i in range(40)]
    _seed_pattern_prices(closes)

    def _harness():
        import app
        app._render_pattern_analysis("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    info_text = "\n".join(str(i.value) for i in at.info)
    assert "歷史不足" in info_text
    md_text = "\n".join(m.value for m in at.markdown)
    assert "W 底分析" not in md_text


# ============================================================================
# 個股頁:主力燈號(出貨 / 吸貨判斷)
# ============================================================================

def _seed_distribution_scenario() -> None:
    """灌出貨情境:close 線性漲到高檔 + institutional 持續賣超。
    n20=−100,000 張 → 強度 +3。Close 在頂端 → is_high True。
    Vol 平 → vol_text=量平 → status='默默出貨'。
    """
    from datetime import date as _date, timedelta as _td
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    rows_p, rows_i = [], []
    start = _date(2026, 1, 1)
    for i in range(70):
        d = start + _td(days=i)
        c = 100.0 + i  # 線性漲 100 → 169
        rows_p.append({
            "stock_id": "2330", "date": d.isoformat(),
            "open": c - 0.3, "high": c + 0.5, "low": c - 0.5, "close": c,
            "volume": 10000,
        })
        rows_i.append({
            "stock_id": "2330", "date": d.isoformat(),
            "foreign_buy_sell": -5_000_000,  # 賣 5,000 張
            "trust_buy_sell": 0, "dealer_buy_sell": 0,
            "total_buy_sell": -5_000_000,
        })
    db.upsert_daily_prices(rows_p)
    db.upsert_institutional(rows_i)


def _seed_accumulation_scenario() -> None:
    """灌吸貨情境:close 線性跌到低檔 + institutional 持續買超。
    n20=+100,000 張 → 強度 +3。Close 在底部 → is_low True。
    Vol 平 → vol_text=量平 → status='默默吸貨'。
    """
    from datetime import date as _date, timedelta as _td
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    rows_p, rows_i = [], []
    start = _date(2026, 1, 1)
    for i in range(70):
        d = start + _td(days=i)
        c = 200.0 - i  # 線性跌 200 → 131
        rows_p.append({
            "stock_id": "2330", "date": d.isoformat(),
            "open": c - 0.3, "high": c + 0.5, "low": c - 0.5, "close": c,
            "volume": 10000,
        })
        rows_i.append({
            "stock_id": "2330", "date": d.isoformat(),
            "foreign_buy_sell": 5_000_000,  # 買 5,000 張
            "trust_buy_sell": 0, "dealer_buy_sell": 0,
            "total_buy_sell": 5_000_000,
        })
    db.upsert_daily_prices(rows_p)
    db.upsert_institutional(rows_i)


def test_main_force_signal_distribution(isolated_db):
    """灌出貨情境(法人賣超 + close 高檔)→ status 含「出貨」+ 強度 ≥ 1。"""
    _seed_distribution_scenario()

    def _harness():
        import app
        app._render_main_force_signal("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "主力燈號" in md_text
    assert "出貨" in md_text, (
        f"法人賣超情境應判出貨, 實際:\n{md_text}"
    )
    # 強度 +3(n20=−100k 張)+ 量價一致 → ≥3 顆 emoji
    assert md_text.count("🟢") >= 3, (
        f"強度應 ≥ 3, 實際 markdown=\n{md_text}"
    )

    info_text = "\n".join(str(i.value) for i in at.info)
    assert "解讀" in info_text


def test_main_force_signal_accumulation(isolated_db):
    """灌吸貨情境(法人買超 + close 低檔)→ status 含「吸貨」。"""
    _seed_accumulation_scenario()

    def _harness():
        import app
        app._render_main_force_signal("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "吸貨" in md_text, (
        f"法人買超情境應判吸貨, 實際:\n{md_text}"
    )


def test_main_force_signal_fallback_no_institutional(isolated_db):
    """daily_prices 有但 institutional 完全沒(同 01002T 情境)→ fallback。"""
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    db.upsert_daily_prices([
        {
            "stock_id": "2330", "date": f"2026-04-{1 + i:02d}",
            "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
            "volume": 10000,
        }
        for i in range(25)
    ])
    # 故意不灌 institutional

    def _harness():
        import app
        app._render_main_force_signal("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    info_text = "\n".join(str(i.value) for i in at.info)
    assert "無法人籌碼資料" in info_text or "歷史不足" in info_text
    md_text = "\n".join(m.value for m in at.markdown)
    assert "強度" not in md_text, "fallback 時不該渲染主力燈號區塊"


def test_main_force_signal_fallback_history_insufficient(isolated_db):
    """< 20 天 → fallback。"""
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    db.upsert_daily_prices([
        {
            "stock_id": "2330", "date": f"2026-04-{1 + i:02d}",
            "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
            "volume": 10000,
        }
        for i in range(15)
    ])

    def _harness():
        import app
        app._render_main_force_signal("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    info_text = "\n".join(str(i.value) for i in at.info)
    assert "歷史不足" in info_text


# ============================================================================
# 推薦卡:詳細分析 expander(reuse 4 個 individual_sections helper)
# ============================================================================

def test_pick_card_expander_renders_4_sections(isolated_db):
    """show_add_button=True 的推薦卡有「📊 詳細分析」expander,展開時 4 個
    section 都渲染(主力燈號 / 技術分析總覽 / 關鍵價位 / 操作建議)。
    """
    # 灌足夠歷史(70 天 線性漲)+ institutional 讓 4 個 helper 都不走 fallback
    _seed_distribution_scenario()  # 含 70 天 daily + institutional

    def _harness():
        from src.ui_cards import render_pick_card
        render_pick_card(
            {"stock_id": "2330", "name": "台積電", "close": 169.0},
            show_add_button=True,
            button_key_prefix="testpick",
        )

    at = AppTest.from_function(_harness, default_timeout=15)
    at.run()
    assert not at.exception, _exc_msgs(at)

    # 卡片本身渲染
    md_text = "\n".join(m.value for m in at.markdown)
    assert "2330" in md_text and "台積電" in md_text

    # 「📊 詳細分析」expander 存在
    assert any(
        "詳細分析" in (e.label or "") for e in at.expander
    ), f"應有「詳細分析」expander, 實際: {[e.label for e in at.expander]}"

    # 展開後 4 個 section 的標題 / 關鍵字都出現在 markdown
    # (AppTest 預設會 render expander 內容,即使 expanded=False)
    assert "主力燈號" in md_text
    assert "技術分析總覽" in md_text
    assert "關鍵價位" in md_text
    assert "操作建議" in md_text


def test_pick_card_expander_renders_for_watchlist(isolated_db):
    """show_add_button=False(watchlist 卡)也應該渲染詳細分析 expander。
    watchlist 不渲染 ☆ 按鈕(已關注不需再加),但 4 個 section 一樣有用。
    """
    _seed_distribution_scenario()

    def _harness():
        from src.ui_cards import render_pick_card
        render_pick_card(
            {"stock_id": "2330", "name": "台積電", "close": 169.0},
            show_add_button=False,
        )

    at = AppTest.from_function(_harness, default_timeout=15)
    at.run()
    assert not at.exception, _exc_msgs(at)

    # watchlist 卡也有 expander
    assert any(
        "詳細分析" in (e.label or "") for e in at.expander
    ), f"watchlist 卡也應有 expander, 實際: {[e.label for e in at.expander]}"
    md_text = "\n".join(m.value for m in at.markdown)
    assert "主力燈號" in md_text
    assert "操作建議" in md_text

    # 但不該有 ☆ 加入按鈕(show_add_button=False)
    btn_labels = [b.label for b in at.button]
    assert not any(
        "加入關注" in (lbl or "") for lbl in btn_labels
    ), f"watchlist 卡不該有「加入關注」按鈕, 實際 buttons: {btn_labels}"


# ============================================================================
# 系統健康監控頁
# ============================================================================

def test_short_page_advanced_expander_has_bias_sliders(isolated_db):
    """短線頁進階參數 expander 含策略 3 的 3 個 slider + 預設值對應 DEFAULT_BIAS_PARAMS。"""
    from src.strategies import DEFAULT_BIAS_PARAMS

    at = _new_at("🔥 短線")
    at.run()
    assert not at.exception, _exc_msgs(at)

    # 三個 bias slider key 都該存在
    bias_low = at.slider(key="short_bias_low")
    bias_high = at.slider(key="short_bias_high")
    vol_ratio = at.slider(key="short_vol_ratio")
    assert bias_low.value == float(DEFAULT_BIAS_PARAMS["bias_low"])
    assert bias_high.value == float(DEFAULT_BIAS_PARAMS["bias_high"])
    assert vol_ratio.value == float(DEFAULT_BIAS_PARAMS["vol_ratio_min"])


def test_short_page_reset_button_resets_all_widgets(isolated_db):
    """改多個 widget 後按重設 → 所有 widget value + session_state[key] 都回預設。

    雲端發現過 bug:widget 同時帶 `value=` 跟 `key=` 時,callback pop
    session_state 不刷新 widget(F5 才生效)。改 key-only + callback 直接
    set value 後,widget 下次 render 立即從 session_state 拿到 default。
    這個 test 同時 assert widget value 跟 session_state[key],兩層都對才算過。
    """
    from src.strategies import DEFAULT_BIAS_PARAMS
    from src.screener_short import DEFAULT_SHORT_PARAMS

    at = _new_at("🔥 短線")
    at.run()
    assert not at.exception, _exc_msgs(at)

    # 改 3 個 widget 到非預設值
    at.slider(key="short_bias_low").set_value(-12.0).run()
    at.slider(key="short_bias_high").set_value(8.0).run()
    at.number_input(key="short_vol_mult").set_value(3.5).run()
    assert at.slider(key="short_bias_low").value == -12.0
    assert at.session_state["short_bias_low"] == -12.0
    assert at.session_state["short_vol_mult"] == 3.5

    # 點重設按鈕(on_click=_reset_short_params 設 default)
    at.button(key="short_reset_params").click().run()
    assert not at.exception, _exc_msgs(at)

    # 兩層都該回預設值:session_state(SoT) + widget value(UI)
    expected = {
        "short_bias_low": float(DEFAULT_BIAS_PARAMS["bias_low"]),
        "short_bias_high": float(DEFAULT_BIAS_PARAMS["bias_high"]),
        "short_vol_ratio": float(DEFAULT_BIAS_PARAMS["vol_ratio_min"]),
        "short_vol_mult": float(DEFAULT_SHORT_PARAMS["volume_multiplier"]),
        "short_kd_low": float(DEFAULT_SHORT_PARAMS["kd_threshold_low"]),
        "short_inst_days": int(DEFAULT_SHORT_PARAMS["inst_buy_days"]),
    }
    for k, v in expected.items():
        assert at.session_state[k] == v, (
            f"session_state[{k}] 應 = {v}, 實際 {at.session_state[k]}"
        )
    # widget value 也應該對齊(雲端 bug 的真正 reproducer:widget 不刷新)
    assert at.slider(key="short_bias_low").value == expected["short_bias_low"]
    assert at.slider(key="short_bias_high").value == expected["short_bias_high"]
    assert at.number_input(key="short_vol_mult").value == expected["short_vol_mult"]


def test_system_health_renders_all_sections(isolated_db):
    """灌假 daily_prices / institutional → 系統頁 5 個 section 都渲染、不炸。"""
    _seed_distribution_scenario()  # 70 天 daily + institutional

    def _harness():
        import app
        app._render_system_health()

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    for section in [
        "資料覆蓋率", "上次更新",
        "Backfill workflow", "API Token", "SQLite 資料庫",
    ]:
        assert section in md_text, (
            f"系統頁缺 section「{section}」, 實際:\n{md_text}"
        )

    # 應該至少有一個 dataframe(更新時間 / token / SQLite tables)
    assert len(at.dataframe) >= 2, (
        f"預期 ≥2 個 dataframe, 實際 {len(at.dataframe)}"
    )
