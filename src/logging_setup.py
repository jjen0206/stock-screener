"""統一 logging 設定:把 cron 腳本的輸出 mirror 一份到 ``logs/`` 持久化。

設計動機
--------
GitHub Actions 上的 cron workflow log 會在數天後被砍掉,事後追問題只能憑
記憶。把每隻 script 的輸出多寫一份到 ``logs/{YYYY-MM-DD}-{script}.log``,
本機 / artifact 上可保留 7 天(由 ``scripts/cleanup_artifacts.py`` 統一清理)。

使用方式
--------
::

    # scripts/daily_notify.py 頂端,在其他 import 之後、main() 之前
    from src.logging_setup import setup_file_logging
    setup_file_logging("daily_notify")

    # 或者用 logger
    import logging
    logger = logging.getLogger(__name__)
    logger.info("這行會同時進 stdout 和 logs/2026-05-16-daily_notify.log")

特性
----
- 檔名:``logs/{Asia/Taipei date}-{script_name}.log``,每天自然 rotate
  (跨日的同一隻 script 會產生新檔)
- stdout 同步保留(StreamHandler),不影響 GitHub Actions log
- ``print()`` 因為不走 logging,**不會**被自動寫進檔案
  (要寫檔請用 ``logger.info()`` 或設 ``mirror_print=True``)
- Idempotent:重複呼叫不會疊 handler
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
LOGS_DIR: Path = PROJECT_ROOT / "logs"
LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
TAIPEI_TZ = timezone(timedelta(hours=8))

# 標記 root logger,避免同隻 script 被重複 setup 時疊 handler
_SETUP_FLAG = "_stock_screener_file_handler"


def _today_taipei() -> str:
    """回 Asia/Taipei 當下日期(YYYY-MM-DD)。"""
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")


def _resolve_log_path(script_name: str, date_str: Optional[str] = None) -> Path:
    """組出 ``logs/{date}-{script_name}.log`` 絕對路徑。"""
    safe = script_name.replace("/", "_").replace("\\", "_").replace(" ", "_")
    return LOGS_DIR / f"{date_str or _today_taipei()}-{safe}.log"


def setup_file_logging(
    script_name: str,
    level: int = logging.INFO,
    *,
    mirror_print: bool = False,
) -> Path:
    """把 root logger 的輸出多寫一份到 ``logs/{date}-{script_name}.log``。

    Parameters
    ----------
    script_name
        腳本名稱(不含 ``.py``),會出現在檔名裡。e.g. ``"daily_notify"``。
    level
        root logger level,預設 ``logging.INFO``。
    mirror_print
        是否把 ``sys.stdout`` / ``sys.stderr`` 的 ``print()`` 也 tee 一份到檔。
        預設 ``False`` — 因為大多 cron 腳本已混用 ``print()`` 跟 logging,
        強制 mirror 反而會跟 logging stream handler 重疊輸出。要乾淨切換成
        全 logging 的腳本再開。

    Returns
    -------
    Path
        實際寫到的 log 檔絕對路徑(主要給測試 / debug 用)。
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _resolve_log_path(script_name)

    root = logging.getLogger()
    root.setLevel(level)

    # 同 script 重複呼叫:只回傳路徑,不再加 handler
    if getattr(root, _SETUP_FLAG, None) == str(log_path):
        return log_path

    # 清掉先前不同 script 留下的 file handler(避免測試 / multi-call 漏)
    for h in list(root.handlers):
        if getattr(h, "_stock_screener_managed", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    formatter = logging.Formatter(LOG_FORMAT)

    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(formatter)
    fh._stock_screener_managed = True  # type: ignore[attr-defined]
    root.addHandler(fh)

    # stdout handler 只在沒有時加,避免跟既有 basicConfig 重複
    has_stream = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )
    if not has_stream:
        sh = logging.StreamHandler(stream=sys.stdout)
        sh.setLevel(level)
        sh.setFormatter(formatter)
        sh._stock_screener_managed = True  # type: ignore[attr-defined]
        root.addHandler(sh)

    if mirror_print:
        _tee_stdout(log_path)

    setattr(root, _SETUP_FLAG, str(log_path))
    return log_path


class _Tee:
    """把寫進原 stream 的內容也複製一份到檔案。

    `_original.write` 用 try/except 包起來避免 Windows cp950 console 在遇到 emoji
    (\\U0001f3af 等)時 raise UnicodeEncodeError 把 cron 跑斷;檔案永遠是 utf-8,
    不會踩這個雷,所以 log 持久化永遠成功。
    """

    def __init__(self, original, file_handle):
        self._original = original
        self._file = file_handle

    def write(self, data):
        try:
            self._original.write(data)
        except UnicodeEncodeError:
            # Windows console 編碼撐不住 emoji → 改用 ascii replace 印 fallback
            try:
                fallback = data.encode("ascii", errors="replace").decode("ascii")
                self._original.write(fallback)
            except Exception:
                pass
        except Exception:
            pass
        try:
            self._file.write(data)
            self._file.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        try:
            self._file.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._original, name)


def _tee_stdout(log_path: Path) -> None:
    """把 ``sys.stdout`` / ``sys.stderr`` 包成 Tee,寫進原 stream 的同時也寫進檔。

    Idempotent:已經包過就略過(避免重複呼叫疊 layer)。
    """
    if getattr(sys.stdout, "_stock_screener_tee", False):
        return
    fh = open(log_path, "a", encoding="utf-8")
    tee_out = _Tee(sys.stdout, fh)
    tee_out._stock_screener_tee = True  # type: ignore[attr-defined]
    sys.stdout = tee_out  # type: ignore[assignment]
    tee_err = _Tee(sys.stderr, fh)
    tee_err._stock_screener_tee = True  # type: ignore[attr-defined]
    sys.stderr = tee_err  # type: ignore[assignment]


__all__ = ["setup_file_logging", "LOGS_DIR", "LOG_FORMAT"]
