"""src.notifier.format_yesterday_recap:單元 + 結構性測試。

對齊既有 test_notifier_premium_section.py pattern,用 db.init_db() 建 production
schema(不自編 CREATE TABLE)。守住:

1. pick_outcomes 空 / load_daily_picks 空 → 回空字串(caller graceful skip)
2. confluence < N → skip(過濾後沒 qualified)
3. ML threshold 過 + return_d1 有資料 → recap text 包含關鍵欄位
4. format_top_picks_message:有 recap → 訊息頂部含「昨日 picks 複盤」
5. format_top_picks_message:無 recap → 不出現該字樣
6. Telegram channel 用 *bold*;Discord channel 用 **bold**
7. Strategy 表現 section 受 min 3 fires 過濾
"""
from __future__ import annotations

import pytest

from src import config, database as db, notifier


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "notifier_recap.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    db._reset_path_cache()
    db.init_db()
    yield tmp_path
    db._reset_path_cache()


def _seed_daily_picks(date: str = "2026-05-11") -> None:
    """灌 daily_picks 模擬一檔 confluence=2 + ML 過閾值 pick(2330 命中兩個 strategies)。"""
    agg = {
        "2330": {
            "name": "台積電",
            "signals": ["量價KD", "多頭排列"],
            "details": {
                "volume_kd": {
                    "stock_id": "2330", "name": "台積電",
                    "close": 600.0, "atr14": 12.0, "matched_at": date,
                },
                "ma_alignment": {
                    "stock_id": "2330", "name": "台積電",
                    "close": 600.0, "ma5": 595.0, "ma20": 580.0,
                },
            },
        },
        # confluence=1(只命中 volume_kd 一個 strategy)— 應被 confluence_n=2 filter 掉
        "2317": {
            "name": "鴻海",
            "signals": ["量價KD"],
            "details": {
                "volume_kd": {
                    "stock_id": "2317", "name": "鴻海",
                    "close": 200.0, "atr14": 5.0, "matched_at": date,
                },
            },
        },
    }
    # ma_alignment ML threshold 0.55 → 給 2330 prob=0.7 過閾
    db.dump_daily_picks(date, "pure_stock", agg, ml_probs={"2330": 0.70, "2317": 0.20})


def _seed_pick_outcomes(date: str = "2026-05-11") -> None:
    db.dump_pick_outcomes([
        {
            "pick_date": date, "sid": "2330", "strategy": "volume_kd",
            "entry_close": 600.0, "return_d1": 2.5,
            "return_d3": 3.0, "return_d5": None, "return_d10": None,
            "hit_target": 0.0, "stopped_out": 0.0,
            "evaluated_at": "2026-05-13T14:00:00+00:00",
        },
        {
            "pick_date": date, "sid": "2330", "strategy": "ma_alignment",
            "entry_close": 600.0, "return_d1": 2.5,
            "return_d3": 3.0, "return_d5": None, "return_d10": None,
            "hit_target": 0.0, "stopped_out": 0.0,
            "evaluated_at": "2026-05-13T14:00:00+00:00",
        },
        # 2317 confluence=1 應被 filter,outcome 有也不會出現在 picks 統計
        {
            "pick_date": date, "sid": "2317", "strategy": "volume_kd",
            "entry_close": 200.0, "return_d1": -0.8,
            "return_d3": None, "return_d5": None, "return_d10": None,
            "hit_target": 0.0, "stopped_out": 0.0,
            "evaluated_at": "2026-05-13T14:00:00+00:00",
        },
    ])


# === Edge cases:空回 ===

def test_recap_empty_when_no_outcomes(tmp_db):
    """pick_outcomes 表空 → 回 ''。"""
    assert notifier.format_yesterday_recap() == ""


def test_recap_empty_when_no_daily_picks(tmp_db):
    """pick_outcomes 有但 daily_picks 沒 → 回 ''(無法 reproduce filter)。"""
    _seed_pick_outcomes()
    assert notifier.format_yesterday_recap() == ""


def test_recap_empty_when_no_confluence_picks(tmp_db):
    """只有 confluence<2 的 picks → 回 ''(qualified empty)。"""
    # 只灌 2317(confluence=1)的 daily_picks + outcome
    agg = {
        "2317": {
            "name": "鴻海", "signals": ["量價KD"],
            "details": {
                "volume_kd": {
                    "stock_id": "2317", "name": "鴻海", "close": 200.0,
                },
            },
        },
    }
    db.dump_daily_picks("2026-05-11", "pure_stock", agg, ml_probs={"2317": 0.20})
    db.dump_pick_outcomes([{
        "pick_date": "2026-05-11", "sid": "2317", "strategy": "volume_kd",
        "entry_close": 200.0, "return_d1": -0.8,
        "return_d3": None, "return_d5": None, "return_d10": None,
        "hit_target": 0.0, "stopped_out": 0.0,
        "evaluated_at": "2026-05-13T14:00:00+00:00",
    }])
    assert notifier.format_yesterday_recap() == ""


# === Happy path ===

def test_recap_telegram_includes_key_fields(tmp_db):
    """有 confluence pick + return_d1 → recap 含日期 / 命中率 / 平均報酬 / 最佳。"""
    _seed_daily_picks()
    _seed_pick_outcomes()
    out = notifier.format_yesterday_recap(channel="telegram")
    assert "昨日 picks 複盤(2026-05-11)" in out
    assert "1/1 picks 上漲" in out
    assert "命中率 100%" in out
    assert "+2.50%" in out  # avg return
    assert "2330 +2.5%" in out  # best
    # Telegram channel:單星
    assert "*昨日 picks 複盤(2026-05-11)*" in out


