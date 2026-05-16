"""GitHub Contents API push/fetch for watchlist.csv (cloud persistence).

雲端容器(Streamlit Cloud)無法直接 git push,改用 GitHub Contents API 在
add/remove 後把最新 watchlist.csv 推到 repo,讓重啟後仍能從 CSV 還原。

設計:推到獨立的 `watchlist-sync` 分支(預設值)避免 Streamlit Cloud 偵測 main
變動觸發 redeploy。Boot 時先嘗試從 watchlist-sync 拉,沒有就 fallback 用 main 上
的 seed CSV。

需要的 secrets / env(雲端設定;本機未設時 push/fetch 早早 return False/None,行為不變):
  - GITHUB_PAT: fine-grained PAT,permissions: Contents=Read+Write
  - GITHUB_REPO       (預設 "jjen0206/stock-screener")
  - GITHUB_BRANCH     (預設 "watchlist-sync")
  - GITHUB_WATCHLIST_PATH (預設 "data/twse_snapshot/watchlist.csv")

Caller 用 fire-and-forget thread 呼叫 push,thread 內部失敗只 log 不 raise。
Boot 時 fetch 是 sync 呼叫,失敗回 None 讓 caller fallback。

Regression guard (watchlist only):
  Boot 時若 fetch_watchlist_from_github 失敗 → safe_boot_load fallback 載入 main
  種子 CSV (通常只有少數幾檔) → SQLite 變殘缺 → 任一 add/remove 觸發 _dump_watchlist_snapshot
  → 把殘缺狀態 push 上 watchlist-sync,**覆蓋遠端真實狀態**。
  push_watchlist_to_github 在 GET remote 後比對 stock_id 集合,若新版相比 remote
  遺失 >= WATCHLIST_LOSS_THRESHOLD 檔 → 拒推、log error。守住 2026-05-16 主公回報的
  「19 檔變 4 檔」regression。
"""
from __future__ import annotations

import base64
import io
import logging
import os
import time
from typing import Callable

import pandas as pd
import requests

logger = logging.getLogger(__name__)

DEFAULT_REPO = "jjen0206/stock-screener"
DEFAULT_BRANCH = "watchlist-sync"
DEFAULT_PATH = "data/twse_snapshot/watchlist.csv"
DEFAULT_TRADES_PATH = "data/twse_snapshot/trades.csv"
DEFAULT_PAPER_TRADES_PATH = "data/twse_snapshot/paper_trades.csv"
DEFAULT_ANALYST_TARGETS_PATH = "data/twse_snapshot/analyst_targets.csv"
COMMITTER = {
    "name": "stock-screener-bot",
    "email": "actions@users.noreply.github.com",
}
# Boot fallback regression 守線:新版 push 相比 remote 若遺失 >= 此 threshold 檔
# 直接拒推。一般 user 一次只移除 1-2 檔,大量遺失基本上 = boot fallback regression。
# 可用 WATCHLIST_LOSS_THRESHOLD env 覆寫(本機 dev 跑全清測試時用)。
WATCHLIST_LOSS_THRESHOLD = 3
DEFAULT_MESSAGE = "chore(watchlist): auto-sync from cloud app"
DEFAULT_TRADES_MESSAGE = "chore(trades): auto-sync P&L from cloud app"
DEFAULT_PAPER_TRADES_MESSAGE = (
    "chore(paper_trades): auto-sync 實測追蹤 from cloud app"
)
DEFAULT_ANALYST_TARGETS_MESSAGE = (
    "chore(analyst_targets): auto-sync 法人目標價 from cloud app"
)
HTTP_TIMEOUT = 15


def _api_url(repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{path}"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_remote_file(
    repo: str, path: str, branch: str, token: str,
) -> tuple[str | None, str | None]:
    """GET 遠端檔案,回 (sha, decoded_content)。404 → (None, None)。

    其他非 2xx 直接 raise(由 caller 統一 try/except),讓 401/403 訊息能傳到 log。
    """
    resp = requests.get(
        _api_url(repo, path),
        headers=_headers(token),
        params={"ref": branch},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code == 404:
        return None, None
    resp.raise_for_status()
    data = resp.json()
    sha = data.get("sha")
    raw_b64 = data.get("content", "")
    decoded = base64.b64decode(raw_b64).decode("utf-8") if raw_b64 else ""
    return sha, decoded


def _put_file(
    repo: str,
    path: str,
    branch: str,
    token: str,
    content: str,
    message: str,
    sha: str | None,
) -> requests.Response:
    body: dict = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
        "committer": COMMITTER,
    }
    if sha:
        body["sha"] = sha
    return requests.put(
        _api_url(repo, path),
        headers=_headers(token),
        json=body,
        timeout=HTTP_TIMEOUT,
    )


