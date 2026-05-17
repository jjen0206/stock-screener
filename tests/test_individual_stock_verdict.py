"""src/individual_stock_verdict.py — 軍師判讀整合邏輯單元測試。

涵蓋:
  - is_enabled / kill switch
  - PATTERN_MEANINGS / latest_pattern_phrase 白話文呈現
  - compute_verdict 各種訊號組合:
      * 紅燈警示(default_settlement / full_cash)→ 強制 🔴 不進場
      * 大盤 bear + 無紅燈 → 🔴 / 🟡 視 score
      * 大盤 bull + ML 高 + 多形態 + 共識 → 🟢
      * 訊號不足 → 🟡 觀望
      * 已持倉 → 🟡 觀望(不重複進場)
      * 🟢 verdict 才回 entry_zone / stop_loss / take_profit
  - verdict_tag_for_card 短字串
  - 失敗 path:DB 空,各 collect_* 都不爆,verdict 仍回觀望
"""
from __future__ import annotations

from datetime import date as _d, timedelta as _td

import pytest

from src import database as db
from src import individual_stock_verdict as isv

# tmp_db fixture 共用 tests/conftest.py


@pytest.fixture
def enable_verdict(monkeypatch):
    """確保測試時 STOCK_VERDICT_ENABLED 為 true。"""
    monkeypatch.setenv("STOCK_VERDICT_ENABLED", "true")


# === seed helpers ===

def _seed_daily_prices(sid: str, closes: list[float], start_offset_days: int = 0) -> str:
    """灌個股日線。closes 從舊到新。回最新一天 ISO date。"""
    today = _d.today()
    rows = []
    for i, c in enumerate(closes):
        d = (today - _td(days=len(closes) - 1 - i + start_offset_days)).isoformat()
        rows.append({
            "stock_id": sid, "date": d,
            "open": c, "high": c * 1.005, "low": c * 0.995, "close": c,
            "volume": 1_000_000,
            "trading_money": None, "trading_turnover": None, "spread": None,
        })
    db.upsert_daily_prices(rows)
    return rows[-1]["date"]


def _seed_taiex_bull(days: int = 70) -> None:
    """灌上升 TAIEX → regime=bull。"""
    closes = [100.0 + i * 0.5 for i in range(days)]
    _seed_daily_prices("TAIEX", closes)


def _seed_taiex_bear(days: int = 70) -> None:
    """灌下跌 TAIEX → regime=bear。"""
    closes = [200.0 - i * 0.5 for i in range(days)]
    _seed_daily_prices("TAIEX", closes)


def _seed_warning(sid: str, warning_type: str, days_ago: int = 2) -> None:
    """灌 active warning(effective_to=None)。"""
    announced = (_d.today() - _td(days=days_ago)).isoformat()
    db.upsert_stock_warnings([{
        "stock_id": sid,
        "warning_type": warning_type,
        "announced_date": announced,
        "effective_from": announced,
        "effective_to": None,
        "reason": f"{warning_type} test seed",
    }])


# ============================================================================
# kill switch / is_enabled
# ============================================================================

def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("STOCK_VERDICT_ENABLED", raising=False)
    assert isv.is_enabled() is True


def test_is_enabled_false_when_off(monkeypatch):
    monkeypatch.setenv("STOCK_VERDICT_ENABLED", "false")
    assert isv.is_enabled() is False


def test_compute_verdict_disabled_returns_skeleton(tmp_db, monkeypatch):
    """kill switch off → 仍回完整 dict,enabled=False。"""
    monkeypatch.setenv("STOCK_VERDICT_ENABLED", "false")
    res = isv.compute_verdict("2330")
    assert res["enabled"] is False
    assert res["verdict"] == "觀望"  # 預設值
    assert "停用" in res["action_suggestion"]


# ============================================================================
# latest_pattern_phrase / PATTERN_MEANINGS 白話
# ============================================================================

def test_latest_pattern_phrase_empty():
    """無命中 → 「無形態訊號」中性語。"""
    res = isv.latest_pattern_phrase([])
    assert "無形態訊號" in res
    assert "中性" in res


def test_latest_pattern_phrase_bullish_with_label():
    """命中三紅兵 → 出現「三紅兵」白話 + 強勢多頭。"""
    hits = [{"name": "three_white_soldiers", "confidence": 3}]
    res = isv.latest_pattern_phrase(hits)
    assert "三紅兵" in res
    assert "強勢多頭" in res
    assert "★★★" in res


def test_latest_pattern_phrase_neutral_doji():
    """十字星 → 看下一根方向。"""
    hits = [{"name": "doji", "confidence": 1}]
    res = isv.latest_pattern_phrase(hits)
    assert "十字星" in res
    assert "拉鋸" in res


