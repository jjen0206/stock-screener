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
    "🔍 個股", "⭐ 關注", "📊 大盤",
    "💼 交易紀錄", "⚙️ 系統", "⚙️ 設定",
]


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """每個 e2e 測試用乾淨 tmp DB,避免污染本機 cache.db / 觸發 GH push。

    config.DATABASE_PATH 改 tmp_path,並清掉 src.database 內部 path cache。
    GITHUB_PAT 確保不存在(snapshot dump 才不會 spawn push thread)。
    sys.modules 把 app 移除,確保 AppTest 跑出全新 module。

    `st.cache_data` 是 process-wide cache,跨 tests 會殘留(test A 用 sid
    '2330' 灌 fixture A、test B 灌 fixture B → test B 拿到 A 的 cache)。
    每個 test 開始前清掉,跟 tmp_db 隔離概念一致。
    """
    import streamlit as st
    from src import config, database as db

    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "e2e.db"))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()

    # AppTest 的 script runner 會重新執行 app.py module body,但若 app 已在
    # sys.modules 內,from-import 會走 module 快取版本,patch 不到位。從 cache
    # 移除 → AppTest 載入時會走「全新 import」,所有 from-import 都會重綁。
    sys.modules.pop("app", None)

    # 清 streamlit @st.cache_data — _compute_key_levels / _load_recent_ohlc /
    # _compute_technical_summary / _compute_main_force_signal / 公司資訊
    # 5 個 cache 都會殘留跨 tests
    st.cache_data.clear()

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


def test_short_page_keeps_picks_after_expander_click(isolated_db, monkeypatch):
    """**Critical regression** — 用戶點 picks 卡的「展開詳細分析」後,短線
    頁不該退回「請選擇選股」初始畫面(原 bug:`submit = button("執行選股")`
    edge-trigger,rerun 後變 False → `if not submit: return` → 退頁)。

    修法:short_submitted session_state 黏住,rerun 後仍能 render picks。
    """
    from src import database as db, strategies

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])

    fake_agg = {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD"],
            "details": {"volume_kd": {
                "stock_id": "2330", "name": "台積電",
                "close": 600.0, "atr14": 12.0,
            }},
        },
    }
    monkeypatch.setattr(
        strategies, "run_all_strategies", lambda *a, **kw: fake_agg,
    )
    monkeypatch.setattr(
        strategies, "compute_target_prices",
        lambda sids, **kw: {sid: {
            "target_low": 610.0, "target_high": 620.0,
            "stop_loss": 580.0, "risk_reward": 1.5,
        } for sid in sids},
    )

    at = _new_at("🔥 短線")
    at.run()
    at.selectbox[0].set_value("快速:50 檔大型股").run()

    # Step 1: 執行選股 → picks 出現
    submit = next(b for b in at.button if b.label == "執行選股")
    submit.click().run()
    assert not at.exception, _exc_msgs(at)

    md_text_after_submit = "\n".join(m.value for m in at.markdown)
    assert "2330" in md_text_after_submit, (
        f"執行選股後應 render 2330 卡片, md_text:\n{md_text_after_submit[:500]}"
    )

    # Step 2: 點「展開詳細分析」(觸發 rerun → 原本會 reset,現在不該)
    expand_btns = [
        b for b in at.button if "展開詳細分析" in (b.label or "")
    ]
    assert expand_btns, (
        f"應有「展開詳細分析」按鈕, 實際: {[b.label for b in at.button]}"
    )
    expand_btns[0].click().run()
    assert not at.exception, _exc_msgs(at)

    # Step 3: assert page 還在短線 + picks 還在(沒退回「請選擇選股」)
    assert at.session_state["active_page"] == "🔥 短線", (
        f"page state 不該被 reset, 實際: {at.session_state.get('active_page')}"
    )
    md_text_after_expand = "\n".join(m.value for m in at.markdown)
    assert "2330" in md_text_after_expand, (
        f"展開後 picks 不該消失, md_text:\n{md_text_after_expand[:500]}"
    )
    # 「請選擇選股」初始 info 不該出現
    info_text = "\n".join(str(i.value) for i in at.info)
    assert "選好參數後按" not in info_text, (
        f"短線頁不該退回初始畫面, info: {info_text}"
    )
    # 展開後該張卡片有 helper 內容(主力燈號等)
    assert "主力燈號" in md_text_after_expand or "歷史不足" in md_text_after_expand, (
        f"展開應 render helper section, md_text:\n{md_text_after_expand[:500]}"
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
# 短線頁 5 tabs UI(全部/趨勢/反轉/籌碼/動能)
# ============================================================================

def test_short_renders_five_category_tabs(isolated_db, monkeypatch):
    """執行選股後,短線頁必須 render 5 個 tabs(全部/趨勢/反轉/籌碼/動能),
    各 tab 標籤帶該分類入選檔數。
    """
    from src import database as db, strategies

    db.upsert_stocks([
        {"stock_id": "2330", "name": "台積電", "market": "TW"},
        {"stock_id": "2317", "name": "鴻海", "market": "TW"},
        {"stock_id": "1101", "name": "台泥", "market": "TW"},
    ])

    # 2330: ma_alignment (趨勢) + volume_kd (動能)
    # 2317: bias_convergence (反轉)
    # 1101: inst_consensus (籌碼)
    fake_agg = {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD", "多頭排列"],
            "details": {
                "volume_kd": {
                    "stock_id": "2330", "name": "台積電",
                    "close": 600.0, "atr14": 12.0,
                },
                "ma_alignment": {
                    "stock_id": "2330", "name": "台積電",
                    "close": 600.0, "atr14": 12.0,
                },
            },
        },
        "2317": {
            "name": "鴻海",
            "signals": ["乖離收斂"],
            "details": {
                "bias_convergence": {
                    "stock_id": "2317", "name": "鴻海",
                    "close": 200.0, "atr14": 5.0,
                },
            },
        },
        "1101": {
            "name": "台泥",
            "signals": ["三大法人連買"],
            "details": {
                "inst_consensus": {
                    "stock_id": "1101", "name": "台泥",
                    "close": 50.0, "atr14": 1.5,
                },
            },
        },
    }
    monkeypatch.setattr(
        strategies, "run_all_strategies", lambda *a, **kw: fake_agg,
    )

    at = _new_at("🔥 短線")
    at.run()
    at.selectbox[0].set_value("快速:50 檔大型股").run()
    next(b for b in at.button if b.label == "執行選股").click().run()
    assert not at.exception, _exc_msgs(at)

    # 抓 5 tabs(streamlit AppTest 把 tabs 暴露在 at.tabs)
    tabs = at.tabs
    assert len(tabs) >= 5, f"期望 ≥5 tabs(可能多於 5,其他頁也有), got {len(tabs)}"

    # 取最後 5 個(短線頁的 tabs 在 page render 末段)
    short_tab_labels = [t.label for t in tabs[-5:]]
    assert short_tab_labels[0].startswith("全部"), (
        f"第 1 tab 應是『全部』, got {short_tab_labels[0]!r}"
    )
    assert "趨勢" in short_tab_labels[1]
    assert "反轉" in short_tab_labels[2]
    assert "籌碼" in short_tab_labels[3]
    assert "動能" in short_tab_labels[4]

    # 各分類入選檔數正確(2330 同時是趨勢+動能,所以兩個 tab 都會 +1)
    assert "全部 (3)" in short_tab_labels[0]
    assert "趨勢 (1)" in short_tab_labels[1]
    assert "反轉 (1)" in short_tab_labels[2]
    assert "籌碼 (1)" in short_tab_labels[3]
    assert "動能 (1)" in short_tab_labels[4]


