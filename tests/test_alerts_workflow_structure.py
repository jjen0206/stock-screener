"""結構性 guard:確保兩個 alerts workflow 檔存在 + cron / script wiring 對。

不跑 GitHub Actions(無法在本地),改 yaml parse + 文字搜尋。
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_INTRADAY_YML = _ROOT / ".github" / "workflows" / "intraday-alerts.yml"
_DATA_HEALTH_YML = _ROOT / ".github" / "workflows" / "data-health-alert.yml"


def _parse_yml(path: Path):
    import yaml
    assert path.exists(), f"{path.name} 不存在"
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed is not None, f"{path.name} 解析空"
    # PyYAML 把 'on:' 解成 boolean True key(YAML 1.1 'on' aliasing)
    assert ("on" in parsed) or (True in parsed), (
        f"{path.name} 缺 on: 觸發定義"
    )
    assert "jobs" in parsed, f"{path.name} 缺 jobs"
    return parsed


# === intraday-alerts.yml ===

def test_intraday_workflow_exists_and_parseable():
    _parse_yml(_INTRADAY_YML)


def test_intraday_workflow_has_correct_cron():
    """*/30 * * * 1-5 — 週一至週五每 30 分鐘掃。"""
    src = _INTRADAY_YML.read_text(encoding="utf-8")
    assert (
        'cron: "*/30 * * * 1-5"' in src
        or "cron: '*/30 * * * 1-5'" in src
    ), "intraday-alerts.yml cron 必須是 '*/30 * * * 1-5'"


def test_intraday_workflow_runs_intraday_alerts_script():
    """workflow 必須執行 scripts/intraday_alerts.py。"""
    src = _INTRADAY_YML.read_text(encoding="utf-8")
    assert "scripts/intraday_alerts.py" in src


def test_intraday_workflow_exposes_telegram_discord_secrets():
    """推播需要 TG + Discord 兩通道的 secrets。"""
    src = _INTRADAY_YML.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN" in src
    assert "TELEGRAM_CHAT_ID" in src
    assert "DISCORD_WEBHOOK_URL" in src


def test_intraday_workflow_supports_dry_run_dispatch():
    src = _INTRADAY_YML.read_text(encoding="utf-8")
    assert "dry_run" in src
    assert "workflow_dispatch" in src


# === data-health-alert.yml ===

def test_data_health_workflow_exists_and_parseable():
    _parse_yml(_DATA_HEALTH_YML)


def test_data_health_workflow_has_correct_cron():
    """01:00 UTC = 09:00 TW(早上)— 每天跑一次。"""
    src = _DATA_HEALTH_YML.read_text(encoding="utf-8")
    assert (
        'cron: "0 1 * * *"' in src
        or "cron: '0 1 * * *'" in src
    ), "data-health-alert.yml cron 必須是 '0 1 * * *'(09:00 TW)"


def test_data_health_workflow_runs_data_health_alert_script():
    src = _DATA_HEALTH_YML.read_text(encoding="utf-8")
    assert "scripts/data_health_alert.py" in src


def test_data_health_workflow_exposes_telegram_discord_secrets():
    src = _DATA_HEALTH_YML.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN" in src
    assert "TELEGRAM_CHAT_ID" in src
    assert "DISCORD_WEBHOOK_URL" in src


def test_data_health_workflow_supports_dry_run_dispatch():
    src = _DATA_HEALTH_YML.read_text(encoding="utf-8")
    assert "dry_run" in src
    assert "workflow_dispatch" in src


# === 兩 script 一起 sanity check ===

def test_alert_scripts_exist():
    assert (_ROOT / "scripts" / "intraday_alerts.py").exists()
    assert (_ROOT / "scripts" / "data_health_alert.py").exists()


def test_alert_dedup_table_in_schema():
    """alert_dedup CREATE TABLE 必須在 src/database.py 內(intraday_alerts 依賴)。"""
    schema = (_ROOT / "src" / "database.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS alert_dedup" in schema, (
        "alert_dedup 表 schema 必須在 init_db 流程中建立"
    )
    assert "PRIMARY KEY (sid, alert_type, alert_date)" in schema, (
        "alert_dedup PK 必須是 (sid, alert_type, alert_date)"
    )
