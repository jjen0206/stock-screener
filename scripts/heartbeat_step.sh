#!/usr/bin/env bash
# heartbeat_step.sh — workflow 收尾統一呼叫;寫 CSV + commit + push retry。
#
# Usage:
#   bash scripts/heartbeat_step.sh TASK_NAME INTERVAL_HOURS JOB_STATUS [REASON]
#
# JOB_STATUS:GitHub Actions `job.status` 變數(success/failure/cancelled)
#   - success → record_success
#   - 其他 → record_failure(reason 含 status)
#
# 此 step 永遠 exit 0:heartbeat 失敗不擋主 workflow。
set -u
TASK="${1:?TASK_NAME required}"
INTERVAL="${2:?INTERVAL_HOURS required}"
STATUS="${3:?JOB_STATUS required}"
REASON="${4:-}"

# 1. write CSV row
if [ "$STATUS" = "success" ]; then
  python scripts/heartbeat_record.py success "$TASK" --interval "$INTERVAL" || {
    echo "[HEARTBEAT] record_success failed (non-fatal)"
    exit 0
  }
else
  msg="GHA job.status=$STATUS"
  if [ -n "$REASON" ]; then msg="$msg; $REASON"; fi
  python scripts/heartbeat_record.py failure "$TASK" --reason "$msg" --interval "$INTERVAL" || {
    echo "[HEARTBEAT] record_failure failed (non-fatal)"
    exit 0
  }
fi

# 2. commit + push retry(同 daily-notify pattern)
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add data/twse_snapshot/sync_log_heartbeat.csv 2>/dev/null || true
if git diff --quiet --staged; then
  echo "[HEARTBEAT] no CSV change"
  exit 0
fi
git commit -m "chore(heartbeat): $TASK $STATUS [skip ci]" || {
  echo "[HEARTBEAT] commit failed (non-fatal)"
  exit 0
}
for i in 1 2 3 4 5; do
  git pull --rebase --autostash origin main || {
    git rebase --abort 2>/dev/null || true
    echo "[HEARTBEAT] rebase failed on attempt $i"
    continue
  }
  if git push origin HEAD:main; then
    echo "[HEARTBEAT] push OK on attempt $i"
    exit 0
  fi
  echo "[HEARTBEAT] push rejected (attempt $i), retry"
  sleep $((i * 3))
done
echo "::warning::heartbeat push 失敗 5 次 (task=$TASK)"
exit 0