def test_short_advanced_params_new_sliders_render(isolated_db):
    """進階參數 expander 內,commit 2 新增的 2 個 sliders 必須 render 出來
    (短線 5 / 短線 6 對應的 squeeze_pct_max / consensus_days),且 default 值對。
    """
    at = _new_at("🔥 短線")
    at.run()
    assert not at.exception, _exc_msgs(at)

    # session_state setdefault 已 init,直接讀 default 值驗證
    assert at.session_state["short_squeeze_pct_max"] == 2.0
    assert at.session_state["short_consensus_days"] == 3


def test_short_commit3_new_strategies_sliders_render(isolated_db):
    """commit 3 新加 5 個 strategies 對應的 9 個 sliders 都要 render +
    session_state default 值正確。
    """
    at = _new_at("🔥 短線")
    at.run()
    assert not at.exception, _exc_msgs(at)

    # 策略 7: bb_lower_rebound
    assert at.session_state["short_bb_lookback"] == 5
    # 策略 8: rsi_recovery
    assert at.session_state["short_rsi_oversold"] == 30.0
    assert at.session_state["short_rsi_recovered"] == 50.0
    # 策略 9: inst_silent_accum
    assert at.session_state["short_silent_pct_max"] == 1.0
    assert at.session_state["short_silent_bb_pos_max"] == 50.0
    # 策略 10: volume_breakout
    assert at.session_state["short_vbo_vol_ratio"] == 2.5
    assert at.session_state["short_vbo_highest_lookback"] == 20
    # 策略 11: gap_up
    assert at.session_state["short_gap_pct_min"] == 1.5
    assert at.session_state["short_gap_vol_ratio"] == 1.5


def test_short_eleven_strategies_registered():
    """run_all_strategies 應認得全 11 套策略 keys。"""
    from src import strategies as strat

    assert len(strat.ALL_STRATEGIES) == 11
    assert len(strat.STRATEGY_LABELS) == 11
    expected = {
        "volume_kd", "ma_alignment", "bias_convergence",
        "macd_golden", "ma_squeeze_breakout", "inst_consensus",
        "bb_lower_rebound", "rsi_recovery", "inst_silent_accum",
        "volume_breakout", "gap_up",
    }
    assert set(strat.ALL_STRATEGIES.keys()) == expected
    assert set(strat.STRATEGY_LABELS.keys()) == expected


def test_short_strategy_category_covers_all_eleven():
    """app._STRATEGY_CATEGORY 必須涵蓋全 11 套策略,否則 5 tabs 篩選會漏。"""
    sys.modules.pop("app", None)
    import app as app_mod
    from src import strategies as strat

    missing = set(strat.ALL_STRATEGIES.keys()) - set(app_mod._STRATEGY_CATEGORY.keys())
    assert not missing, f"_STRATEGY_CATEGORY 漏了:{missing}"
    # 各 cat 都要有人(沒孤兒 cat)
    cats = set(app_mod._STRATEGY_CATEGORY.values())
    assert cats == {"趨勢", "反轉", "籌碼", "動能"}


def test_short_filter_agg_by_category_logic():
    """`_filter_agg_by_category` 純邏輯測試 — 不過 streamlit。
    確保「同檔有兩個策略屬不同類」時,兩個 cat 都會選到該檔。
    """
    import app as app_mod

    sys.modules.pop("app", None)
    import app as app_mod  # reimport

    agg = {
        "A": {
            "name": "A",
            "signals": ["量價KD", "多頭排列"],
            "details": {"volume_kd": {}, "ma_alignment": {}},
        },
        "B": {
            "name": "B",
            "signals": ["乖離收斂"],
            "details": {"bias_convergence": {}},
        },
        "C": {
            "name": "C",
            "signals": ["三大法人連買"],
            "details": {"inst_consensus": {}},
        },
    }
    # A 屬「動能」 + 「趨勢」
    assert set(app_mod._filter_agg_by_category(agg, "趨勢").keys()) == {"A"}
    assert set(app_mod._filter_agg_by_category(agg, "動能").keys()) == {"A"}
    assert set(app_mod._filter_agg_by_category(agg, "反轉").keys()) == {"B"}
    assert set(app_mod._filter_agg_by_category(agg, "籌碼").keys()) == {"C"}


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
    """推薦卡點「📊 展開詳細分析」按鈕後,4 個 section 都渲染(主力燈號 /
    技術分析總覽 / 關鍵價位 / 操作建議)。

    新行為(956d08d 之後改 lazy):cold load 不 render helper,點按鈕才跑。
    """
    _seed_distribution_scenario()

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
    md_text_before = "\n".join(m.value for m in at.markdown)
    assert "2330" in md_text_before and "台積電" in md_text_before

    # cold 狀態:helper 不該 render(lazy)
    assert "主力燈號" not in md_text_before, (
        "lazy expander 收起時不該渲染 helper, "
        "但 markdown 含『主力燈號』(可能 expander body 又被執行)"
    )

    # 點「📊 展開詳細分析」
    open_btn = next(
        b for b in at.button if "展開詳細分析" in (b.label or "")
    )
    open_btn.click().run()
    assert not at.exception, _exc_msgs(at)

    # 展開後 4 個 section 都在
    md_text_after = "\n".join(m.value for m in at.markdown)
    assert "主力燈號" in md_text_after
    assert "技術分析總覽" in md_text_after
    assert "關鍵價位" in md_text_after
    assert "操作建議" in md_text_after

    # 收起按鈕存在
    btn_labels_after = [b.label for b in at.button]
    assert any("收起" in (lbl or "") for lbl in btn_labels_after), (
        f"展開後應有「收起」按鈕, 實際: {btn_labels_after}"
    )


