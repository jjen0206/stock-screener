"""題材熱度動態權重 — 根據近 N 日題材表現自動加減 score。

設計初衷(主公 2026-05-15 拍板):
  熱題材(HBM / 矽光子 / CoWoS)在噴 → 自動加分
  冷題材(國防 / 重電 / 低軌衛星)在修正 → 自動降權
  輪動到時不用手動改,系統每天自己重算。

公式:
  - 對 `data/themes/*.yaml` 每個題材撈成分股近 N 日:
      avg_return  = mean((latest_close - oldest_close) / oldest_close * 100)
      win_rate    = # sids with positive return / # sids with valid window
      heat_score  = avg_return × 0.6 + win_rate × 0.4
        (avg_return 為 percent 例 8.86;win_rate 為 fraction 例 0.7;
         result 約等於 avg_return 數量級,可跟 % 閥值比)
  - multiplier 規則(主公拍板):
      heat_score > 3.0  AND win_rate > 0.5  → ×1.3 (🔥 熱)
      heat_score < -2.0 OR  win_rate < 0.3  → ×0.7 (🧊 冷)
      其他                                  → ×1.0 (➖ 中性)
  - sid 屬多題材 → 取**最高**multiplier(避免熱題材被冷題材稀釋)

Kill-switch:env `THEME_HEAT_ENABLED=true`(預設 on)。off 時 notifier
應走 multiplier=1.0 path,等同行為被關掉。
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import date as _date
from pathlib import Path
from typing import Optional

from src import config


# === Constants(主公拍板,寫死) ===

WINDOW_DAYS_DEFAULT = 5
RETURN_WEIGHT = 0.6        # heat_score 公式權重
WIN_RATE_WEIGHT = 0.4

HOT_HEAT_THR = 3.0         # heat_score 超過 3% 視為熱
HOT_WIN_RATE_THR = 0.5     # win_rate 超過 50% 才算熱(雙條件)
COLD_HEAT_THR = -2.0       # heat_score 低於 -2% 視為冷
COLD_WIN_RATE_THR = 0.3    # 或 win_rate 低於 30% 也算冷

HOT_MULTIPLIER = 1.3
COLD_MULTIPLIER = 0.7
NEUTRAL_MULTIPLIER = 1.0


THEMES_DIR = config.PROJECT_ROOT / "data" / "themes"


def _is_enabled() -> bool:
    """讀 env THEME_HEAT_ENABLED(預設 true)— 給 notifier / app 共用 kill-switch。

    runtime 讀(不在 import time 鎖死)讓測試 monkeypatch.setenv 可以即時生效。
    """
    raw = os.getenv("THEME_HEAT_ENABLED", "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


# Backwards-compat module flag(對齊 STRATEGY_DYNAMIC_WEIGHT_ENABLED 風格)
THEME_HEAT_ENABLED = _is_enabled()


# === Module-level cache ===
# key: (as_of_iso_or_'', window_days) → heat dict
_HEAT_CACHE: dict[tuple[str, int], dict[str, dict]] = {}


def reset_cache() -> None:
    """測試用:清掉 module-level cache(避免不同測試互污染)。"""
    _HEAT_CACHE.clear()


# === Theme loading ===

_DISPLAY_NAME_TRIM_RE = re.compile(r"\s*[\(（].*$")


def _parse_display_name(first_comment: str) -> str:
    """從 yaml 第一行 comment 取顯示名,丟掉年份/註腳括號。

    例:
      "# HBM 高頻寬記憶體概念股 (2024-2026 市場共識 union)"
      → "HBM 高頻寬記憶體概念股"
    """
    line = first_comment.lstrip("#").strip()
    return _DISPLAY_NAME_TRIM_RE.sub("", line).strip()


def _load_themes(themes_dir: Path | None = None) -> dict[str, dict]:
    """讀 `data/themes/*.yaml` → dict[theme_key → {display_name, sids}]。

    theme_key = file stem(e.g. "hbm_memory")。
    沒裝 yaml / 目錄不存在 / yaml 解析失敗的單檔 silent skip。
    """
    d = themes_dir or THEMES_DIR
    if not d.exists():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    out: dict[str, dict] = {}
    for fp in sorted(d.glob("*.yaml")):
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        # 抓第一行 comment 當顯示名
        first_line = text.split("\n", 1)[0] if text else ""
        display_name = (
            _parse_display_name(first_line) if first_line.startswith("#")
            else fp.stem
        )
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        raw_sids = data.get("sids") or []
        if not isinstance(raw_sids, list):
            continue
        sids = sorted({str(s).strip() for s in raw_sids if s})
        if not sids:
            continue
        out[fp.stem] = {"display_name": display_name, "sids": sids}
    return out


# === Per-sid return ===

def _sid_window_return_pct(
    conn: sqlite3.Connection,
    sid: str,
    as_of: str,
    window_days: int,
) -> Optional[float]:
    """撈 sid 在 as_of(含)往前 N 個交易日的 close,算頭尾 % 報酬。

    交易日數 < 2 → None(無法算 return)。oldest_close ≤ 0 → None(防 div 0)。
    """
    rows = conn.execute(
        "SELECT close FROM daily_prices "
        "WHERE stock_id = ? AND date <= ? "
        "ORDER BY date DESC LIMIT ?",
        (sid, as_of, window_days),
    ).fetchall()
    if len(rows) < 2:
        return None
    latest = rows[0]["close"] if hasattr(rows[0], "keys") else rows[0][0]
    oldest = rows[-1]["close"] if hasattr(rows[-1], "keys") else rows[-1][0]
    if latest is None or oldest is None or oldest <= 0:
        return None
    return (float(latest) - float(oldest)) / float(oldest) * 100.0


def _classify_multiplier(heat_score: float, win_rate: float) -> float:
    """主公拍板規則:熱 ×1.3 / 冷 ×0.7 / 中性 ×1.0。

    熱:heat_score > 3% AND win_rate > 50%
    冷:heat_score < -2% OR  win_rate < 30%
    其他:1.0(中性)
    """
    if heat_score > HOT_HEAT_THR and win_rate > HOT_WIN_RATE_THR:
        return HOT_MULTIPLIER
    if heat_score < COLD_HEAT_THR or win_rate < COLD_WIN_RATE_THR:
        return COLD_MULTIPLIER
    return NEUTRAL_MULTIPLIER


def _resolve_as_of(conn: sqlite3.Connection, as_of: str | None) -> str:
    """as_of None → 取 daily_prices 最新一日;空表 → 系統 today。"""
    if as_of:
        return as_of[:10]
    try:
        row = conn.execute(
            "SELECT MAX(date) AS mx FROM daily_prices"
        ).fetchone()
    except sqlite3.OperationalError:
        return _date.today().isoformat()
    if not row:
        return _date.today().isoformat()
    mx = row["mx"] if hasattr(row, "keys") else row[0]
    return mx if mx else _date.today().isoformat()


# === Public API ===

def compute_theme_heat(
    conn: sqlite3.Connection,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS_DEFAULT,
    themes_dir: Path | None = None,
    use_cache: bool = True,
) -> dict[str, dict]:
    """算所有題材近 N 日熱度。

    回 dict[theme_key → {
        display_name, sids, n_total, n_valid, n_up,
        avg_return, win_rate, heat_score, multiplier, badge,
    }]

    沒成分股有效 close 的題材 → avg_return / win_rate / heat_score=0.0 →
    multiplier=COLD_MULTIPLIER(0.7,因為 win_rate=0 < 0.3)。caller 對
    這種 degenerate case 要心理準備(空題材檔表 / 全 SID 都沒 daily_prices)。

    Cache:同 (as_of, window_days) 重算 → 直接回 cache(避免單次推播流程內多次
    計算)。reset_cache() 可清。
    """
    as_of_iso = _resolve_as_of(conn, as_of)
    cache_key = (as_of_iso, int(window_days))
    if use_cache and cache_key in _HEAT_CACHE:
        return _HEAT_CACHE[cache_key]

    themes = _load_themes(themes_dir)
    out: dict[str, dict] = {}
    for theme_key, meta in themes.items():
        sids: list[str] = meta["sids"]
        rets: list[float] = []
        for sid in sids:
            r = _sid_window_return_pct(conn, sid, as_of_iso, window_days)
            if r is not None:
                rets.append(r)
        n_total = len(sids)
        n_valid = len(rets)
        if n_valid == 0:
            avg_return = 0.0
            win_rate = 0.0
            n_up = 0
        else:
            avg_return = sum(rets) / n_valid
            n_up = sum(1 for r in rets if r > 0)
            win_rate = n_up / n_valid
        heat_score = avg_return * RETURN_WEIGHT + win_rate * WIN_RATE_WEIGHT
        multiplier = _classify_multiplier(heat_score, win_rate)
        if multiplier > 1.0:
            badge = "🔥"
        elif multiplier < 1.0:
            badge = "🧊"
        else:
            badge = "➖"
        out[theme_key] = {
            "display_name": meta["display_name"],
            "sids": sids,
            "n_total": n_total,
            "n_valid": n_valid,
            "n_up": n_up,
            "avg_return": float(avg_return),
            "win_rate": float(win_rate),
            "heat_score": float(heat_score),
            "multiplier": float(multiplier),
            "badge": badge,
        }

    if use_cache:
        _HEAT_CACHE[cache_key] = out
    return out


def get_pick_theme_multiplier(
    conn: sqlite3.Connection,
    sid: str,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS_DEFAULT,
) -> float:
    """sid 屬於哪些題材 → 取最高 multiplier。

    不屬任何題材 → 1.0(中性,不影響排序)。

    取最高(而非平均)是主公拍板:避免熱題材股(e.g. 2330 同屬 CoWoS 熱 +
    tsmc_supply 中性)被冷題材稀釋掉應有的加分。
    """
    if not _is_enabled():
        return NEUTRAL_MULTIPLIER
    heat = compute_theme_heat(
        conn, as_of=as_of, window_days=window_days,
    )
    multipliers: list[float] = []
    for info in heat.values():
        if sid in info["sids"]:
            multipliers.append(info["multiplier"])
    if not multipliers:
        return NEUTRAL_MULTIPLIER
    return max(multipliers)


# === 推播 caption ===

def format_theme_heat_caption(heat: dict[str, dict]) -> str:
    """組推播訊息用的單塊 caption(熱題材 / 冷題材 名單)。

    格式:
        📡 題材熱度（近 N 日）
        🔥 熱題材: HBM / 矽光子 / CoWoS — 自動加分
        🧊 冷題材: 國防 / 重電 / 低軌衛星 — 自動降權

    沒任何熱 / 冷題材 → 回空 string,caller graceful skip 不顯該 section。
    name 顯示用 yaml 第一行 comment 的 display_name 第一段(空白 split[0])
    讓「HBM 高頻寬記憶體概念股」→「HBM」,推播省字數。
    """
    if not heat:
        return ""
    hot: list[str] = []
    cold: list[str] = []
    for info in heat.values():
        # 推播省字數:取 display_name 第一個 token(e.g. "HBM 高頻寬..." → "HBM")
        short = (info.get("display_name") or "").split()[0:1]
        label = short[0] if short else ""
        if not label:
            continue
        m = info.get("multiplier", 1.0)
        if m > 1.0:
            hot.append(label)
        elif m < 1.0:
            cold.append(label)
    if not hot and not cold:
        return ""
    lines: list[str] = ["📡 題材熱度(近 5 日)"]
    if hot:
        lines.append(f"🔥 熱題材: {' / '.join(hot)} — 自動加分")
    if cold:
        lines.append(f"🧊 冷題材: {' / '.join(cold)} — 自動降權")
    return "\n".join(lines)


__all__ = [
    "THEME_HEAT_ENABLED",
    "WINDOW_DAYS_DEFAULT",
    "HOT_MULTIPLIER",
    "COLD_MULTIPLIER",
    "NEUTRAL_MULTIPLIER",
    "compute_theme_heat",
    "get_pick_theme_multiplier",
    "format_theme_heat_caption",
    "reset_cache",
]
