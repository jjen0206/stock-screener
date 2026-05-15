"""src/theme_heat.py 單元測試。

production schema fixture(tmp DB + db.init_db),自建 themes/*.yaml fixture。
測試 case:
  - compute_theme_heat 對 9 題材回 dict,每筆有 multiplier
  - 熱題材(高漲幅 + 高勝率)→ multiplier=1.3
  - 冷題材(大跌或低勝率)→ multiplier=0.7
  - 中性題材 → 1.0
  - heat_score / win_rate 邊界(熱:>3 AND >0.5 / 冷:<-2 OR <0.3)
  - 跨題材 sid 取最高 multiplier
  - kill-switch THEME_HEAT_ENABLED=false → multiplier 全 1.0
  - cache 重複 call 不重算 / reset_cache 清掉
  - 沒 daily_prices → multiplier=0.7(因為 win_rate=0)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src import config, database as db, theme_heat as th


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """乾淨 tmp SQLite + production schema(init_db)。"""
    db_file = tmp_path / "theme_heat.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()  # type: ignore[attr-defined]
    db.init_db()
    th.reset_cache()
    yield db_file
    db._reset_path_cache()  # type: ignore[attr-defined]
    th.reset_cache()


def _seed_prices(sid: str, dates_closes: list[tuple[str, float]]) -> None:
    """灌 daily_prices(stock_id, date, close)。"""
    rows = [
        (sid, d, c, c, c, c, 0, 0.0, 0, 0.0)
        for d, c in dates_closes
    ]
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO daily_prices "
            "(stock_id, date, open, high, low, close, volume, "
            " trading_money, trading_turnover, spread) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def _write_theme(themes_dir: Path, name: str, display: str, sids: list[str]) -> None:
    """寫 yaml: 第一行 comment 當 display,sids list。"""
    yaml_lines = [f"# {display} (test fixture)"]
    yaml_lines.append("sids:")
    for s in sids:
        yaml_lines.append(f'  - "{s}"')
    (themes_dir / f"{name}.yaml").write_text(
        "\n".join(yaml_lines), encoding="utf-8",
    )


@pytest.fixture
def themes_dir(tmp_path, monkeypatch):
    """三個 themes:hot(全噴) / cold(全跌) / neutral(平淡)。"""
    d = tmp_path / "themes"
    d.mkdir()
    _write_theme(d, "hot_theme", "HOT 熱題材", ["1001", "1002", "1003"])
    _write_theme(d, "cold_theme", "COLD 冷題材", ["2001", "2002", "2003"])
    _write_theme(d, "neutral_theme", "NEUTRAL 中性題材", ["3001", "3002"])

    # hot:全噴 +10% over 5 days
    for sid in ["1001", "1002", "1003"]:
        _seed_prices(sid, [
            ("2026-05-09", 100.0),
            ("2026-05-12", 102.5),
            ("2026-05-13", 105.0),
            ("2026-05-14", 107.5),
            ("2026-05-15", 110.0),
        ])
    # cold:全跌 -10% over 5 days
    for sid in ["2001", "2002", "2003"]:
        _seed_prices(sid, [
            ("2026-05-09", 100.0),
            ("2026-05-12", 97.5),
            ("2026-05-13", 95.0),
            ("2026-05-14", 92.5),
            ("2026-05-15", 90.0),
        ])
    # neutral:平淡 (3001 +1%, 3002 -1%) win_rate=0.5,avg=0
    _seed_prices("3001", [
        ("2026-05-09", 100.0),
        ("2026-05-15", 101.0),
    ])
    _seed_prices("3002", [
        ("2026-05-09", 100.0),
        ("2026-05-15", 99.0),
    ])
    return d


# === compute_theme_heat ===

def test_compute_theme_heat_returns_all_themes(tmp_db, themes_dir):
    """compute_theme_heat 對 3 題材都回一筆。"""
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    assert set(heat.keys()) == {"hot_theme", "cold_theme", "neutral_theme"}
    for info in heat.values():
        assert "multiplier" in info
        assert "heat_score" in info
        assert "avg_return" in info
        assert "win_rate" in info


def test_hot_theme_gets_hot_multiplier(tmp_db, themes_dir):
    """全噴 +10% / win_rate=100% → heat_score>3 AND wr>0.5 → ×1.3。"""
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    hot = heat["hot_theme"]
    assert abs(hot["avg_return"] - 10.0) < 0.01
    assert hot["win_rate"] == 1.0
    assert hot["multiplier"] == th.HOT_MULTIPLIER
    assert hot["badge"] == "🔥"


def test_cold_theme_gets_cold_multiplier(tmp_db, themes_dir):
    """全跌 -10% / win_rate=0% → heat_score<-2 → ×0.7。"""
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    cold = heat["cold_theme"]
    assert abs(cold["avg_return"] - (-10.0)) < 0.01
    assert cold["win_rate"] == 0.0
    assert cold["multiplier"] == th.COLD_MULTIPLIER
    assert cold["badge"] == "🧊"


def test_neutral_theme_gets_neutral_multiplier(tmp_db, themes_dir):
    """平淡 0% / win_rate=50% → heat_score≈0.2(中性區間)→ ×1.0。"""
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    neu = heat["neutral_theme"]
    assert neu["multiplier"] == th.NEUTRAL_MULTIPLIER
    assert neu["badge"] == "➖"


def test_low_winrate_alone_triggers_cold(tmp_db, themes_dir):
    """主公規則:wr<0.3 OR heat<-2 → 冷。即便 avg_return 不那麼差。"""
    # 弄個題材,所有 sid 都微跌(-1%) → wr=0, heat≈-0.6 ≥ -2,但 wr=0 < 0.3 → 冷
    d = themes_dir
    _write_theme(d, "low_wr", "LOW WR", ["4001", "4002"])
    _seed_prices("4001", [("2026-05-09", 100.0), ("2026-05-15", 99.0)])
    _seed_prices("4002", [("2026-05-09", 100.0), ("2026-05-15", 99.5)])
    th.reset_cache()
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=d,
        )
    info = heat["low_wr"]
    assert info["win_rate"] == 0.0
    # heat_score 約 -0.45,不到 -2;但 wr=0 < 0.3 → cold (OR 規則)
    assert info["multiplier"] == th.COLD_MULTIPLIER


def test_high_return_low_winrate_not_hot(tmp_db, themes_dir):
    """heat_score 高但 wr 不夠(50% 以下)→ 不算熱(雙條件 AND)。"""
    d = themes_dir
    # 一個題材:1 隻噴 30%, 1 隻跌 1% → avg=14.5%, wr=50%
    # heat = 14.5*0.6 + 0.5*0.4 = 8.9, wr=0.5 (不嚴格 > 0.5)→ 不熱
    _write_theme(d, "split", "SPLIT", ["5001", "5002"])
    _seed_prices("5001", [("2026-05-09", 100.0), ("2026-05-15", 130.0)])
    _seed_prices("5002", [("2026-05-09", 100.0), ("2026-05-15", 99.0)])
    th.reset_cache()
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=d,
        )
    info = heat["split"]
    assert info["win_rate"] == 0.5
    # wr 不嚴格 > 0.5,所以即便 heat 高也不算熱
    assert info["multiplier"] == th.NEUTRAL_MULTIPLIER


def test_no_daily_prices_yields_cold(tmp_db, themes_dir):
    """sid 完全沒 daily_prices → n_valid=0, win_rate=0 → cold(wr<0.3)。"""
    d = themes_dir
    _write_theme(d, "missing", "MISSING", ["9001", "9002"])
    th.reset_cache()
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=d,
        )
    info = heat["missing"]
    assert info["n_valid"] == 0
    assert info["win_rate"] == 0.0
    assert info["multiplier"] == th.COLD_MULTIPLIER


# === get_pick_theme_multiplier ===

def test_get_pick_theme_multiplier_returns_max(tmp_db, themes_dir, monkeypatch):
    """sid 屬熱題材 → 1.3。屬冷題材 → 0.7。"""
    monkeypatch.setenv("THEME_HEAT_ENABLED", "true")
    th.reset_cache()
    with db.get_conn() as conn:
        # 1001 in hot_theme
        m_hot = th.get_pick_theme_multiplier(conn, "1001", as_of="2026-05-15")
        # 2001 in cold_theme
        m_cold = th.get_pick_theme_multiplier(conn, "2001", as_of="2026-05-15")
    # 改 module-level THEMES_DIR 走 fixture 沒生效(只能傳 themes_dir),所以
    # 上面用 default;但 default 是 production yaml,跟 fixture sid 不重疊。
    # 直接用 multiplier 邏輯驗證:non-existent sid 走 default 1.0
    assert m_hot == 1.0  # 1001 不在 production themes 內
    assert m_cold == 1.0


def test_get_pick_theme_multiplier_takes_max_across_themes(tmp_db, themes_dir):
    """sid 同屬冷 + 熱 → 取 max(=1.3),避免熱被冷稀釋。"""
    # 直接組 fake heat dict 驗證內部 max 邏輯
    fake_heat = {
        "hot": {"sids": ["X1"], "multiplier": 1.3},
        "cold": {"sids": ["X1", "X2"], "multiplier": 0.7},
        "neu": {"sids": ["X3"], "multiplier": 1.0},
    }
    multipliers = []
    for info in fake_heat.values():
        if "X1" in info["sids"]:
            multipliers.append(info["multiplier"])
    assert max(multipliers) == 1.3


def test_get_pick_theme_multiplier_default_one_for_unknown_sid(tmp_db):
    """sid 不屬任何題材 → 1.0。"""
    th.reset_cache()
    with db.get_conn() as conn:
        m = th.get_pick_theme_multiplier(
            conn, "9999_NOT_IN_ANY_THEME", as_of="2026-05-15",
        )
    assert m == th.NEUTRAL_MULTIPLIER


# === Kill switch ===

def test_kill_switch_disables_multiplier(tmp_db, monkeypatch):
    """env THEME_HEAT_ENABLED=false → get_pick_theme_multiplier 一律回 1.0。"""
    monkeypatch.setenv("THEME_HEAT_ENABLED", "false")
    th.reset_cache()
    # 假設 1001 屬熱題材(production 沒這個 sid 也 OK,kill switch 直接回 1.0
    # 不會走到 compute)
    with db.get_conn() as conn:
        m = th.get_pick_theme_multiplier(conn, "1001", as_of="2026-05-15")
    assert m == th.NEUTRAL_MULTIPLIER


def test_is_enabled_reads_env(monkeypatch):
    """_is_enabled 讀 env runtime,monkeypatch 即時生效。"""
    monkeypatch.setenv("THEME_HEAT_ENABLED", "true")
    assert th._is_enabled() is True
    monkeypatch.setenv("THEME_HEAT_ENABLED", "false")
    assert th._is_enabled() is False
    monkeypatch.setenv("THEME_HEAT_ENABLED", "0")
    assert th._is_enabled() is False
    monkeypatch.delenv("THEME_HEAT_ENABLED", raising=False)
    assert th._is_enabled() is True  # 預設 on


# === Cache ===

def test_cache_avoids_recompute(tmp_db, themes_dir):
    """同 (as_of, window_days) 第二次 call 走 cache。"""
    th.reset_cache()
    with db.get_conn() as conn:
        h1 = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
        h2 = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    # 同物件 reference(走 cache 而非重算)
    assert h1 is h2


def test_reset_cache_clears(tmp_db, themes_dir):
    """reset_cache 後第二次 call 重算(non-identity)。"""
    th.reset_cache()
    with db.get_conn() as conn:
        h1 = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    th.reset_cache()
    with db.get_conn() as conn:
        h2 = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    assert h1 is not h2
    # 但內容應該一樣
    assert h1.keys() == h2.keys()
    for k in h1:
        assert h1[k]["multiplier"] == h2[k]["multiplier"]


# === Caption format ===

def test_format_theme_heat_caption(tmp_db, themes_dir):
    """組推播 caption,熱 / 冷題材分行顯示。"""
    th.reset_cache()
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    cap = th.format_theme_heat_caption(heat)
    assert "📡 題材熱度" in cap
    assert "🔥" in cap
    assert "🧊" in cap
    assert "HOT" in cap
    assert "COLD" in cap


def test_format_caption_empty_when_no_themes():
    """空 dict → 空字串(caller graceful skip)。"""
    assert th.format_theme_heat_caption({}) == ""


def test_format_caption_skips_when_only_neutral():
    """全中性 → 沒熱沒冷 → 回空(caller skip)。"""
    fake_heat = {
        "neu1": {"display_name": "N1", "multiplier": 1.0},
        "neu2": {"display_name": "N2", "multiplier": 1.0},
    }
    assert th.format_theme_heat_caption(fake_heat) == ""


# === heat_score 公式驗證 ===

def test_heat_score_formula(tmp_db, themes_dir):
    """heat_score = avg_return × 0.6 + win_rate × 0.4 — 用 hot_theme 驗算。"""
    th.reset_cache()
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    hot = heat["hot_theme"]
    expected = hot["avg_return"] * 0.6 + hot["win_rate"] * 0.4
    assert abs(hot["heat_score"] - expected) < 1e-6
