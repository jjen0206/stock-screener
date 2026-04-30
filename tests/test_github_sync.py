"""github_sync.push_watchlist_to_github 測試(全程 mock requests,絕不打網路)。"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest
import requests

from src import github_sync


CSV_CONTENT = "stock_id,added_at,note\n2330,2026-04-30T00:00:00+00:00,\n"
DIFF_CSV = "stock_id,added_at,note\n3680,2026-04-30T00:00:00+00:00,\n"


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _mock_resp(status: int, json_body: dict | None = None, text: str = "") -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.json.return_value = json_body or {}
    r.text = text
    if status >= 400:
        err = requests.HTTPError(response=r)
        r.raise_for_status.side_effect = err
    else:
        r.raise_for_status.return_value = None
    return r


@pytest.fixture
def with_pat(monkeypatch):
    """提供測試用 PAT;確保各測試環境一致。"""
    monkeypatch.setenv("GITHUB_PAT", "test-token-123")
    monkeypatch.setenv("GITHUB_REPO", "jjen0206/stock-screener")
    monkeypatch.setenv("GITHUB_BRANCH", "main")
    monkeypatch.setenv("GITHUB_WATCHLIST_PATH", "data/twse_snapshot/watchlist.csv")


@pytest.fixture
def no_pat(monkeypatch):
    monkeypatch.delenv("GITHUB_PAT", raising=False)


def test_no_token_returns_false_without_http(no_pat):
    """無 GITHUB_PAT → 立即 return False,完全不發 HTTP request。"""
    with patch.object(github_sync, "requests") as mock_req:
        ok = github_sync.push_watchlist_to_github(CSV_CONTENT)
    assert ok is False
    mock_req.get.assert_not_called()
    mock_req.put.assert_not_called()


def test_create_new_file_when_remote_404(with_pat):
    """遠端 404(新檔)→ PUT body 無 sha,成功 201 → True。"""
    get_resp = _mock_resp(404)
    put_resp = _mock_resp(201, {"content": {"sha": "newsha"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp) as m_get, \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_watchlist_to_github(CSV_CONTENT)
    assert ok is True
    assert m_get.call_count == 1
    assert m_put.call_count == 1
    body = m_put.call_args.kwargs["json"]
    assert "sha" not in body
    assert body["content"] == _b64(CSV_CONTENT)
    assert body["branch"] == "main"
    assert body["committer"]["email"] == "actions@users.noreply.github.com"


def test_update_existing_file_carries_sha(with_pat):
    """遠端有檔且內容不同 → PUT 帶上既有 sha,200 → True。"""
    get_resp = _mock_resp(200, {"sha": "oldsha", "content": _b64(DIFF_CSV)})
    put_resp = _mock_resp(200, {"content": {"sha": "newsha"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_watchlist_to_github(CSV_CONTENT, message="custom msg")
    assert ok is True
    body = m_put.call_args.kwargs["json"]
    assert body["sha"] == "oldsha"
    assert body["message"] == "custom msg"


def test_skip_when_remote_content_identical(with_pat):
    """遠端內容已等於 csv_content → skip(不打 PUT),回 False。"""
    get_resp = _mock_resp(200, {"sha": "samesha", "content": _b64(CSV_CONTENT)})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put") as m_put:
        ok = github_sync.push_watchlist_to_github(CSV_CONTENT)
    assert ok is False
    m_put.assert_not_called()


def test_401_on_get_returns_false(with_pat):
    """GET 401(token 失效)→ False,不應嘗試 PUT。"""
    get_resp = _mock_resp(401, text="Bad credentials")
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put") as m_put:
        ok = github_sync.push_watchlist_to_github(CSV_CONTENT)
    assert ok is False
    m_put.assert_not_called()


def test_409_conflict_retries_once_and_succeeds(with_pat):
    """PUT 409 SHA 衝突 → refetch 拿新 sha → 第二次 PUT 200 → True。"""
    get_resp_1 = _mock_resp(200, {"sha": "stalesha", "content": _b64(DIFF_CSV)})
    put_resp_409 = _mock_resp(409, text="sha mismatch")
    get_resp_2 = _mock_resp(200, {"sha": "freshsha", "content": _b64(DIFF_CSV)})
    put_resp_200 = _mock_resp(200, {"content": {"sha": "x"}})

    with patch.object(github_sync.requests, "get",
                      side_effect=[get_resp_1, get_resp_2]), \
         patch.object(github_sync.requests, "put",
                      side_effect=[put_resp_409, put_resp_200]) as m_put:
        ok = github_sync.push_watchlist_to_github(CSV_CONTENT)

    assert ok is True
    assert m_put.call_count == 2
    second_body = m_put.call_args_list[1].kwargs["json"]
    assert second_body["sha"] == "freshsha"


def test_403_on_put_returns_false_without_retry(with_pat):
    """PUT 403(權限不足)→ False,不 retry。"""
    get_resp = _mock_resp(404)
    put_resp = _mock_resp(403, text="forbidden")
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_watchlist_to_github(CSV_CONTENT)
    assert ok is False
    assert m_put.call_count == 1


def test_500_retries_once_then_succeeds(with_pat):
    """PUT 500 → 背退 retry,第二次 201 → True。"""
    get_resp = _mock_resp(404)
    put_500 = _mock_resp(500, text="internal")
    put_201 = _mock_resp(201, {"content": {"sha": "x"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put",
                      side_effect=[put_500, put_201]) as m_put, \
         patch.object(github_sync.time, "sleep") as m_sleep:
        ok = github_sync.push_watchlist_to_github(CSV_CONTENT)
    assert ok is True
    assert m_put.call_count == 2
    m_sleep.assert_called_once_with(0.5)
