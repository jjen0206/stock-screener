"""data/twse_snapshot/*.csv 各檔健康狀態檢查 — 給系統頁顯示。

設計初衷:2026-05-06 主公發現「卡片股價 39.55 vs 📡 40.85」差 3 元的根因
是 daily_market_update 漏 dump daily_prices.csv → snapshot 卡 4/30 沒人察覺。
此 module 暴露各 CSV 最新資料日期 + 距今天差距,異常時系統頁醒目顯示
紅色 banner,主公一打開系統頁就能自助診斷,不必每次找亮 debug。

純 read-only — 不修任何 CSV / SQLite。
"""
from __future__ import annotations

from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import TypedDict

import pandas as pd

from src import config


# 各 CSV 預期更新頻率 + (warn_lag_days, error_lag_days)
# warn:稍微落後但可接受;error:明顯有問題該處理
_EXPECTED: dict[str, tuple[str, int, int, str]] = {
    # csv_name:           (expected_freq,    warn,  error,  date_column)
    "daily_prices":       ("trading_day",    2,     5,      "date"),
    "daily_metrics":      ("trading_day",    2,     5,      "date"),
    "taiex":              ("trading_day",    2,     5,      "date"),
    "institutional":      ("trading_day",    3,     7,      "date"),
    # quarterly:台股季報法定 5/15 / 8/14 / 11/14 / 3/31 publish,自然 lag 30-60 天
    # warn=60 / error=100(原 7/30 太嚴 → 季中會誤報 error)。
    "financials_quarterly": ("daily_inc",    60,    100,    "period"),
    "monthly_revenue":    ("daily_inc",      14,    45,     "period"),
    "dividend":           ("daily_inc",      30,    180,    "ex_dividend_date"),
    "news":               ("hourly",         2,     7,      "publish_date"),
    "daily_picks":        ("trading_day",    2,     5,      "trade_date"),
    "strategy_backtest":  ("weekly",         8,     21,     "period_end"),
}


class SnapshotHealth(TypedDict):
    table: str
    csv_path: str
    exists: bool
    last_date: str | None
    days_lag: int | None
    row_count: int
    expected_freq: str
    status: str  # 'ok' / 'warn' / 'error' / 'missing'
    note: str    # 可選說明


def _read_max_date(csv_path: Path, date_col: str) -> tuple[str | None, int]:
    """讀 CSV,回 (max_value of date_col, row_count)。空檔 / 解析失敗回 (None, 0)。

    period 欄是字串("2024-Q4" / "2024-12"),max() 字串比較剛好 chronological ✓
    """
    if not csv_path.exists():
        return None, 0
    try:
        # 只讀必要欄位節省記憶體
        df = pd.read_csv(csv_path, usecols=[date_col], dtype=str)
    except (pd.errors.EmptyDataError, ValueError):
        return None, 0
    if df.empty:
        return None, 0
    series = df[date_col].dropna()
    if series.empty:
        return None, 0
    return str(series.max()), len(df)


def _compute_days_lag(last_date: str | None, today_iso: str | None = None) -> int | None:
    """算 today - last_date 的日曆天數。last_date 不是 ISO 日期(例 quarterly
    period '2024-Q4' / monthly '2024-12')→ 取年月當該月底 / 該季最後一天。
    解析失敗回 None。
    """
    if not last_date:
        return None
    today = (
        _date.today() if today_iso is None
        else _date.fromisoformat(today_iso)
    )
    s = last_date.strip()
    try:
        # ISO date "YYYY-MM-DD"
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            d = _date.fromisoformat(s[:10])
            return (today - d).days
        # quarterly "YYYY-QN" → 該季最後一天
        if "Q" in s:
            year_s, q_s = s.split("Q")
            year = int(year_s.rstrip("-"))
            q = int(q_s)
            month_end = {1: 3, 2: 6, 3: 9, 4: 12}[q]
            day_end = {3: 31, 6: 30, 9: 30, 12: 31}[month_end]
            d = _date(year, month_end, day_end)
            return (today - d).days
        # monthly "YYYY-MM" → 該月最後一天
        if len(s) == 7 and s[4] == "-":
            year, month = int(s[:4]), int(s[5:7])
            # 簡單 28/30/31 拍板:取 28 保守(寧 lag 多點不要 negative)
            d = _date(year, month, 28)
            return (today - d).days
    except (ValueError, KeyError):
        return None
    return None


def _classify(days_lag: int | None, warn: int, error: int) -> str:
    """lag 跟 warn / error 門檻比 → ok / warn / error / unknown。"""
    if days_lag is None:
        return "unknown"
    if days_lag <= warn:
        return "ok"
    if days_lag <= error:
        return "warn"
    return "error"


def get_snapshot_health(
    snapshot_dir: Path | str | None = None,
    today_iso: str | None = None,
) -> list[SnapshotHealth]:
    """掃 data/twse_snapshot/ 所有 CSV → 回各檔最新資料日期 + 健康狀態。

    snapshot_dir=None 用 config.PROJECT_ROOT / data/twse_snapshot。
    today_iso=None 取系統今日。
    """
    if snapshot_dir is None:
        base = config.PROJECT_ROOT / "data" / "twse_snapshot"
    else:
        base = Path(snapshot_dir)

    out: list[SnapshotHealth] = []
    for table, (freq, warn, error, date_col) in _EXPECTED.items():
        csv_path = base / f"{table}.csv"
        exists = csv_path.exists()
        if not exists:
            out.append(SnapshotHealth(
                table=table,
                csv_path=str(csv_path),
                exists=False,
                last_date=None,
                days_lag=None,
                row_count=0,
                expected_freq=freq,
                status="missing",
                note="CSV 不存在(尚未首次 backfill?)",
            ))
            continue
        max_date, row_count = _read_max_date(csv_path, date_col)
        days_lag = _compute_days_lag(max_date, today_iso=today_iso)
        status = _classify(days_lag, warn, error)
        note = ""
        if status == "error":
            note = f"⚠️ 落後 {days_lag} 天(警戒線 {error})"
        elif status == "warn":
            note = f"落後 {days_lag} 天(預期 ≤ {warn})"
        elif status == "missing":
            note = "CSV 不存在"
        out.append(SnapshotHealth(
            table=table,
            csv_path=str(csv_path),
            exists=True,
            last_date=max_date,
            days_lag=days_lag,
            row_count=row_count,
            expected_freq=freq,
            status=status,
            note=note,
        ))
    return out


def overall_status(rows: list[SnapshotHealth]) -> str:
    """整體狀態:任一 error → 'error';任一 warn → 'warn';否則 'ok'。"""
    if any(r["status"] == "error" for r in rows):
        return "error"
    if any(r["status"] == "warn" for r in rows):
        return "warn"
    if any(r["status"] == "missing" for r in rows):
        return "warn"
    return "ok"


def get_last_update_text(snapshot_dir: Path | str | None = None) -> str | None:
    """讀 data/twse_snapshot/last_update.txt(daily_market_update 寫入的 timestamp)。

    回完整檔案內容(含 updated_at / git_sha / run_id / 各表 row 數)或 None。
    """
    if snapshot_dir is None:
        base = config.PROJECT_ROOT / "data" / "twse_snapshot"
    else:
        base = Path(snapshot_dir)
    path = base / "last_update.txt"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


__all__ = [
    "SnapshotHealth",
    "get_snapshot_health",
    "overall_status",
    "get_last_update_text",
]
