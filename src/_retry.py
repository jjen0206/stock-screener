"""共用 HTTP retry helper(指數退避或自訂 schedule)。

用法:
    from src._retry import with_retry

    def attempt():
        r = requests.get(...)
        r.raise_for_status()
        return r

    # 預設:指數退避 1s → 2s → 4s
    response = with_retry(attempt, max_attempts=3, label="FinMind X")

    # 自訂 long backoff(IP / token ban 用,等銀行解凍)
    response = with_retry(
        attempt, delays=[60, 120, 300, 600, 900], label="FinMind X (aggressive)",
    )
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Callable, Sequence, TypeVar


logger = logging.getLogger(__name__)
T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "fetch",
    quiet: bool = False,
    delays: Sequence[float] | None = None,
    no_retry_exceptions: tuple[type[BaseException], ...] = (),
) -> T:
    """重試指定函式。

    任何 exception 觸發 retry,除非 type 落在 `no_retry_exceptions`。
    全部失敗(或遇到不可重試 exception)重新 raise。

    參數:
        max_attempts: 嘗試次數(僅 delays=None 時生效)。預設 3。
        base_delay: 指數退避基準秒數(僅 delays=None 時生效)。預設 1.0。
        delays: 自訂每次 sleep 秒數的序列。提供時:
            - 嘗試次數 = len(delays) + 1(最後一次失敗不 sleep)
            - 第 i 次失敗後 sleep delays[i-1] 秒,例 [60, 120, 300] = 4 次嘗試
            - 用於 IP / token ban 場景:長等比短等成功率高(等限額窗口 reset)
        label, quiet: 同前。
        no_retry_exceptions: 不該重試的 exception type tuple。例如 FinMind 402
            quota 爆,retry 沒意義(quota window 是小時級才 reset)— 直接 fail-fast。
    """
    if delays is not None:
        sleep_schedule = list(delays)
        attempts = len(sleep_schedule) + 1
    else:
        sleep_schedule = [base_delay * (2 ** i) for i in range(max_attempts - 1)]
        attempts = max_attempts

    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except no_retry_exceptions:
            # fail-fast:特定例外(e.g. 配額爆)等再久也沒用,立刻 propagate
            raise
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < attempts:
                delay = sleep_schedule[attempt - 1]
                if not quiet:
                    msg = (
                        f"[RETRY {attempt}/{attempts}] {label} 失敗 "
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
