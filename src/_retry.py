"""共用 HTTP retry helper(指數退避)。

用法:
    from src._retry import with_retry

    def attempt():
        r = requests.get(...)
        r.raise_for_status()
        return r

    response = with_retry(attempt, max_attempts=3, label="FinMind X")
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Callable, TypeVar


logger = logging.getLogger(__name__)
T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "fetch",
    quiet: bool = False,
) -> T:
    """指數退避重試;失敗等 base_delay × 2^(attempt-1) 秒(1s → 2s → 4s)。

    任何 exception 觸發 retry。全部失敗重新 raise 最後的 exception。
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                if not quiet:
                    msg = (
                        f"[RETRY {attempt}/{max_attempts}] {label} 失敗 "
                        f"({type(e).__name__}): {str(e)[:120]} — "
                        f"等 {delay:.0f} 秒重試"
                    )
                    logger.warning(msg)
                    print(msg, file=sys.stderr, flush=True)
                time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("with_retry: 沒抓到任何 exception 但所有 attempt 都退出")


__all__ = ["with_retry"]
