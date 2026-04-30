"""GitHub Contents API push for watchlist.csv (cloud persistence).

雲端容器(Streamlit Cloud)無法直接 git push,改用 GitHub Contents API 在
add/remove 後把最新 watchlist.csv 推到 repo,讓重啟後仍能從 CSV 還原。

需要的 secrets / env(雲端設定;本機未設時 push_watchlist_to_github 早早 return False,行為不變):
  - GITHUB_PAT: fine-grained PAT,permissions: Contents=Read+Write
  - GITHUB_REPO       (預設 "jjen0206/stock-screener")
  - GITHUB_BRANCH     (預設 "main")
  - GITHUB_WATCHLIST_PATH (預設 "data/twse_snapshot/watchlist.csv")

Caller 用 fire-and-forget thread 呼叫,thread 內部失敗只 log 不 raise。
"""
from __future__ import annotations

import base64
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

DEFAULT_REPO = "jjen0206/stock-screener"
DEFAULT_BRANCH = "main"
DEFAULT_PATH = "data/twse_snapshot/watchlist.csv"
COMMITTER = {
    "name": "stock-screener-bot",
    "email": "actions@users.noreply.github.com",
}
DEFAULT_MESSAGE = "chore(watchlist): auto-sync from cloud app"
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


def push_watchlist_to_github(
    csv_content: str,
    message: str = DEFAULT_MESSAGE,
) -> bool:
    """把 csv_content 推到遠端 watchlist.csv。

    Returns:
        True  -> 成功 PUT(新建或更新)
        False -> 略過(無 token / 內容相同 / 任何錯誤)

    本機 dev 不設 GITHUB_PAT 時直接回 False,完全不發 HTTP request。
    """
    token = os.environ.get("GITHUB_PAT")
    if not token:
        logger.debug("[GH_SYNC] 未設 GITHUB_PAT,跳過 push")
        return False

    repo = os.environ.get("GITHUB_REPO", DEFAULT_REPO)
    branch = os.environ.get("GITHUB_BRANCH", DEFAULT_BRANCH)
    path = os.environ.get("GITHUB_WATCHLIST_PATH", DEFAULT_PATH)

    try:
        sha, remote = _get_remote_file(repo, path, branch, token)
    except requests.HTTPError as ex:
        code = ex.response.status_code if ex.response is not None else "?"
        if code in (401, 403):
            logger.error(
                "[GH_SYNC] GET 認證失敗(%s),GITHUB_PAT 可能失效或權限不足", code,
            )
        else:
            logger.error("[GH_SYNC] GET 失敗:%s", ex)
        return False
    except requests.RequestException as ex:
        logger.error("[GH_SYNC] GET 連線錯誤:%s", ex)
        return False

    if remote is not None and remote == csv_content:
        logger.debug("[GH_SYNC] 內容相同,skip noise commit")
        return False

    for attempt in range(2):
        try:
            resp = _put_file(
                repo, path, branch, token, csv_content, message, sha,
            )
        except requests.RequestException as ex:
            logger.error("[GH_SYNC] PUT 連線錯誤:%s", ex)
            return False

        if resp.status_code in (200, 201):
            logger.info(
                "[GH_SYNC] 已推送 %s@%s (%s)",
                path, branch, "new" if sha is None else "update",
            )
            return True

        if resp.status_code in (401, 403):
            logger.error(
                "[GH_SYNC] PUT 認證失敗(%s),GITHUB_PAT 可能失效或權限不足",
                resp.status_code,
            )
            return False

        if resp.status_code == 409 and attempt == 0:
            logger.warning("[GH_SYNC] PUT 409 SHA 衝突,refetch 後 retry")
            try:
                sha, remote = _get_remote_file(repo, path, branch, token)
            except requests.RequestException as ex:
                logger.error("[GH_SYNC] retry 前 GET 失敗:%s", ex)
                return False
            if remote is not None and remote == csv_content:
                logger.info("[GH_SYNC] retry 前發現內容已相同,skip")
                return False
            continue

        if 500 <= resp.status_code < 600 and attempt == 0:
            logger.warning(
                "[GH_SYNC] PUT %s,背退 0.5s 重試", resp.status_code,
            )
            time.sleep(0.5)
            continue

        logger.error(
            "[GH_SYNC] PUT 失敗 status=%s body=%s",
            resp.status_code, resp.text[:200],
        )
        return False

    return False


__all__ = ["push_watchlist_to_github"]
