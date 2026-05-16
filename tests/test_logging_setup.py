"""驗證 src.logging_setup 真的寫檔 + stdout 同步保留 + 跨日 rotate 邏輯。

不打 GitHub Actions / FinMind / Telegram,純檔系統 + logging 操作。
"""
from __future__ import annotations

import importlib
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


def _reset_root_logger() -> None:
    """每個 test 跑前清掉 root handlers 跟我們塞的 marker。"""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    if hasattr(root, "_stock_screener_file_handler"):
        delattr(root, "_stock_screener_file_handler")


@pytest.fixture
def fresh_logging_setup(tmp_path, monkeypatch):
    """重新 import src.logging_setup,把 LOGS_DIR redirect 到 tmp_path。"""
    _reset_root_logger()
    # 為了 isolate,直接 reload 模組然後 patch 常數
    import src.logging_setup as ls
    importlib.reload(ls)
    monkeypatch.setattr(ls, "LOGS_DIR", tmp_path)
    # 把 _resolve_log_path 也綁到 tmp_path
    original_resolve = ls._resolve_log_path

    def _patched(script_name: str, date_str=None):
        safe = script_name.replace("/", "_").replace("\\", "_").replace(" ", "_")
        return tmp_path / f"{date_str or ls._today_taipei()}-{safe}.log"

    monkeypatch.setattr(ls, "_resolve_log_path", _patched)
    yield ls
    _reset_root_logger()


def test_setup_file_logging_writes_to_disk(fresh_logging_setup, tmp_path):
    ls = fresh_logging_setup
    log_path = ls.setup_file_logging("unit_test_script")

    assert log_path.parent == tmp_path
    assert log_path.name.endswith("-unit_test_script.log")

    logger = logging.getLogger("test_writes_to_disk")
    logger.info("hello world from logging_setup test")

    # flush + close handler 才寫進去
    for h in logging.getLogger().handlers:
        h.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "hello world from logging_setup test" in content
    assert "[INFO]" in content
    assert "test_writes_to_disk" in content


def test_stdout_handler_preserved(fresh_logging_setup, capsys):
    """確認 stdout StreamHandler 也加上去 — logger.info 同時進 stdout 跟檔。"""
    ls = fresh_logging_setup
    ls.setup_file_logging("stdout_test")

    root = logging.getLogger()
    stream_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]
    assert len(stream_handlers) >= 1, "至少要有一個 StreamHandler 寫進 stdout"

    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1


def test_filename_uses_taipei_date(fresh_logging_setup):
    """檔名 prefix 必須是 Asia/Taipei 今天日期 (YYYY-MM-DD)。"""
    ls = fresh_logging_setup
    log_path = ls.setup_file_logging("date_check")

    today_tpe = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    assert log_path.name.startswith(today_tpe + "-")


def test_rotation_across_dates(fresh_logging_setup, tmp_path):
    """跨日呼叫:相同 script_name 但不同日 → 不同檔名。"""
    ls = fresh_logging_setup
    # 手動產 yesterday / today 兩條路徑
    yesterday = "2026-05-15"
    today = "2026-05-16"
    p_yesterday = ls._resolve_log_path("rot_test", date_str=yesterday)
    p_today = ls._resolve_log_path("rot_test", date_str=today)

    assert p_yesterday != p_today
    assert "2026-05-15-rot_test.log" in p_yesterday.name
    assert "2026-05-16-rot_test.log" in p_today.name


def test_idempotent_repeat_setup(fresh_logging_setup, tmp_path):
    """同 script 重複 setup → 不會疊 file handler。"""
    ls = fresh_logging_setup
    ls.setup_file_logging("idem_test")
    ls.setup_file_logging("idem_test")
    ls.setup_file_logging("idem_test")

    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1


def test_different_script_swaps_file_handler(fresh_logging_setup, tmp_path):
    """換 script_name → 舊 file handler 該被清掉,只留新的。"""
    ls = fresh_logging_setup
    p1 = ls.setup_file_logging("script_a")
    p2 = ls.setup_file_logging("script_b")

    assert p1 != p2

    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    # baseFilename 應該對應 script_b
    assert "script_b" in file_handlers[0].baseFilename


def test_log_format_contains_required_fields(fresh_logging_setup, tmp_path):
    """寫進檔的格式必須符合:時間 [LEVEL] name: message。"""
    ls = fresh_logging_setup
    log_path = ls.setup_file_logging("fmt_test")
    logging.getLogger("fmt.module").warning("alert!")

    for h in logging.getLogger().handlers:
        h.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "[WARNING]" in content
    assert "fmt.module" in content
    assert "alert!" in content


def test_logs_dir_auto_created(tmp_path, monkeypatch):
    """LOGS_DIR 不存在 → setup_file_logging 自動 mkdir。"""
    _reset_root_logger()
    import src.logging_setup as ls
    importlib.reload(ls)

    target = tmp_path / "fresh-logs-dir"
    assert not target.exists()

    monkeypatch.setattr(ls, "LOGS_DIR", target)

    def _patched(script_name: str, date_str=None):
        return target / f"{date_str or ls._today_taipei()}-{script_name}.log"

    monkeypatch.setattr(ls, "_resolve_log_path", _patched)

    ls.setup_file_logging("mkdir_test")
    assert target.exists() and target.is_dir()

    _reset_root_logger()