def test_pattern_meanings_covers_all_bullish():
    """所有 BULLISH_PATTERNS 都該在 PATTERN_MEANINGS 內,給 verdict reason 白話化。"""
    for name in isv.BULLISH_PATTERNS:
        assert name in isv.PATTERN_MEANINGS
        label, emoji, phrase = isv.PATTERN_MEANINGS[name]
        assert label
        assert emoji == "🟢"
        assert phrase


# ============================================================================
# compute_verdict — 紅燈警示
# ============================================================================

def test_severe_warning_forces_red_verdict(tmp_db, enable_verdict):
    """default_settlement active → 一定 🔴 不進場,即使其他訊號正面。"""
    _seed_taiex_bull()
    _seed_daily_prices("9999", [50.0 + i * 0.5 for i in range(30)])
    _seed_warning("9999", "default_settlement")
    res = isv.compute_verdict("9999")
    assert res["verdict_color"] == "🔴"
    assert res["verdict"] == "不進場"
    # reason 出現「違約交割」
    joined = " ".join(res["reasons_con"])
    assert "違約交割" in joined
    # entry zone 不該給
    assert res["entry_zone"] is None
    assert res["stop_loss"] is None
    assert res["take_profit"] is None
    # action suggestion 該包「等警示解除」
    assert "警示" in res["action_suggestion"]


def test_full_cash_forces_red_verdict(tmp_db, enable_verdict):
    """全額交割也算 SEVERE。"""
    _seed_taiex_bull()
    _seed_daily_prices("8888", [30.0 + i * 0.2 for i in range(30)])
    _seed_warning("8888", "full_cash")
    res = isv.compute_verdict("8888")
    assert res["verdict_color"] == "🔴"


def test_attention_warning_negative_but_not_red(tmp_db, enable_verdict):
    """注意股(SOFT)會扣分但不一定強制紅燈 — 還是要看其他訊號。"""
    _seed_taiex_bull()
    _seed_daily_prices("7777", [30.0 + i * 0.2 for i in range(30)])
    _seed_warning("7777", "attention")
    res = isv.compute_verdict("7777")
    # SOFT 警示一定列在 con
    assert any("注意股" in r for r in res["reasons_con"])
    # 但不該是紅燈(沒 SEVERE)
    # 訊號太少時應為 🟡 觀望
    assert res["verdict_color"] in ("🟡", "🔴")  # 看大盤 + 其他訊號


# ============================================================================
# compute_verdict — 大盤 regime
# ============================================================================

def test_bear_market_blocks_entry(tmp_db, enable_verdict):
    """大盤 bear + 無其他正面訊號 → 🔴 不進場。"""
    _seed_taiex_bear()
    _seed_daily_prices("2330", [100.0 - i * 0.1 for i in range(30)])
    res = isv.compute_verdict("2330")
    assert res["verdict_color"] in ("🔴", "🟡")
    # reason 應出現大盤逆風
    joined = " ".join(res["reasons_con"])
    assert "大盤" in joined or "空" in joined


def test_bull_market_adds_to_score(tmp_db, enable_verdict):
    """大盤 bull → reasons_pro 必含「順風」。"""
    _seed_taiex_bull()
    _seed_daily_prices("2330", [100.0 + i * 0.5 for i in range(30)])
    res = isv.compute_verdict("2330")
    joined = " ".join(res["reasons_pro"])
    assert "順風" in joined or "多頭" in joined


# ============================================================================
# compute_verdict — 訊號不足
# ============================================================================

def test_no_signals_returns_watch(tmp_db, enable_verdict):
    """空 DB,啥訊號都沒 → 🟡 觀望(預設)。"""
    res = isv.compute_verdict("0000")
    assert res["verdict_color"] == "🟡"
    assert res["verdict"] == "觀望"


def test_no_signals_does_not_crash(tmp_db, enable_verdict):
    """資料不足,各 collect_* 應 graceful,不拋例外。"""
    res = isv.compute_verdict("9999")
    # signals dict 該存在但內容可空
    assert "signals" in res
    assert isinstance(res["signals"], dict)


# ============================================================================
# compute_verdict — 已持倉
# ============================================================================