def _push_csv_generic(
    csv_content: str,
    path: str,
    message: str,
    log_label: str,
    regression_check: Callable[[str, str], tuple[bool, str]] | None = None,
) -> bool:
    """Generic CSV push 給多個 caller(watchlist / trades)reuse。

    讀 GITHUB_PAT / GITHUB_REPO / GITHUB_BRANCH env,內容相同 skip,409 retry,500 retry。

    regression_check: optional callable(remote_csv, new_csv) -> (allow, reason)。
        在 GET remote 後、PUT 之前呼叫;回 (False, reason) → 拒推、log error。
        watchlist 用這個守 boot fallback regression(主公 2026-05-16 19 檔變 4 檔)。
        trades / paper_trades / analyst_targets 是「累積式」snapshot,沒這風險,callable
        傳 None 即可。
    """
    token = os.environ.get("GITHUB_PAT")
    if not token:
        logger.debug("[GH_SYNC] 未設 GITHUB_PAT,跳過 %s push", log_label)
        return False

    repo = os.environ.get("GITHUB_REPO", DEFAULT_REPO)
    branch = os.environ.get("GITHUB_BRANCH", DEFAULT_BRANCH)

    try:
        sha, remote = _get_remote_file(repo, path, branch, token)
    except requests.HTTPError as ex:
        code = ex.response.status_code if ex.response is not None else "?"
        if code in (401, 403):
            logger.error(
                "[GH_SYNC] %s GET 認證失敗(%s),GITHUB_PAT 可能失效或權限不足",
                log_label, code,
            )
        else:
            logger.error("[GH_SYNC] %s GET 失敗:%s", log_label, ex)
        return False
    except requests.RequestException as ex:
        logger.error("[GH_SYNC] %s GET 連線錯誤:%s", log_label, ex)
        return False

    if remote is not None and remote == csv_content:
        logger.debug("[GH_SYNC] %s 內容相同,skip noise commit", log_label)
        return False

    if regression_check is not None and remote is not None:
        try:
            allow, reason = regression_check(remote, csv_content)
        except Exception as ex:  # noqa: BLE001
            # regression_check 是 best-effort 守線,內部 parse 失敗不該擋住正常 push
            logger.warning(
                "[GH_SYNC] %s regression_check 拋例外:%s — 放行 push",
                log_label, ex,
            )
            allow, reason = True, ""
        if not allow:
            logger.error(
                "[GH_SYNC] %s push 被 regression guard 拒絕:%s",
                log_label, reason,
            )
            return False

    for attempt in range(2):
        try:
            resp = _put_file(
                repo, path, branch, token, csv_content, message, sha,
            )
        except requests.RequestException as ex:
            logger.error("[GH_SYNC] %s PUT 連線錯誤:%s", log_label, ex)
            return False

        if resp.status_code in (200, 201):
            logger.info(
                "[GH_SYNC] %s 已推送 %s@%s (%s)",
                log_label, path, branch, "new" if sha is None else "update",
            )
            return True

        if resp.status_code in (401, 403):
            logger.error(
                "[GH_SYNC] %s PUT 認證失敗(%s),GITHUB_PAT 可能失效或權限不足",
                log_label, resp.status_code,
            )
            return False

        if resp.status_code == 409 and attempt == 0:
            logger.warning("[GH_SYNC] %s PUT 409 SHA 衝突,refetch retry", log_label)
            try:
                sha, remote = _get_remote_file(repo, path, branch, token)
            except requests.RequestException as ex:
                logger.error("[GH_SYNC] %s retry 前 GET 失敗:%s", log_label, ex)
                return False
            if remote is not None and remote == csv_content:
                logger.info("[GH_SYNC] %s retry 前發現內容已相同,skip", log_label)
                return False
            # 409 refetch 後 remote 變新版,要重新跑 regression_check —
            # 別處 push 把 watchlist 改寬,我們這份還是 fallback 種子,該攔下
            if regression_check is not None and remote is not None:
                try:
                    allow, reason = regression_check(remote, csv_content)
                except Exception as ex:  # noqa: BLE001
                    logger.warning(
                        "[GH_SYNC] %s 409 retry regression_check 拋例外:%s — 放行",
                        log_label, ex,
                    )
                    allow, reason = True, ""
                if not allow:
                    logger.error(
                        "[GH_SYNC] %s 409 retry 被 regression guard 拒絕:%s",
                        log_label, reason,
                    )
                    return False
            continue

        if 500 <= resp.status_code < 600 and attempt == 0:
            logger.warning(
                "[GH_SYNC] %s PUT %s,背退 0.5s 重試",
                log_label, resp.status_code,
            )
            time.sleep(0.5)
            continue

        logger.error(
            "[GH_SYNC] %s PUT 失敗 status=%s body=%s",
            log_label, resp.status_code, resp.text[:200],
        )
        return False

    return False


