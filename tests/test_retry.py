"""src/_retry.py with_retry helper 測試。"""
from __future__ import annotations

import time

import pytest

from src._retry import with_retry


def test_with_retry_succeeds_first_try():
    """成功不該 sleep。"""
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        return "ok"
    t0 = time.time()
    result = with_retry(fn, max_attempts=3, base_delay=1.0, quiet=True)
    elapsed = time.time() - t0
    assert result == "ok"
    assert calls["n"] == 1
    assert elapsed < 0.5  # 沒 sleep


def test_with_retry_succeeds_after_2_failures():
    """前 2 次 fail,第 3 次 OK。"""
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"fail {calls['n']}")
        return "ok"
    t0 = time.time()
    # 用較小 base_delay 加速測試
    result = with_retry(fn, max_attempts=3, base_delay=0.05, quiet=True)
    elapsed = time.time() - t0
    assert result == "ok"
    assert calls["n"] == 3
    # 兩次 sleep:0.05 + 0.10 = 0.15s,加 fn 開銷 < 0.3s
    assert 0.1 < elapsed < 0.5


def test_with_retry_all_fail_raises_last():
    """全 fail → raise 最後的 exception。"""
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        raise RuntimeError(f"attempt {calls['n']}")
    with pytest.raises(RuntimeError, match="attempt 3"):
        with_retry(fn, max_attempts=3, base_delay=0.01, quiet=True)
    assert calls["n"] == 3


def test_with_retry_exponential_backoff(monkeypatch):
    """delay 該是 1 → 2 → 4(指數)。"""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda d: sleeps.append(d))
    def fn():
        raise RuntimeError("always fail")
    with pytest.raises(RuntimeError):
        with_retry(fn, max_attempts=4, base_delay=1.0, quiet=True)
    # attempts 1, 2, 3 之後各 sleep 1, 2, 4(第 4 次失敗不再 sleep)
    assert sleeps == [1.0, 2.0, 4.0]


# === custom delays(IP / token ban 用 long backoff) ===


def test_with_retry_custom_delays_used_in_order(monkeypatch):
    """提供 delays 時,該按序 sleep,attempts = len(delays) + 1。"""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda d: sleeps.append(d))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("always fail")

    with pytest.raises(RuntimeError):
        with_retry(fn, delays=[60, 120, 300, 600, 900], quiet=True)
    # 6 次 attempt(= len(delays) + 1),5 次 sleep
    assert calls["n"] == 6
    assert sleeps == [60, 120, 300, 600, 900]


def test_with_retry_custom_delays_succeeds_mid_run(monkeypatch):
    """delays 中途成功該停 sleep。"""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda d: sleeps.append(d))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"fail {calls['n']}")
        return "ok"

    result = with_retry(fn, delays=[60, 120, 300, 600], quiet=True)
    assert result == "ok"
    assert calls["n"] == 3
    # 兩次 sleep:60, 120
    assert sleeps == [60, 120]


def test_with_retry_custom_delays_overrides_max_attempts(monkeypatch):
    """同時給 max_attempts 跟 delays 時,以 delays 為準(避免歧義)。"""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda d: sleeps.append(d))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("fail")

    # max_attempts=99 應被 ignore,實際走 delays 的 3+1 = 4 次
    with pytest.raises(RuntimeError):
        with_retry(fn, max_attempts=99, delays=[1, 2, 3], quiet=True)
    assert calls["n"] == 4
    assert sleeps == [1, 2, 3]
