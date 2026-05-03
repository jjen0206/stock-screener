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
"""
from __future__ import annotations

import base64
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

DEFAULT_REPO = "jjen0206/stock-screener"
DEFAULT_BRANCH = "watchlist-sync"
DEFAULT_PATH = "data/twse_snapshot/watchlist.csv"
DEFAULT_TRADES_PATH = "data/twse_snapshot/trades.csv"
COMMITTER = {
    "name": "stock-screener-bot",
    "email": "actions@users.noreply.github.com",
}
DEFAULT_MESSAGE = "chore(watchlist): auto-sync from cloud app"
DEFAULT_TRADES_MESSAGE = "chore(trades): auto-sync P&L from cloud app"
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
) -> bool:
    """Generic CSV push 給多個 caller(watchlist / trades)reuse。
    讀 GITHUB_PAT / GITHUB_REPO / GITHUB_BRANCH env,內容相同 skip,409 retry,500 retry。
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


def push_watchlist_to_github(
    csv_content: str,
    message: str = DEFAULT_MESSAGE,
) -> bool:
    """把 csv_content 推到遠端 watchlist.csv(thin wrapper)。

    Returns:
        True  -> 成功 PUT(新建或更新)
        False -> 略過(無 token / 內容相同 / 任何錯誤)

    本機 dev 不設 GITHUB_PAT 時直接回 False,完全不發 HTTP request。
    """
    path = os.environ.get("GITHUB_WATCHLIST_PATH", DEFAULT_PATH)
    return _push_csv_generic(csv_content, path, message, "watchlist")


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


__all__ = [
    "push_watchlist_to_github",
    "fetch_watchlist_from_github",
    "push_trades_to_github",
    "fetch_trades_from_github",
]