def _fetch_csv_generic(path: str, log_label: str) -> str | None:
    """Generic CSV fetch 給多個 caller reuse。讀 env,失敗回 None(caller fallback)。"""
    token = os.environ.get("GITHUB_PAT")
    if not token:
        logger.debug("[GH_SYNC] 未設 GITHUB_PAT,跳過 %s fetch", log_label)
        return None

    repo = os.environ.get("GITHUB_REPO", DEFAULT_REPO)
    branch = os.environ.get("GITHUB_BRANCH", DEFAULT_BRANCH)

    try:
        _, content = _get_remote_file(repo, path, branch, token)
    except requests.HTTPError as ex:
        code = ex.response.status_code if ex.response is not None else "?"
        if code in (401, 403):
            logger.warning(
                "[GH_SYNC] %s fetch 認證失敗(%s),fallback 到本機 seed",
                log_label, code,
            )
        else:
            logger.warning(
                "[GH_SYNC] %s fetch 失敗(%s),fallback:%s",
                log_label, code, ex,
            )
        return None
    except requests.RequestException as ex:
        logger.warning(
            "[GH_SYNC] %s fetch 連線錯誤,fallback:%s", log_label, ex,
        )
        return None

    if content is None:
        logger.info(
            "[GH_SYNC] %s@%s 不存在(404),fallback 到本機 seed",
            path, branch,
        )
        return None
    logger.info("[GH_SYNC] 從 %s@%s 拉到 %s", path, branch, log_label)
    return content


def _parse_watchlist_csv_sids(csv_text: str) -> set[str]:
    """解析 watchlist CSV 字串 → stock_id set。

    parse 失敗 / 空字串回 set()。caller(regression_check)會把空 set 當「我不知道,
    別擋」處理 — 因為對「沒能解析的 remote」我們無法判定 regression。
    """
    if not csv_text or not csv_text.strip():
        return set()
    try:
        df = pd.read_csv(io.StringIO(csv_text), dtype={"stock_id": str})
    except Exception:  # noqa: BLE001
        return set()
    if "stock_id" not in df.columns:
        return set()
    return {
        str(s).strip()
        for s in df["stock_id"].dropna().tolist()
        if str(s).strip()
    }


def _watchlist_regression_check(
    remote_csv: str, new_csv: str,
) -> tuple[bool, str]:
    """守線:新 push 相比 remote 若遺失 >= WATCHLIST_LOSS_THRESHOLD 檔 → 拒推。

    Reason:
      Boot 時 fetch 失敗 fallback 到 main 種子 CSV(可能只剩 4 檔)→ 之後 dump
      會把這 4 檔 push 上 watchlist-sync,把遠端 19 檔覆蓋掉。這個 check 守住此情況。

    Threshold 可用 WATCHLIST_LOSS_THRESHOLD env 覆寫(本機 dev 跑全清測試時用)。

    Returns: (allow, reason)
      - allow=True 條件:remote 解析不出股(視為「沒有可信對照」放行)、新版未遺失任何
        股、或遺失 < threshold
      - allow=False:遺失 >= threshold,reason 列出前幾檔 sid 給 log
    """
    remote_sids = _parse_watchlist_csv_sids(remote_csv)
    new_sids = _parse_watchlist_csv_sids(new_csv)
    if not remote_sids:
        # remote 為空 / 解析失敗 → 視為「沒有可信對照」,放行
        return True, ""
    lost = remote_sids - new_sids
    threshold_env = os.environ.get("WATCHLIST_LOSS_THRESHOLD")
    try:
        threshold = (
            int(threshold_env) if threshold_env else WATCHLIST_LOSS_THRESHOLD
        )
    except ValueError:
        threshold = WATCHLIST_LOSS_THRESHOLD
    if len(lost) >= threshold:
        sample = ", ".join(sorted(lost)[:8])
        more = "..." if len(lost) > 8 else ""
        reason = (
            f"would lose {len(lost)} stocks (>={threshold}): {sample}{more} — "
            f"可能是 boot fallback regression 或 SQLite 被意外清空"
        )
        return False, reason
    return True, ""


