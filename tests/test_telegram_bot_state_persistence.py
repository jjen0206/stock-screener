"""telegram_bot.state e2e:dump → CSV → load roundtrip + boot wiring regression。

Memory rule(主公 confirmed):persistence push 路徑必須兩層保護:
  1. dump_to_csv → CSV → load_from_csv 不掉資料
  2. preload_snapshots 在 boot 從 CSV ingest 回 SQLite

兩個都要 e2e test 守住,否則 silent fail。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.telegram_bot import state  # noqa: E402


@pytest.fixture
def tmp_state_env(monkeypatch, tmp_path):
    """獨立的 SNAPSHOT_DIR + DB,模擬 GHA runner 環境。

    為了讓 dump_to_csv 不被 `_db_inside_project` 擋掉,monkeypatch 把
    config.PROJECT_ROOT、SNAPSHOT_DIR、STATE_CSV、DATABASE_PATH 全指向 tmp_path。
    """
    snap = tmp_path / "twse_snapshot"
    snap.mkdir()
    db_file = tmp_path / "state.db"

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    monkeypatch.setattr(state, "SNAPSHOT_DIR", snap)
    monkeypatch.setattr(state, "STATE_CSV", snap / "telegram_bot_state.csv")

    db._reset_path_cache()
    db.init_db()
    yield {"snap": snap, "db": db_file}
    db._reset_path_cache()


# === KV API ===

def test_get_state_returns_default_when_missing(tmp_state_env):
    assert state.get_state("nope") is None
    assert state.get_state("nope", default="x") == "x"


def test_set_then_get_state(tmp_state_env):
    state.set_state("foo", "bar")
    assert state.get_state("foo") == "bar"


def test_last_update_id_defaults_zero(tmp_state_env):
    assert state.get_last_update_id() == 0


def test_set_get_last_update_id(tmp_state_env):
    state.set_last_update_id(12345)
    assert state.get_last_update_id() == 12345


def test_last_update_id_handles_corrupt_value(tmp_state_env):
    """寫進非整數 → get 回 0 不 raise。"""
    state.set_state(state.KEY_LAST_UPDATE_ID, "not-a-number")
    assert state.get_last_update_id() == 0


# === dump / load roundtrip (層 1 保護)===

def test_dump_to_csv_writes_file(tmp_state_env):
    state.set_last_update_id(999)
    n = state.dump_to_csv()
    assert n >= 1
    assert state.STATE_CSV.exists()
    body = state.STATE_CSV.read_text(encoding="utf-8")
    assert "last_update_id" in body
    assert "999" in body


def test_dump_load_roundtrip(tmp_state_env):
    state.set_last_update_id(42)
    state.set_state("misc", "hello")
    state.dump_to_csv()

    # 清空 SQLite 模擬 GHA fresh container
    with db.get_conn() as conn:
        conn.execute("DELETE FROM telegram_bot_state")

    assert state.get_last_update_id() == 0
    n = state.load_from_csv()
    assert n >= 2
    assert state.get_last_update_id() == 42
    assert state.get_state("misc") == "hello"


def test_dump_to_string_does_not_need_snapshot_dir(tmp_state_env):
    state.set_last_update_id(7)
    s = state.dump_to_string()
    assert "last_update_id" in s
    assert "7" in s


def test_load_from_string_ingest(tmp_state_env):
    csv = "key,value,updated_at\nlast_update_id,888,2026-05-18T00:00:00\n"
    n = state.load_from_string(csv)
    assert n == 1
    assert state.get_last_update_id() == 888


def test_load_skips_when_csv_missing(tmp_state_env):
    assert not state.STATE_CSV.exists()
    assert state.load_from_csv() == 0


def test_dump_skips_when_snapshot_dir_missing(monkeypatch, tmp_path):
    """SNAPSHOT_DIR 不存在 → silent skip (-1),不爆。"""
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(state, "SNAPSHOT_DIR", tmp_path / "doesnotexist")
    monkeypatch.setattr(state, "STATE_CSV", tmp_path / "doesnotexist" / "x.csv")
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "x.db"))
    db._reset_path_cache()
    db.init_db()
    state.set_last_update_id(1)
    assert state.dump_to_csv() == -1
    db._reset_path_cache()


def test_dump_skips_when_db_outside_project(monkeypatch, tmp_path):
    """DB 在 PROJECT_ROOT 外(pytest tmp_path 預設)→ silent skip,
    不污染 commit。確保任何 pytest run 都安全。"""
    # 注意:tmp_state_env fixture monkeypatch 了 PROJECT_ROOT,此 test 不用 fixture
    snap = tmp_path / "twse_snapshot"
    snap.mkdir()
    # PROJECT_ROOT 故意不蓋,DB 路徑會在 _db_inside_project 判否
    other_root = tmp_path / "other_root"
    other_root.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", other_root)
    monkeypatch.setattr(state, "SNAPSHOT_DIR", snap)
    monkeypatch.setattr(state, "STATE_CSV", snap / "telegram_bot_state.csv")
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "x.db"))
    db._reset_path_cache()
    db.init_db()
    state.set_last_update_id(1)
    assert state.dump_to_csv() == -1
    db._reset_path_cache()


# === boot wiring(層 2 保護)===
# preload_snapshots 在容器 boot 時必須 ingest telegram_bot_state.csv,
# 否則 last_update_id 永遠 0,雙向問答會重複處理同一條訊息

def test_preload_snapshots_wires_telegram_bot_state(tmp_state_env):
    """regression guard:db.preload_snapshots 必須讀回 telegram_bot_state.csv。

    流程:
      1. 寫一筆 state + dump CSV
      2. 清空 SQLite
      3. 跑 preload_snapshots
      4. SQLite 應該重新有 last_update_id
    """
    state.set_last_update_id(2024)
    state.dump_to_csv()

    with db.get_conn() as conn:
        conn.execute("DELETE FROM telegram_bot_state")
    assert state.get_last_update_id() == 0

    counts = db.preload_snapshots(
        snapshot_dir=tmp_state_env["snap"],
    )
    # 沒 require key 一定要在 counts(其他 CSV 可能缺),但 state 應該已經回來
    assert state.get_last_update_id() == 2024
    # 順便驗 counts 帶這個 key(讓 workflow log 看得到)
    assert counts.get("telegram_bot_state", 0) >= 1