def test_recap_discord_uses_double_star(tmp_db):
    _seed_daily_picks()
    _seed_pick_outcomes()
    out = notifier.format_yesterday_recap(channel="discord")
    assert "**昨日 picks 複盤(2026-05-11)**" in out


def test_recap_skips_low_sample_strategies(tmp_db):
    """單一 strategy fire 數 < 3 → 不出現在「策略表現」行。"""
    _seed_daily_picks()
    _seed_pick_outcomes()
    out = notifier.format_yesterday_recap()
    # 各策略只 1-2 fires → min 3 fires 過濾後該行不顯
    assert "策略表現:" not in out


def test_recap_strategy_breakdown_when_enough_fires(tmp_db):
    """≥ 3 fires of same strategy → 出現在策略表現行。"""
    _seed_daily_picks()
    # 灌 4 筆 volume_kd outcomes(不同 sids)
    extra = [
        {
            "pick_date": "2026-05-11", "sid": f"100{i}", "strategy": "volume_kd",
            "entry_close": 100.0, "return_d1": 1.5 if i % 2 == 0 else -0.5,
            "return_d3": None, "return_d5": None, "return_d10": None,
            "hit_target": 0.0, "stopped_out": 0.0,
            "evaluated_at": "2026-05-13T14:00:00+00:00",
        }
        for i in range(4)
    ]
    db.dump_pick_outcomes(extra)
    _seed_pick_outcomes()  # 把 2330/2317 也灌進去
    out = notifier.format_yesterday_recap()
    assert "策略表現:" in out
    assert "量價KD" in out  # volume_kd label


# === 整合進 format_top_picks_message ===

def _fake_pick_dict(rank: int = 1) -> dict:
    return {
        "rank": rank, "sid": "2330", "name": "台積電",
        "close": 600.0, "pct_change": 1.0,
        "matched_strategies": ["volume_kd", "ma_alignment"],
        "matched_labels": ["量價KD", "多頭排列"],
        "ml_prob": 0.70, "target_low": 620.0, "target_high": 640.0,
        "stop": 580.0, "ev": 0.025, "risk_reward": 2.0,
        "industry": "半導體業", "industry_heat": 0,
        "win_rate": None,
        "analyst_target_mean": None, "analyst_num": None,
        "holders_1000up_count": None,
    }


def test_top_picks_message_includes_recap_at_top(tmp_db):
    """主訊息含 recap section,且在第一個 SEPARATOR 之後 / 第一個 pick 之前。"""
    _seed_daily_picks()
    _seed_pick_outcomes()
    out = notifier.format_top_picks_message(
        picks=[_fake_pick_dict()], date="2026-05-12",
    )
    assert "昨日 picks 複盤(2026-05-11)" in out

    # 順序檢查:recap 在 picks 之前
    idx_recap = out.index("昨日 picks 複盤")
    idx_pick = out.index("2330")
    # 「2330」也會出現在 recap 內(最佳 2330 +2.5%),所以找 picks 區段內的
    idx_pick_block = out.index("命中 2 策略")
    assert idx_recap < idx_pick_block


def test_top_picks_message_no_recap_when_outcomes_empty(tmp_db):
    """pick_outcomes 空 → 訊息不含「昨日 picks 複盤」字樣。"""
    out = notifier.format_top_picks_message(
        picks=[_fake_pick_dict()], date="2026-05-12",
    )
    assert "昨日 picks 複盤" not in out


def test_top_picks_message_recap_survives_no_picks(tmp_db):
    """picks 空 + recap 有資料 → recap 仍出現(放在 empty fallback 之前)。"""
    _seed_daily_picks()
    _seed_pick_outcomes()
    out = notifier.format_top_picks_message(
        picks=[], date="2026-05-12",
    )
    assert "昨日 picks 複盤(2026-05-11)" in out
    assert "今日無符合" in out  # empty fallback 也出現


# === Shared scoring helper(M4/U1 known issue:recap 排序對齊 _select_top_picks) ===

def test_compute_pick_score_analyst_boost_breaks_ml_prob_tie():
    """兩 picks 同 ml_prob、同命中策略數 → 有 analyst_target_mean 共識的排前面。"""
    key_with_analyst = notifier._compute_pick_score(
        sid="2330", ml_prob=0.70,
        matched_strategies=["volume_kd", "ma_alignment"],
        analyst_target_mean=620.0,
    )
    key_no_analyst = notifier._compute_pick_score(
        sid="2454", ml_prob=0.70,
        matched_strategies=["volume_kd", "ma_alignment"],
        analyst_target_mean=None,
    )
    # tuple ascending → 有 analyst 的 key 較小 → sort 後排前
    assert key_with_analyst < key_no_analyst


def test_compute_pick_score_falls_back_to_ml_prob_when_no_analyst():
    """都沒 analyst 欄位 → 只看 ml_prob desc(graceful fallback)。"""
    key_hi = notifier._compute_pick_score(
        sid="2330", ml_prob=0.80,
        matched_strategies=["volume_kd"],
        analyst_target_mean=None,
    )
    key_lo = notifier._compute_pick_score(
        sid="2317", ml_prob=0.55,
        matched_strategies=["volume_kd"],
        analyst_target_mean=None,
    )
    # ml_prob 較高 → key 較小 → 排前
    assert key_hi < key_lo
    # 缺欄位 / ml_prob=None 也不爆
    assert notifier._compute_pick_score(
        sid="9999", ml_prob=None, matched_strategies=None,
    ) == (0, 0.0, 0, "9999")