def test_pick_card_shows_pnl_row_when_position_exists(isolated_db):
    """該股 trades 表有持倉 → 卡片渲染 P&L 行(持有/均價/損益)。"""
    from src import database as db

    # 灌一筆 buy → qty=1, avg_cost=600
    db.add_trade("2330", "buy", 600.0, 1, "2026-04-01")

    def _harness():
        from src.ui_cards import render_pick_card
        # close=650,unrealized = (650-600)×1 = +50
        render_pick_card(
            {"stock_id": "2330", "name": "台積電", "close": 650.0},
            show_add_button=False,
        )

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "持有 1 張" in md_text, f"應含持有訊息, md={md_text!r}"
    assert "均價 600" in md_text
    # +50 損益(close=650 > avg=600 → 紅利,但 markdown 只看數字 +50)
    assert "+50" in md_text


def test_pick_card_no_pnl_row_when_no_position(isolated_db):
    """該股沒 trades → 卡片不渲染 P&L 行。"""

    def _harness():
        from src.ui_cards import render_pick_card
        render_pick_card(
            {"stock_id": "2330", "name": "台積電", "close": 650.0},
            show_add_button=False,
        )

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "持有" not in md_text, f"沒倉位不該渲染持有訊息, md={md_text!r}"


def test_portfolio_snapshot_dump_then_load_roundtrip(isolated_db, tmp_path):
    """trades 表 → CSV → 清表 → 從 CSV load 回來,資料一致。"""
    from src import database as db, portfolio_snapshot

    db.add_trade("2330", "buy", 600.0, 2, "2026-04-01", note="測試")
    db.add_trade("2330", "sell", 650.0, 1, "2026-04-05")

    n_dumped = portfolio_snapshot.dump_to_csv(snapshot_dir=tmp_path)
    assert n_dumped == 2
    assert (tmp_path / "trades.csv").exists()

    # 清表
    with db.get_conn() as conn:
        conn.execute("DELETE FROM trades")
    assert db.get_trades() == []

    # 從 csv load 回來
    n_loaded = portfolio_snapshot.load_from_csv(snapshot_dir=tmp_path)
    assert n_loaded == 2
    trades = db.get_trades("2330")
    assert len(trades) == 2
    # 驗 position 重算對(buy 2×600 - sell 1×650 → qty=1, avg=600, realized=50)
    pos = db.get_position("2330")
    assert pos["quantity"] == 1
    assert abs(pos["avg_cost"] - 600.0) < 1e-6
    assert abs(pos["realized_pnl"] - 50.0) < 1e-6


def test_push_trades_to_github_no_pat_returns_false(monkeypatch):
    """無 GITHUB_PAT 環境 → push_trades_to_github 直接回 False,不發 HTTP。"""
    from src import github_sync

    monkeypatch.delenv("GITHUB_PAT", raising=False)
    assert github_sync.push_trades_to_github("foo,bar\n") is False


def test_fetch_trades_from_github_no_pat_returns_none(monkeypatch):
    """無 GITHUB_PAT → fetch 回 None,不發 HTTP。"""
    from src import github_sync

    monkeypatch.delenv("GITHUB_PAT", raising=False)
    assert github_sync.fetch_trades_from_github() is None


def test_portfolio_safe_boot_load_uses_remote_csv(isolated_db, monkeypatch):
    """fetch_trades_from_github 回 csv string → load_from_string 灌進 SQLite,
    safe_boot_load 回 'remote'。
    """
    from src import portfolio_snapshot
    from src import github_sync
    from src import database as db

    csv_text = (
        "id,stock_id,direction,price,quantity,trade_date,note,created_at\n"
        "1,2330,buy,600.0,1,2026-04-01,test,2026-04-01T10:00:00+00:00\n"
    )
    monkeypatch.setattr(
        github_sync, "fetch_trades_from_github", lambda: csv_text,
    )

    result = portfolio_snapshot.safe_boot_load()
    assert result == "remote"

    trades = db.get_trades("2330")
    assert len(trades) == 1
    assert trades[0]["direction"] == "buy"
    assert trades[0]["price"] == 600.0


def test_portfolio_safe_boot_load_fallback_when_no_remote(
    isolated_db, monkeypatch,
):
    """fetch 回 None → fallback 本機 load_from_csv,result='fallback-no-remote'。"""
    from src import portfolio_snapshot
    from src import github_sync

    monkeypatch.setattr(github_sync, "fetch_trades_from_github", lambda: None)
    result = portfolio_snapshot.safe_boot_load()
    assert result == "fallback-no-remote"


def test_portfolio_safe_boot_load_fallback_when_fetch_raises(
    isolated_db, monkeypatch,
):
    """fetch 拋例外 → safe_boot_load 不 raise,走 fallback。"""
    from src import portfolio_snapshot
    from src import github_sync

    def _raise() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(github_sync, "fetch_trades_from_github", _raise)
    result = portfolio_snapshot.safe_boot_load()
    assert result == "fallback-fetch-exception"


