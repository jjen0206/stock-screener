"""盤前快訊(每交易日 08:30 TW 推播)。

主公昨晚 22:13 推的訊號 = 用昨日收盤算的;隔天早上開盤前 30 分鐘可能有:
  - 新警示股(MOPS 重大訊息 / TWSE 公告凌晨更新)
  - 新 news(news_fetcher 每小時跑,凌晨有新 news)
  - 大盤期貨變化(夜盤 / 美股影響開盤情緒)

**盤前快訊只推「變動 + 重要警示」**,不重複推昨晚整套(太吵)。
若三項全無變動 → 推極簡訊「✅ 盤前無重大變動」。

執行流程
---------
1. 重抓 warnings — 跑 fetch_stock_warnings.run() 拿最新警示
2. 重抓 news — fetch_and_store_news() 看是否有 picks sid 的新 news(< 12 小時)
3. 比對昨晚 22:13 推播的 picks(從 daily_picks 表撈 latest trade_date):
   - 若該 pick 的 sid 出現新警示 → ⚠️ 標出來
   - 若該 pick 有重要新 news(關鍵字「重大/違約/裁員/下修/召回」等)→ 📰 標出來
4. 重新跑 _select_top_picks(用最新 warnings + theme heat):
   - 對比昨晚推播,標出「新增 / 移除 / 排序變動」
5. 大盤期貨變化(yfinance 抓 ^DJI / ^IXIC / ^GSPC):
   - 三大美股 -1% 以上 → 🚨 大盤恐開低,軍師建議暫不追多
6. 推播 Telegram + Discord(HTML / Markdown)

CLI
----
    # 正式跑
    python scripts/morning_brief.py

    # 指定日期(yesterday picks 比對基準)
    python scripts/morning_brief.py --date 2026-05-17

    # dry-run(只 print)
    python scripts/morning_brief.py --dry-run

    # 跳過某通道
    python scripts/morning_brief.py --no-telegram
    python scripts/morning_brief.py --no-discord

Kill-switch
-----------
    MORNING_BRIEF_ENABLED=false → script exit 0 不推播(不影響其他 cron)

Exit code
---------
    0 = 跑完(無變動 / 推極簡 / 推完整都算成功)
    1 = 嚴重錯誤(secrets 缺等)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date as _date, datetime, timedelta, timezone
from html import escape as _h
from pathlib import Path
from typing import Iterable


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import config, database as db  # noqa: E402
from src.logging_setup import setup_file_logging  # noqa: E402


logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))

# 重大關鍵字 — news subject grep,命中即標 📰 提示主公注意
# 主公拍板:這幾個字眼出現在 picks 公司的 news 裡就是 pre-market 該注意的事件
IMPORTANT_NEWS_KEYWORDS: tuple[str, ...] = (
    "重大", "違約", "裁員", "下修", "召回", "停牌", "停止交易",
    "處置", "注意", "全額交割", "減資", "解散", "破產", "重整",
    "彈劾", "起訴", "搜索", "罷工", "火災", "爆炸", "停工",
    "減產", "下調", "示警", "警示", "終止", "和解金", "賠償",
)

# 美股期貨 / 指數收盤(夜盤 ~ 開盤前) — 主公看美股對台股早盤情緒影響
US_INDEX_TICKERS: dict[str, str] = {
    "^DJI": "道瓊",
    "^IXIC": "那斯達克",
    "^GSPC": "S&P 500",
}

# 大盤情緒判讀門檻(美股三大指數平均跌幅)
SENTIMENT_BEARISH_THRESHOLD = -0.01  # -1% 以上 → 軍師建議暫不追多
SENTIMENT_BULLISH_THRESHOLD = 0.005  # +0.5% 以上 → 中性偏多


# ============================================================================
# Kill-switch / 環境檢查
# ============================================================================

def _is_enabled() -> bool:
    """讀 MORNING_BRIEF_ENABLED env(預設 true)。主公出事 escape hatch。"""
    val = os.environ.get("MORNING_BRIEF_ENABLED", "true").strip().lower()
    return val not in ("false", "0", "no", "off", "")


# ============================================================================
# 1. 重抓 warnings(包 fetch_stock_warnings.run)
# ============================================================================

def refetch_warnings() -> dict:
    """跑一次 fetch_stock_warnings.run 拿最新警示;失敗 graceful 回 empty dict。"""
    try:
        from scripts.fetch_stock_warnings import run as fsw_run
        summary = fsw_run()
        logger.info(
            "[MORNING_BRIEF] warnings refetched: rows_parsed=%s, "
            "rows_written=%s, by_type=%s",
            summary.get("rows_parsed"),
            summary.get("rows_written"),
            summary.get("by_type"),
        )
        return summary
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[MORNING_BRIEF] refetch_warnings 失敗(non-blocking): %s: %s",
            type(ex).__name__, ex,
        )
        return {}


# ============================================================================
# 2. 重抓 news + 過濾「< 12 小時 + picks sid + 關鍵字」
# ============================================================================

def refetch_news() -> int:
    """跑一次 fetch_and_store_news 拿凌晨可能出現的新 news;失敗 graceful 回 0。"""
    try:
        from src.news_fetcher import fetch_and_store_news
        _rows, inserted, _skipped = fetch_and_store_news()
        logger.info(
            "[MORNING_BRIEF] news refetched: inserted=%d", inserted,
        )
        return inserted
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[MORNING_BRIEF] refetch_news 失敗(non-blocking): %s: %s",
            type(ex).__name__, ex,
        )
        return 0


def is_important_news_subject(subject: str | None) -> bool:
    """subject 包含 IMPORTANT_NEWS_KEYWORDS 任一字 → True。"""
    if not subject:
        return False
    s = str(subject)
    return any(kw in s for kw in IMPORTANT_NEWS_KEYWORDS)


def find_recent_picks_news(
    picks_sids: Iterable[str],
    hours: int = 12,
    db_path: str | Path | None = None,
) -> list[dict]:
    """撈昨晚 picks 的 sid 在最近 N 小時內、subject 含關鍵字的 news。

    Returns: list of dict with sid/company_name/publish_date/publish_time/subject/article_no
    """
    sids = [str(s) for s in picks_sids if s]
    if not sids:
        return []
    cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_iso = cutoff_utc.isoformat(timespec="seconds")

    placeholders = ",".join("?" * len(sids))
    sql = (
        f"SELECT sid, company_name, publish_date, publish_time, subject, "
        f"       article_no FROM news "
        f"WHERE sid IN ({placeholders}) "
        f"AND fetched_at >= ? "
        f"ORDER BY publish_date DESC, publish_time DESC LIMIT 50"
    )
    try:
        with db.get_conn(db_path) as conn:
            rows = conn.execute(sql, list(sids) + [cutoff_iso]).fetchall()
    except Exception as ex:  # noqa: BLE001
        logger.warning("[MORNING_BRIEF] find_recent_picks_news 失敗: %s", ex)
        return []

    items: list[dict] = []
    for r in rows:
        d = dict(r)
        if is_important_news_subject(d.get("subject")):
            items.append(d)
    return items


# ============================================================================
# 3. 撈「昨晚推播的 picks」基準 — 從 daily_picks 撈最新 trade_date
# ============================================================================

def get_last_pushed_picks(
    target_date: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """撈昨晚 22:13 推播時用的 picks list。

    走 _select_top_picks 同樣的 query path,確保比對基準一致。

    target_date None → 用 daily_prices MAX(date) 當基準(= 昨晚 picks 的 trade_date)。
    回 list of {sid, name, rank, ml_prob, matched_strategies, ...}。
    """
    if target_date is None:
        target_date = db.get_latest_trading_date(db_path=db_path)
        if not target_date:
            logger.info("[MORNING_BRIEF] daily_prices 無資料,昨晚 picks 視為空")
            return []

    # 走 _select_top_picks 同邏輯 — top_n=5 / confluence=2(daily_notify 預設)
    try:
        from src.notifier import compute_top_picks
        picks = compute_top_picks(
            date=target_date, top_n=5, confluence_n=2,
        )
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[MORNING_BRIEF] compute_top_picks(yesterday) 失敗: %s: %s",
            type(ex).__name__, ex,
        )
        return []
    return picks or []


# ============================================================================
# 4. 重跑 picks(用最新 warnings + theme heat)+ diff
# ============================================================================

def diff_picks(
    yesterday: list[dict], today: list[dict],
) -> dict:
    """比對昨晚 vs 今晨重跑的 picks,標出新增 / 移除 / 排序變動。

    Returns:
        {
          'added': [{sid, name, rank, ml_prob}, ...],     # 今晨多出來的
          'removed': [{sid, name}, ...],                  # 昨晚有今晨沒
          'reranked': [{sid, name, old_rank, new_rank}],  # 兩邊都有但排名變
        }
    """
    by_y = {str(p.get("sid", "")): p for p in yesterday if p.get("sid")}
    by_t = {str(p.get("sid", "")): p for p in today if p.get("sid")}

    added: list[dict] = []
    removed: list[dict] = []
    reranked: list[dict] = []

    for sid, p in by_t.items():
        if sid not in by_y:
            added.append({
                "sid": sid,
                "name": p.get("name", ""),
                "rank": p.get("rank", 0),
                "ml_prob": p.get("ml_prob"),
            })

    for sid, p in by_y.items():
        if sid not in by_t:
            removed.append({"sid": sid, "name": p.get("name", "")})

    for sid in by_t.keys() & by_y.keys():
        old_rank = by_y[sid].get("rank")
        new_rank = by_t[sid].get("rank")
        if old_rank and new_rank and old_rank != new_rank:
            reranked.append({
                "sid": sid,
                "name": by_t[sid].get("name", ""),
                "old_rank": old_rank,
                "new_rank": new_rank,
            })

    return {
        "added": sorted(added, key=lambda x: x["rank"] or 99),
        "removed": removed,
        "reranked": sorted(reranked, key=lambda x: x["new_rank"] or 99),
    }


# ============================================================================
# 5. 標出昨晚 picks 出現新警示
# ============================================================================

def find_newly_warned_picks(
    yesterday_picks: list[dict],
    as_of: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """掃昨晚 picks 的 sid 在 stock_warnings 表有沒有「生效中的警示」。

    Returns:
        [{sid, name, rank, warning_types: [...]}, ...]
        — 任一 active warning 命中就回該筆。
        無警示 / 表不存在 / 空 picks → 空 list。
    """
    if not yesterday_picks:
        return []
    if as_of is None:
        as_of = _date.today().isoformat()
    try:
        from src.warnings_filter import (
            ALL_WARNING_TYPES, WARNING_TYPE_LABELS, _query_active_warnings,
        )
        sids = [str(p.get("sid", "")) for p in yesterday_picks if p.get("sid")]
        with db.get_conn(db_path) as conn:
            warned = _query_active_warnings(
                conn, sids, list(ALL_WARNING_TYPES), as_of,
            )
    except Exception as ex:  # noqa: BLE001
        logger.warning("[MORNING_BRIEF] find_newly_warned_picks 失敗: %s", ex)
        return []

    out: list[dict] = []
    for p in yesterday_picks:
        sid = str(p.get("sid", ""))
        hits = warned.get(sid)
        if not hits:
            continue
        types = sorted({w["warning_type"] for w in hits})
        labels = [WARNING_TYPE_LABELS.get(t, t) for t in types]
        out.append({
            "sid": sid,
            "name": p.get("name", ""),
            "rank": p.get("rank"),
            "warning_types": types,
            "warning_labels": labels,
        })
    return out


# ============================================================================
# 6. 大盤情緒(美股三大指數收盤)
# ============================================================================

def fetch_us_market_sentiment() -> dict:
    """yfinance 抓 ^DJI / ^IXIC / ^GSPC 最近兩個交易日收盤 → pct_change。

    Returns:
        {
          'indices': {'^DJI': {'name': '道瓊', 'pct': -0.003}, ...},
          'avg_pct': float | None,        # 三大平均
          'sentiment': 'bearish'|'neutral'|'bullish',
          'caption': str,                 # 軍師判讀建議
        }
        抓不到任何指數 → {'indices': {}, 'avg_pct': None, 'sentiment': 'unknown',
                         'caption': '(美股資料不可用,跳過情緒判讀)'}
    """
    out_indices: dict[str, dict] = {}
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("[MORNING_BRIEF] yfinance 未安裝,跳過美股情緒")
        return _empty_sentiment()

    for ticker, name in US_INDEX_TICKERS.items():
        try:
            tk = yf.Ticker(ticker)
            hist = tk.history(period="5d")
            if hist is None or len(hist) < 2:
                continue
            closes = list(hist["Close"].tail(2))
            prev, last = float(closes[0]), float(closes[1])
            if prev <= 0:
                continue
            pct = (last - prev) / prev
            out_indices[ticker] = {
                "name": name,
                "pct": pct,
                "last": last,
            }
        except Exception as ex:  # noqa: BLE001
            logger.warning(
                "[MORNING_BRIEF] yfinance fetch %s 失敗(graceful skip): %s",
                ticker, ex,
            )
            continue

    if not out_indices:
        return _empty_sentiment()

    pcts = [v["pct"] for v in out_indices.values()]
    avg = sum(pcts) / len(pcts)
    if avg <= SENTIMENT_BEARISH_THRESHOLD:
        sentiment = "bearish"
        caption = "🚨 大盤恐開低,軍師建議暫不追多"
    elif avg >= SENTIMENT_BULLISH_THRESHOLD:
        sentiment = "bullish"
        caption = "✅ 美股偏多,台股可正常進場"
    else:
        sentiment = "neutral"
        caption = "🟡 中性,可正常進場但留意盤中量縮"

    return {
        "indices": out_indices,
        "avg_pct": avg,
        "sentiment": sentiment,
        "caption": caption,
    }


def _empty_sentiment() -> dict:
    return {
        "indices": {},
        "avg_pct": None,
        "sentiment": "unknown",
        "caption": "(美股資料不可用,跳過情緒判讀)",
    }


# ============================================================================
# 7. 訊息格式化(簡潔、mobile-first)
# ============================================================================

def _today_taipei_iso() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")


def has_any_change(
    newly_warned: list[dict],
    important_news: list[dict],
    pick_diff: dict,
    sentiment: dict,
) -> bool:
    """三項任一有變化 → True。大盤 sentiment 只有 bearish 才算「變動」
    (中性 / 多頭 / 不可用都不算 — 不該因為「美股小漲 0.5%」就把主公早上吵醒)。
    """
    if newly_warned:
        return True
    if important_news:
        return True
    if pick_diff.get("added") or pick_diff.get("removed") or pick_diff.get("reranked"):
        return True
    if sentiment.get("sentiment") == "bearish":
        return True
    return False


def format_brief_message(
    today_iso: str,
    newly_warned: list[dict],
    important_news: list[dict],
    pick_diff: dict,
    sentiment: dict,
    channel: str = "telegram",
) -> str:
    """組盤前快訊訊息。Telegram 走 HTML(parse_mode="HTML"),Discord 走 Markdown。

    無任何變動 → 回極簡訊。
    """
    if not has_any_change(newly_warned, important_news, pick_diff, sentiment):
        return _format_no_change(today_iso, channel=channel)

    if channel == "telegram":
        return _format_full_telegram(
            today_iso, newly_warned, important_news, pick_diff, sentiment,
        )
    return _format_full_discord(
        today_iso, newly_warned, important_news, pick_diff, sentiment,
    )


def _build_drawdown_alert_lines(channel: str) -> list[str]:
    """組整體 drawdown 警報行(從 user_positions 撈)。

    RISK_MGMT_ENABLED=false / 無持倉 / severity=ok → 回 []。
    severity=warn → 黃燈;danger → 紅燈。
    """
    try:
        from src import risk_management as _rm
    except Exception:  # noqa: BLE001
        return []
    if not _rm.is_enabled():
        return []
    try:
        positions = db.get_all_positions(include_closed=True)
    except Exception:  # noqa: BLE001
        return []
    if not positions:
        return []

    open_sids = [p["stock_id"] for p in positions if int(p.get("is_open", 1) or 0) == 1]
    px_map: dict[str, float] = {}
    if open_sids:
        placeholders = ",".join(["?"] * len(open_sids))
        try:
            with db.get_conn() as conn:
                rows = conn.execute(
                    "SELECT stock_id, close FROM daily_prices WHERE stock_id IN "
                    f"({placeholders}) AND date = ("
                    "  SELECT MAX(date) FROM daily_prices dp2 "
                    "  WHERE dp2.stock_id = daily_prices.stock_id"
                    ")",
                    open_sids,
                ).fetchall()
                for r in rows:
                    if r["close"] is not None:
                        px_map[r["stock_id"]] = float(r["close"])
        except Exception:  # noqa: BLE001
            pass

    enriched = [{**p, "current_price": px_map.get(p["stock_id"])} for p in positions]
    dd = _rm.drawdown_pct(enriched)
    if dd["severity"] == "ok":
        return []
    dd_pct = dd["drawdown_pct"]
    if channel == "telegram":
        if dd["severity"] == "danger":
            header = "🚨 <b>持倉警報</b>"
            body = (
                f"整體 drawdown {dd_pct:+.2f}% "
                f"(loss ≥ {_rm.DRAWDOWN_DANGER_PCT:.0f}%) — 軍師建議停手 + 全面檢視"
            )
        else:
            header = "⚠️ <b>持倉警報</b>"
            body = (
                f"整體 drawdown {dd_pct:+.2f}% "
                f"(loss ≥ {_rm.DRAWDOWN_WARN_PCT:.0f}%) — 暫停加碼,檢視持倉"
            )
        return [header, body, ""]
    if dd["severity"] == "danger":
        header = "🚨 **持倉警報**"
        body = (
            f"整體 drawdown {dd_pct:+.2f}% "
            f"(loss ≥ {_rm.DRAWDOWN_DANGER_PCT:.0f}%) — 軍師建議停手"
        )
    else:
        header = "⚠️ **持倉警報**"
        body = (
            f"整體 drawdown {dd_pct:+.2f}% "
            f"(loss ≥ {_rm.DRAWDOWN_WARN_PCT:.0f}%) — 暫停加碼"
        )
    return [header, body, ""]


def _build_price_alert_lines(channel: str) -> list[str]:
    """組「警報快訊」section — 顯示已觸發 / 即將觸發的 G 個股價格警報。

    PRICE_ALERT_ENABLED=false 或無觸發 → 回 []。同時撈 active price_alerts 對
    最新 daily close 算的觸發,以及持倉急殺(intraday_drop)。
    morning_brief 是「盤前快訊」,主要顯示昨日 close 觸發的 alert(隔日提醒)。
    """
    try:
        from src import price_alerts as _pa
    except Exception:  # noqa: BLE001
        return []
    if not _pa.is_enabled():
        return []
    try:
        with db.get_conn() as conn:
            triggered = _pa.check_price_alerts(conn)
            drops = _pa.check_intraday_drop(conn)
    except Exception:  # noqa: BLE001
        return []
    if not triggered and not drops:
        return []

    if channel == "telegram":
        header = "🚨 <b>警報快訊</b>"
    else:
        header = "🚨 **警報快訊**"
    lines: list[str] = [header]
    for t in triggered[:5]:
        sid = str(t.get("stock_id", ""))
        name = str(t.get("name") or "")
        atype = str(t.get("alert_type", ""))
        cur = t.get("current_price")
        tv = t.get("target_value")
        cur_s = f"${cur:.2f}" if cur is not None else "—"
        tv_s = f"${tv:.2f}" if tv is not None else "—"
        if channel == "telegram":
            lines.append(
                f"• {_h(sid)} {_h(name)} — {_h(atype)} 觸發(現價 {_h(cur_s)} / 門檻 {_h(tv_s)})"
            )
        else:
            lines.append(
                f"• {sid} {name} — {atype} 觸發(現價 {cur_s} / 門檻 {tv_s})"
            )
    for d in drops[:3]:
        sid = str(d.get("stock_id", ""))
        name = str(d.get("name") or "")
        change = d.get("change_pct")
        change_s = f"{change:+.2f}%" if change is not None else "—"
        if channel == "telegram":
            lines.append(
                f"• {_h(sid)} {_h(name)} — 持倉急殺 {_h(change_s)}"
            )
        else:
            lines.append(f"• {sid} {name} — 持倉急殺 {change_s}")
    lines.append("")
    return lines


def _format_no_change(today_iso: str, channel: str) -> str:
    """極簡訊:三項全無變動。"""
    if channel == "telegram":
        return (
            f"🌅 <b>盤前快訊 {_h(today_iso)}</b>\n\n"
            "✅ 盤前無重大變動,昨晚推薦繼續有效\n"
            "⏰ 開盤後 9:30 再進(避開非理性波動)"
        )
    return (
        f"🌅 **盤前快訊 {today_iso}**\n\n"
        "✅ 盤前無重大變動,昨晚推薦繼續有效\n"
        "⏰ 開盤後 9:30 再進(避開非理性波動)"
    )


def _format_full_telegram(
    today_iso: str,
    newly_warned: list[dict],
    important_news: list[dict],
    pick_diff: dict,
    sentiment: dict,
) -> str:
    lines: list[str] = [f"🌅 <b>盤前快訊 {_h(today_iso)}</b>", ""]

    # 持倉 drawdown 警報(2026-05-17 加)— RISK_MGMT_ENABLED + 有持倉 + severity != ok
    lines.extend(_build_drawdown_alert_lines("telegram"))

    # G 個股價格警報快訊(2026-05-17 加)— PRICE_ALERT_ENABLED + 有觸發
    lines.extend(_build_price_alert_lines("telegram"))

    # 警示更新
    if newly_warned:
        lines.append("⚠️ <b>警示更新 (vs 昨晚)</b>")
        for w in newly_warned:
            sid = _h(str(w.get("sid", "")))
            name = _h(str(w.get("name", "")))
            labels = "/".join(_h(x) for x in (w.get("warning_labels") or []))
            lines.append(
                f"• {sid} {name} — 新增「{labels}」警示,建議不進場"
            )
        lines.append("")

    # 重大 news
    if important_news:
        lines.append("📰 <b>重大 news (近 12h)</b>")
        for n in important_news[:5]:
            sid = _h(str(n.get("sid", "")))
            name = _h(str(n.get("company_name", "")))
            subj = str(n.get("subject", ""))
            subj_display = subj if len(subj) <= 60 else subj[:57] + "..."
            time_str = str(n.get("publish_time", "")).zfill(6) if n.get("publish_time") else ""
            time_label = (
                f"{time_str[:2]}:{time_str[2:4]}"
                if time_str and time_str.isdigit() and len(time_str) >= 4
                else ""
            )
            date_str = str(n.get("publish_date", ""))
            ts = f"({_h(date_str)} {_h(time_label)})".strip(" ()")
            ts_display = f" ({ts})" if ts else ""
            lines.append(
                f"• {sid} {name} — 「{_h(subj_display)}」{ts_display}"
            )
        lines.append("")

    # 推薦變動
    if pick_diff.get("added") or pick_diff.get("removed") or pick_diff.get("reranked"):
        lines.append("🔁 <b>推薦變動 (vs 昨晚 22:13)</b>")
        for a in pick_diff.get("added", [])[:5]:
            sid = _h(str(a.get("sid", "")))
            name = _h(str(a.get("name", "")))
            rank = a.get("rank") or "?"
            lines.append(f"• 新增 #{rank}: {sid} {name}")
        for r in pick_diff.get("removed", [])[:5]:
            sid = _h(str(r.get("sid", "")))
            name = _h(str(r.get("name", "")))
            lines.append(f"• 移除: {sid} {name}")
        for rr in pick_diff.get("reranked", [])[:5]:
            sid = _h(str(rr.get("sid", "")))
            name = _h(str(rr.get("name", "")))
            old_r = rr.get("old_rank")
            new_r = rr.get("new_rank")
            lines.append(
                f"• 排序 #{new_r}: {sid} {name}(原 #{old_r})"
            )
        lines.append("")

    # 大盤情緒
    lines.append("📊 <b>大盤情緒</b>")
    indices = sentiment.get("indices") or {}
    if indices:
        parts = []
        for ticker, info in indices.items():
            pct = info.get("pct") or 0.0
            pct_sign = "+" if pct >= 0 else ""
            parts.append(
                f"{_h(info.get('name', ticker))} {pct_sign}{pct*100:.2f}%"
            )
        lines.append("• 美股: " + ", ".join(parts))
    else:
        lines.append("• 美股: (資料不可用)")
    cap = sentiment.get("caption") or ""
    if cap:
        lines.append(f"• 軍師判讀: {_h(cap)}")
    lines.append("")

    lines.append("⏰ 開盤後 9:30 再進(避開非理性波動)")
    return "\n".join(lines)


def _format_full_discord(
    today_iso: str,
    newly_warned: list[dict],
    important_news: list[dict],
    pick_diff: dict,
    sentiment: dict,
) -> str:
    lines: list[str] = [f"🌅 **盤前快訊 {today_iso}**", ""]

    lines.extend(_build_drawdown_alert_lines("discord"))
    lines.extend(_build_price_alert_lines("discord"))

    if newly_warned:
        lines.append("⚠️ **警示更新 (vs 昨晚)**")
        for w in newly_warned:
            sid = str(w.get("sid", ""))
            name = str(w.get("name", ""))
            labels = "/".join(w.get("warning_labels") or [])
            lines.append(
                f"• {sid} {name} — 新增「{labels}」警示,建議不進場"
            )
        lines.append("")

    if important_news:
        lines.append("📰 **重大 news (近 12h)**")
        for n in important_news[:5]:
            sid = str(n.get("sid", ""))
            name = str(n.get("company_name", ""))
            subj = str(n.get("subject", ""))
            subj_display = subj if len(subj) <= 60 else subj[:57] + "..."
            time_str = str(n.get("publish_time", "")).zfill(6) if n.get("publish_time") else ""
            time_label = (
                f"{time_str[:2]}:{time_str[2:4]}"
                if time_str and time_str.isdigit() and len(time_str) >= 4
                else ""
            )
            date_str = str(n.get("publish_date", ""))
            ts = f"({date_str} {time_label})".strip(" ()")
            ts_display = f" ({ts})" if ts else ""
            lines.append(f"• {sid} {name} — 「{subj_display}」{ts_display}")
        lines.append("")

    if pick_diff.get("added") or pick_diff.get("removed") or pick_diff.get("reranked"):
        lines.append("🔁 **推薦變動 (vs 昨晚 22:13)**")
        for a in pick_diff.get("added", [])[:5]:
            sid = str(a.get("sid", ""))
            name = str(a.get("name", ""))
            rank = a.get("rank") or "?"
            lines.append(f"• 新增 #{rank}: {sid} {name}")
        for r in pick_diff.get("removed", [])[:5]:
            sid = str(r.get("sid", ""))
            name = str(r.get("name", ""))
            lines.append(f"• 移除: {sid} {name}")
        for rr in pick_diff.get("reranked", [])[:5]:
            sid = str(rr.get("sid", ""))
            name = str(rr.get("name", ""))
            old_r = rr.get("old_rank")
            new_r = rr.get("new_rank")
            lines.append(f"• 排序 #{new_r}: {sid} {name}(原 #{old_r})")
        lines.append("")

    lines.append("📊 **大盤情緒**")
    indices = sentiment.get("indices") or {}
    if indices:
        parts = []
        for ticker, info in indices.items():
            pct = info.get("pct") or 0.0
            pct_sign = "+" if pct >= 0 else ""
            parts.append(
                f"{info.get('name', ticker)} {pct_sign}{pct*100:.2f}%"
            )
        lines.append("• 美股: " + ", ".join(parts))
    else:
        lines.append("• 美股: (資料不可用)")
    cap = sentiment.get("caption") or ""
    if cap:
        lines.append(f"• 軍師判讀: {cap}")
    lines.append("")

    lines.append("⏰ 開盤後 9:30 再進(避開非理性波動)")
    return "\n".join(lines)


# ============================================================================
# 8. 主流程
# ============================================================================

def run_morning_brief(
    target_date: str | None = None,
    dry_run: bool = False,
    send_telegram: bool = True,
    send_discord: bool = True,
    skip_refetch: bool = False,
) -> dict:
    """主流程 — 重抓警示 + news → diff picks → 美股情緒 → 推播。

    Returns: {'pushed_telegram': bool, 'pushed_discord': bool,
              'has_change': bool, 'tg_msg': str, 'dc_msg': str}
    """
    # init DB(production cron 首次 run 需要;test 走 monkeypatch fixture)
    db.init_db()

    today_iso = _today_taipei_iso()

    # 1. refetch warnings(網路抓 TWSE / TPEx / MOPS RSS,~10-30s)
    if not skip_refetch:
        refetch_warnings()
    # 2. refetch news(TWSE OpenAPI t187ap04_L,~3-5s)
    if not skip_refetch:
        refetch_news()

    # 3. 撈昨晚 picks(用 daily_prices MAX(date) 當基準日)
    yesterday_picks = get_last_pushed_picks(target_date=target_date)
    yesterday_sids = [str(p.get("sid", "")) for p in yesterday_picks]
    logger.info(
        "[MORNING_BRIEF] yesterday_picks: %d 檔 sids=%s",
        len(yesterday_picks), yesterday_sids,
    )

    # 4. find newly warned picks(對昨晚 picks 跑 query)
    newly_warned = find_newly_warned_picks(yesterday_picks, as_of=today_iso)
    logger.info("[MORNING_BRIEF] newly_warned: %d 檔", len(newly_warned))

    # 5. find recent important news for picks(< 12h + 關鍵字)
    important_news = find_recent_picks_news(yesterday_sids, hours=12)
    logger.info("[MORNING_BRIEF] important_news: %d 則", len(important_news))

    # 6. 今晨重跑 picks(同 confluence=2 / top_n=5)→ diff
    today_picks = get_last_pushed_picks(target_date=target_date)
    pick_diff = diff_picks(yesterday_picks, today_picks)
    logger.info(
        "[MORNING_BRIEF] pick_diff: added=%d, removed=%d, reranked=%d",
        len(pick_diff.get("added", [])),
        len(pick_diff.get("removed", [])),
        len(pick_diff.get("reranked", [])),
    )

    # 7. 美股情緒
    sentiment = fetch_us_market_sentiment()
    logger.info(
        "[MORNING_BRIEF] sentiment: %s, avg_pct=%s",
        sentiment.get("sentiment"), sentiment.get("avg_pct"),
    )

    has_change = has_any_change(
        newly_warned, important_news, pick_diff, sentiment,
    )

    # 8. format messages
    tg_msg = format_brief_message(
        today_iso, newly_warned, important_news, pick_diff, sentiment,
        channel="telegram",
    )
    dc_msg = format_brief_message(
        today_iso, newly_warned, important_news, pick_diff, sentiment,
        channel="discord",
    )

    result: dict = {
        "has_change": has_change,
        "tg_msg": tg_msg,
        "dc_msg": dc_msg,
        "pushed_telegram": False,
        "pushed_discord": False,
    }

    # 9. 推播
    if dry_run:
        print("\n=== Telegram (HTML) ===\n", flush=True)
        print(tg_msg, flush=True)
        print("\n=== Discord ===\n", flush=True)
        print(dc_msg, flush=True)
        result["pushed_telegram"] = True
        result["pushed_discord"] = True
        return result

    if send_telegram and config.TELEGRAM_BOT_TOKEN:
        from src.notifier import send_telegram_message
        # HTML 模式 — 跟 news_notify 同走 HTML 避開 Markdown entity 解析坑
        result["pushed_telegram"] = send_telegram_message(
            tg_msg, parse_mode="HTML",
        )

    if send_discord and config.DISCORD_WEBHOOK_URL:
        from src.discord_notifier import send_discord_message
        result["pushed_discord"] = send_discord_message(dc_msg)

    return result


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="盤前快訊(每交易日 08:30 TW)— 警示 / news / 推薦變動 / 美股情緒",
    )
    p.add_argument(
        "--date", default=None,
        help="比對基準日 YYYY-MM-DD(預設 daily_prices MAX(date))",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="不真送,print 訊息到 stdout 看排版",
    )
    p.add_argument(
        "--no-telegram", action="store_true",
        help="跳過 Telegram",
    )
    p.add_argument(
        "--no-discord", action="store_true",
        help="跳過 Discord",
    )
    p.add_argument(
        "--skip-refetch", action="store_true",
        help="跳過 refetch warnings / news(test 用,production cron 不該開)",
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    setup_file_logging("morning_brief", mirror_print=True)

    if not _is_enabled():
        print(
            "[MORNING_BRIEF] MORNING_BRIEF_ENABLED=false,kill-switch on → 不推播",
            flush=True,
        )
        return 0

    try:
        result = run_morning_brief(
            target_date=args.date,
            dry_run=args.dry_run,
            send_telegram=not args.no_telegram,
            send_discord=not args.no_discord,
            skip_refetch=args.skip_refetch,
        )
    except Exception as ex:  # noqa: BLE001
        print(
            f"[MORNING_BRIEF] FATAL: {type(ex).__name__}: {ex}",
            file=sys.stderr, flush=True,
        )
        return 1

    print(
        f"[MORNING_BRIEF] has_change={result['has_change']} "
        f"telegram={'✅' if result['pushed_telegram'] else '❌'} "
        f"discord={'✅' if result['pushed_discord'] else '❌'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
