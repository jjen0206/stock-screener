"""共用 pytest fixtures。

Round 2 重構新增 — 之前 50 個 test file 各自寫一份 `tmp_db`,
重複度 100%。這裡集中提供 canonical fixture:

- tmp_db: 黃金路徑(yield + 前後 reset path cache + init_db)
  共 32 個檔走這個 pattern,可以漸進 refactor 拿掉 local 定義。
- tmp_db_no_teardown: return-form(18 個 test file 走這個,沒 teardown
  reset)。語意不同所以**另一個 fixture name**,不會誤蓋。

Local fixture 仍然會 override conftest fixture(pytest 預設行為),所以
這個檔案只是把「default」拉到 tests/ 層,不會破壞任何既有客製化。
"""
from __future__ import annotations

import pytest

from src import config, database as db


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """tmp DATABASE_PATH + init schema + 前後 reset _path_cache。

    yield db_file(test 內若需要絕對 path 可拿來用)。teardown 一定
    reset cache 避免 cross-test 污染 — db._path_cache 是模組級全域。
    """
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    yield db_file
    db._reset_path_cache()
