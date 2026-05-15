"""系統結論 brief — DB 統整 + 軍師主觀判斷。

設計初衷:主公拍板「不要再給我 dashboard 上 10 個獨立指標，要一份 *結論*。」
此 module 是「結論層」:
  1. 把 DB 散落的健康度 / 策略表現 / ML 校準 / 市場狀態統整成一份 dict
  2. 軍師(主觀規則引擎)根據門檻 wr / 樣本量 / 趨勢生 3-5 條建議
  3. 同步暴露 Telegram markdown formatter(週報 cron 直接用)

input 一律是已開好的 sqlite3.Connection(caller 負責 with get_conn(): ...),
讓 Streamlit page / GitHub Actions script / unit test 共用同一 helper。

軍師判斷規則(寫死,不接外部 config — 規則本身就是主觀):
  - WR > 60% & N >= 30 → 🔥 發燙（過去 30 天命中率明顯高於 random）
  - WR < 40% & N >= 30 → 🥶 該休息
  - N < 30          → 🌱 觀察中（樣本太小不下結論）
  - 法人共識檔數 7 天負趨勢      → 📉 散戶情緒分歧警示
  - 千張戶突破檔數 > 30          → 💰 籌碼集中爆量
  - ML calibration ratio < 0.5  → 🤖 ML 偏離校準（預測 > 0.6 但實際命中 < 50%）
  - 大盤 regime = bear           → 🔴 空頭，短線權重建議降
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src import config
from src.market_regime import compute_regime


# === Helper：日期 / staleness ===

def _today_iso() -> str:
    return date.today().isoformat()


def _days_between(later_iso: str, earlier_iso: str | None) -> int | None:
    """later - earlier 日曆天數。earlier None 回 None。解析失敗回 None。"""
    if not earlier_iso:
        return None
    try:
        d_later = date.fromisoformat(later_iso[:10])
        d_earlier = date.fromisoformat(earlier_iso[:10])
        return (d_later - d_earlier).days
    except (ValueError, IndexError):
        return None


# === Phase 1.1 — DB 健康度 ===

def _build_health(conn: sqlite3.Connection) -> dict[str, Any]:
    """讀 daily_prices / institutional / shareholder_concentration 最新日期 → stale 判斷。

    is_healthy False 的條件:
      - daily_prices 落後 > 3 天 (週末容忍)
      - institutional 落後 > 5 天
      - shareholder 落後 > 14 天
      - 任一表完全沒資料
    warnings: 人類可讀字串列。
    """
    today_iso = _today_iso()
    warnings: list[str] = []

    def _safe_max(table: str, col: str, extra_where: str = "") -> str | None:
        try:
            sql = f"SELECT MAX({col}) AS m FROM {table}"
            if extra_where:
                sql += f" WHERE {extra_where}"
            row = conn.execute(sql).fetchone()
            return row["m"] if row and row["m"] else None
        except sqlite3.OperationalError:
            return None

    dp_max = _safe_max("daily_prices", "date", "stock_id != 'TAIEX'")
    inst_max = _safe_max("institutional", "date")
    sh_max = _safe_max("shareholder_concentration", "week_end")

    dp_stale = _days_between(today_iso, dp_max)
    inst_stale = _days_between(today_iso, inst_max)
    sh_stale = _days_between(today_iso, sh_max)

    is_healthy = True
    if dp_max is None:
        warnings.append("daily_prices 無資料")
        is_healthy = False
    elif dp_stale is not None and dp_stale > 3:
        warnings.append(f"daily_prices 落後 {dp_stale} 天")
        is_healthy = False

    if inst_max is None:
        warnings.append("institutional 無資料")
        is_healthy = False
    elif inst_stale is not None and inst_stale > 5:
        warnings.append(f"institutional 落後 {inst_stale} 天")
        is_healthy = False

    if sh_max is None:
        warnings.append("shareholder_concentration 無資料")
    elif sh_stale is not None and sh_stale > 14:
        warnings.append(f"shareholder 落後 {sh_stale} 天")

    return {
        "daily_prices_max_date": dp_max,
        "daily_prices_stale_days": dp_stale,
        "institutional_max_date": inst_max,
        "institutional_stale_days": inst_stale,
        "shareholder_max_week": sh_max,
        "shareholder_stale_days": sh_stale,
        "is_healthy": is_healthy,
        "warnings": warnings,
    }


# === Phase 1.2 — 策略表現（過去 30 天 pick_outcomes） ===

# verdict 門檻 — 寫死，規則本身就是主觀
_VERDICT_HOT_WR = 0.60
_VERDICT_COLD_WR = 0.40
_VERDICT_MIN_N = 30


def _classify_verdict(n: int, wr: float | None) -> str:
    """根據樣本量 + WR 決定 verdict。"""
    if n < _VERDICT_MIN_N:
        return "🌱 觀察中"
    if wr is None:
        return "—"
    if wr >= _VERDICT_HOT_WR:
        return "🔥 發燙"
    if wr <= _VERDICT_COLD_WR:
        return "🥶 該休息"
    return "—"


def _build_strategy_performance(
    conn: sqlite3.Connection,
    days: int = 30,
) -> list[dict[str, Any]]:
    """過去 N 天 pick_outcomes by strategy → 命中率 / 平均 D5 / verdict。

    return_d5 已是百分比（看 sample data 5.024 而非 0.05），所以 avg_d5 直接保留。
    WR = AVG(return_d5 > 0)（不用 hit_target，因為 hit_target 是觸 +3% 目標，較嚴）。

    用 today 而非 max(pick_date) 算窗口起點—讓 cron 跑時的「過去 30 天」永遠包到
    最近實際結算的單，避免某天 daily_picks 沒跑就把窗口往前拉。
    """
    today_iso = _today_iso()
    start_iso = (date.fromisoformat(today_iso) - timedelta(days=days)).isoformat()
    try:
        rows = conn.execute(
            """
            SELECT strategy AS name,
                   COUNT(*) AS n,
                   AVG(CASE WHEN return_d5 > 0 THEN 1.0 ELSE 0.0 END) AS wr,
                   AVG(return_d5) AS avg_d5
            FROM pick_outcomes
            WHERE pick_date >= ?
              AND return_d5 IS NOT NULL
            GROUP BY strategy
            ORDER BY n DESC, strategy ASC
            """,
            (start_iso,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        n = int(r["n"] or 0)
        wr = float(r["wr"]) if r["wr"] is not None else None
        avg_d5 = float(r["avg_d5"]) if r["avg_d5"] is not None else None
        out.append({
            "name": r["name"],
            "n": n,
            "wr": wr,
            "avg_d5": avg_d5,
            "verdict": _classify_verdict(n, wr),
        })
    return out


# === Phase 1.3 — ML 校準 ===

def _build_ml_performance(
    conn: sqlite3.Connection,
    models_dir: Path | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """讀 models/short_pick.meta.json + 算最近 N 天 ml_prob > 0.6 的實際命中率。

    short_pick_roc_auc:meta.json 沒存 ROC AUC（schema 只有 accuracy/precision/
    recall/f1）→ 用 accuracy 代替，欄位名稱保留 short_pick_roc_auc 對齊主公規格。
    若主公後續校準腳本補 roc_auc 進 meta，自動切換用之。

    `calibration_health`(2026-05-15 加):從 short_pick.meta.json + per_strategy
    meta.json 內 calibration block 拿 raw/calibrated Brier,給 UI 畫 per-strategy
    校準健康度。每個 entry:{strategy, raw_brier, calibrated_brier, method,
    n_holdout, is_healthy(< 0.25 視為健康)}。
    """
    if models_dir is None:
        models_dir = config.PROJECT_ROOT / "models"
    meta_path = Path(models_dir) / "short_pick.meta.json"

    out: dict[str, Any] = {
        "short_pick_roc_auc": None,
        "calibration_7d": None,
        "calibration_sample_n": 0,
        "top_features": [],
        "model_trained_at": None,
        "calibration_health": [],
    }

    def _cal_entry(name: str, cal_block: dict) -> dict:
        cb = float(cal_block.get("calibrated_brier", float("nan")))
        rb = float(cal_block.get("raw_brier", float("nan")))
        return {
            "strategy": name,
            "method": cal_block.get("method"),
            "n_holdout": int(cal_block.get("n_holdout", 0)),
            "raw_brier": rb,
            "calibrated_brier": cb,
            "is_healthy": (cb == cb) and cb < 0.25,  # NaN 自動 False
        }

    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            metrics = meta.get("metrics", {}) or {}
            out["short_pick_roc_auc"] = (
                metrics.get("roc_auc")
                or metrics.get("accuracy")  # fallback：現行 meta 無 roc_auc
            )
            out["model_trained_at"] = meta.get("trained_at")
            features = meta.get("feature_names") or []
            out["top_features"] = list(features)[:5]
            cal_block = meta.get("calibration")
            if cal_block:
                out["calibration_health"].append(
                    _cal_entry("short_pick", cal_block)
                )
        except (OSError, json.JSONDecodeError):
            pass

    # per-strategy calibrators 健康度(meta.json 內 calibration 區塊)
    per_strategy_dir = Path(models_dir) / "per_strategy"
    if per_strategy_dir.exists():
        for meta_file in sorted(per_strategy_dir.glob("*.meta.json")):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                cal_block = meta.get("calibration")
                if not cal_block:
                    continue
                strategy_name = meta.get("strategy") or meta_file.stem.replace(
                    ".meta", "",
                )
                out["calibration_health"].append(
                    _cal_entry(strategy_name, cal_block)
                )
            except (OSError, json.JSONDecodeError):
                continue

    # calibration:最近 N 天 ml_prob > 0.6 的 picks 真實命中率
    today_iso = _today_iso()
    start_iso = (date.fromisoformat(today_iso) - timedelta(days=days)).isoformat()
    try:
        # JOIN daily_picks ON (trade_date, sid, strategy) 拿 ml_prob
        row = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   AVG(CASE WHEN po.return_d5 > 0 THEN 1.0 ELSE 0.0 END) AS wr
            FROM pick_outcomes po
            JOIN daily_picks dp
              ON dp.trade_date = po.pick_date
             AND dp.sid = po.sid
             AND dp.strategy = po.strategy
            WHERE po.pick_date >= ?
              AND dp.ml_prob > 0.6
              AND po.return_d5 IS NOT NULL
            """,
            (start_iso,),
        ).fetchone()
        if row and row["n"]:
            out["calibration_sample_n"] = int(row["n"])
            out["calibration_7d"] = float(row["wr"]) if row["wr"] is not None else None
    except sqlite3.OperationalError:
        pass

    return out


