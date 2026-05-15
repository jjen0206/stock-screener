"""src/theme_heat.py 單元測試(2026-05-15 v2:冷改 hard exclude)。

production schema fixture(tmp DB + db.init_db),自建 themes/*.yaml fixture。
測試 case:
  - compute_theme_heat 對 N 題材回 dict,每筆有 multiplier(可能 None)
  - 熱題材(高漲幅 + 高勝率)→ multiplier=1.3,badge=🔥
  - 冷題材(大跌或低勝率)→ multiplier=None,badge=🚫(hard exclude)
  - 中性題材 → 1.0,badge=➖
  - heat_score / win_rate 邊界(熱:>3 AND >0.5 / 冷:<-2 OR <0.3)
  - 跨題材 sid 取最熱 multiplier:有熱→1.3 / 沒熱有中性→1.0(中性壓過冷)/
    全冷→None
  - 不屬任何題材 → 1.0(沒題材 ≠ 冷,主公明確拍板)
  - kill-switch THEME_HEAT_ENABLED=false → multiplier 全 1.0,不擋任何 sid
  - cache 重複 call 不重算 / reset_cache 清掉
  - 沒 daily_prices → multiplier=None(因為 win_rate=0)
  - format_theme_heat_caption 含「🚫 冷題材已擋」+ excluded count
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


def test_cold_theme_gets_none_multiplier_for_hard_exclude(tmp_db, themes_dir):
    """全跌 -10% / win_rate=0% → heat_score<-2 → multiplier=None(擋)+ badge=🚫。"""
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    cold = heat["cold_theme"]
    assert abs(cold["avg_return"] - (-10.0)) < 0.01
    assert cold["win_rate"] == 0.0
    assert cold["multiplier"] is None  # hard exclude sentinel
    assert cold["multiplier"] is th.COLD_EXCLUDE
    assert cold["badge"] == "🚫"


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
    """主公規則:wr<0.3 OR heat<-2 → 冷(None 擋掉)。即便 avg_return 不那麼差。"""
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
    # heat_score 約 -0.45,不到 -2;但 wr=0 < 0.3 → cold (OR 規則)→ None
    assert info["multiplier"] is None


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


def test_no_daily_prices_yields_neutral_not_cold(tmp_db, themes_dir):
    """sid 完全沒 daily_prices → n_valid=0 < MIN_VALID_FOR_CLASSIFY →
    退保守 multiplier=1.0(不分類成冷)。

    避免「DB 資料缺口」被誤判成「冷」造成不必要 hard exclude
    (e.g. 雲端容器剛重啟還沒 sync FinMind 時整題材被誤擋)。
    """
    d = themes_dir
    _write_theme(d, "missing", "MISSING", ["9001", "9002"])
    th.reset_cache()
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=d,
        )
    info = heat["missing"]
    assert info["n_valid"] == 0
    assert info["multiplier"] == th.NEUTRAL_MULTIPLIER
    assert info["badge"] == "➖"


# === get_pick_theme_multiplier ===

def test_get_pick_theme_multiplier_default_one_for_unknown_sid(tmp_db):
    """sid 不屬任何題材 → 1.0(沒題材 ≠ 冷,主公拍板)。"""
    th.reset_cache()
    with db.get_conn() as conn:
        m = th.get_pick_theme_multiplier(
            conn, "9999_NOT_IN_ANY_THEME", as_of="2026-05-15",
        )
    assert m == th.NEUTRAL_MULTIPLIER


def test_get_pick_theme_multiplier_returns_none_for_cold_only_sid():
    """直接驗 max-rule 內部邏輯:sid 只在冷題材 → None。"""
    fake_heat = {
        "cold1": {"sids": ["X1"], "multiplier": None},
        "cold2": {"sids": ["X1"], "multiplier": None},
    }
    found = False
    valid = []
    for info in fake_heat.values():
        if "X1" in info["sids"]:
            found = True
            if info["multiplier"] is not None:
                valid.append(info["multiplier"])
    assert found
    assert not valid  # 全部是 None → caller 回 COLD_EXCLUDE


def test_hot_beats_cold_when_sid_in_both():
    """sid 同屬冷 + 熱 → 取熱(1.3),不被冷擋掉。"""
    fake_heat = {
        "hot": {"sids": ["X1"], "multiplier": 1.3},
        "cold": {"sids": ["X1", "X2"], "multiplier": None},
    }
    valid = [
        info["multiplier"] for info in fake_heat.values()
        if "X1" in info["sids"] and info["multiplier"] is not None
    ]
    assert valid == [1.3]
    # X2 只在冷題材
    valid_x2 = [
        info["multiplier"] for info in fake_heat.values()
        if "X2" in info["sids"] and info["multiplier"] is not None
    ]
    assert valid_x2 == []


def test_neutral_beats_cold_when_sid_in_both():
    """sid 同屬中性 + 冷 → 取中性(1.0),不被擋。中性壓過冷。"""
    fake_heat = {
        "neu": {"sids": ["X1"], "multiplier": 1.0},
        "cold": {"sids": ["X1"], "multiplier": None},
    }
    valid = [
        info["multiplier"] for info in fake_heat.values()
        if "X1" in info["sids"] and info["multiplier"] is not None
    ]
    assert max(valid) == 1.0  # 不被冷擋


def test_get_pick_theme_multiplier_real_pipeline_excludes_cold(
    tmp_db, themes_dir, monkeypatch,
):
    """End-to-end:real pipeline,sid 只在 fixture 冷題材內 → None。

    注意:get_pick_theme_multiplier 使用 module-level THEMES_DIR,fixture
    沒法傳 dir。改 monkeypatch 把 THEMES_DIR 指到 fixture 路徑。
    """
    monkeypatch.setenv("THEME_HEAT_ENABLED", "true")
    monkeypatch.setattr(th, "THEMES_DIR", themes_dir)
    th.reset_cache()
    with db.get_conn() as conn:
        # 2001 in cold_theme(全跌 -10%)→ multiplier=None
        m_cold = th.get_pick_theme_multiplier(conn, "2001", as_of="2026-05-15")
        # 1001 in hot_theme(全噴 +10%)→ multiplier=1.3
        m_hot = th.get_pick_theme_multiplier(conn, "1001", as_of="2026-05-15")
    assert m_cold is None  # hard exclude
    assert m_hot == th.HOT_MULTIPLIER


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
    """組推播 caption,熱題材加分 / 冷題材已擋 分行顯示。"""
    th.reset_cache()
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    cap = th.format_theme_heat_caption(heat)
    assert "📡 題材熱度" in cap
    assert "🔥" in cap
    assert "🚫" in cap
    assert "已擋" in cap
    assert "加分" in cap
    assert "HOT" in cap
    assert "COLD" in cap


def test_format_caption_includes_excluded_count(tmp_db, themes_dir):
    """excluded dict 提供 → caption 顯示「N 檔」+ 各題材數字。

    excluded 的 key 是 display_name(parser strip 完括號後的值),
    跟 heat dict 內 info["display_name"] 一致。
    """
    th.reset_cache()
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    cold_display = heat["cold_theme"]["display_name"]
    excluded = {cold_display: ["2001", "2002"]}
    cap = th.format_theme_heat_caption(heat, excluded=excluded)
    assert "2 檔" in cap
    assert "COLD 2" in cap  # short label = "COLD",count = 2


def test_format_caption_no_excluded_falls_back_to_names(tmp_db, themes_dir):
    """沒提供 excluded → 只列題材名,不顯數字(legacy caller 不爆)。"""
    th.reset_cache()
    with db.get_conn() as conn:
        heat = th.compute_theme_heat(
            conn, as_of="2026-05-15", themes_dir=themes_dir,
        )
    cap = th.format_theme_heat_caption(heat, excluded=None)
    assert "🚫" in cap
    assert "COLD" in cap
    # 不含 "N 檔" 數字摘要(降級)
    assert "檔 (" not in cap


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
