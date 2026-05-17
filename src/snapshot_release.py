"""GitHub Releases as bulk-snapshot storage(根治 100MB / Git LFS quota 上限)。

設計動機
--------
2026-05-17 健診:`data/twse_snapshot/` 內 institutional.csv 已逼近 100MB 上限
(daily_prices.csv 200K+ 行)。再加 22 月 backfill 直接撞牆,git push 拒收。
LFS 又有月流量上限+額外計費。**最乾淨的方案是 GitHub Releases**:

  - 單個 asset 上限 2 GB,repo 總量無限
  - 不污染 git history(`git clone` 不會帶下來)
  - Versioned 天然(每次 backfill = 1 個 release tag,可 rollback)
  - 匿名 download rate-limit 60/hr(個人專案綽綽)
  - Token 用 `GITHUB_TOKEN`(GH Actions 自動帶)或 `GITHUB_PAT`(本機)

Public API
----------
- :func:`upload_snapshot_to_release(tag, files, notes)` — 上傳/覆寫 asset
- :func:`download_snapshot_from_release(tag, asset_name, dest)` — 拉 asset
- :func:`get_latest_snapshot_tag(prefix)` — 找最新 tag(`snapshot-institutional-*`)
- :func:`is_releases_enabled()` — kill-switch (env)

Cache:`data/twse_snapshot/.snapshot_releases.json` 記 (tag, asset, size, sha256),
loader 第一次 download 後 SHA 寫進 cache,下次 boot 若本地檔 SHA 對就 skip download。

Kill switch:`SNAPSHOT_USE_RELEASES_ENABLED=false` 完全停用 release 路徑(只走
原本 CSV preload)。預設 on。

實作策略
--------
- Upload 一律走 `gh` CLI(需 auth scope,REST 太囉嗦)。CLI 缺 → log warning 跳過
- Download 優先 `gh` CLI,缺 → fallback REST API `https://github.com/{repo}/releases/download/{tag}/{asset}`
  (public repo 匿名可用,Streamlit Cloud 無 gh CLI 走這條)
- `get_latest_snapshot_tag` 同上 — gh CLI 優先,REST API fallback
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


DEFAULT_REPO = "jjen0206/stock-screener"
CACHE_FILENAME = ".snapshot_releases.json"
HTTP_TIMEOUT = 60


# === Public env helpers ===

def is_releases_enabled() -> bool:
    """Read `SNAPSHOT_USE_RELEASES_ENABLED` kill-switch (default on)."""
    raw = os.getenv("SNAPSHOT_USE_RELEASES_ENABLED", "").strip().lower()
    if raw in {"false", "0", "no", "off"}:
        return False
    return True


def _resolve_repo(repo: str | None = None) -> str:
    """Repo override priority: arg → GITHUB_REPO env → DEFAULT_REPO."""
    if repo:
        return repo
    env = os.getenv("GITHUB_REPO", "").strip()
    return env or DEFAULT_REPO


def _resolve_token() -> str | None:
    """Pick first non-empty token: GITHUB_TOKEN (Actions) → GITHUB_PAT (本機)."""
    for key in ("GITHUB_TOKEN", "GITHUB_PAT"):
        v = os.getenv(key, "").strip()
        if v:
            return v
    return None


def _has_gh_cli() -> bool:
    """Check `gh` CLI 是否在 PATH 內。"""
    return shutil.which("gh") is not None


# === SHA cache (.snapshot_releases.json) ===

def compute_sha256(file_path: str | Path, chunk_size: int = 1 << 20) -> str:
    """Stream-hash 一個檔案 → hex digest。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def read_release_cache(snapshot_dir: str | Path) -> dict:
    """讀 `.snapshot_releases.json`,缺檔/格式錯回 {}。"""
    p = Path(snapshot_dir) / CACHE_FILENAME
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as ex:
        logger.warning("read_release_cache 讀 %s 失敗: %s", p, ex)
        return {}