# === Phase 1.4 — 市場狀態 ===

def _build_market_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """大盤 regime + 法人共識 / 千張戶 / 三維精選 即時數量 + 趨勢。

    inst_consensus_count_today:institutional 最新日,三家同 net > 0 連 3 天 sid 數
    inst_consensus_trend_7d:今天 vs 7 個交易日前同條件的 sid 數量比 → 上升 / 下降 / 持平
    shareholder_movers_count:最新一週 holders_delta_w > 0 的 sid 數
    premium_picks_count:三維交集（法人連買 ∩ 千張戶 ∩ ML）的 sid 數
    """
    out: dict[str, Any] = {
        "regime": "unknown",
        "regime_label": "未知",
        "regime_emoji": "❔",
        "inst_consensus_count_today": 0,
        "inst_consensus_count_7d_ago": 0,
        "inst_consensus_trend_7d": "持平",
        "shareholder_movers_count": 0,
        "premium_picks_count": 0,
    }

    # 大盤 regime：compute_regime 自己開 conn,沒辦法傳入(legacy 接口)
    # 用 try/except 兜住缺資料情境
    try:
        regime_info = compute_regime()
        out["regime"] = regime_info["regime"]
        out["regime_label"] = regime_info["label"]
        out["regime_emoji"] = regime_info["badge_emoji"]
    except Exception:  # noqa: BLE001
        pass

    # 法人連 3 天共識（今天 + 7 個交易日前）
    def _consensus_count(as_of_date: str | None) -> int:
        """as_of_date None → 取 institutional 最新日。回三家連 3 天 net > 0 的 sid 數。"""
        try:
            if as_of_date is None:
                row = conn.execute(
                    "SELECT MAX(date) AS m FROM institutional"
                ).fetchone()
                as_of_date = row["m"] if row else None
            if not as_of_date:
                return 0
            row = conn.execute(
                """
                WITH ranked AS (
                    SELECT stock_id AS sid,
                           foreign_buy_sell AS fb,
                           trust_buy_sell AS tb,
                           dealer_buy_sell AS dn,
                           ROW_NUMBER() OVER (
                               PARTITION BY stock_id ORDER BY date DESC
                           ) AS rn
                    FROM institutional
                    WHERE date <= ?
                )
                SELECT COUNT(*) AS c
                FROM (
                    SELECT sid
                    FROM ranked
                    WHERE rn <= 3
                    GROUP BY sid
                    HAVING COUNT(*) = 3
                       AND MIN(fb) > 0
                       AND MIN(tb) > 0
                       AND MIN(dn) > 0
                )
                """,
                (as_of_date,),
            ).fetchone()
            return int(row["c"]) if row else 0
        except sqlite3.OperationalError:
            return 0

    out["inst_consensus_count_today"] = _consensus_count(None)
    # 7 個自然日前(approximation,夠用)
    try:
        row = conn.execute("SELECT MAX(date) AS m FROM institutional").fetchone()
        latest = row["m"] if row else None
        if latest:
            d7_ago = (date.fromisoformat(latest[:10]) - timedelta(days=7)).isoformat()
            out["inst_consensus_count_7d_ago"] = _consensus_count(d7_ago)
    except (sqlite3.OperationalError, ValueError):
        pass

    today_c = out["inst_consensus_count_today"]
    week_ago_c = out["inst_consensus_count_7d_ago"]
    if today_c > week_ago_c * 1.2:
        out["inst_consensus_trend_7d"] = "上升"
    elif today_c < week_ago_c * 0.8:
        out["inst_consensus_trend_7d"] = "下降"
    else:
        out["inst_consensus_trend_7d"] = "持平"

    # 千張戶 movers
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM shareholder_concentration
            WHERE week_end = (SELECT MAX(week_end) FROM shareholder_concentration)
              AND holders_delta_w IS NOT NULL
              AND holders_delta_w > 0
            """
        ).fetchone()
        out["shareholder_movers_count"] = int(row["c"]) if row else 0
    except sqlite3.OperationalError:
        pass

    # 三維精選 —— 走 database helper 算數量（容忍 helper 自己開 conn）
    try:
        from src import database as db
        premium = db.get_strong_follower_premium(top_n=50)
        out["premium_picks_count"] = len(premium)
    except Exception:  # noqa: BLE001
        pass

    return out


# === Phase 1.5 — 觀察清單 Top 5 ===

def _build_watchlist_today(
    conn: sqlite3.Connection,
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """組今日觀察清單 Top N。

    優先：三維交集（premium picks），不足 N 再補千張戶 movers Top。
    保證每 row 有 sid / name / reason 三鍵。
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
        from src import database as db
        premium = db.get_strong_follower_premium(top_n=top_n)
        for r in premium:
            sid = r.get("sid")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            items.append({
                "sid": sid,
                "name": r.get("name") or "—",
                "reason": (
                    r.get("reason_text")
                    or f"三維交集 · 連買 {r.get('consensus_days', '?')} 天"
                ),
            })
            if len(items) >= top_n:
                return items
    except Exception:  # noqa: BLE001
        pass

    if len(items) < top_n:
        try:
            from src import database as db
            movers = db.get_top_shareholder_movers(limit=top_n * 2)
            for r in movers:
                sid = r.get("sid")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                items.append({
                    "sid": sid,
                    "name": r.get("name") or "—",
                    "reason": (
                        f"千張戶週增 +{r.get('holders_delta_w', '?')} 人"
                    ),
                })
                if len(items) >= top_n:
                    break
        except Exception:  # noqa: BLE001
            pass

    return items


