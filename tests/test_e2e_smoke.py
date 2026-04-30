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
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest


APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
PAGE_KEYS = [
    "🏠 首頁", "🔥 短線", "💎 長線", "📈 回測",
    "🔍 個股", "⭐ 關注", "📊 大盤", "⚙️ 設定",
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