def push_watchlist_to_github(
    csv_content: str,
    message: str = DEFAULT_MESSAGE,
) -> bool:
    """把 csv_content 推到遠端 watchlist.csv(thin wrapper)。

    Returns:
        True  -> 成功 PUT(新建或更新)
        False -> 略過(無 token / 內容相同 / regression guard 拒絕 / 任何錯誤)

    本機 dev 不設 GITHUB_PAT 時直接回 False,完全不發 HTTP request。

    Regression guard:_watchlist_regression_check 守住「相比 remote 遺失 >=3 檔」
    的可疑 push(boot fallback regression 守線,2026-05-16 主公回報的 19 檔變 4 檔
    根因 fix)。一般 user 一次只移除 1-2 檔,大量遺失基本上 = bug。
    """
    path = os.environ.get("GITHUB_WATCHLIST_PATH", DEFAULT_PATH)
    return _push_csv_generic(
        csv_content, path, message, "watchlist",
        regression_check=_watchlist_regression_check,
    )


def fetch_watchlist_from_github() -> str | None:
    """從 watchlist-sync 分支拉最新 watchlist.csv(thin wrapper)。

    Returns:
        str  -> 解碼後的 CSV 文字
        None -> 無 PAT / 不存在(404) / 認證失敗 / 連線錯誤
    """
    path = os.environ.get("GITHUB_WATCHLIST_PATH", DEFAULT_PATH)
    return _fetch_csv_generic(path, "watchlist")


def push_trades_to_github(
    csv_content: str,
    message: str = DEFAULT_TRADES_MESSAGE,
) -> bool:
    """把 csv_content 推到遠端 trades.csv(P&L 永久化)。

    跟 watchlist 同一個 branch(watchlist-sync,避免觸發 main redeploy)。
    Returns 同 push_watchlist_to_github。
    """
    path = os.environ.get("GITHUB_TRADES_PATH", DEFAULT_TRADES_PATH)
    return _push_csv_generic(csv_content, path, message, "trades")


def fetch_trades_from_github() -> str | None:
    """從 watchlist-sync 分支拉最新 trades.csv(雲端 boot 時 remote-first 載入)。

    Returns 同 fetch_watchlist_from_github。
    """
    path = os.environ.get("GITHUB_TRADES_PATH", DEFAULT_TRADES_PATH)
    return _fetch_csv_generic(path, "trades")


def push_paper_trades_to_github(
    csv_content: str,
    message: str = DEFAULT_PAPER_TRADES_MESSAGE,
) -> bool:
    """把 csv_content 推到遠端 paper_trades.csv(實測追蹤永久化)。

    跟 watchlist / trades 同一個 branch(watchlist-sync,避免觸發 main redeploy)。
    Returns 同 push_watchlist_to_github。
    """
    path = os.environ.get("GITHUB_PAPER_TRADES_PATH", DEFAULT_PAPER_TRADES_PATH)
    return _push_csv_generic(csv_content, path, message, "paper_trades")


def fetch_paper_trades_from_github() -> str | None:
    """從 watchlist-sync 分支拉最新 paper_trades.csv(雲端 boot 時 remote-first 載入)。

    Returns 同 fetch_watchlist_from_github。
    """
    path = os.environ.get("GITHUB_PAPER_TRADES_PATH", DEFAULT_PAPER_TRADES_PATH)
    return _fetch_csv_generic(path, "paper_trades")


def push_analyst_targets_to_github(
    csv_content: str,
    message: str = DEFAULT_ANALYST_TARGETS_MESSAGE,
) -> bool:
    """把 csv_content 推到遠端 analyst_targets.csv(法人目標價永久化)。

    跟 watchlist / trades / paper_trades 同一個 branch(watchlist-sync,
    避免觸發 main redeploy)。
    """
    path = os.environ.get(
        "GITHUB_ANALYST_TARGETS_PATH", DEFAULT_ANALYST_TARGETS_PATH,
    )
    return _push_csv_generic(csv_content, path, message, "analyst_targets")


def fetch_analyst_targets_from_github() -> str | None:
    """從 watchlist-sync 分支拉最新 analyst_targets.csv(雲端 boot remote-first)。"""
    path = os.environ.get(
        "GITHUB_ANALYST_TARGETS_PATH", DEFAULT_ANALYST_TARGETS_PATH,
    )
    return _fetch_csv_generic(path, "analyst_targets")


__all__ = [
    "push_watchlist_to_github",
    "fetch_watchlist_from_github",
    "push_trades_to_github",
    "fetch_trades_from_github",
    "push_paper_trades_to_github",
    "fetch_paper_trades_from_github",
    "push_analyst_targets_to_github",
    "fetch_analyst_targets_from_github",
]