def write_release_cache(snapshot_dir: str | Path, mapping: dict) -> None:
    """覆寫 `.snapshot_releases.json`。"""
    p = Path(snapshot_dir)
    p.mkdir(parents=True, exist_ok=True)
    out = p / CACHE_FILENAME
    with open(out, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2, sort_keys=True)


def update_release_cache(
    snapshot_dir: str | Path,
    tag: str,
    assets: list[dict],
    notes: str | None = None,
) -> dict:
    """Merge 一筆 release record 到 cache。"""
    cache = read_release_cache(snapshot_dir)
    cache[tag] = {
        "tag": tag,
        "assets": assets,
        "notes": notes or "",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_release_cache(snapshot_dir, cache)
    return cache


# === gh CLI 包一層,可被 mock ===

def _run_gh(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """跑 `gh args...`,raise CalledProcessError 若 exit != 0。"""
    cmd = ["gh", *args]
    logger.debug("run: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


# === Upload ===

def upload_snapshot_to_release(
    tag: str,
    files: list[Path],
    notes: str | None = None,
    *,
    repo: str | None = None,
    snapshot_dir: str | Path | None = None,
) -> bool:
    """上傳一組檔案到 GH release。Tag 不存在 → 先建立 release。

    Args:
        tag: release tag,慣例 `snapshot-{kind}-{YYYY-MM-DD}`
        files: 要上傳的本地路徑(已存在)
        notes: release body(markdown OK)
        repo: 預設 `jjen0206/stock-screener`,可用 `GITHUB_REPO` env 蓋
        snapshot_dir: 寫 `.snapshot_releases.json` 用,None = 不更新 cache

    Returns:
        True 上傳成功(全部 asset 都 ok),False 失敗(無 gh CLI / kill-switch off /
        所有 asset 上傳爆)。

    Notes:
        - 已存在 tag → 同 tag 重跑會用 `--clobber` 覆寫同名 asset(idempotent)
        - 失敗只 log warning,不 raise:caller 是 backfill workflow,不應因 release
          上傳失敗整個 abort(snapshot 還在 SQLite 裡)
    """
    if not is_releases_enabled():
        logger.info(
            "SNAPSHOT_USE_RELEASES_ENABLED=false,upload_snapshot_to_release skip",
        )
        return False
    if not _has_gh_cli():
        logger.warning(
            "`gh` CLI 不存在,無法 upload release。"
            "請裝 gh (https://cli.github.com/) 或在 GH Actions 用 setup-gh-cli。",
        )
        return False
    if not files:
        logger.info("upload_snapshot_to_release: 沒檔案要傳,skip")
        return False

    files = [Path(f) for f in files]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        logger.warning("upload_snapshot_to_release 漏檔:%s", missing)
        return False

    repo_full = _resolve_repo(repo)
    body = notes or f"Snapshot release {tag}"

    # Step 1: 確認/建立 release。沒有 → create,有 → 直接 upload --clobber 即可
    try:
        _run_gh(
            ["release", "view", tag, "--repo", repo_full, "--json", "tagName"],
        )
        existed = True
    except subprocess.CalledProcessError:
        existed = False
    except (FileNotFoundError, subprocess.TimeoutExpired) as ex:
        logger.warning("gh release view 異常: %s", ex)
        return False

    if not existed:
        try:
            _run_gh([
                "release", "create", tag,
                "--repo", repo_full,
                "--title", tag,
                "--notes", body,
            ])
        except subprocess.CalledProcessError as ex:
            logger.warning(
                "gh release create %s 失敗: %s | %s",
                tag, ex.stderr.strip() if ex.stderr else ex, ex.returncode,
            )
            return False

    # Step 2: 上傳每個 asset(--clobber 允許覆寫)
    ok_count = 0
    asset_records: list[dict] = []
    for f in files:
        try:
            _run_gh([
                "release", "upload", tag, str(f),
                "--repo", repo_full,
                "--clobber",
            ], timeout=1800)
        except subprocess.CalledProcessError as ex:
            logger.warning(
                "gh release upload %s 失敗: %s",
                f.name, ex.stderr.strip() if ex.stderr else ex,
            )
            continue
        ok_count += 1
        asset_records.append({
            "name": f.name,
            "size": f.stat().st_size,
            "sha256": compute_sha256(f),
        })
        logger.info("[RELEASE] uploaded %s → %s (%s bytes)",
                    f.name, tag, asset_records[-1]["size"])

    # Step 3: 更新 cache
    if snapshot_dir and asset_records:
        try:
            update_release_cache(
                snapshot_dir, tag, asset_records, notes=body,
            )
        except OSError as ex:
            logger.warning("update_release_cache 失敗: %s", ex)

    return ok_count == len(files)


# === Download ===

def _download_via_rest(
    repo_full: str, tag: str, asset_name: str, dest_file: Path,
) -> bool:
    """Anonymous public-asset download via direct CDN URL。"""
    url = (
        f"https://github.com/{repo_full}/releases/download/{tag}/{asset_name}"
    )
    headers: dict[str, str] = {}
    token = _resolve_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with requests.get(
            url, headers=headers, stream=True, timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        ) as resp:
            if resp.status_code != 200:
                logger.warning(
                    "_download_via_rest %s HTTP %s",
                    url, resp.status_code,
                )
                return False
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest_file.with_suffix(dest_file.suffix + ".partial")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
            tmp.replace(dest_file)
        return True
    except requests.RequestException as ex:
        logger.warning("_download_via_rest %s 失敗: %s", url, ex)
        return False


def download_snapshot_from_release(
    tag: str,
    asset_name: str,
    dest: str | Path,
    *,
    repo: str | None = None,
    snapshot_dir: str | Path | None = None,
    expected_sha256: str | None = None,
) -> Path | None:
    """下載一個 asset 到 `dest`(可 dir 或 file path)。

    Args:
        tag: release tag (`snapshot-institutional-2026-05-17`)
        asset_name: asset filename (`institutional.parquet`)
        dest: 目的 — dir 就放 `{dest}/{asset_name}`,file path 就用 file path
        repo: 預設 `jjen0206/stock-screener`
        snapshot_dir: 讀 `.snapshot_releases.json` 用,有 cached SHA 就 skip download
        expected_sha256: 額外驗證 SHA(可選);若給且 mismatch → 重抓一次

    Returns:
        本地檔 path 若成功,None 若失敗(kill-switch / 都拿不到 / SHA mismatch)。

    Idempotency:
        若 dest 已存在且 SHA(從 cache 或 expected)對 → 直接 return,不打 GH。
    """
    if not is_releases_enabled():
        logger.info(
            "SNAPSHOT_USE_RELEASES_ENABLED=false,download_snapshot_from_release skip",
        )
        return None

    dest = Path(dest)
    if dest.is_dir() or (dest.suffix == "" and not dest.exists()):
        dest_file = dest / asset_name
    else:
        dest_file = dest
    dest_file.parent.mkdir(parents=True, exist_ok=True)

    # cache 內的 SHA(若有)當權威 expected,優先於 caller 的 expected
    cached_sha: str | None = None
    if snapshot_dir is not None:
        cache = read_release_cache(snapshot_dir)
        rec = cache.get(tag) or {}
        for a in rec.get("assets") or []:
            if a.get("name") == asset_name:
                cached_sha = a.get("sha256")
                break
    want_sha = cached_sha or expected_sha256

    # Idempotent skip
    if dest_file.exists() and want_sha:
        try:
            actual = compute_sha256(dest_file)
        except OSError:
            actual = ""
        if actual == want_sha:
            logger.info(
                "[RELEASE] %s 已在本地且 SHA 對,skip download",
                dest_file,
            )
            return dest_file

    repo_full = _resolve_repo(repo)

    # 試 gh CLI 先
    used_gh = False
    if _has_gh_cli():
        try:
            _run_gh([
                "release", "download", tag,
                "--repo", repo_full,
                "--pattern", asset_name,
                "--dir", str(dest_file.parent),
                "--clobber",
            ], timeout=1800)
            used_gh = True
        except subprocess.CalledProcessError as ex:
            logger.warning(
                "gh release download %s/%s 失敗,改 fallback REST: %s",
                tag, asset_name, ex.stderr.strip() if ex.stderr else ex,
            )

    if not used_gh:
        ok = _download_via_rest(repo_full, tag, asset_name, dest_file)
        if not ok:
            return None

    if not dest_file.exists():
        logger.warning(
            "download_snapshot_from_release: dest_file 不存在 (%s),失敗",
            dest_file,
        )
        return None

    if want_sha:
        actual = compute_sha256(dest_file)
        if actual != want_sha:
            logger.warning(
                "[RELEASE] SHA mismatch for %s: want=%s actual=%s",
                dest_file.name, want_sha, actual,
            )
            return None

    # 沒 cache 紀錄 → 寫進去(下次 boot skip)
    if snapshot_dir is not None and not cached_sha:
        try:
            actual_sha = compute_sha256(dest_file)
            update_release_cache(
                snapshot_dir,
                tag,
                [{
                    "name": asset_name,
                    "size": dest_file.stat().st_size,
                    "sha256": actual_sha,
                }],
                notes=f"Downloaded {tag}",
            )
        except OSError as ex:
            logger.warning("write cache after download 失敗: %s", ex)

    return dest_file


# === Latest tag lookup ===

def _list_releases_via_gh(
    repo_full: str, limit: int = 30,
) -> list[str]:
    """Return list of tag names ordered by GH default (newest first)."""
    try:
        out = _run_gh([
            "release", "list",
            "--repo", repo_full,
            "--limit", str(limit),
            "--json", "tagName",
        ])
    except (subprocess.CalledProcessError, FileNotFoundError) as ex:
        logger.warning("gh release list 失敗: %s", ex)
        return []
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    return [r.get("tagName") for r in data if r.get("tagName")]


def _list_releases_via_rest(
    repo_full: str, limit: int = 30,
) -> list[str]:
    """REST fallback (Streamlit Cloud has no gh CLI)。"""
    headers = {"Accept": "application/vnd.github+json"}
    token = _resolve_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{repo_full}/releases?per_page={limit}"
    try:
        resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    except requests.RequestException as ex:
        logger.warning("REST releases 失敗: %s", ex)
        return []
    if resp.status_code != 200:
        logger.warning("REST releases HTTP %s", resp.status_code)
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [r.get("tag_name") for r in data if r.get("tag_name")]


def list_release_tags(
    prefix: str | None = None,
    *,
    repo: str | None = None,
    limit: int = 30,
) -> list[str]:
    """回 newest-first tag list。`prefix` 給定就只回符合的。"""
    repo_full = _resolve_repo(repo)
    tags = _list_releases_via_gh(repo_full, limit=limit) if _has_gh_cli() else []
    if not tags:
        tags = _list_releases_via_rest(repo_full, limit=limit)
    if prefix:
        tags = [t for t in tags if t.startswith(prefix)]
    return tags


def get_latest_snapshot_tag(
    prefix: str,
    *,
    repo: str | None = None,
) -> str | None:
    """回最新一筆符合 `prefix` 的 release tag,沒有就 None。

    用例:`get_latest_snapshot_tag('snapshot-institutional-')`
    """
    tags = list_release_tags(prefix=prefix, repo=repo, limit=30)
    return tags[0] if tags else None


def make_snapshot_tag(kind: str, date_iso: str | None = None) -> str:
    """`snapshot-{kind}-{YYYY-MM-DD}`。預設 today (UTC)."""
    if not date_iso:
        date_iso = datetime.now(timezone.utc).date().isoformat()
    return f"snapshot-{kind}-{date_iso}"


__all__ = [
    "DEFAULT_REPO",
    "CACHE_FILENAME",
    "is_releases_enabled",
    "compute_sha256",
    "read_release_cache",
    "write_release_cache",
    "update_release_cache",
    "upload_snapshot_to_release",
    "download_snapshot_from_release",
    "list_release_tags",
    "get_latest_snapshot_tag",
    "make_snapshot_tag",
]