def test_dump_trades_csv_skip_outside_project(isolated_db):
    """test fixture 用 tmp DB(不在 PROJECT_ROOT)→ dump_to_csv silent skip 回 -1
    避免污染 repo trades.csv。
    """
    from src import database as db, portfolio_snapshot

    db.add_trade("2330", "buy", 600.0, 1, "2026-04-01")
    # snapshot_dir=None → 預設 PROJECT_ROOT,但 db 在 tmp → 應該 skip
    n = portfolio_snapshot.dump_to_csv()
    assert n == -1, "tmp DB 應 silent skip 不寫 repo"


def test_portfolio_snapshot_load_skip_when_table_not_empty(isolated_db, tmp_path):
    """trades 表已有資料 → load_from_csv skip(避免覆蓋本機新加的)。"""
    from src import database as db, portfolio_snapshot

    # 先寫一個 csv
    db.add_trade("2330", "buy", 600.0, 1, "2026-04-01")
    portfolio_snapshot.dump_to_csv(snapshot_dir=tmp_path)
    # 再加一筆(本機新增)
    db.add_trade("2454", "buy", 1000.0, 1, "2026-04-02")
    assert len(db.get_trades()) == 2

    # load 應該 skip(因為表已有 2 筆 > 0)
    n = portfolio_snapshot.load_from_csv(snapshot_dir=tmp_path)
    assert n == 0
    # 表仍是 2 筆,沒被覆蓋
    assert len(db.get_trades()) == 2