# === Phase 1.6 — 軍師主觀建議 ===

def _build_recommendations(
    health: dict,
    strategy_perf: list[dict],
    ml_perf: dict,
    market_state: dict,
) -> list[str]:
    """軍師主觀規則 → 3-5 條建議。空資料 fallback：至少 1 條中性提示。"""
    recs: list[str] = []

    # 1. 健康度警告（壓最前）
    if not health.get("is_healthy"):
        warns = health.get("warnings") or []
        if warns:
            recs.append(f"🚨 資料健康異常：{', '.join(warns[:2])}，本日結論可能失真")

    # 2. 發燙策略（最多 2 個）
    hot = [s for s in strategy_perf if s["verdict"] == "🔥 發燙"]
    hot_sorted = sorted(hot, key=lambda s: (s["wr"] or 0), reverse=True)[:2]
    for s in hot_sorted:
        wr_pct = int((s["wr"] or 0) * 100)
        recs.append(
            f"🔥 {s['name']} 近 30 天命中 {wr_pct}%（N={s['n']}），短線可多參考"
        )

    # 3. 冷凍策略（最多 2 個）
    cold = [s for s in strategy_perf if s["verdict"] == "🥶 該休息"]
    cold_sorted = sorted(cold, key=lambda s: (s["wr"] or 1.0))[:2]
    for s in cold_sorted:
        wr_pct = int((s["wr"] or 0) * 100)
        recs.append(
            f"🥶 {s['name']} 近 30 天命中 {wr_pct}%（N={s['n']}），短線少跟"
        )

    # 4. 法人共識趨勢
    today_c = market_state.get("inst_consensus_count_today", 0)
    week_c = market_state.get("inst_consensus_count_7d_ago", 0)
    if market_state.get("inst_consensus_trend_7d") == "下降" and week_c >= 3:
        recs.append(
            f"📉 法人共識最近 7 天從 {week_c} 檔降到 {today_c} 檔，"
            "警示散戶情緒分歧"
        )
    elif (
        market_state.get("inst_consensus_trend_7d") == "上升"
        and today_c >= 5
    ):
        recs.append(
            f"📈 法人共識最近 7 天從 {week_c} 檔升到 {today_c} 檔，"
            "資金回流訊號"
        )

    # 5. 千張戶爆量
    mv_count = market_state.get("shareholder_movers_count", 0)
    if mv_count > 30:
        recs.append(
            f"💰 本週千張戶突破 {mv_count} 檔，籌碼集中爆量，留意大戶選股"
        )

    # 6. ML 校準
    cal = ml_perf.get("calibration_7d")
    n_cal = ml_perf.get("calibration_sample_n", 0)
    if cal is not None and n_cal >= 10 and cal < 0.5:
        recs.append(
            f"🤖 ML 偏離校準：近 7 天高信心（>0.6）實際命中 {int(cal * 100)}%"
            f"（N={n_cal}），ML 訊號暫時打折"
        )

    # 7. 大盤 regime
    regime = market_state.get("regime")
    if regime == "bear":
        recs.append("🔴 大盤空頭，建議降低短線權重、加重防禦/籌碼策略")
    elif regime == "weak_bull":
        recs.append("⚠️ 大盤弱多頭，留意反轉風險，短線偏防守")

    # fallback：空資料情境保 1 條中性語句
    if not recs:
        recs.append("📊 資料準備中，尚無足夠樣本下結論（軍師待命）")

    return recs[:7]  # 最多 7 條（健康度警告 + 主要 5-6 條），UI 顯示用 st.info/warning


