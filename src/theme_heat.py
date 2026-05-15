"""題材熱度動態權重 — 根據近 N 日題材表現自動加減 score 或擋掉冷題材。

設計初衷(主公 2026-05-15 拍板,二次修正):
  熱題材(HBM / 矽光子 / CoWoS)在噴 → 自動加分
  冷題材(國防 / 重電 / 低軌衛星)在修正 → **直接擋掉,不推播**
  輪動到時不用手動改,系統每天自己重算。

公式:
  - 對 `data/themes/*.yaml` 每個題材撈成分股近 N 日:
      avg_return  = mean((latest_close - oldest_close) / oldest_close * 100)
      win_rate    = # sids with positive return / # sids with valid window
      heat_score  = avg_return × 0.6 + win_rate × 0.4
        (avg_return 為 percent 例 8.86;win_rate 為 fraction 例 0.7;
         result 約等於 avg_return 數量級,可跟 % 閥值比)
  - multiplier 規則(主公拍板,2026-05-15 二次修正:冷題材改 hard exclude):
      heat_score > 3.0  AND win_rate > 0.5  → 1.3 (🔥 熱,加分推薦)
      heat_score < -2.0 OR  win_rate < 0.3  → None (🚫 冷,擋掉不推)
      其他                                  → 1.0 (➖ 中性,照常)
  - sid 跨多題材規則:
      * 至少一個熱題材 → 取最熱 multiplier(熱不被冷稀釋)
      * 至少一個中性題材(沒熱)→ 取中性 1.0(中性壓過冷,不被擋)
      * 只在冷題材中 → None(擋掉)
      * 不在任何題材 → 1.0(照常,沒題材 ≠ 冷)

Kill-switch:env `THEME_HEAT_ENABLED=true`(預設 on)。off 時 notifier
應走 multiplier=1.0 path 且不擋任何 sid,等同行為被關掉。
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

# 樣本不足保守用基準(對齊 strategy_weighting.MIN_N=30 的精神):
# 一個題材若 daily_prices 有效成分股 < 此值 → 不分類,直接視為中性(1.0)。
# 避免「空 cache.db / 部分 sids 沒抓到」狀況下整題材被誤擋。
MIN_VALID_FOR_CLASSIFY = 2

HOT_MULTIPLIER: float = 1.3
NEUTRAL_MULTIPLIER: float = 1.0
# Cold 從 ×0.7 soft-降權改成 hard exclude(2026-05-15 主公二次修正)。
# 任何讀 multiplier 的 caller 看到 None 應視為「該 sid 被擋,不推播」。
COLD_EXCLUDE: None = None


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


def _classify_multiplier(heat_score: float, win_rate: float) -> Optional[float]:
    """主公拍板規則:熱 1.3 / 冷 None(擋) / 中性 1.0。

    熱:heat_score > 3% AND win_rate > 50%  → 1.3
    冷:heat_score < -2% OR  win_rate < 30%  → None(擋掉,caller 應 exclude)
    其他:1.0(中性)
    """
    if heat_score > HOT_HEAT_THR and win_rate > HOT_WIN_RATE_THR:
        return HOT_MULTIPLIER
    if heat_score < COLD_HEAT_THR or win_rate < COLD_WIN_RATE_THR:
        return COLD_EXCLUDE  # None
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

    multiplier 為 float | None — None 代表「冷題材,該題材內 sids 應被擋掉」。

    n_valid < MIN_VALID_FOR_CLASSIFY(預設 2)→ 退保守,multiplier=1.0
    (中性照常),避免「DB 資料缺口」被誤判成「冷」整題材被擋。對齊
    strategy_weighting.MIN_N=30 的精神。

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
        # n_valid 太少(< MIN_VALID_FOR_CLASSIFY)→ 退保守,當中性 1.0
        # (避免「資料缺口」被誤判成「冷」造成不必要 hard exclude)
        if n_valid < MIN_VALID_FOR_CLASSIFY:
            multiplier: Optional[float] = NEUTRAL_MULTIPLIER
            badge = "➖"
        else:
            multiplier = _classify_multiplier(heat_score, win_rate)
            if multiplier is None:
                badge = "🚫"   # 冷題材 hard exclude
            elif multiplier > 1.0:
                badge = "🔥"   # 熱題材 ×1.3
            else:
                badge = "➖"   # 中性 ×1.0
        out[theme_key] = {
            "display_name": meta["display_name"],
            "sids": sids,
            "n_total": n_total,
            "n_valid": n_valid,
            "n_up": n_up,
            "avg_return": float(avg_return),
            "win_rate": float(win_rate),
            "heat_score": float(heat_score),
            "multiplier": (float(multiplier) if multiplier is not None else None),
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
) -> Optional[float]:
    """sid 應套的題材權重 — 回 float 或 None(擋掉)。

    決策(主公 2026-05-15 二次拍板):
      * 至少一個熱題材 → 取最熱 multiplier(熱不被冷稀釋)
      * 至少一個中性題材(沒熱)→ 取中性 1.0(中性壓過冷,不被擋)
      * 只在冷題材中 → None(caller 應 exclude)
      * 不屬任何題材 → 1.0(沒題材 ≠ 冷,正常推薦)

    Kill-switch THEME_HEAT_ENABLED=false → 一律回 NEUTRAL_MULTIPLIER(1.0),
    不擋任何 sid。
    """
    if not _is_enabled():
        return NEUTRAL_MULTIPLIER
    heat = compute_theme_heat(
        conn, as_of=as_of, window_days=window_days,
    )
    # 收集 sid 在哪些題材內的 multiplier(可能 None)
    found = False
    valid_mults: list[float] = []
    for info in heat.values():
        if sid in info.get("sids", []):
            found = True
            m = info.get("multiplier")
            if m is not None:
                valid_mults.append(float(m))
    if not found:
        return NEUTRAL_MULTIPLIER  # 不在任何題材 → 照常
    if not valid_mults:
        return COLD_EXCLUDE  # 全部都在冷題材內 → 擋掉
    return max(valid_mults)  # 至少一個非冷 → 取最熱(中性 1.0 會壓過冷的 None)


# === 推播 caption ===

def format_theme_heat_caption(
    heat: dict[str, dict],
    excluded: dict[str, list[str]] | None = None,
) -> str:
    """組推播訊息用的單塊 caption(熱題材加分 / 冷題材已擋)。

    格式(2026-05-15 主公二次拍板,冷改 hard exclude):
        📡 題材熱度(近 N 日)
        🔥 熱題材加分: HBM / 矽光子 / CoWoS
        🚫 冷題材已擋: N 檔 (國防 X / 重電 Y / 低軌衛星 Z)

    excluded:dict[theme_display_name → list[sid]] — 由 _select_top_picks
    在套 hard exclude 時填,用來算 N 檔 + 各題材幾檔被擋。傳 None / 空 dict
    → caption 只列冷題材名稱不列數字(降級顯示)。

    沒任何熱 / 冷題材 → 回空 string,caller graceful skip 不顯該 section。
    name 顯示用 yaml 第一行 comment 的 display_name 第一段(空白 split[0])
    讓「HBM 高頻寬記憶體概念股」→「HBM」,推播省字數。
    """
    if not heat:
        return ""
    hot: list[str] = []
    cold: list[str] = []  # display_name 第一個 token,給「冷題材已擋」名單
    cold_full_names: list[str] = []  # display_name 完整,給 excluded count lookup
    for info in heat.values():
        full_name = (info.get("display_name") or "").strip()
        short = full_name.split()[0:1]
        label = short[0] if short else ""
        if not label:
            continue
        m = info.get("multiplier")
        if m is None:
            cold.append(label)
            cold_full_names.append(full_name)
        elif m > 1.0:
            hot.append(label)
    if not hot and not cold:
        return ""
    lines: list[str] = ["📡 題材熱度(近 5 日)"]
    if hot:
        lines.append(f"🔥 熱題材加分: {' / '.join(hot)}")
    if cold:
        ex_map = excluded or {}
        # excluded key 是 display_name(完整),反查 short label
        per_theme_parts: list[str] = []
        total_excluded: set[str] = set()
        for short_label, full_name in zip(cold, cold_full_names):
            sids_blocked = ex_map.get(full_name) or []
            total_excluded.update(sids_blocked)
            if sids_blocked:
                per_theme_parts.append(f"{short_label} {len(sids_blocked)}")
            else:
                per_theme_parts.append(short_label)
        if total_excluded:
            lines.append(
                f"🚫 冷題材已擋: {len(total_excluded)} 檔 "
                f"({' / '.join(per_theme_parts)})"
            )
        else:
            # 沒提供 excluded(legacy caller)→ 只列題材名,不顯數字
            lines.append(f"🚫 冷題材已擋: {' / '.join(cold)}")
    return "\n".join(lines)


__all__ = [
    "THEME_HEAT_ENABLED",
    "WINDOW_DAYS_DEFAULT",
    "HOT_MULTIPLIER",
    "NEUTRAL_MULTIPLIER",
    "COLD_EXCLUDE",
    "compute_theme_heat",
    "get_pick_theme_multiplier",
    "format_theme_heat_caption",
    "reset_cache",
]