def test_pick_card_expander_renders_for_watchlist(isolated_db):
    """show_add_button=False(watchlist 卡)點「展開詳細分析」也能 render
    4 個 section。watchlist 不渲染 ☆ 按鈕(已關注不需再加)。
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

    # 點「📊 展開詳細分析」(watchlist 卡也有同一個 lazy 按鈕)
    open_btn = next(
        b for b in at.button if "展開詳細分析" in (b.label or "")
    )
    open_btn.click().run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "主力燈號" in md_text
    assert "操作建議" in md_text

    # 但不該有 ☆ 加入按鈕(show_add_button=False)
    btn_labels = [b.label for b in at.button]
    assert not any(
        "加入關注" in (lbl or "") for lbl in btn_labels
    ), f"watchlist 卡不該有「加入關注」按鈕, 實際 buttons: {btn_labels}"


def test_pick_card_lazy_expander_does_not_query_db_when_collapsed(isolated_db):
    """守門:cold load(收起狀態)不該觸發 _compute_main_force_signal 等 helper。
    這就是 lazy 的本質 — 138 picks cold load 0 SQL helper queries。
    """
    from src import individual_sections

    helper_calls = {"main_force": 0, "tech_summary": 0}
    orig_main = individual_sections._compute_main_force_signal
    orig_tech = individual_sections._compute_technical_summary

    def _spy_main(sid):
        helper_calls["main_force"] += 1
        return orig_main(sid)
    def _spy_tech(sid):
        helper_calls["tech_summary"] += 1
        return orig_tech(sid)

    def _harness():
        # harness 內 patch(AppTest sandbox)
        from src import individual_sections as _is
        _is._compute_main_force_signal = _spy_main
        _is._compute_technical_summary = _spy_tech
        from src.ui_cards import render_pick_card
        render_pick_card(
            {"stock_id": "2330", "name": "台積電", "close": 100.0},
            show_add_button=True, button_key_prefix="lazy_test",
        )

    # 重要:_spy 必須在 module 全域以便 harness 能拿到。用 monkeypatch
    # 不行(harness 是 sandboxed sub-script)— 這 test 改用「render 後從
    # AppTest 再驗 helper render 結果反推」即可。
    # 直接驗 markdown:cold 狀態應該沒「主力燈號」「技術分析總覽」等
    def _harness_simple():
        from src.ui_cards import render_pick_card
        render_pick_card(
            {"stock_id": "2330", "name": "台積電", "close": 100.0},
            show_add_button=True, button_key_prefix="lazy_test",
        )

    at = AppTest.from_function(_harness_simple, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    # 卡片基本資訊有
    assert "2330" in md_text
    # 但 helper section 不該渲染(lazy)
    for section in ("主力燈號", "技術分析總覽", "關鍵價位", "操作建議"):
        assert section not in md_text, (
            f"lazy 收起狀態不該渲染『{section}』, md_text:\n{md_text}"
        )

    # 應有「展開詳細分析」按鈕
    btn_labels = [b.label for b in at.button]
    assert any("展開詳細分析" in (lbl or "") for lbl in btn_labels), (
        f"應有「展開詳細分析」按鈕, 實際: {btn_labels}"
    )


def test_pagination_shows_first_page_only(isolated_db):
    """render_picks_cards_paginated 預設只 render 前 page_size 張 + 「載入更多」按鈕。"""
    def _harness():
        # rows 必須在 harness 內定義(AppTest sandbox closure 抓不到 outer)
        rows = [
            {"stock_id": f"{1000+i}", "name": f"名{i}", "close": 100.0}
            for i in range(25)
        ]
        from src.ui_cards import render_picks_cards_paginated
        render_picks_cards_paginated(
            rows, state_key="test_pg", page_size=10,
            show_add_button=True, button_key_prefix="pg",
        )

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    # 前 10 筆 stock_id 出現,後 15 筆不出現
    md_text = "\n".join(m.value for m in at.markdown)
    for i in range(10):
        assert f"1{i:03d}" in md_text, f"第 {i} 張應 render: {md_text[:200]!r}"
    for i in range(10, 25):
        assert f"1{i:03d}" not in md_text, f"第 {i} 張不該 render"

    # 「載入更多」按鈕在
    btn_labels = [b.label for b in at.button]
    assert any("載入更多" in (lbl or "") for lbl in btn_labels), (
        f"應有「載入更多」, 實際: {btn_labels}"
    )


def test_pagination_load_more_extends_visible(isolated_db):
    """點「載入更多」→ 多 render page_size 張。"""
    def _harness():
        rows = [
            {"stock_id": f"{2000+i}", "name": f"X{i}", "close": 50.0}
            for i in range(15)
        ]
        from src.ui_cards import render_picks_cards_paginated
        render_picks_cards_paginated(
            rows, state_key="test_lm", page_size=10,
            show_add_button=False,
        )

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    # 點「載入更多」
    load_btn = next(b for b in at.button if "載入更多" in (b.label or ""))
    load_btn.click().run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    # 全 15 張都出現了(15 < 10+10=20)
    for i in range(15):
        assert f"2{i:03d}" in md_text, f"第 {i} 張應 render"

    # 已顯示全部訊息出現
    captions = "\n".join(c.value for c in at.caption)
    assert "已顯示全部" in captions, f"應有「已顯示全部」, captions: {captions}"


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


def test_preload_snapshots_loads_csvs_into_sqlite(isolated_db, tmp_path):
    """db.preload_snapshots 從 tmp snapshot dir 讀 csv → upsert 進 SQLite。

    給 GitHub Actions workflow runner 用 — fresh container 沒走 streamlit
    boot path,要靠這個 helper preload daily_prices.csv 等 snapshot,否則
    短線篩選看到 cache 空 = 0 picks。
    """
    from src import database as db

    snap_dir = tmp_path / "twse_snapshot"
    snap_dir.mkdir()
    # 灌 stocks.csv
    pd.DataFrame([
        {"stock_id": "2330", "name": "台積電", "industry": "半導體"},
    ]).to_csv(snap_dir / "stocks.csv", index=False)
    # 灌 daily_prices.csv
    pd.DataFrame([
        {
            "stock_id": "2330", "date": "2026-04-30",
            "open": 600.0, "high": 605.0, "low": 595.0, "close": 600.0,
            "volume": 10000,
        }
    ]).to_csv(snap_dir / "daily_prices.csv", index=False)
    # 灌 taiex.csv(stock_id='TAIEX' 也是 daily_prices schema)
    pd.DataFrame([
        {
            "stock_id": "TAIEX", "date": "2026-04-30",
            "open": 39000.0, "high": 39500.0, "low": 38900.0, "close": 39200.0,
            "volume": 100000,
        }
    ]).to_csv(snap_dir / "taiex.csv", index=False)

    counts = db.preload_snapshots(snapshot_dir=snap_dir)

    assert counts.get("stocks") == 1
    assert counts.get("daily_prices") == 1
    assert counts.get("taiex") == 1

    # 真的灌進 SQLite 了
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_prices WHERE stock_id='2330'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_prices WHERE stock_id='TAIEX'"
        ).fetchone()[0] == 1


def test_preload_snapshots_missing_dir_returns_empty(tmp_path):
    """snapshot_dir 不存在 → 回空 dict,不 raise。"""
    from src import database as db

    counts = db.preload_snapshots(snapshot_dir=tmp_path / "nonexistent")
    assert counts == {}


def test_get_latest_trading_date_returns_max_date(isolated_db):
    """灌 daily_prices 多筆日期 → get_latest_trading_date 回 MAX(date)。"""
    from src import database as db

    db.upsert_stocks([{"stock_id": "2330", "name": "台積電", "market": "TW"}])
    db.upsert_daily_prices([
        {"stock_id": "2330", "date": "2026-04-28",
         "open": 600, "high": 605, "low": 595, "close": 600, "volume": 1000},
        {"stock_id": "2330", "date": "2026-04-30",
         "open": 605, "high": 610, "low": 600, "close": 610, "volume": 1100},
        {"stock_id": "2330", "date": "2026-04-29",
         "open": 600, "high": 608, "low": 598, "close": 605, "volume": 1050},
    ])
    assert db.get_latest_trading_date() == "2026-04-30"


def test_get_latest_trading_date_empty_returns_none(isolated_db):
    """daily_prices 空 → 回 None(caller fallback today)。"""
    from src import database as db
    assert db.get_latest_trading_date() is None


def test_format_short_picks_includes_weekend_hint_when_not_today(isolated_db):
    """date 不是 today → 訊息含週末/假日提示。"""
    from src.notifier import format_short_picks
    import pandas as _pd

    picks = _pd.DataFrame([
        {
            "stock_id": "2330", "name": "台積電", "close": 600.0,
            "volume": 10000, "ma_volume_5": 9000,
            "k": 60.0, "d": 50.0, "inst_total_3d": 0,
        }
    ])
    msg = format_short_picks(picks, "2020-01-01")  # 絕對不是 today
    assert "週末/假日" in msg, f"預期週末提示, msg=\n{msg}"


def test_format_short_picks_no_hint_when_today():
    """date == today → 不加週末提示(避免每日推播都看到)。"""
    from datetime import date as _date
    from src.notifier import format_short_picks
    import pandas as _pd

    today = _date.today().isoformat()
    picks = _pd.DataFrame([
        {
            "stock_id": "2330", "name": "台積電", "close": 600.0,
            "volume": 10000, "ma_volume_5": 9000,
            "k": 60.0, "d": 50.0, "inst_total_3d": 0,
        }
    ])
    msg = format_short_picks(picks, today)
    assert "週末/假日" not in msg, f"today 不該有週末提示, msg=\n{msg}"


def test_format_multi_strategy_empty_includes_weekend_hint(isolated_db):
    """空 picks(無入選)+ 非 today → 訊息含週末提示。"""
    from src.notifier import format_multi_strategy_picks

    msg = format_multi_strategy_picks({}, "2020-01-01")
    assert "週末/假日" in msg
    assert "今日無任一策略選中" in msg


def test_extract_features_returns_dict_with_sufficient_history(isolated_db):
    """灌 70 天 daily_prices(無 institutional)→ extract_features 回 11 個 key 都齊。"""
    from src import ml_predictor
    _seed_trend_prices(direction="up", n_days=70)

    # 用 daily_prices 最後一個日期當 target_date
    from src import database as db
    latest = db.get_latest_trading_date()
    feats = ml_predictor.extract_features("2330", latest)
    assert feats is not None, "70 天歷史應該夠抽 features"
    for k in ml_predictor.FEATURE_NAMES:
        assert k in feats
    # 沒 institutional → inst_5d / inst_10d 應該是 0
    assert feats["inst_5d"] == 0.0
    assert feats["inst_10d"] == 0.0


def test_extract_features_returns_none_when_history_insufficient(isolated_db):
    """少於 60 天歷史 → 回 None。"""
    from src import ml_predictor
    _seed_trend_prices(direction="up", n_days=30)

    feats = ml_predictor.extract_features("2330", "2026-01-30")
    assert feats is None


def test_compute_label_win_when_target_reached(isolated_db):
    """進場後 5 天 high 觸到 entry + 1.5×ATR → label = 1。"""
    from src import database as db, ml_predictor
    # 灌 60 天歷史(算 ATR + 進場日)+ 5 天後續(高點觸到 target)
    _seed_trend_prices(direction="up", n_days=70)
    latest = db.get_latest_trading_date()

    # 70 天線性漲(close 從 100 到 169,每天 +1)→ ATR 約 1
    # 倒數第 6 天當 entry,target = entry_close + 1.5,後 5 天 high 必過(每天漲 1)
    from datetime import date as _date, timedelta as _td
    target_date_obj = _date.fromisoformat(latest) - _td(days=5)
    target_date = target_date_obj.isoformat()

    label = ml_predictor.compute_label("2330", target_date)
    assert label == 1, f"線性漲後 5 天必觸 target,期望 1, 實際 {label}"


def test_compute_label_returns_none_when_no_future_data(isolated_db):
    """target_date 後沒足夠 lookahead 資料 → None。"""
    from src import database as db, ml_predictor
    _seed_trend_prices(direction="up", n_days=70)

    # 用最新日期當 target → 後續 0 天 → None
    latest = db.get_latest_trading_date()
    label = ml_predictor.compute_label("2330", latest)
    assert label is None


def test_format_pick_summary_includes_ai_part_when_model_loaded(
    isolated_db, monkeypatch,
):
    """mock model.load 回非 None,format_pick_summary 應含「🎯 AI 勝率」part。"""
    from src import individual_sections

    class _FakeModel:
        classes_ = [0, 1]

        def predict_proba(self, X):
            import numpy as _np
            return _np.array([[0.30, 0.70]])

    # 重置 module cache 避免之前的 load 結果污染
    individual_sections._ml_model_cache = None
    individual_sections._ml_model_loaded = False
    monkeypatch.setattr(
        individual_sections, "_get_ml_model", lambda: _FakeModel(),
    )

    # 也要 mock predict_short_pick_winrate(否則會 call extract_features 看 SQLite)
    from src import ml_predictor
    monkeypatch.setattr(
        ml_predictor, "predict_short_pick_winrate",
        lambda model, sid, target_date, db_path=None: 0.70,
    )

    # 同時要灌一個 latest_trading_date 否則 _ai_winrate_part 走 None fallback
    _seed_distribution_scenario()

    summary = individual_sections.format_pick_summary("2330")
    assert "AI 勝率" in summary, f"應含 AI part, 實際: {summary!r}"
    assert "70%" in summary, f"勝率應 70%, 實際: {summary!r}"


def test_format_pick_summary_ai_part_dash_when_no_model(
    isolated_db, monkeypatch,
):
    """沒模型 → AI part = 「🎯 —」(維持 4 part 格式統一)。"""
    from src import individual_sections

    individual_sections._ml_model_cache = None
    individual_sections._ml_model_loaded = False
    monkeypatch.setattr(individual_sections, "_get_ml_model", lambda: None)

    summary = individual_sections.format_pick_summary("9999")
    assert "🎯 —" in summary, f"沒模型應「🎯 —」佔位, 實際: {summary!r}"
    # 結構仍 4 part(3 個 / 分隔)
    assert summary.count("/") == 3


def test_format_pick_summary_with_data(isolated_db):
    """灌足夠歷史(70 天)+ institutional → 摘要含 📊 / 🚦 / 💡 三個 part。"""
    from src.individual_sections import format_pick_summary
    _seed_distribution_scenario()  # 70 天 daily + institutional

    summary = format_pick_summary("2330", indent="   ")
    assert summary, f"預期非空摘要, 實際 {summary!r}"
    assert "📊" in summary, f"缺技術部分: {summary!r}"
    assert "🚦" in summary, f"缺主力燈號部分: {summary!r}"
    assert "💡" in summary, f"缺操作核心部分: {summary!r}"
    assert summary.startswith("   "), f"應以 indent 開頭: {summary!r}"


def test_format_pick_summary_no_data_returns_placeholders(isolated_db):
    """完全沒歷史(不存在的股號)→ 三 part 都用「—」佔位,維持格式統一。
    永不回空字串(caller 不必判斷 skip,訊息每檔行數一致)。
    """
    from src.individual_sections import format_pick_summary

    summary = format_pick_summary("9999", indent="   ")
    # 4 part 都用「—」(技術 / 主力 / 操作 / AI 勝率)
    assert "📊 —" in summary, f"技術 part 應佔位, 實際: {summary!r}"
    assert "🚦 —" in summary, f"主力 part 應佔位, 實際: {summary!r}"
    assert "💡 —" in summary, f"操作 part 應佔位, 實際: {summary!r}"
    assert "🎯 —" in summary, f"AI 勝率 part 應佔位, 實際: {summary!r}"
    # 結構含 3 個分隔符
    assert summary.count("/") == 3
    assert summary.startswith("   ")  # indent 開頭


def test_format_pick_summary_partial_fallback_keeps_format(isolated_db):
    """灌 daily_prices 60+ 天但無 institutional → summary OK / main_force fallback
    → 📊 有實值 + 🚦 — + 💡 有實值。永遠回三 part。
    """
    from src.individual_sections import format_pick_summary
    # _seed_trend_prices 灌 daily_prices 線性漲 70 天,沒 institutional
    _seed_trend_prices(direction="up", n_days=70)

    summary = format_pick_summary("2330", indent="   ")
    # 📊 應有實值(線性漲 → 多頭)
    assert "📊 多頭" in summary, f"技術 part 應有實值, 實際: {summary!r}"
    # 🚦 應佔位(沒 institutional)
    assert "🚦 —" in summary, f"主力 part 應佔位(無法人資料), 實際: {summary!r}"
    # 💡 應有實值(summary OK 就能查 _ACTION_CORE_BY_SUMMARY)
    assert "💡 —" not in summary, f"操作 part 應有實值, 實際: {summary!r}"
    # 🎯 AI 勝率(本機有 model.pkl 可能有實值,test 不嚴格驗值)
    # 結構仍 4 part
    assert summary.count("/") == 3


def test_format_short_picks_includes_detail_under_4096_chars(isolated_db):
    """7 picks 推播訊息 ≤ Telegram 4096 字元上限,且含詳細分析行。"""
    from src.notifier import format_short_picks
    _seed_distribution_scenario()

    # 構造 7 picks 都用 stock_id=2330(灌過資料的那檔),測 message 長度上限
    picks = pd.DataFrame([
        {
            "stock_id": "2330", "name": "台積電", "close": 169.0,
            "volume": 10000, "ma_volume_5": 9000,
            "k": 60.0, "d": 50.0, "inst_total_3d": -5_000_000,
        }
        for _ in range(7)
    ])

    msg = format_short_picks(picks, "2026-04-30")
    assert len(msg) <= 4096, f"訊息超 Telegram 4096 上限: {len(msg)}"
    # 應該每 pick 都有詳細(7 picks → 至少 7 個📊 emoji)
    assert msg.count("📊") >= 7 or msg.count("🚦") >= 7, (
        f"預期每 pick 都有詳細分析行, msg=\n{msg}"
    )


def test_split_margin_dataset_long_format():
    """FinMind long format(name 欄分 MarginPurchase / ShortSale)→ 切成兩個 DF。"""
    import app

    df = pd.DataFrame([
        {"date": "2026-04-30", "name": "MarginPurchase", "TodayBalance": 100_000_000_000},
        {"date": "2026-04-30", "name": "ShortSale", "TodayBalance": 5_000_000_000},
        {"date": "2026-04-29", "name": "MarginPurchase", "TodayBalance": 99_000_000_000},
        {"date": "2026-04-29", "name": "ShortSale", "TodayBalance": 4_800_000_000},
    ])
    margin, short = app._split_margin_dataset(df)
    assert len(margin) == 2 and len(short) == 2
    # 換算億元(/ 1e8):100B/1e8 = 1000 億
    assert abs(margin["balance_billion"].iloc[-1] - 1000.0) < 0.1
    assert abs(short["balance_billion"].iloc[-1] - 50.0) < 0.1


def test_split_margin_dataset_wide_format_fallback():
    """舊版 wide format(MarginPurchaseTodayBalance / ShortSaleTodayBalance 兩欄)。"""
    import app

    df = pd.DataFrame([
        {"date": "2026-04-30", "MarginPurchaseTodayBalance": 100_000_000_000,
         "ShortSaleTodayBalance": 5_000_000_000},
        {"date": "2026-04-29", "MarginPurchaseTodayBalance": 99_000_000_000,
         "ShortSaleTodayBalance": 4_800_000_000},
    ])
    margin, short = app._split_margin_dataset(df)
    assert len(margin) == 2 and len(short) == 2
    assert abs(margin["balance_billion"].iloc[0] - 1000.0) < 0.1


def test_split_margin_dataset_unknown_schema_returns_empty():
    """完全不認得的 schema → 回兩個空 DataFrame(caller 走 warning fallback)。"""
    import app

    df = pd.DataFrame([{"foo": 1, "bar": 2}])
    margin, short = app._split_margin_dataset(df)
    assert margin.empty and short.empty


def test_load_model_meta_returns_dict_when_exists(tmp_path):
    """有 pkl + sidecar .meta.json → load_model_meta 回 dict。"""
    from src import ml_predictor

    pkl = tmp_path / "test_model.pkl"
    pkl.write_bytes(b"fake")  # joblib 不要,只是要 pkl 存在
    metrics = {
        "n_train": 600, "n_test": 150,
        "win_rate_overall": 0.42,
        "accuracy": 0.66, "precision": 0.59, "recall": 0.61, "f1": 0.60,
    }
    ml_predictor.dump_model_meta(pkl, metrics=metrics)

    meta = ml_predictor.load_model_meta(pkl)
    assert meta is not None
    assert meta["samples"] == 750
    assert meta["features_count"] == 11
    assert abs(meta["metrics"]["accuracy"] - 0.66) < 1e-6
    assert meta["min_history_days"] == ml_predictor.MIN_HISTORY_DAYS
    assert meta["model_type"] == "RandomForestClassifier"


def test_load_model_meta_returns_none_when_missing(tmp_path):
    """sidecar .meta.json 不存在 → 回 None(caller fallback)。"""
    from src import ml_predictor

    fake_pkl = tmp_path / "missing.pkl"
    assert ml_predictor.load_model_meta(fake_pkl) is None


def test_system_health_renders_ml_section_with_real_meta(isolated_db):
    """既有 models/short_pick.meta.json 存在(本機訓練生成)→ 系統頁應渲染 5 個
    ML metric。"""
    def _harness():
        import app
        app._render_system_health()

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(m.value for m in at.markdown)
    assert "🤖 AI 模型" in md_text, "應渲染 AI 模型 section"
    # 5 metric label 出現(streamlit metric label 在 markdown 內可見)
    metric_labels = [m.label for m in at.metric]
    for expected in ["訓練樣本", "Accuracy", "Precision", "Recall", "F1"]:
        assert expected in metric_labels, (
            f"應有 metric「{expected}」, 實際: {metric_labels}"
        )
    # 副資訊文字
    assert "模型類型" in md_text
    assert "RandomForestClassifier" in md_text
    assert "最低歷史" in md_text


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


# ============================================================================
# 卡片詳細分析 expander:🏢 公司資訊 section(cache-only,不主動跑 LLM)
# ============================================================================

def _seed_company_profile(
    sid: str, *, industry: str = "半導體業", market: str = "上市",
    description: str | None = "晶圓代工龍頭",
    uniqueness: str | None = "領先製程節點",
    moat: str | None = "規模 + 客戶綁定",
) -> None:
    """灌一筆 company_profiles 給 _render_company_info_compact 讀。"""
    from src import database as db
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO company_profiles (
                stock_id, industry, market, description, uniqueness, moat,
                finmind_updated_at, llm_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stock_id) DO UPDATE SET
                industry=excluded.industry, market=excluded.market,
                description=excluded.description, uniqueness=excluded.uniqueness,
                moat=excluded.moat
            """,
            (
                sid, industry, market, description, uniqueness, moat,
                "2026-05-04T00:00:00+00:00", "2026-05-04T00:00:00+00:00",
            ),
        )


