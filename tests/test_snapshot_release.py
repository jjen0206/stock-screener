"""Unit tests for src/snapshot_release.py。

策略:`gh` CLI 跟 REST 都用 monkeypatch fake,測 module 自身的 routing /
SHA cache / kill-switch / fallback 邏輯,不打真 GitHub。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import snapshot_release as sr


# === fixtures ===

@pytest.fixture
def snap_dir(tmp_path):
    d = tmp_path / "snap"
    d.mkdir()
    return d


@pytest.fixture
def sample_file(snap_dir):
    f = snap_dir / "institutional.parquet"
    f.write_bytes(b"FAKE_PARQUET_PAYLOAD_FOR_TEST")
    return f


@pytest.fixture(autouse=True)
def restore_env(monkeypatch):
    """Clean env 隔離各 test 不被前一個 set 的 env 污染。"""
    for key in ("SNAPSHOT_USE_RELEASES_ENABLED", "GITHUB_TOKEN",
                "GITHUB_PAT", "GITHUB_REPO"):
        monkeypatch.delenv(key, raising=False)


# === kill-switch ===

def test_is_releases_enabled_default_on():
    assert sr.is_releases_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off",
                                 "False", "OFF"])
def test_is_releases_enabled_off(monkeypatch, val):
    monkeypatch.setenv("SNAPSHOT_USE_RELEASES_ENABLED", val)
    assert sr.is_releases_enabled() is False


def test_is_releases_enabled_truthy(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_USE_RELEASES_ENABLED", "true")
    assert sr.is_releases_enabled() is True


# === SHA / cache ===

def test_compute_sha256(sample_file):
    expected = hashlib.sha256(sample_file.read_bytes()).hexdigest()
    assert sr.compute_sha256(sample_file) == expected


def test_read_release_cache_missing(snap_dir):
    assert sr.read_release_cache(snap_dir) == {}


def test_read_release_cache_invalid_json(snap_dir):
    (snap_dir / sr.CACHE_FILENAME).write_text("not-json{{", encoding="utf-8")
    assert sr.read_release_cache(snap_dir) == {}


def test_write_then_read_cache_roundtrip(snap_dir):
    mapping = {"snapshot-x-2026-05-17": {
        "tag": "snapshot-x-2026-05-17",
        "assets": [{"name": "x.parquet", "size": 123, "sha256": "abc"}],
        "notes": "hello",
    }}
    sr.write_release_cache(snap_dir, mapping)
    assert sr.read_release_cache(snap_dir) == mapping


def test_update_release_cache_merges_entries(snap_dir):
    sr.update_release_cache(
        snap_dir, "tag1", [{"name": "a.parquet", "size": 1, "sha256": "x"}],
    )
    sr.update_release_cache(
        snap_dir, "tag2", [{"name": "b.parquet", "size": 2, "sha256": "y"}],
    )
    cache = sr.read_release_cache(snap_dir)
    assert set(cache.keys()) == {"tag1", "tag2"}
    assert cache["tag1"]["assets"][0]["name"] == "a.parquet"


# === Upload ===

def test_upload_skipped_when_kill_switch_off(monkeypatch, sample_file):
    monkeypatch.setenv("SNAPSHOT_USE_RELEASES_ENABLED", "false")
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: True)
    assert sr.upload_snapshot_to_release("tag", [sample_file]) is False


def test_upload_skipped_when_no_gh_cli(monkeypatch, sample_file):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: False)
    assert sr.upload_snapshot_to_release("tag", [sample_file]) is False


def test_upload_skipped_when_file_missing(monkeypatch, snap_dir):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: True)
    bogus = snap_dir / "nope.parquet"
    assert sr.upload_snapshot_to_release("tag", [bogus]) is False


def test_upload_creates_release_and_uploads(monkeypatch, sample_file, snap_dir):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "release" and args[1] == "view":
            # 模擬 tag 不存在 → CalledProcessError
            raise subprocess.CalledProcessError(1, ["gh", *args])
        return MagicMock(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(sr, "_run_gh", fake_run)
    ok = sr.upload_snapshot_to_release(
        "snapshot-x-2026-05-17", [sample_file],
        notes="test", snapshot_dir=snap_dir,
    )
    assert ok is True
    # call sequence: view → create → upload
    assert calls[0][:2] == ["release", "view"]
    assert calls[1][:2] == ["release", "create"]
    assert calls[2][:2] == ["release", "upload"]
    assert "--clobber" in calls[2]
    cache = sr.read_release_cache(snap_dir)
    assert "snapshot-x-2026-05-17" in cache
    assert cache["snapshot-x-2026-05-17"]["assets"][0]["name"] == sample_file.name
    assert cache["snapshot-x-2026-05-17"]["assets"][0]["sha256"] \
        == sr.compute_sha256(sample_file)


def test_upload_existing_tag_skips_create(monkeypatch, sample_file, snap_dir):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return MagicMock(stdout="{}", stderr="", returncode=0)

    monkeypatch.setattr(sr, "_run_gh", fake_run)
    ok = sr.upload_snapshot_to_release(
        "tag-exists", [sample_file], snapshot_dir=snap_dir,
    )
    assert ok is True
    assert not any(c[:2] == ["release", "create"] for c in calls)


def test_upload_one_asset_failure_returns_false(monkeypatch, sample_file, snap_dir):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: True)
    other = snap_dir / "other.parquet"
    other.write_bytes(b"other-payload")

    def fake_run(args, **kwargs):
        if args[:2] == ["release", "view"]:
            return MagicMock(stdout="{}", stderr="", returncode=0)
        if args[:2] == ["release", "upload"] and args[3] == str(other):
            raise subprocess.CalledProcessError(1, args, stderr="boom")
        return MagicMock(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(sr, "_run_gh", fake_run)
    ok = sr.upload_snapshot_to_release(
        "tag", [sample_file, other], snapshot_dir=snap_dir,
    )
    assert ok is False


# === Download ===

def test_download_skipped_when_kill_switch_off(monkeypatch, snap_dir):
    monkeypatch.setenv("SNAPSHOT_USE_RELEASES_ENABLED", "false")
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: True)
    out = sr.download_snapshot_from_release(
        "tag", "f.parquet", dest=snap_dir,
    )
    assert out is None


def test_download_idempotent_when_local_sha_matches(monkeypatch, snap_dir,
                                                     sample_file):
    sha = sr.compute_sha256(sample_file)
    sr.update_release_cache(
        snap_dir, "tag",
        [{"name": sample_file.name, "size": sample_file.stat().st_size,
          "sha256": sha}],
    )

    call_flag = {"n": 0}

    def fake_run(args, **kwargs):
        call_flag["n"] += 1
        return MagicMock(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(sr, "_has_gh_cli", lambda: True)
    monkeypatch.setattr(sr, "_run_gh", fake_run)

    out = sr.download_snapshot_from_release(
        "tag", sample_file.name,
        dest=snap_dir, snapshot_dir=snap_dir,
    )
    assert out == sample_file
    # 不應該打 gh CLI
    assert call_flag["n"] == 0


def test_download_via_gh_cli(monkeypatch, snap_dir):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: True)

    def fake_run(args, **kwargs):
        # 模擬 gh download 真的把檔案寫到 dest
        if args[:2] == ["release", "download"]:
            dest = Path(args[args.index("--dir") + 1])
            (dest / "x.parquet").write_bytes(b"DOWNLOADED")
        return MagicMock(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(sr, "_run_gh", fake_run)
    out = sr.download_snapshot_from_release(
        "tag", "x.parquet", dest=snap_dir, snapshot_dir=snap_dir,
    )
    assert out is not None and out.exists()
    assert out.read_bytes() == b"DOWNLOADED"
    cache = sr.read_release_cache(snap_dir)
    assert "tag" in cache


def test_download_falls_back_to_rest_when_no_gh(monkeypatch, snap_dir):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: False)

    def fake_rest(repo, tag, asset, dest_file):
        dest_file.write_bytes(b"REST_DOWNLOAD")
        return True

    monkeypatch.setattr(sr, "_download_via_rest", fake_rest)
    out = sr.download_snapshot_from_release(
        "tag", "z.parquet", dest=snap_dir, snapshot_dir=snap_dir,
    )
    assert out is not None and out.read_bytes() == b"REST_DOWNLOAD"


def test_download_rest_failure_returns_none(monkeypatch, snap_dir):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: False)
    monkeypatch.setattr(sr, "_download_via_rest",
                        lambda repo, tag, asset, dest_file: False)
    out = sr.download_snapshot_from_release(
        "tag", "z.parquet", dest=snap_dir, snapshot_dir=snap_dir,
    )
    assert out is None


def test_download_sha_mismatch_returns_none(monkeypatch, snap_dir):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: False)

    def fake_rest(repo, tag, asset, dest_file):
        dest_file.write_bytes(b"NOT_MATCHING_SHA_PAYLOAD")
        return True

    monkeypatch.setattr(sr, "_download_via_rest", fake_rest)
    out = sr.download_snapshot_from_release(
        "tag", "z.parquet", dest=snap_dir, snapshot_dir=snap_dir,
        expected_sha256="deadbeef",
    )
    assert out is None


# === latest tag ===

def test_get_latest_snapshot_tag_via_gh(monkeypatch):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: True)

    def fake_run(args, **kwargs):
        # gh release list --json tagName → newest first
        return MagicMock(
            stdout=json.dumps([
                {"tagName": "snapshot-institutional-2026-05-17"},
                {"tagName": "snapshot-institutional-2026-05-10"},
                {"tagName": "snapshot-financials-2026-05-16"},
            ]),
            stderr="", returncode=0,
        )

    monkeypatch.setattr(sr, "_run_gh", fake_run)
    assert sr.get_latest_snapshot_tag("snapshot-institutional-") \
        == "snapshot-institutional-2026-05-17"
    assert sr.get_latest_snapshot_tag("snapshot-financials-") \
        == "snapshot-financials-2026-05-16"
    assert sr.get_latest_snapshot_tag("snapshot-nonexistent-") is None


def test_get_latest_snapshot_tag_via_rest_fallback(monkeypatch):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: False)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = [
        {"tag_name": "snapshot-institutional-2026-05-17"},
        {"tag_name": "other-tag-2024"},
    ]

    def fake_get(url, headers=None, timeout=60):
        return fake_resp

    monkeypatch.setattr(sr.requests, "get", fake_get)
    assert sr.get_latest_snapshot_tag("snapshot-institutional-") \
        == "snapshot-institutional-2026-05-17"


def test_get_latest_returns_none_on_rest_failure(monkeypatch):
    monkeypatch.setattr(sr, "_has_gh_cli", lambda: False)

    def fake_get(*a, **kw):
        raise sr.requests.RequestException("nope")

    monkeypatch.setattr(sr.requests, "get", fake_get)
    assert sr.get_latest_snapshot_tag("any-") is None


def test_make_snapshot_tag_format():
    assert sr.make_snapshot_tag("institutional", "2026-05-17") \
        == "snapshot-institutional-2026-05-17"
    auto = sr.make_snapshot_tag("financials")
    assert auto.startswith("snapshot-financials-")
    # ISO date 10 chars
    assert len(auto) == len("snapshot-financials-") + 10
