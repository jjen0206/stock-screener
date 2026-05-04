"""個股頁的「精簡可重用 section」— 4 個 render helper(主力燈號 / 技術
總覽 / 關鍵價位 / 操作建議)+ 所需 compute helper。

抽出這個 module 是為了讓推薦頁 (短線 / 長線) 的卡片也能在 expander 內
reuse 同樣的 4 個 section 而不必跨 streamlit page 跳轉到個股頁。

不在這個 module 的個股專屬區塊(留在 app.py):
- 三大法人籌碼明細表 / 主力進出累計表
- 多週期趨勢分析
- W 底 / M 頭型態識別

它們不是 reuse 重點(資料量大、跟卡片定位不符),所以 app.py 自己保留。
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

from src import database as db, indicators as ind


def _load_recent_ohlc(sid: str, limit: int) -> pd.DataFrame:
    """SQL 撈該股最近 N 天 daily_prices 並升序回 DataFrame(空 → 空 DataFrame)。
    給 _compute_key_levels / _compute_technical_summary 共用,避免重複 query。
    """
    db.init_db()
    with db.get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT date, open, high, low, close, volume "
                "FROM daily_prices "
                "WHERE stock_id=? ORDER BY date DESC LIMIT ?",
                (sid, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([
        {
            "date": r["date"], "open": r["open"], "high": r["high"],
            "low": r["low"], "close": r["close"], "volume": r["volume"],
        }
        for r in rows
    ])
    return df.sort_values("date").reset_index(drop=True)


def _compute_key_levels(sid: str) -> dict:
    """算壓力 / 回檔 / 支撐三區間。給 _render_key_levels + _render_action_suggestion 共用。

    回 dict:
      - {'error': str}  歷史不足 / NaN
      - {'close', 'atr14', 'bb_upper', 'bb_mid', 'bb_lower', 'levels': [(label, hint, low, high), ...]}
    """
    df = _load_recent_ohlc(sid, limit=90)
    if len(df) < 20:
        return {"error": f"歷史不足,需 ≥20 天計算(目前 {len(df)} 天)"}

    bb = ind.bollinger(df, period=20, num_std=2.0)
    atr_series = ind.atr(df, period=14)

    bb_upper = bb["upper"].iloc[-1] if not bb.empty else None
    bb_mid = bb["mid"].iloc[-1] if not bb.empty else None
    bb_lower = bb["lower"].iloc[-1] if not bb.empty else None
    atr14 = atr_series.iloc[-1] if not atr_series.empty else None
    close_last = df["close"].iloc[-1]

    if any(
        v is None or pd.isna(v)
        for v in [bb_upper, bb_mid, bb_lower, atr14]
    ):
        return {"error": "技術指標 NaN(歷史可能不足或資料異常)"}

    half_atr = 0.5 * atr14
    return {
        "close": float(close_last),
        "atr14": float(atr14),
        "bb_upper": float(bb_upper),
        "bb_mid": float(bb_mid),
        "bb_lower": float(bb_lower),
        "levels": [
            ("🔴 壓力區", "上漲遇阻", bb_upper - half_atr, bb_upper + half_atr),
            ("🟡 回檔區", "中性整理", bb_mid - half_atr, bb_mid + half_atr),
            ("🟢 支撐區", "下跌支撐", bb_lower - half_atr, bb_lower + half_atr),
        ],
    }


def _compute_technical_summary(sid: str) -> dict:
    """算 7 項 rule-based 技術解讀。給 _render_technical_summary + _render_action_suggestion 共用。

    回 dict:
      - {'error': str}  歷史不足 / NaN
      - {'trend', 'price_pos', 'ma_align', 'vol_text', 'bb_pos', 'bb_width_state', 'summary'}
    """
    df = _load_recent_ohlc(sid, limit=120)
    if len(df) < 60:
        return {"error": f"歷史不足,需 ≥60 天計算 MA60(目前 {len(df)} 天)"}

    ma5 = ind.sma(df, 5)
    ma20 = ind.sma(df, 20)
    ma60 = ind.sma(df, 60)
    bb = ind.bollinger(df, period=20, num_std=2.0)

    close_last = df["close"].iloc[-1]
    close_prev = df["close"].iloc[-2]
    vol_last = df["volume"].iloc[-1]
    vol_ma20 = df["volume"].rolling(20).mean().iloc[-1]

    ma5_last = ma5.iloc[-1]
    ma20_last = ma20.iloc[-1]
    ma60_last = ma60.iloc[-1]
    bb_upper = bb["upper"].iloc[-1]
    bb_mid = bb["mid"].iloc[-1]
    bb_lower = bb["lower"].iloc[-1]
    # σ 從 BB 反推:upper = mid + 2σ → σ = (upper - mid) / 2
    sigma = (bb_upper - bb_mid) / 2.0

    if any(pd.isna(v) for v in [
        close_last, ma5_last, ma20_last, ma60_last,
        bb_upper, bb_mid, bb_lower, sigma, vol_last, vol_ma20,
    ]):
        return {"error": "技術指標 NaN(歷史可能不足或資料異常)"}

    # 1. 趨勢
    if close_last > ma20_last > ma60_last:
        trend = "多頭趨勢"
    elif close_last < ma20_last < ma60_last:
        trend = "空頭趨勢"
    else:
        trend = "盤整"

    # 2. 價格位置(離 BB 中軌幾個 σ)
    diff_to_mid = close_last - bb_mid
    half_sigma = 0.5 * sigma
    if abs(diff_to_mid) < 0.3 * sigma:
        price_pos = "中軌附近"
    elif diff_to_mid > half_sigma:
        price_pos = "貼近上軌"
    elif diff_to_mid < -half_sigma:
        price_pos = "貼近下軌"
    else:
        price_pos = "整理"

    # 3. 均線排列
    if ma5_last > ma20_last > ma60_last:
        ma_align = "多頭排列(5>20>60)"
    elif ma5_last < ma20_last < ma60_last:
        ma_align = "空頭排列(5<20<60)"
    else:
        ma_align = "糾結整理"

    # 4. 量能
    is_up = close_last > close_prev
    vol_ratio = vol_last / vol_ma20 if vol_ma20 > 0 else 1.0
    if vol_ratio < 0.8 and is_up:
        vol_text = "量縮上漲(動能降溫)"
    elif vol_ratio > 1.2 and is_up:
        vol_text = "量增上漲(動能強勢)"
    elif vol_ratio < 0.8 and not is_up:
        vol_text = "量縮下跌(賣壓減弱)"
    elif vol_ratio > 1.2 and not is_up:
        vol_text = "量增下跌(賣壓沉重)"
    else:
        vol_text = "量平"

    # 5. 布林位置
    if close_last > bb_upper:
        bb_pos = "突破上軌(強勢但超買)"
    elif close_last < bb_lower:
        bb_pos = "跌破下軌(超賣)"
    elif diff_to_mid > half_sigma:
        bb_pos = "貼近上軌(偏強)"
    elif diff_to_mid < -half_sigma:
        bb_pos = "貼近下軌(偏弱)"
    else:
        bb_pos = "通道內整理"

    # 6. 布林通道寬度
    bb_width_now = bb_upper - bb_lower
    bb_width_avg20 = (bb["upper"] - bb["lower"]).rolling(20).mean().iloc[-1]
    if pd.isna(bb_width_avg20) or bb_width_avg20 == 0:
        bb_width_state = "通道穩定"
    elif bb_width_now > bb_width_avg20 * 1.2:
        bb_width_state = "開口擴大(趨勢加大)"
    elif bb_width_now < bb_width_avg20 * 0.8:
        bb_width_state = "開口收斂(整理)"
    else:
        bb_width_state = "通道穩定"

    # 7. 綜合評估
    if trend == "多頭趨勢" and "突破上軌" in bb_pos:
        summary = "高檔強勢"
    elif trend == "多頭趨勢" and "貼近上軌" in bb_pos:
        summary = "高檔區震盪出貨初期"
    elif trend == "多頭趨勢" and price_pos == "中軌附近":
        summary = "回檔測試"
    elif trend == "多頭趨勢":
        summary = "多頭整理"
    elif trend == "空頭趨勢" and "跌破下軌" in bb_pos:
        summary = "弱勢探底"
    elif trend == "空頭趨勢" and "貼近下軌" in bb_pos:
        summary = "低檔築底機會"
    elif trend == "空頭趨勢":
        summary = "空頭整理"
    else:
        summary = "盤整待方向"

    return {
        "trend": trend,
        "price_pos": price_pos,
        "ma_align": ma_align,
        "vol_text": vol_text,
        "bb_pos": bb_pos,
        "bb_width_state": bb_width_state,
        "summary": summary,
    }


def _render_technical_summary(sid: str) -> None:
    """個股頁:7 項 rule-based 技術分析總覽 — 渲染層。"""
    result = _compute_technical_summary(sid)
    if "error" in result:
        st.info(f"📊 技術分析總覽:{result['error']}")
        return

    st.markdown("### 📊 技術分析總覽")
    st.markdown(
        f"- **趨勢分析**:{result['trend']}\n"
        f"- **價格位置**:{result['price_pos']}\n"
        f"- **均線排列**:{result['ma_align']}\n"
        f"- **量能分析**:{result['vol_text']}\n"
        f"- **布林位置**:{result['bb_pos']}\n"
        f"- **布林通道**:{result['bb_width_state']}\n"
        f"- **綜合評估**:{result['summary']}"
    )


def _render_key_levels(sid: str) -> None:
    """個股頁:壓力 / 回檔 / 支撐 三個關鍵價位區間 — 渲染層。"""
    result = _compute_key_levels(sid)
    if "error" in result:
        st.info(f"🎯 關鍵價位:{result['error']}")
        return

    st.markdown("### 🎯 關鍵價位")
    cols = st.columns(3)
    for i, (label, hint, low, high) in enumerate(result["levels"]):
        with cols[i]:
            st.markdown(
                f"**{label}**  \n"
                f"<span style='color:#888;font-size:0.85rem'>{hint}</span>  \n"
                f"`{low:.2f}` ~ `{high:.2f}`",
                unsafe_allow_html=True,
            )
    st.caption(
        "計算:布林通道 (20, 2) + ATR(14) × 0.5 半幅。**參考用,非預測**"
    )


# 操作核心模板:綜合評估 → 文字建議。覆蓋 7 個 _compute_technical_summary
# 可能回的 summary 值,以及兜底「盤整待方向」。
_ACTION_CORE_BY_SUMMARY = {
    "高檔強勢": "順勢續抱,跌破 MA20 或量縮警戒,不追高",
    "高檔區震盪出貨初期": "短中線觀望或減碼,等回檔區再進",
    "回檔測試": "等支撐企穩(量縮止跌)再小量試多",
    "多頭整理": "區間操作,支撐進、壓力出;突破需量增確認",
    "弱勢探底": "停損紀律執行,不接刀,等量增企穩",
    "低檔築底機會": "等量增反彈確認再小量試多,設嚴停損",
    "空頭整理": "空方主導,反彈視為逢高出貨,不追多",
    "盤整待方向": "區間內操作,等突破方向再加碼",
}


def _render_action_suggestion(sid: str) -> None:
    """個股頁:短 / 中 / 長線進場 / 目標 / 停損建議 + 操作核心。

    用 _compute_key_levels 拿 BB / ATR 區間,_compute_technical_summary
    拿綜合評估字串。兩者都 OK 才渲染,任一 error → fallback。

    線型定義:
      - 短線:支撐區進,壓力下緣出,停損 = 支撐 - 1.5 ATR(2 ATR 風險)
      - 中線:回檔區進,壓力上緣出,停損 = 支撐上緣
      - 長線:支撐區進,目標「順勢看多」(不給數字),停損「跌破支撐」
    """
    levels = _compute_key_levels(sid)
    summary = _compute_technical_summary(sid)

    if "error" in levels or "error" in summary:
        # 兩者任一壞就走 fallback(通常 levels 比較容易過 — 只需 20 天)
        msg = levels.get("error") or summary.get("error")
        st.info(f"💡 操作建議:{msg}")
        return

    atr = levels["atr14"]
    bb_upper = levels["bb_upper"]
    bb_mid = levels["bb_mid"]
    bb_lower = levels["bb_lower"]
    half_atr = 0.5 * atr

    # 短線(支撐區買、壓力下緣賣)
    short_entry_low = bb_lower - half_atr
    short_entry_high = bb_lower + half_atr
    short_target = bb_upper - half_atr  # 壓力區下緣
    short_stop = bb_lower - 1.5 * atr  # 支撐 - 1.5 ATR(從支撐區下緣再 1 ATR)
    short_entry_mid = bb_lower
    short_r = short_entry_mid - short_stop  # 風險
    short_t = short_target - short_entry_mid  # 報酬
    short_rr = short_t / short_r if short_r > 0 else 0.0

    # 中線(回檔區買、壓力上緣賣)
    mid_entry_low = bb_mid - half_atr
    mid_entry_high = bb_mid + half_atr
    mid_target = bb_upper + half_atr  # 壓力區上緣
    mid_stop = bb_lower + half_atr  # 支撐區上緣
    mid_entry_mid = bb_mid
    mid_r = mid_entry_mid - mid_stop
    mid_t = mid_target - mid_entry_mid
    mid_rr = mid_t / mid_r if mid_r > 0 else 0.0

    # 長線(支撐區買、目標看順勢、停損跌破支撐)
    long_entry_low = bb_lower - half_atr
    long_entry_high = bb_lower + half_atr
    long_stop_text = f"跌破 {bb_lower - half_atr:.2f}(支撐區下緣)"

    # 操作核心:從綜合評估查表
    summary_str = summary["summary"]
    trend_str = summary["trend"]
    action_core = _ACTION_CORE_BY_SUMMARY.get(
        summary_str, "區間內操作,等趨勢明朗再加碼",
    )

    st.markdown("### 💡 操作建議")
    st.markdown(
        f"**短線**\n"
        f"- 進場區間:`{short_entry_low:.2f}` ~ `{short_entry_high:.2f}`(支撐區)\n"
        f"- 目標:`{short_target:.2f}`(壓力區下緣)\n"
        f"- 停損:`{short_stop:.2f}`(支撐 − 1.5 ATR)\n"
        f"- 風險報酬:**{short_rr:.1f} : 1**\n\n"
        f"**中線**\n"
        f"- 進場區間:`{mid_entry_low:.2f}` ~ `{mid_entry_high:.2f}`(回檔區)\n"
        f"- 目標:`{mid_target:.2f}`(壓力區上緣)\n"
        f"- 停損:`{mid_stop:.2f}`(支撐區上緣)\n"
        f"- 風險報酬:**{mid_rr:.1f} : 1**\n\n"
        f"**長線**\n"
        f"- 進場區間:`{long_entry_low:.2f}` ~ `{long_entry_high:.2f}`(支撐區)\n"
        f"- 目標:更高(順勢看多,突破壓力後加碼)\n"
        f"- 停損:{long_stop_text}\n"
    )
    st.warning(
        f"⚠️ **操作核心**:{trend_str} + {summary_str} → {action_core}"
    )
    st.caption(
        "計算:支撐 / 壓力 / 回檔 = 布林通道 ± 0.5 ATR;"
        "停損用 ATR 倍數。**參考用,非投資建議**,個人交易仍應自行評估。"
    )


def _compute_main_force_signal(sid: str) -> dict:
    """主力燈號:法人 5/10/20 日累計 + BB 位置 + 量價配對 → 出貨 / 吸貨判斷。

    回 dict 含 status / emoji / strength(0-5)/ n5,n10,n20 / vol_text /
    bb_pos_ratio / reading,或 {'error': str}。

    五種狀態:
    - 出貨初期:5/10/20 全負 + 高檔 + 量增下跌
    - 默默出貨:全負 + 量平 / 量縮
    - 吸貨初期:全正 + 低檔 + 量增上漲
    - 默默吸貨:全正 + 低檔 + 量平 / 量縮上漲
    - 中性:其他混合
    """
    db.init_db()
    with db.get_conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT p.date, p.close,
                       i.foreign_buy_sell AS f, i.trust_buy_sell AS t,
                       i.dealer_buy_sell AS d
                FROM daily_prices p
                LEFT JOIN institutional i
                  ON p.stock_id = i.stock_id AND p.date = i.date
                WHERE p.stock_id = ?
                ORDER BY p.date DESC
                LIMIT 30
                """,
                (sid,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []

    if len(rows) < 20:
        return {"error": f"歷史不足,需 ≥20 天計算 20 日累計(目前 {len(rows)} 天)"}

    if all(r["f"] is None and r["t"] is None and r["d"] is None for r in rows):
        return {"error": "無法人籌碼資料,主力燈號不可用"}

    df = pd.DataFrame([
        {
            "date": r["date"], "close": r["close"],
            # 股 → 張(整除丟小數,跟其他法人區塊一致)
            "inst_total": (
                (r["f"] or 0) + (r["t"] or 0) + (r["d"] or 0)
            ) // 1000,
        }
        for r in rows
    ]).sort_values("date").reset_index(drop=True)

    n5 = int(df["inst_total"].tail(5).sum())
    n10 = int(df["inst_total"].tail(10).sum())
    n20 = int(df["inst_total"].tail(20).sum())

    # reuse _compute_key_levels 拿 BB / ATR / close
    levels = _compute_key_levels(sid)
    if "error" in levels:
        return {"error": f"關鍵價位算不出:{levels['error']}"}
    bb_upper = levels["bb_upper"]
    bb_lower = levels["bb_lower"]
    close_last = levels["close"]

    # close 在 BB 上下軌之間的位置(0=下軌, 1=上軌)
    bb_range = bb_upper - bb_lower
    if bb_range > 0:
        bb_pos_ratio = (close_last - bb_lower) / bb_range
    else:
        bb_pos_ratio = 0.5
    bb_pos_ratio = max(0.0, min(1.0, bb_pos_ratio))
    is_high = bb_pos_ratio > 0.6
    is_low = bb_pos_ratio < 0.4

    # reuse _compute_technical_summary 拿 vol_text(MA60 不夠時走 fallback)
    summary = _compute_technical_summary(sid)
    vol_text = summary.get("vol_text", "量平") if "error" not in summary else "量平"

    # 燈號規則
    all_neg = n5 < 0 and n10 < 0 and n20 < 0
    all_pos = n5 > 0 and n10 > 0 and n20 > 0

    if all_neg and is_high and "量增下跌" in vol_text:
        status, emoji = "出貨初期", "🔴"
    elif all_neg and (vol_text == "量平" or "量縮" in vol_text):
        status, emoji = "默默出貨", "🟠"
    elif all_pos and is_low and "量增上漲" in vol_text:
        status, emoji = "吸貨初期", "🟢"
    elif all_pos and is_low and (
        vol_text == "量平" or "量縮上漲" in vol_text
    ):
        status, emoji = "默默吸貨", "🟡"
    else:
        status, emoji = "中性", "⚪"

    # 強度(0-5):累計絕對值 + 量價一致 + BB 極端位置
    strength = 0
    abs_n20 = abs(n20)
    if abs_n20 > 100_000:
        strength += 3
    elif abs_n20 > 30_000:
        strength += 2
    elif abs_n20 > 10_000:
        strength += 1

    # 量價配對與訊號方向一致 → +1
    if status == "出貨初期" and "量增下跌" in vol_text:
        strength += 1
    elif status == "吸貨初期" and "量增上漲" in vol_text:
        strength += 1
    elif status in ("默默出貨", "默默吸貨") and (
        "量縮" in vol_text or vol_text == "量平"
    ):
        strength += 1

    # BB 位置極端
    if bb_pos_ratio > 0.7 or bb_pos_ratio < 0.3:
        strength += 1

    strength = min(5, max(0, strength))

    # 解讀文字模板
    if status == "出貨初期":
        reading = (
            f"20 日法人累計 {n20:+,} 張 + {vol_text} → 主力減碼,"
            "短線留意風險"
        )
    elif status == "默默出貨":
        reading = (
            f"法人持續流出({n20:+,} 張)+ {vol_text} → 主力低調出貨"
        )
    elif status == "吸貨初期":
        reading = (
            f"20 日法人累計 {n20:+,} 張 + {vol_text} → 主力進場,"
            "可留意進場機會"
        )
    elif status == "默默吸貨":
        reading = (
            f"法人持續流入({n20:+,} 張)+ {vol_text} + 低檔位置 → 主力默默吸貨"
        )
    else:
        reading = (
            f"5/10/20 日累計({n5:+,} / {n10:+,} / {n20:+,})混合,訊號中性"
        )

    return {
        "status": status,
        "emoji": emoji,
        "strength": strength,
        "n5": n5, "n10": n10, "n20": n20,
        "vol_text": vol_text,
        "bb_pos_ratio": bb_pos_ratio,
        "reading": reading,
    }


_ML_MODEL_PATH = "models/short_pick.pkl"
# Module-level cache:避免每張卡都 joblib.load(50ms × 138 picks = 7s 浪費)
_ml_model_cache: object | None = None
_ml_model_loaded = False


def _get_ml_model():
    """lazy load 模型 + cache。檔不存在 / load 失敗 → 永遠回 None,後續呼叫直接 short-circuit。

    印診斷 log(模組初始化只跑一次,印一行 OK / 一行 fail 都不嫌多)。
    """
    global _ml_model_cache, _ml_model_loaded
    if _ml_model_loaded:
        return _ml_model_cache
    _ml_model_loaded = True
    try:
        from src.ml_predictor import load_model
        from src import config as _config
        from pathlib import Path as _Path
        path = _Path(_config.PROJECT_ROOT) / _ML_MODEL_PATH
        if not path.exists():
            print(
                f"[ML] model 檔不存在:{path}(雲端 git checkout 沒帶到?)",
                flush=True,
            )
            _ml_model_cache = None
        else:
            _ml_model_cache = load_model(path)
            if _ml_model_cache is None:
                print(
                    f"[ML] joblib.load 回 None(可能 sklearn 版本不相容):{path}",
                    flush=True,
                )
            else:
                print(f"[ML] model loaded OK 從 {path}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ML] _get_ml_model exception:{type(e).__name__}: {e}",
            flush=True,
        )
        _ml_model_cache = None
    return _ml_model_cache


# 用 set 記錄已印過 log 的 (sid, reason) tuple,避免同 reason 對同 sid 重複印
# (e.g. 一頁 7 picks 各自 fail 印 7 行還算可接受;但避免下次 rerun 同檔再印)
_ml_log_dedup: set[tuple[str, str]] = set()


def _ai_log_once(sid: str, reason: str, msg: str) -> None:
    """同檔同原因只印一次,避免 streamlit rerun 噪音。"""
    key = (sid, reason)
    if key in _ml_log_dedup:
        return
    _ml_log_dedup.add(key)
    print(msg, flush=True)


def _ai_winrate_part(sid: str, target_date: str | None = None) -> str:
    """產 「🎯 AI 勝率 N%」 part。沒模型 / 預測失敗 → 「🎯 —」(維持格式)。

    target_date None → 用 SQLite daily_prices 最新日期(週末/假日也能跑)。
    各 fallback 點印診斷 log 排查雲端 / workflow runner 預測失敗的根因。
    """
    model = _get_ml_model()
    if model is None:
        # _get_ml_model 自己已印過 root cause(只印一次),這裡每 sid 印一次
        # 提示 fallback 原因,確保 logs 看得到「為何此 sid 沒 AI」
        _ai_log_once(
            sid, "no_model",
            f"[ML] {sid} fallback「🎯 —」: model is None"
            "(見上方 _get_ml_model 印的 root cause)",
        )
        return "🎯 —"
    try:
        if target_date is None:
            target_date = db.get_latest_trading_date()
        if target_date is None:
            _ai_log_once(
                sid, "no_latest_date",
                f"[ML] {sid} fallback「🎯 —」: 無 latest_trading_date"
                "(daily_prices 空)",
            )
            return "🎯 —"
        from src.ml_predictor import predict_short_pick_winrate
        prob = predict_short_pick_winrate(model, sid, target_date)
        if prob is None:
            # 無條件重跑 verbose extract_features(每 sid 必印一次)
            # — 之前 dedup 太緊,extract 成功(features OK)時沒任何 verbose log,
            # 看不出是 sklearn 端失敗還是 features 抽不到。現在改無條件,根據
            # extract 結果判斷:
            #   features = None → verbose 印 [ML/extract] 失敗點(歷史 / NaN 等)
            #   features ≠ None → 確認 sklearn 端失敗(predict_proba 已印 [ML/predict])
            try:
                from src.ml_predictor import extract_features as _ef
                feats_check = _ef(sid, target_date, verbose=True)
                if feats_check is not None:
                    print(
                        f"[ML] {sid}@{target_date} verbose extract 成功"
                        f"(features 抽到 11 keys),所以 prob is None 是"
                        f"**sklearn predict 端**失敗,見上方 [ML/predict] log",
                        flush=True,
                    )
            except Exception as e:  # noqa: BLE001
                print(
                    f"[ML] {sid}@{target_date} verbose extract 拋例外"
                    f":{type(e).__name__}: {e}",
                    flush=True,
                )
            _ai_log_once(
                sid, f"prob_none@{target_date}",
                f"[ML] {sid}@{target_date} fallback「🎯 —」: prob is None",
            )
            return "🎯 —"
        # SUCCESS log:確認真的有預測成功 + 印出機率值
        _ai_log_once(
            sid, f"ok@{target_date}",
            f"[ML] {sid}@{target_date} predict OK prob={prob:.3f}"
            f" → 「🎯 AI 勝率 {prob * 100:.0f}%」",
        )
        return f"🎯 AI 勝率 {prob * 100:.0f}%"
    except Exception as e:  # noqa: BLE001
        _ai_log_once(
            sid, f"exc@{target_date}",
            f"[ML] {sid}@{target_date} fallback「🎯 —」: predict exception"
            f" {type(e).__name__}: {e}",
        )
        return "🎯 —"


def format_pick_summary(sid: str, indent: str = "   ") -> str:
    """產生推播 / 卡片用的 1-line 精簡分析摘要(沒 streamlit 依賴,純字串)。

    格式永遠 4 part(用「—」佔位保持格式統一,即使全 fallback):
        `{indent}📊 {trend}+{bb_pos} / 🚦 主力 {status} / 💡 {操作核心} / 🎯 AI 勝率 N%`

    任一 part 沒資料(歷史不足 / 無法人籌碼 / 無 ML 模型)→ 該 part 顯示「—」,
    不 skip。user 一看格式就知道該檔哪一面缺資料。

    給 src.notifier / src.discord_notifier reuse,讓推播訊息每檔加一行詳細
    (原本只 close + 量比 + KD + 法人 3 日)。
    """
    summary = _compute_technical_summary(sid)
    main_force = _compute_main_force_signal(sid)

    # 📊 技術:trend + bb_pos
    if "error" not in summary:
        trend = summary.get("trend", "")
        bb_pos = summary.get("bb_pos", "")
        # bb_pos 含括號註解(如「貼近上軌(偏強)」)— 取「(」前精簡
        bb_short = bb_pos.split("(")[0].strip()
        tech_part = (
            f"📊 {trend} + {bb_short}" if trend and bb_short else "📊 —"
        )
    else:
        tech_part = "📊 —"

    # 🚦 主力燈號 status
    if "error" not in main_force:
        status = main_force.get("status", "")
        force_part = f"🚦 主力 {status}" if status else "🚦 —"
    else:
        force_part = "🚦 —"

    # 💡 操作核心(查 _ACTION_CORE_BY_SUMMARY,缺就「—」)
    if "error" not in summary:
        summary_key = summary.get("summary", "")
        action_core = _ACTION_CORE_BY_SUMMARY.get(summary_key, "")
        if action_core:
            # action_core 模板較長(「順勢續抱,跌破 MA20 ...」)→ 取「,」前精簡
            short = action_core.split(",")[0]
            action_part = f"💡 {short}"
        else:
            action_part = "💡 —"
    else:
        action_part = "💡 —"

    # 🎯 AI 勝率(如果 model 存在)
    ai_part = _ai_winrate_part(sid)

    return indent + " / ".join([tech_part, force_part, action_part, ai_part])


@st.cache_data(ttl=300, show_spinner=False)
def _read_company_profile_cached(sid: str) -> dict | None:
    """從 SQLite company_profiles 讀整筆資料(read-only,**不**呼叫 LLM)。

    `@st.cache_data(ttl=300)` 避免一張清單 N 張卡片重複查同股(138 picks
    一次展開 = 138 次 SELECT 是純浪費)。TTL 5 分鐘給 user 點「重新生成」
    後一段時間能看到新值。

    回 dict(同 company_profiles 欄位)或 None。
    """
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT industry, market, description, uniqueness, moat, "
                "llm_updated_at FROM company_profiles WHERE stock_id=?",
                (sid,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def _render_company_info_compact(sid: str) -> None:
    """卡片 expander 內的精簡公司資訊(read-only,純讀 SQLite cache)。

    設計:
    - **不主動跑 LLM** — 138 picks 全展開會打爆 Gemini quota;想生成走個股頁
    - **不放重新生成按鈕** — 同上
    - facts 即時(industry / market 從 cache 一行帶出)
    - LLM 3 段(description / uniqueness / moat)有就顯,沒就 placeholder
    """
    profile = _read_company_profile_cached(sid)
    st.markdown("### 🏢 公司資訊")

    if profile is None:
        st.caption(
            "🤖 公司資訊尚未生成 — 進「🔍 個股」頁查詢此股可自動生成"
            "(LLM 描述會 cache,之後卡片就看得到)"
        )
        return

    # facts(industry / market)— 沒寫進 cache 用 — 佔位
    industry = profile.get("industry") or "—"
    market = profile.get("market") or "—"
    st.markdown(f"**產業**:{industry}　|　**市場**:{market}")

    desc = profile.get("description")
    uniq = profile.get("uniqueness")
    moat = profile.get("moat")
    if not (desc or uniq or moat):
        st.caption(
            "📝 LLM 描述尚未生成 — 進「🔍 個股」頁查詢此股可自動生成"
        )
        return

    if desc:
        st.markdown(f"**📝 業務**:{desc}")
    if uniq:
        st.markdown(f"**✨ 獨特性**:{uniq}")
    if moat:
        st.markdown(f"**🏰 護城河**:{moat}")


def _render_main_force_signal(sid: str) -> None:
    """個股頁:主力燈號(出貨 / 吸貨判斷 + 強度條 + 解讀)。"""
    result = _compute_main_force_signal(sid)
    if "error" in result:
        st.info(f"🚦 主力燈號:{result['error']}")
        return

    filled = "🟢" * result["strength"]
    empty = "⚪" * (5 - result["strength"])
    bar = filled + empty

    st.markdown("### 🚦 主力燈號")
    st.markdown(
        f"**主要狀態**:{result['emoji']} **{result['status']}**\n\n"
        f"**強度**:{bar}({result['strength']}/5)\n\n"
        f"- 近 5 日法人合計:`{result['n5']:+,}` 張\n"
        f"- 近 10 日法人合計:`{result['n10']:+,}` 張\n"
        f"- 近 20 日法人合計:`{result['n20']:+,}` 張\n"
        f"- 量價配對:{result['vol_text']}\n"
        f"- BB 位置:`{result['bb_pos_ratio']:.0%}`(0% = 下軌, 100% = 上軌)"
    )
    st.info(f"💬 解讀:{result['reading']}")