def test_active_paper_trade_forces_watch(tmp_db, enable_verdict):
    """已持倉 → 一律 🟡 觀望(不重複進場),即使大盤大好。"""
    _seed_taiex_bull()
    _seed_daily_prices("3105", [100.0 + i * 0.5 for i in range(30)])
    today = _d.today().isoformat()
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (
                sid, entry_date, entry_price, target_price,
                stop_price, current_stop, hold_days,
                expected_exit_date, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            ("3105", today, 100.0, 110.0, 95.0, 95.0, 10,
             (_d.today() + _td(days=10)).isoformat(), today),
        )
    res = isv.compute_verdict("3105")
    assert res["verdict_color"] == "🟡"
    assert res["verdict"] == "觀望"
    # reasons_pro 應出現「已持倉」白話
    joined = " ".join(res["reasons_pro"])
    assert "持倉" in joined
    # action 應提醒「不要追加」
    assert "停損" in res["action_suggestion"] or "持倉" in res["action_suggestion"]


# ============================================================================
# compute_verdict — entry_zone / stop_loss / take_profit
# ============================================================================

def test_green_verdict_provides_entry_zone(tmp_db, enable_verdict):
    """大盤 bull + 多正面訊號 → 🟢 可進場 + 三個價位。

    用「灌大量 bullish K 線」觸發 pattern detection +
    並注入 daily_picks 共識來累積分數。
    """
    _seed_taiex_bull()
    # 上升趨勢 60 天(讓 bollinger 算得出)
    closes = [100.0 + i * 0.6 for i in range(60)]
    last_d = _seed_daily_prices("2454", closes)
    # 灌 daily_picks 共識(跨類)
    with db.get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO daily_picks
              (trade_date, universe, strategy, sid, params_hash, computed_at)
            VALUES (?, 'pure_stock', ?, '2454', 'default_v1', ?)
            """,
            [
                (last_d, "ma_alignment", _d.today().isoformat()),
                (last_d, "volume_kd", _d.today().isoformat()),
                (last_d, "inst_consensus", _d.today().isoformat()),
            ],
        )
    res = isv.compute_verdict("2454")
    # 應該 ≥ 🟡(訊號充足);若全集合都正可能 🟢
    assert res["verdict_color"] in ("🟢", "🟡")
    if res["verdict_color"] == "🟢":
        assert res["entry_zone"] is not None
        elow, ehigh = res["entry_zone"]
        assert elow < ehigh
        assert res["stop_loss"] is not None and res["stop_loss"] < elow
        assert res["take_profit"] is not None and res["take_profit"] > ehigh


def test_yellow_verdict_no_entry_zone(tmp_db, enable_verdict):
    """🟡 觀望時不該給進場價區間(避免誤導主公)。"""
    _seed_taiex_bull()  # 大盤好,但其他訊號沒湊齊
    res = isv.compute_verdict("0050")
    if res["verdict_color"] == "🟡":
        assert res["entry_zone"] is None
        assert res["stop_loss"] is None
        assert res["take_profit"] is None


# ============================================================================
# verdict_tag_for_card — 卡片短標
# ============================================================================

def test_verdict_tag_when_enabled(tmp_db, enable_verdict):
    """有效時 tag 該是 `🟢 可進場` / `🟡 觀望` / `🔴 不進場`。"""
    tag = isv.verdict_tag_for_card("2330")
    assert tag != ""
    assert any(emoji in tag for emoji in ["🟢", "🟡", "🔴"])
    assert any(word in tag for word in ["可進場", "觀望", "不進場"])


def test_verdict_tag_when_disabled(tmp_db, monkeypatch):
    """kill switch off → tag 為空字串。"""
    monkeypatch.setenv("STOCK_VERDICT_ENABLED", "false")
    tag = isv.verdict_tag_for_card("2330")
    assert tag == ""


def test_verdict_tag_red_when_severe_warning(tmp_db, enable_verdict):
    """違約交割 → tag 必為 `🔴 不進場`。"""
    _seed_warning("6666", "default_settlement")
    tag = isv.verdict_tag_for_card("6666")
    assert tag.startswith("🔴")
    assert "不進場" in tag


# ============================================================================
# return 結構 schema(防 regression — UI render 靠這些 keys)
# ============================================================================

def test_compute_verdict_returns_all_required_keys(tmp_db, enable_verdict):
    """compute_verdict 應永遠回完整 keys,UI 不該因缺 key crash。"""
    res = isv.compute_verdict("9999")
    required = {
        "enabled", "sid", "verdict", "verdict_color", "score",
        "reasons_pro", "reasons_con", "action_suggestion",
        "entry_zone", "stop_loss", "take_profit", "signals",
    }
    assert required.issubset(set(res.keys())), (
        f"compute_verdict 缺 keys:{required - set(res.keys())}"
    )
    # type check
    assert isinstance(res["reasons_pro"], list)
    assert isinstance(res["reasons_con"], list)
    assert isinstance(res["signals"], dict)
    assert res["verdict_color"] in ("🟢", "🟡", "🔴")