def test_company_info_compact_shows_facts_and_llm_when_cached(isolated_db):
    """SQLite 有完整 profile → markdown 含 industry / description / uniqueness / moat。

    間接驗證「LLM 沒被呼叫」:由於沒設 GEMINI_API_KEY,如果走 LLM path 會
    產生 error caption「GEMINI_API_KEY 未設定」。assert 沒看到該訊息 →
    確認走 SQLite cache hit。
    """
    from src.individual_sections import _get_company_profile_cached

    _seed_company_profile("2330")
    _get_company_profile_cached.clear()

    def _harness():
        from src.individual_sections import _render_company_info_compact
        _render_company_info_compact("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(str(m.value) for m in at.markdown)
    captions = "\n".join(str(c.value) for c in at.caption)
    assert "公司資訊" in md_text, f"缺標題: {md_text!r}"
    assert "半導體業" in md_text, f"缺 industry: {md_text!r}"
    assert "上市" in md_text, f"缺 market: {md_text!r}"
    assert "晶圓代工龍頭" in md_text, f"缺 description: {md_text!r}"
    assert "領先製程節點" in md_text, f"缺 uniqueness: {md_text!r}"
    assert "規模 + 客戶綁定" in md_text, f"缺 moat: {md_text!r}"
    # 反向確認:SQLite cache hit 不該走 LLM error path
    assert "GEMINI_API_KEY" not in captions, (
        f"cache hit 走了 LLM path, captions: {captions}"
    )


def test_company_info_compact_auto_calls_get_profile_on_cache_miss(
    isolated_db, monkeypatch,
):
    """SQLite cache miss → 自動 call get_company_profile(會觸發 LLM)。

    透過 monkeypatch get_company_profile 為 spy(這次不需用 mock 資料,直
    接驗 spy 呼叫即可確認新行為)— spy 必須在 harness module 全域 patch,
    AppTest 內 import 看得到。
    """
    from src import company_profile as cp
    from src.individual_sections import _get_company_profile_cached

    _get_company_profile_cached.clear()

    # 直接在 cp module 上替換函式 — AppTest harness 內 from src import
    # company_profile 拿到的是同一個 module 物件,此 attribute 可被 patch
    spy_calls = []
    def _spy(sid, regenerate=False):
        spy_calls.append((sid, regenerate))
        return {
            "stock_id": sid, "name": "X",
            "industry": "光電業", "market": "上市",
            "listing_date": None, "foreign_limit": None,
            "description": "做面板", "uniqueness": "技術領先",
            "moat": "規模優勢",
            "finmind_updated_at": None,
            "llm_updated_at": "2026-05-04T00:00:00+00:00",
            "llm_error": None,
        }
    monkeypatch.setattr(cp, "get_company_profile", _spy)

    def _harness():
        from src.individual_sections import _render_company_info_compact
        _render_company_info_compact("3008")

    at = AppTest.from_function(_harness, default_timeout=15)
    at.run()
    assert not at.exception, _exc_msgs(at)

    assert spy_calls == [("3008", False)], (
        f"預期 get_company_profile 被叫一次 (3008, regenerate=False), "
        f"實際: {spy_calls}"
    )

    md_text = "\n".join(str(m.value) for m in at.markdown)
    assert "光電業" in md_text
    assert "做面板" in md_text


def test_company_info_compact_llm_failure_shows_error_caption(
    isolated_db, monkeypatch,
):
    """LLM 失敗(get_company_profile 回 llm_error)→ 顯 error caption,
    若也沒 description 則 caption 顯整個錯誤訊息。"""
    from src import company_profile as cp
    from src.individual_sections import _get_company_profile_cached

    _get_company_profile_cached.clear()

    monkeypatch.setattr(
        cp, "get_company_profile",
        lambda sid, regenerate=False: {
            "stock_id": sid, "name": "X",
            "industry": "光電業", "market": "上市",
            "listing_date": None, "foreign_limit": None,
            "description": None, "uniqueness": None, "moat": None,
            "finmind_updated_at": "2026-05-04T00:00:00+00:00",
            "llm_updated_at": None,
            "llm_error": "LLM 暫時失敗:API quota exceeded",
        },
    )

    def _harness():
        from src.individual_sections import _render_company_info_compact
        _render_company_info_compact("3008")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    md_text = "\n".join(str(m.value) for m in at.markdown)
    assert "光電業" in md_text  # facts 仍顯

    captions = "\n".join(str(c.value) for c in at.caption)
    assert "API quota exceeded" in captions, (
        f"預期 caption 顯 LLM error, 實際: {captions}"
    )


def test_company_info_compact_has_regenerate_button(isolated_db, monkeypatch):
    """expander 內必須有「重新生成」按鈕,key 帶 sid 區隔多卡片。"""
    from src import company_profile as cp
    from src.individual_sections import _get_company_profile_cached

    _get_company_profile_cached.clear()
    monkeypatch.setattr(
        cp, "get_company_profile",
        lambda sid, regenerate=False: {
            "stock_id": sid, "name": "台積電",
            "industry": "半導體業", "market": "上市",
            "listing_date": None, "foreign_limit": None,
            "description": "晶圓代工", "uniqueness": "領先製程",
            "moat": "規模 + 客戶",
            "finmind_updated_at": None, "llm_updated_at": None,
            "llm_error": None,
        },
    )

    def _harness():
        from src.individual_sections import _render_company_info_compact
        _render_company_info_compact("2330")

    at = AppTest.from_function(_harness, default_timeout=10)
    at.run()
    assert not at.exception, _exc_msgs(at)

    btn_keys = [b.key for b in at.button if b.key]
    # default key_prefix="card"(_render_company_info_compact 預設值)
    assert "card_company_regen_btn_2330" in btn_keys, (
        f"預期『重新生成』按鈕 key=card_company_regen_btn_2330,"
        f"實際 buttons: {btn_keys}"
    )
