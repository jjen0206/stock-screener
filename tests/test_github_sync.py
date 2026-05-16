"""github_sync push/fetch 測試(全程 mock requests,絕不打網路)。"""
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
    """提供測試用 PAT,固定 repo / branch / path,讓 body assert 穩定。"""
    monkeypatch.setenv("GITHUB_PAT", "test-token-123")
    monkeypatch.setenv("GITHUB_REPO", "jjen0206/stock-screener")
    monkeypatch.setenv("GITHUB_BRANCH", "watchlist-sync")
    monkeypatch.setenv("GITHUB_WATCHLIST_PATH", "data/twse_snapshot/watchlist.csv")


@pytest.fixture
def with_pat_no_branch(monkeypatch):
    """只設 PAT,讓 GITHUB_BRANCH 走預設值(驗證 default 是 watchlist-sync)。"""
    monkeypatch.setenv("GITHUB_PAT", "test-token-123")
    monkeypatch.delenv("GITHUB_BRANCH", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.delenv("GITHUB_WATCHLIST_PATH", raising=False)


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
    assert body["branch"] == "watchlist-sync"
    assert body["committer"]["email"] == "actions@users.noreply.github.com"


def test_default_branch_is_watchlist_sync(with_pat_no_branch):
    """未設 GITHUB_BRANCH 時 push 預設打 watchlist-sync。"""
    get_resp = _mock_resp(404)
    put_resp = _mock_resp(201, {"content": {"sha": "x"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp) as m_get, \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_watchlist_to_github(CSV_CONTENT)
    assert ok is True
    assert m_get.call_args.kwargs["params"] == {"ref": "watchlist-sync"}
    assert m_put.call_args.kwargs["json"]["branch"] == "watchlist-sync"


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


# === fetch_watchlist_from_github ===


def test_fetch_no_token_returns_none(no_pat):
    """無 PAT → 直接 None,不發 HTTP。"""
    with patch.object(github_sync, "requests") as mock_req:
        result = github_sync.fetch_watchlist_from_github()
    assert result is None
    mock_req.get.assert_not_called()


def test_fetch_returns_decoded_content(with_pat):
    """200 OK → 回傳解碼後的 CSV 文字。"""
    get_resp = _mock_resp(
        200, {"sha": "abc", "content": _b64(CSV_CONTENT)},
    )
    with patch.object(github_sync.requests, "get", return_value=get_resp) as m_get:
        result = github_sync.fetch_watchlist_from_github()
    assert result == CSV_CONTENT
    # 確認打的是 watchlist-sync 分支
    assert m_get.call_args.kwargs["params"] == {"ref": "watchlist-sync"}


def test_fetch_404_returns_none(with_pat):
    """分支或檔案不存在 → None,讓 caller fallback。"""
    get_resp = _mock_resp(404)
    with patch.object(github_sync.requests, "get", return_value=get_resp):
        result = github_sync.fetch_watchlist_from_github()
    assert result is None


def test_fetch_401_returns_none(with_pat):
    """token 失效 → None(warning log)。"""
    get_resp = _mock_resp(401, text="Bad credentials")
    with patch.object(github_sync.requests, "get", return_value=get_resp):
        result = github_sync.fetch_watchlist_from_github()
    assert result is None


def test_fetch_network_error_returns_none(with_pat):
    """連線錯誤 → None。"""
    with patch.object(github_sync.requests, "get",
                      side_effect=requests.ConnectionError("DNS fail")):
        result = github_sync.fetch_watchlist_from_github()
    assert result is None


# === Regression guard:守 boot fallback 把遠端 19 檔覆蓋掉的 bug (2026-05-16) ===


def _wl_csv(sids: list[str]) -> str:
    """組 watchlist CSV — 給 regression guard 測試用。"""
    lines = ["stock_id,added_at,note"]
    for sid in sids:
        lines.append(f"{sid},2026-04-30T00:00:00+00:00,")
    return "\n".join(lines) + "\n"


REMOTE_19 = _wl_csv([
    "3105", "6223", "3017", "2449", "2344", "2337", "4971",
    "2303", "2379", "3034", "7810", "4442", "2484", "3711",
    "2454", "2317", "2330", "3680", "2308",
])
LOCAL_4_SEED = _wl_csv(["2454", "2317", "2330", "3680"])


def test_push_watchlist_blocks_regression_loss_19_to_4(with_pat):
    """主公 2026-05-16 bug 重現:remote 19 檔 / local 4 檔 → push 拒絕,不打 PUT。"""
    get_resp = _mock_resp(200, {"sha": "oldsha", "content": _b64(REMOTE_19)})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put") as m_put:
        ok = github_sync.push_watchlist_to_github(LOCAL_4_SEED)
    assert ok is False
    m_put.assert_not_called()


def test_push_watchlist_allows_small_legitimate_removal(with_pat):
    """remote 19 / local 18(移除 1 檔)→ < threshold,放行 PUT。"""
    local_18 = _wl_csv([
        "3105", "6223", "3017", "2449", "2344", "2337", "4971",
        "2303", "2379", "3034", "7810", "4442", "2484", "3711",
        "2454", "2317", "2330", "3680",  # 砍掉 2308
    ])
    get_resp = _mock_resp(200, {"sha": "oldsha", "content": _b64(REMOTE_19)})
    put_resp = _mock_resp(200, {"content": {"sha": "x"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_watchlist_to_github(local_18)
    assert ok is True
    m_put.assert_called_once()


def test_push_watchlist_allows_pure_addition(with_pat):
    """remote 19 / local 20(純新增 1 檔)→ 沒任何 loss,放行。"""
    local_20 = _wl_csv([
        "3105", "6223", "3017", "2449", "2344", "2337", "4971",
        "2303", "2379", "3034", "7810", "4442", "2484", "3711",
        "2454", "2317", "2330", "3680", "2308", "1101",  # 新增 1101
    ])
    get_resp = _mock_resp(200, {"sha": "oldsha", "content": _b64(REMOTE_19)})
    put_resp = _mock_resp(200, {"content": {"sha": "x"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_watchlist_to_github(local_20)
    assert ok is True
    m_put.assert_called_once()


def test_push_watchlist_allows_empty_remote(with_pat):
    """remote 404 / 完全新檔 → 沒有對照,放行(對應 seed commit 場景)。"""
    get_resp = _mock_resp(404)
    put_resp = _mock_resp(201, {"content": {"sha": "x"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_watchlist_to_github(LOCAL_4_SEED)
    assert ok is True
    m_put.assert_called_once()


def test_push_watchlist_threshold_default_blocks_at_3(with_pat):
    """預設 threshold=3:remote 5 / local 2(遺失 3 檔)→ 拒推。"""
    remote = _wl_csv(["1101", "2317", "2330", "2454", "3680"])
    local = _wl_csv(["1101", "2317"])  # 遺失 3
    get_resp = _mock_resp(200, {"sha": "old", "content": _b64(remote)})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put") as m_put:
        ok = github_sync.push_watchlist_to_github(local)
    assert ok is False
    m_put.assert_not_called()


def test_push_watchlist_threshold_below_default_passes(with_pat):
    """remote 5 / local 3(遺失 2 檔 < threshold 3)→ 放行。"""
    remote = _wl_csv(["1101", "2317", "2330", "2454", "3680"])
    local = _wl_csv(["1101", "2317", "2330"])  # 遺失 2
    get_resp = _mock_resp(200, {"sha": "old", "content": _b64(remote)})
    put_resp = _mock_resp(200, {"content": {"sha": "x"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_watchlist_to_github(local)
    assert ok is True
    m_put.assert_called_once()


def test_push_watchlist_threshold_env_override(with_pat, monkeypatch):
    """WATCHLIST_LOSS_THRESHOLD=10 → 遺失 5 檔 < 10,放行。"""
    monkeypatch.setenv("WATCHLIST_LOSS_THRESHOLD", "10")
    remote = _wl_csv(["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"])
    local = _wl_csv(["1", "2", "3", "4", "5", "6"])  # 遺失 5
    get_resp = _mock_resp(200, {"sha": "old", "content": _b64(remote)})
    put_resp = _mock_resp(200, {"content": {"sha": "x"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_watchlist_to_github(local)
    assert ok is True
    m_put.assert_called_once()


def test_push_watchlist_threshold_env_blocks_below_default(with_pat, monkeypatch):
    """WATCHLIST_LOSS_THRESHOLD=2 → 遺失 2 檔(預設會放行)該被擋。"""
    monkeypatch.setenv("WATCHLIST_LOSS_THRESHOLD", "2")
    remote = _wl_csv(["1101", "2317", "2330", "2454"])
    local = _wl_csv(["1101", "2317"])  # 遺失 2
    get_resp = _mock_resp(200, {"sha": "old", "content": _b64(remote)})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put") as m_put:
        ok = github_sync.push_watchlist_to_github(local)
    assert ok is False
    m_put.assert_not_called()


def test_push_watchlist_garbage_remote_csv_allows_push(with_pat):
    """remote 是壞 CSV 解析失敗 → 視為「無可信對照」,放行。

    parse 失敗不該擋住正常 push — 否則一次 remote 壞掉會把所有後續 push 全凍住。
    """
    bad_csv = "\x00\x01\x02not-a-csv"
    get_resp = _mock_resp(200, {"sha": "old", "content": _b64(bad_csv)})
    put_resp = _mock_resp(200, {"content": {"sha": "x"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_watchlist_to_github(LOCAL_4_SEED)
    assert ok is True
    m_put.assert_called_once()


def test_push_trades_not_affected_by_watchlist_guard(with_pat, monkeypatch):
    """trades / paper_trades / analyst_targets 沒接 regression guard,
    模擬 remote 5 row / local 1 row 不該被擋(那些是累積式 snapshot,不適用)。
    """
    monkeypatch.delenv("GITHUB_TRADES_PATH", raising=False)
    remote_trades = (
        "id,sid,entry_date,return_pct\n"
        "1,2330,2026-04-30,5.0\n2,2454,2026-04-30,3.0\n"
        "3,2317,2026-04-30,2.0\n4,3680,2026-04-30,1.0\n"
        "5,2308,2026-04-30,4.0\n"
    )
    local_trades_short = "id,sid,entry_date,return_pct\n1,2330,2026-04-30,5.0\n"
    get_resp = _mock_resp(200, {"sha": "x", "content": _b64(remote_trades)})
    put_resp = _mock_resp(200, {"content": {"sha": "y"}})
    with patch.object(github_sync.requests, "get", return_value=get_resp), \
         patch.object(github_sync.requests, "put", return_value=put_resp) as m_put:
        ok = github_sync.push_trades_to_github(local_trades_short)
    # trades 沒接 regression guard → 即便 row 銳減仍會 PUT
    assert ok is True
    m_put.assert_called_once()


def test_push_watchlist_409_retry_re_checks_regression(with_pat):
    """409 SHA 衝突 refetch 後,新 remote 變寬(有人 push 了 19 檔),我們的 4 檔
    再次跑 regression check → 該被擋,不該第二次 PUT。
    """
    get_resp_1 = _mock_resp(
        200, {"sha": "stale", "content": _b64(_wl_csv(["2330"]))},
    )
    # 第一次 GET 只有 1 檔,我們 push 4 檔(會 +3) → 純新增放行
    # PUT 收到 409 → refetch → 此時 remote 已被別人改成 19 檔 → regression 拒推
    put_409 = _mock_resp(409, text="conflict")
    get_resp_2 = _mock_resp(
        200, {"sha": "fresh", "content": _b64(REMOTE_19)},
    )

    with patch.object(
        github_sync.requests, "get",
        side_effect=[get_resp_1, get_resp_2],
    ), patch.object(
        github_sync.requests, "put", side_effect=[put_409],
    ) as m_put:
        ok = github_sync.push_watchlist_to_github(LOCAL_4_SEED)
    assert ok is False
    # 應該只 PUT 一次（第一次嘗試），refetch 後被 regression guard 攔下
    assert m_put.call_count == 1


# === _watchlist_regression_check unit tests ===


def test_regression_check_returns_allow_for_empty_remote():
    """remote 為空字串 → 視為「無對照」,放行。"""
    allow, _ = github_sync._watchlist_regression_check("", LOCAL_4_SEED)
    assert allow is True


def test_regression_check_returns_block_for_19_to_4():
    """遺失 15 檔 >= threshold 3 → 拒推 + reason 描述遺失。"""
    allow, reason = github_sync._watchlist_regression_check(
        REMOTE_19, LOCAL_4_SEED,
    )
    assert allow is False
    assert "would lose 15" in reason
    # reason 該列出至少一個 sid 範例
    assert any(s in reason for s in ["2308", "2484", "3711", "3105"])


def test_regression_check_returns_allow_for_same_sids():
    """sid 集合相同(只改 added_at)→ 放行。"""
    new = _wl_csv([
        "3105", "6223", "3017", "2449", "2344", "2337", "4971",
        "2303", "2379", "3034", "7810", "4442", "2484", "3711",
        "2454", "2317", "2330", "3680", "2308",
    ])
    allow, _ = github_sync._watchlist_regression_check(REMOTE_19, new)
    assert allow is True