# === 對外 API ===

def build_system_brief(
    conn: sqlite3.Connection,
    models_dir: Path | None = None,
) -> dict[str, Any]:
    """主入口：把 DB 統整 + 軍師判斷打包成 dict。

    caller 負責開 conn（with db.get_conn() as conn: ...）— 讓 Streamlit page /
    weekly cron / unit test 共用同一個 helper。
    """
    health = _build_health(conn)
    strategy_perf = _build_strategy_performance(conn)
    ml_perf = _build_ml_performance(conn, models_dir=models_dir)
    market_state = _build_market_state(conn)
    watchlist = _build_watchlist_today(conn)
    recommendations = _build_recommendations(
        health, strategy_perf, ml_perf, market_state,
    )
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "health": health,
        "strategy_performance": strategy_perf,
        "ml_performance": ml_perf,
        "market_state": market_state,
        "watchlist_today": watchlist,
        "recommendations": recommendations,
    }


# === Telegram markdown formatter（Phase 3 cron 用） ===

def format_brief_for_telegram(brief: dict[str, Any]) -> str:
    """把 build_system_brief 結果 format 成單則 Telegram 訊息（< 4096 字）。

    使用 Telegram Markdown(legacy)語法,跟 src/notifier.py 對齊。
    """
    lines: list[str] = []
    gen = brief.get("generated_at", "—")
    # ISO week — 主公規格寫 2026-W19
    try:
        d = datetime.strptime(gen[:10], "%Y-%m-%d").date()
        year, week, _ = d.isocalendar()
        week_tag = f"{year}-W{week:02d}"
    except (ValueError, TypeError):
        week_tag = gen[:10]

    lines.append(f"📋 *系統結論週報* · {week_tag}")
    lines.append("")

    # === 健康度 ===
    health = brief.get("health") or {}
    health_emoji = "🟢" if health.get("is_healthy") else "🔴"
    lines.append(f"{health_emoji} *系統健康*")
    if health.get("is_healthy"):
        dp_lag = health.get("daily_prices_stale_days")
        inst_lag = health.get("institutional_stale_days")
        lines.append(
            f"資料新鮮 ✓ · daily +{dp_lag}d / inst +{inst_lag}d"
        )
    else:
        for w in (health.get("warnings") or [])[:3]:
            lines.append(f"• {w}")
    lines.append("")

    # === 發燙策略 ===
    perf = brief.get("strategy_performance") or []
    hot = [s for s in perf if s["verdict"] == "🔥 發燙"]
    hot = sorted(hot, key=lambda s: (s["wr"] or 0), reverse=True)[:3]
    if hot:
        lines.append("🔥 *本週發燙策略*")
        for i, s in enumerate(hot, 1):
            wr_pct = int((s["wr"] or 0) * 100)
            avg = s["avg_d5"]
            avg_str = f"{avg:+.2f}%" if avg is not None else "—"
            lines.append(
                f"{i}. {s['name']} · WR {wr_pct}% / D5 {avg_str} · N={s['n']}"
            )
        lines.append("")

    # === 該休息 ===
    cold = [s for s in perf if s["verdict"] == "🥶 該休息"]
    cold = sorted(cold, key=lambda s: (s["wr"] or 1.0))[:3]
    if cold:
        lines.append("🥶 *該休息*")
        for i, s in enumerate(cold, 1):
            wr_pct = int((s["wr"] or 0) * 100)
            lines.append(f"{i}. {s['name']} · WR {wr_pct}% · N={s['n']}")
        lines.append("")

    # === 市場狀態 ===
    ms = brief.get("market_state") or {}
    lines.append("🌡️ *市場狀態*")
    lines.append(
        f"regime: {ms.get('regime_emoji', '❔')} {ms.get('regime_label', '未知')}"
    )
    today_c = ms.get("inst_consensus_count_today", 0)
    week_c = ms.get("inst_consensus_count_7d_ago", 0)
    trend = ms.get("inst_consensus_trend_7d", "持平")
    lines.append(
        f"法人共識 7 天趨勢：{trend} {week_c}→{today_c}"
    )
    lines.append(f"千張戶進場：{ms.get('shareholder_movers_count', 0)} 檔")
    pp = ms.get("premium_picks_count", 0)
    if pp > 0:
        lines.append(f"三維精選：{pp} 檔")
    lines.append("")

    # === 觀察清單 ===
    wl = brief.get("watchlist_today") or []
    if wl:
        lines.append("🎯 *觀察清單*")
        for i, item in enumerate(wl[:5], 1):
            lines.append(
                f"{i}. [{item.get('sid', '?')}] {item.get('name', '—')} — "
                f"{item.get('reason', '')}"
            )
        lines.append("")

    # === 軍師建議 ===
    recs = brief.get("recommendations") or []
    if recs:
        lines.append("🎖️ *軍師建議*")
        for r in recs[:6]:
            lines.append(f"• {r}")
        lines.append("")

    text = "\n".join(lines).rstrip()
    # Telegram 4096 字上限保險
    if len(text) > 4000:
        text = text[:3990] + "\n…(截斷)"
    return text


__all__ = [
    "build_system_brief",
    "format_brief_for_telegram",
]
