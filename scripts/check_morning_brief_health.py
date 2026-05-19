"""Morning Brief 健康檢查 — 預期推播後 1 小時跑,沒推到就警告主公。

Why
----
2026-05-19 GHA schedule 雙班全 drop, 主公延 1 hr 43 min 才收到盤前快訊。
為防同樣事件再發生不被察覺, 拍板:外部 cron-job.org 主班 + GHA 兩條 fallback
+ 本 health monitor 當第三層守門員。每天 09:30 TW(預期推播 08:30 + 1 hr buffer)
查最近 24 hr 有沒有 morning-brief.yml 成功 run, 沒有就推 Telegram 警告主公
**手動 trigger**(避免今天整個錯過盤前判斷視窗)。

Logic
-----
1. 用 `gh api` (在 workflow 內走 GITHUB_TOKEN) 查 morning-brief.yml 最近 24 hr runs
2. filter status=completed + conclusion=success
3. 若 count > 0 → silent exit 0 (符合 data_health_alert 模式, 全 fresh 不擾)
4. 若 count = 0 → 推 Telegram warning, 附:
   - GH Actions UI 連結(主公點進去手動 EXECUTE workflow_dispatch)
   - cron-job.org dashboard URL(查 history 看 trigger 是否打出去)

Why 不送 Discord:主公拍板 fallback alert 走 Telegram(手機 push 即時),
Discord 用於 daily 推播主管道。alert 走單一渠道避免主公被多個來源轟炸。

CLI
----
    python scripts/check_morning_brief_health.py
    python scripts/check_morning_brief_health.py --dry-run
    python scripts/check_morning_brief_health.py --lookback-hours 24

Exit
----
  0 = 成功(無論有 / 無 alert)
  1 = 嚴重錯誤(GH API 打不通 / Telegram 完全送不出)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.logging_setup import setup_file_logging  # noqa: E402
from src.notifier import send_telegram_message  # noqa: E402

logger = logging.getLogger(__name__)

# 預設往回查 24 hr — cover 主班 cron-job.org(08:30) + GHA fallback(07:47 / 08:17)
# 全部時間窗, 任一成功就算健康。
DEFAULT_LOOKBACK_HOURS = 24

# Repo 固定 jjen0206/stock-screener — health monitor 不需要參數化(它就是這個 repo)
REPO = "jjen0206/stock-screener"
WORKFLOW_FILE = "morning-brief.yml"

# 主公手動 trigger 入口 — alert 訊息會貼這條 link, 主公一鍵點進去 Run workflow
GH_ACTIONS_URL = (
    f"https://github.com/{REPO}/actions/workflows/{WORKFLOW_FILE}"
)
CRON_JOB_ORG_DASHBOARD = "https://console.cron-job.org/jobs"


def _query_recent_runs(lookback_hours: int) -> list[dict]:
    """走 `gh api` 查最近 N 小時的 workflow runs。

    用 gh CLI 而不是 Python http 是因為:
      - workflow 內 GITHUB_TOKEN 已 export → gh auth 自動 work, 不用自己 sign
      - gh 自動處理 pagination / retry, 比 requests 少寫程式
      - 跟本 repo 既有 workflow pattern 一致(其他 script 也走 gh)

    回:每筆 {id, status, conclusion, created_at, html_url, event}。
    失敗 raise RuntimeError(讓 caller 決定 exit code)。
    """
    # ISO 8601 with Z suffix (gh api 接受) — created>=YYYY-MM-DDTHH:MM:SSZ filter
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    # API path:/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs
    # query params:created=>=since (gh api 用 -f 或 -F 傳 query string)
    cmd = [
        "gh",
        "api",
        f"/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs",
        "-f",
        f"created=>={since}",
        "-f",
        "per_page=50",
    ]
    logger.info("[HEALTH] querying GH API: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"gh api 失敗 (exit {e.returncode}): {e.stderr.strip()}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"gh api 超時 (30s): {e}") from e

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh api 回應非 JSON: {e}") from e

    runs = data.get("workflow_runs", [])
    logger.info("[HEALTH] 收到 %d 筆 runs (since %s)", len(runs), since)
    return runs


def _has_recent_success(runs: list[dict]) -> tuple[bool, list[dict]]:
    """從 runs 找完成 + 成功的;回 (bool, success_runs_brief)。"""
    success = [
        {
            "id": r.get("id"),
            "created_at": r.get("created_at"),
            "event": r.get("event"),
            "url": r.get("html_url"),
        }
        for r in runs
        if r.get("status") == "completed" and r.get("conclusion") == "success"
    ]
    return (len(success) > 0, success)


def _format_alert(lookback_hours: int, all_runs: list[dict]) -> str:
    """組 Telegram alert 訊息(純文字 parse_mode='', 避開 Markdown entity 坑)。"""
    # 列最近 3 筆 run (任何狀態) 給主公看到底發生啥
    recent_brief = []
    for r in all_runs[:3]:
        ts = r.get("created_at", "?")
        st = r.get("status", "?")
        cc = r.get("conclusion") or "running"
        ev = r.get("event", "?")
        recent_brief.append(f"  • {ts} [{ev}] {st}/{cc}")
    recent_block = "\n".join(recent_brief) if recent_brief else "  (無任何 run 紀錄)"

    return (
        f"⚠️ Morning Brief 未推播警告\n"
        f"\n"
        f"最近 {lookback_hours} 小時內無成功的 morning-brief.yml run,"
        f"今天的盤前快訊可能漏推。\n"
        f"\n"
        f"最近紀錄(任何狀態):\n"
        f"{recent_block}\n"
        f"\n"
        f"請立刻處理:\n"
        f"1. 開 cron-job.org 看 trigger 是否打出去 → {CRON_JOB_ORG_DASHBOARD}\n"
        f"2. 開 GH Actions 手動 Run workflow → {GH_ACTIONS_URL}\n"
        f"   (Run workflow 按鈕, ref=main, 直接 Run)\n"
        f"\n"
        f"若 cron-job.org 失效 → 檢查 PAT 是否過期"
        f"(docs/external-cron-morning-brief-setup.md Step 5)"
    )


def run(
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    dry_run: bool = False,
) -> dict:
    """主 entry point。回 {healthy, success_count, alert_sent}。"""
    try:
        runs = _query_recent_runs(lookback_hours)
    except RuntimeError as e:
        logger.error("[HEALTH] 查詢失敗: %s", e)
        # 自己掛了至少推一聲讓主公知道 monitor 也壞了
        if not dry_run:
            send_telegram_message(
                f"⚠️ Morning Brief health monitor 故障\n"
                f"GH API 查詢失敗: {e}\n"
                f"請手動檢查 morning-brief.yml 今日是否有 run。",
                parse_mode="",
            )
        return {"healthy": None, "success_count": 0, "alert_sent": not dry_run}

    healthy, success_runs = _has_recent_success(runs)
    result = {
        "healthy": healthy,
        "success_count": len(success_runs),
        "total_runs": len(runs),
        "alert_sent": False,
    }

    if healthy:
        # 走 data_health_alert pattern:全好 → silent, 不噪
        msg = (
            f"[HEALTH] OK — 最近 {lookback_hours}h 有 "
            f"{len(success_runs)} 筆 morning-brief success run"
        )
        print(msg, flush=True)
        for r in success_runs[:5]:
            print(f"  ✓ {r['created_at']} [{r['event']}] {r['url']}", flush=True)
        return result

    msg = _format_alert(lookback_hours, runs)
    print(f"[HEALTH] ALERT:\n{msg}", flush=True)
    if dry_run:
        return result

    result["alert_sent"] = send_telegram_message(msg, parse_mode="")
    if not result["alert_sent"]:
        logger.error("[HEALTH] Telegram 推送失敗 — alert 未送達主公")
    return result


def main() -> int:
    p = argparse.ArgumentParser(
        description="Morning Brief 健康檢查 — 查最近 24h 是否有成功 run,沒有警告"
    )
    p.add_argument("--dry-run", action="store_true", help="不真送 Telegram,只 print")
    p.add_argument(
        "--lookback-hours",
        type=int,
        default=DEFAULT_LOOKBACK_HOURS,
        help=f"往回查幾小時(預設 {DEFAULT_LOOKBACK_HOURS})",
    )
    args = p.parse_args()

    setup_file_logging(
        "morning_brief_health", level=logging.INFO, mirror_print=True
    )

    if not os.environ.get("GITHUB_TOKEN") and not os.environ.get("GH_TOKEN"):
        logger.warning(
            "[HEALTH] 環境無 GITHUB_TOKEN / GH_TOKEN — gh api 可能無法 auth"
        )

    try:
        result = run(
            lookback_hours=args.lookback_hours,
            dry_run=args.dry_run,
        )
        if result["healthy"] is None:
            return 1
        return 0
    except Exception as e:  # noqa: BLE001
        logger.exception("[HEALTH] 嚴重錯誤: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
