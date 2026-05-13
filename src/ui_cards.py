"""手機優先卡片渲染 helpers。

Streamlit st.dataframe 在手機螢幕橫向 scroll 不友善;改用 st.container(border=True)
組合 columns + metric + markdown,讓每檔個股像「卡片」直立排列。

桌機仍保留表格選擇 — UI 加 toggle 讓使用者切「📋 表格 / 🃏 卡片」。
"""
from __future__ import annotations

from typing import Any

import streamlit as st


def _fmt_num(v: Any, fmt: str = "{:.2f}", default: str = "—") -> str:
    """數字格式化;None / NaN 顯示 default。"""
    if v is None:
        return default
    try:
        if v != v:  # NaN
            return default
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return default


def _on_add_to_watchlist(stock_id: str) -> None:
    """⭐ 按鈕的 on_click callback。

    用 on_click+args 而不是 `if st.button(...): add(sid)`:args 在 widget
    註冊當下把 sid「以值」鎖進 widget state,Streamlit 收到 click 後會在
    下個 rerun **跑 script body 之前**就觸發 callback。即使外層 page 因
    其他 gating(例如 _page_short 的 `if not submit: return`)沒重渲染卡片,
    callback 仍以原本綁定的 sid 執行 — 杜絕「點 A 卻加進 B」的 index 漂移。
    """
    from src import database as db
    db.add_to_watchlist(stock_id)
    st.toast(f"已加入 {stock_id}", icon="⭐")


def _on_remove_from_watchlist(stock_id: str) -> None:
    """⭐ 按鈕反向 callback — 已關注時點按鈕變「移除關注」。"""
    from src import database as db
    db.remove_from_watchlist(stock_id)
    st.toast(f"已移除 {stock_id}", icon="🗑️")


def _fire_emoji(n_signals: int) -> str:
    """信號數 → 🔥 視覺。"""
    if n_signals <= 0:
        return ""
    if n_signals == 1:
        return "🔥"
    if n_signals == 2:
        return "🔥🔥"
    return "🔥🔥🔥"  # 3+


def _build_card_html(
    sid: str,
    name: str,
    close: Any,
    change_pct: float | None,
    signals_label: str,
    target_low: Any,
    target_high: Any,
    stop_loss: Any,
    win_rate: float | None,
    risk_reward: float | None,
    n_signals: int,
    industry: str | None = None,
    industry_heat: int = 0,
) -> str:
    """組整張卡片的 HTML(row 1 + row 2 + 右側 metadata)— 給 st.markdown
    一次吐出,省 streamlit widget 開銷。row 3 的「加入關注」/「展開詳細」
    button 不在這裡(button 必須走 st.button,不能塞 HTML)。

    回字串(unsafe_allow_html=True render)。
    """
    from src.ui_format import color_for, arrow_for, COLOR_FLAT

    # 產業 badge:industry_heat ≥ 3 → 🔥 紅色加粗(熱門類股輪動);
    # 否則 🏭 灰色 secondary;industry None / 空 → 不顯
    industry_html = ""
    if industry:
        ind_str = str(industry).strip()
        if ind_str:
            if industry_heat and industry_heat >= 3:
                industry_html = (
                    f"<div style='font-size:11px;color:#d62728;font-weight:500'>"
                    f"🔥 {ind_str} ({int(industry_heat)})</div>"
                )
            else:
                industry_html = (
                    f"<div style='font-size:11px;color:#888'>"
                    f"🏭 {ind_str}</div>"
                )

    # row 1 / 股號 + 名稱(股名為主視覺:18px 500;股號 13px secondary 化為 label)
    sid_block = (
        f"<div><div style='font-size:11px;color:#888'>股號</div>"
        f"<div style='font-size:13px;font-weight:400;color:#888'>{sid}</div>"
        f"<div style='font-size:18px;font-weight:500'>{name}</div>"
        f"{industry_html}</div>"
    )

    # row 1 / 股價 + 漲跌(股價數字 18px + 依漲跌染色)
    if close is not None and close == close:  # not NaN
        try:
            close_str = f"{float(close):.2f}"
        except (TypeError, ValueError):
            close_str = "—"
    else:
        close_str = "—"
    # 漲跌色:漲 → 紅 / 跌 → 綠 / None or 平盤 → 不加 color attr 走 streamlit 預設
    # (streamlit 暗色 / 亮色主題各自有合適 default,寫死灰會跟主題不協調)
    if change_pct is None or float(change_pct) == 0:
        price_color_style = ""
        change_color_style = ""
    else:
        col = color_for(change_pct)
        price_color_style = f";color:{col}"
        change_color_style = f"color:{col}"

    if change_pct is None:
        change_html = "—"
    else:
        c = float(change_pct)
        arr = arrow_for(c)
        change_html = (
            f"<span style='{change_color_style}'>{arr} {abs(c):.2f}%</span>"
            if change_color_style else
            f"{arr} {abs(c):.2f}%"
        )
    price_block = (
        f"<div><div style='font-size:11px;color:#888'>股價</div>"
        f"<div style='font-size:18px;font-weight:500{price_color_style}'>{close_str}</div>"
        f"<div style='font-size:11px'>{change_html}</div></div>"
    )

    # row 1 / 分析建議 — 目前 11 套策略全 long-side,先全顯「買進」
    # 紅色(看漲),搭配台股慣例。命中策略以 ` · ` 串接顯下方 caption
    signals_text = signals_label.replace(",", " ·").replace("、", " ·")
    if signals_text and "·" not in signals_text:
        # 既有逗號 join,refresh 成 ` · ` 一致風格
        pass
    advice_block = (
        f"<div><div style='font-size:11px;color:#888'>分析建議</div>"
        f"<div style='font-size:13px;font-weight:500;color:#d62728'>買進</div>"
        f"<div style='font-size:11px;color:#888'>{signals_text or '—'}</div></div>"
    )

    row1 = (
        "<div style='display:grid;"
        "grid-template-columns:108px 90px minmax(0,1fr);"
        "gap:12px;padding:8px 0;'>"
        f"{sid_block}{price_block}{advice_block}"
        "</div>"
    )

    # row 2 / 4 等寬:保守 / 積極 / 停損 / 勝率
    def _fmt_or_dash(v: Any, fmt: str = "{:.2f}") -> str:
        if v is None:
            return "—"
        try:
            f = float(v)
            if f != f:  # NaN
                return "—"
            return fmt.format(f)
        except (TypeError, ValueError):
            return "—"

    if win_rate is None:
        wr_html = "<span style='color:#888'>—</span>"
    else:
        wr = float(win_rate)
        # 勝率染色:>=60 紅(好,台股慣例)/ 50-60 灰 / <50 綠(差)
        if wr >= 0.60:
            wr_color = "#d62728"
        elif wr >= 0.50:
            wr_color = COLOR_FLAT
        else:
            wr_color = "#2ca02c"
        wr_html = f"<span style='color:{wr_color}'>{wr * 100:.0f}%</span>"

    cell_style = "font-size:14px;font-weight:500"
    label_style = "font-size:11px;color:#888"
    row2 = (
        "<div style='display:grid;grid-template-columns:repeat(4,1fr);"
        "gap:8px;padding:8px 0;'>"
        f"<div><div style='{label_style}'>保守</div>"
        f"<div style='{cell_style}'>{_fmt_or_dash(target_low)}</div></div>"
        f"<div><div style='{label_style}'>積極</div>"
        f"<div style='{cell_style}'>{_fmt_or_dash(target_high)}</div></div>"
        f"<div><div style='{label_style}'>停損</div>"
        f"<div style='{cell_style}'>{_fmt_or_dash(stop_loss)}</div></div>"
        f"<div><div style='{label_style}'>勝率</div>"
        f"<div style='{cell_style}'>{wr_html}</div></div>"
        "</div>"
    )

    # 分隔線(0.5px secondary 色)
    sep = (
        "<hr style='margin:0;border:none;"
        "border-top:0.5px solid rgba(128,128,128,0.25);' />"
    )

    return f"<div class='pick-card-content'>{row1}{sep}{row2}{sep}</div>"


def render_pick_card(
    row: dict,
    show_signal: bool = True,
    show_targets: bool = True,
    show_change: bool = False,
    show_add_button: bool = False,
    button_key_prefix: str = "card",
) -> None:
    """渲染單檔股票卡片(3 行 grid 表格式)。

    Row 1:股號+名 | 股價+漲跌 | 分析建議+命中策略
    Row 2:保守 | 積極 | 停損 | 勝率
    Row 3:[加入/移除關注] [展開詳細分析] | 右側 metadata(信號數·R:R)

    row dict 必含:stock_id, name, close
    可選:信號數, 信號, target_low/high, stop_loss, risk_reward, atr14,
          change_pct (漲跌%), volume, ma5, win_rate(0-1, Phase B 加)

    show_add_button=True 會 render 「加入關注 / 移除關注」 button。
    show_change / show_targets / show_signal 控制各欄位是否顯示(全 False
    仍會渲整個 grid,只是欄位顯 「—」)。
    """
    sid = row.get("stock_id", "?")
    name = row.get("name") or row.get("名稱") or "—"
    close = row.get("close")
    if close is None and "收盤" in row:
        close = row["收盤"]

    n_signals = int(row.get("信號數") or 0)
    signal_text = row.get("信號") or ""
    change_pct = row.get("change_pct") if show_change else None
    if change_pct is None and "漲跌%" in row and show_change:
        try:
            change_pct = float(str(row["漲跌%"]).rstrip("%"))
        except (TypeError, ValueError):
            change_pct = None

    target_low = row.get("target_low") if show_targets else None
    target_high = row.get("target_high") if show_targets else None
    stop_loss = row.get("stop_loss") if show_targets else None
    risk_reward = row.get("risk_reward") if show_targets else None
    win_rate = row.get("win_rate")  # Phase B 加;Phase A 通常 None
    ml_prob = row.get("ml_prob")    # Stage 1 加;雲端 enrich 後有值
    analyst_target_mean = row.get("analyst_target_mean")
    analyst_num = row.get("analyst_num")

    # 產業 badge(2026-05-06 加):caller 傳 row['industry'] / row['industry_heat']
    # 才顯;沒傳該欄(舊 caller) → 不影響 layout
    industry_raw = row.get("industry")
    industry = (
        str(industry_raw) if industry_raw is not None
        and not (isinstance(industry_raw, float) and industry_raw != industry_raw)
        else None
    )
    try:
        industry_heat = int(row.get("industry_heat") or 0)
    except (TypeError, ValueError):
        industry_heat = 0

    with st.container(border=True):
        # Row 1 + Row 2(整段 HTML 一次吐出 — 省 streamlit widget tree)
        st.markdown(
            _build_card_html(
                sid=sid,
                name=name,
                close=close,
                change_pct=change_pct,
                signals_label=signal_text if show_signal else "",
                target_low=target_low,
                target_high=target_high,
                stop_loss=stop_loss,
                win_rate=win_rate,
                risk_reward=risk_reward,
                n_signals=n_signals,
                industry=industry,
                industry_heat=industry_heat,
            ),
            unsafe_allow_html=True,
        )

        # 盤中即時行(caller 注入 row["intraday_quote"] 時才顯)— 漲紅跌綠
        intra = row.get("intraday_quote")
        if intra and intra.get("current") is not None:
            from src.ui_format import color_for
            cur = float(intra["current"])
            cp = intra.get("change_pct")
            if cp is not None:
                arrow = "↑" if cp > 0 else ("↓" if cp < 0 else "→")
                _color = color_for(cp)
                st.markdown(
                    f"<span style='color:{_color}'>📡 {cur:.2f} "
                    f"({arrow}{abs(cp):.1f}%)</span>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(f"📡 {cur:.2f}")

        # P&L 行(if 該股在 trades 表有持倉)— 漲紅跌綠
        try:
            from src import database as _db
            from src.ui_format import color_for, arrow_for
            _pos = _db.get_position(sid)
            if _pos["quantity"] > 0 and close is not None:
                _close_f = float(close) if not isinstance(close, float) else close
                _avg = _pos["avg_cost"]
                _qty = _pos["quantity"]
                _unrealized = (_close_f - _avg) * _qty
                _pct = (_close_f - _avg) / _avg * 100 if _avg > 0 else 0
                _color = color_for(_unrealized)
                _arrow = arrow_for(_unrealized)
                _sign = "+" if _unrealized > 0 else ("-" if _unrealized < 0 else "")
                _pct_sign = "+" if _pct > 0 else ("-" if _pct < 0 else "")
                st.markdown(
                    f"<span style='color:{_color}'>📈 持有 {_qty} 張 @ "
                    f"均價 {_avg:.2f} / 損益 {_arrow} {_sign}{abs(_unrealized):,.0f} "
                    f"({_pct_sign}{abs(_pct):.1f}%)</span>",
                    unsafe_allow_html=True,
                )
        except Exception:  # noqa: BLE001
            pass  # 沒倉位 / DB 錯誤都 silent skip(不影響卡片本體)

        # 法人目標價 inline 行(只在有資料時顯)— 比 row 3 metadata badge 顯眼
        # 漲幅 ≥ +10% 紅(看好)/ ≤ -5% 綠(下修)/ 其他灰
        _render_analyst_target_inline(
            analyst_target_mean=analyst_target_mean,
            analyst_num=analyst_num,
            close=close,
            sid=sid,
        )

        # 千張大戶 inline 行(只在有資料時顯)— 主公拍板:不納入 ML、只當附加資訊
        # Mobile-first:單行文字,不用 st.columns / st.metric 多欄(手機會 collapse)
        # 格式 `👥 千張戶 12 (+3)`,delta=None 時只顯人數
        _render_shareholder_inline(
            holders_1000up_count=row.get("holders_1000up_count"),
            holders_delta_w=row.get("holders_delta_w"),
        )

        # U3 進場區間建議(ATR / BB based)— 只在 caller 注入 entry_low/high 時顯
        # Mobile-first 單行 st.markdown,無 st.columns
        _render_entry_range_inline(
            entry_low=row.get("entry_low"),
            entry_high=row.get("entry_high"),
        )

        # Row 3:button 列(加入關注 / 展開詳細分析)+ 右側 metadata
        # st.columns 把 row 3 分成 3 區塊:button | button | metadata
        if show_add_button:
            r3_cols = st.columns([1, 1, 1])
            with r3_cols[0]:
                _render_watchlist_toggle_button(sid, button_key_prefix)
            with r3_cols[1]:
                _render_lazy_detail_section_compact(sid, button_key_prefix)
            with r3_cols[2]:
                _render_card_metadata(
                    n_signals, risk_reward, ml_prob=ml_prob,
                    analyst_target_mean=analyst_target_mean,
                    analyst_num=analyst_num,
                )
        else:
            # 沒 add button(watchlist 卡 / 表格附帶卡)— 只 render 詳細 + metadata
            r3_cols = st.columns([1, 1])
            with r3_cols[0]:
                _render_lazy_detail_section_compact(sid, button_key_prefix)
            with r3_cols[1]:
                _render_card_metadata(
                    n_signals, risk_reward, ml_prob=ml_prob,
                    analyst_target_mean=analyst_target_mean,
                    analyst_num=analyst_num,
                )

        # 展開詳細分析「打開」狀態時,full helper section 還是要在 button 之外
        # render(button 觸發後 flag=True,下一輪 rerun render 完整 section)
        flag_key = f"card_exp_{button_key_prefix}_{sid}"
        if st.session_state.get(flag_key, False):
            _render_lazy_detail_section_body(sid, button_key_prefix, flag_key)


def _render_analyst_target_inline(
    analyst_target_mean: float | None,
    analyst_num: int | None,
    close: float | None,
    sid: str,
) -> None:
    """卡片內顯眼的「法人共識目標」行 — 比 row 3 metadata 大,有資料才渲。

    格式:
      📊 法人共識 850 (+21%) · 券商 23 家 · Yahoo
    染色(漲幅 vs close):
      ≥ +10% → 紅 #d62728(看好)
      ≤ -5%  → 綠 #2ca02c(下修)
      其他   → 灰 #555
    來源 'yfinance' → 顯「Yahoo」;'gemini_news' → 顯「新聞解析」。
    """
    if analyst_target_mean is None or (
        isinstance(analyst_target_mean, float)
        and analyst_target_mean != analyst_target_mean  # NaN
    ):
        return
    try:
        mean_val = float(analyst_target_mean)
    except (TypeError, ValueError):
        return

    upside_str = ""
    color = "#555"
    if close is not None:
        try:
            close_f = float(close)
            if close_f > 0:
                upside = (mean_val - close_f) / close_f * 100
                sign = "+" if upside >= 0 else "-"
                upside_str = f" ({sign}{abs(upside):.0f}%)"
                if upside >= 10:
                    color = "#d62728"
                elif upside <= -5:
                    color = "#2ca02c"
        except (TypeError, ValueError):
            pass

    n_str = (
        str(int(analyst_num)) if analyst_num is not None
        and not (isinstance(analyst_num, float) and analyst_num != analyst_num)
        else "?"
    )

    # 來源:從 SQLite analyst_targets 撈 source 顯小字
    source_label = ""
    try:
        from src.analyst_targets import get_analyst_target
        target = get_analyst_target(sid)
        if target:
            src = target.get("source")
            if src == "yfinance":
                source_label = " · Yahoo"
            elif src == "gemini_news":
                source_label = " · 新聞解析"
    except Exception:  # noqa: BLE001
        pass  # 撈不到就不顯來源(badge 仍正常)

    st.markdown(
        f"<span style='color:{color};font-size:14px;'>"
        f"📊 <strong>法人共識 {mean_val:.0f}{upside_str}</strong>"
        f" · 券商 {n_str} 家{source_label}"
        f"</span>",
        unsafe_allow_html=True,
    )


def _render_shareholder_inline(
    holders_1000up_count: int | None,
    holders_delta_w: int | None,
) -> None:
    """卡片內顯眼的「千張大戶」行 — 主公拍板:長線卡才顯,有資料才渲。

    格式(緊湊,主公手機 iPhone 用,字元短):
      👥 千張戶 12 (+3)        # 有 delta
      👥 千張戶 12             # 沒 delta(第一次抓,沒上週可比)
      (整行不渲)              # 沒人數資料

    染色(delta 方向):
      delta > 0 → 紅 #d62728(大戶進場,看漲台股慣例)
      delta < 0 → 綠 #2ca02c(大戶減少,籌碼鬆動)
      delta = 0 / None → 灰 #555(中性)

    Mobile-first(主公主要 iPhone 用):
      - 用 st.markdown 單行文字 + inline color,不用 st.columns / st.metric
        (手機 narrow viewport 多欄會 collapse 變難讀)
    """
    if holders_1000up_count is None:
        return
    try:
        count_int = int(holders_1000up_count)
    except (TypeError, ValueError):
        return

    delta_str = ""
    color = "#555"
    if holders_delta_w is not None:
        try:
            delta_int = int(holders_delta_w)
            delta_str = f" ({delta_int:+d})"
            if delta_int > 0:
                color = "#d62728"
            elif delta_int < 0:
                color = "#2ca02c"
        except (TypeError, ValueError):
            pass

    import streamlit as st
    st.markdown(
        f"<span style='color:{color};font-size:14px;'>"
        f"👥 <strong>千張戶 {count_int}{delta_str}</strong>"
        f"</span>",
        unsafe_allow_html=True,
    )


def _render_entry_range_inline(
    entry_low: float | None,
    entry_high: float | None,
) -> None:
    """U3 進場區間建議(ATR / BB based)— 卡片內顯眼單行,有資料才渲。

    格式(緊湊,主公手機 iPhone 用):
      💰 進場區間 1232.50 ~ 1245.00

    沒資料(歷史 < 20 天 / ATR/BB 算不出)→ caller 不傳 entry_low/high
    → 整行不渲(graceful skip)。

    Mobile-first:單行 st.markdown,不用 st.columns / st.metric。
    """
    if entry_low is None or entry_high is None:
        return
    try:
        low_f = float(entry_low)
        high_f = float(entry_high)
    except (TypeError, ValueError):
        return
    if low_f <= 0 or high_f <= 0:
        return

    import streamlit as st
    st.markdown(
        f"<span style='color:#1f77b4;font-size:14px;'>"
        f"💰 <strong>進場區間 {low_f:.2f} ~ {high_f:.2f}</strong>"
        f"</span>",
        unsafe_allow_html=True,
    )


def _render_watchlist_toggle_button(sid: str, button_key_prefix: str) -> None:
    """加入關注 / 移除關注 toggle button(已關注顯移除,反之加入)。"""
    from src import database as db
    already = db.is_in_watchlist(sid)
    if already:
        st.button(
            "🗑️ 移除關注",
            key=f"{button_key_prefix}_remove_{sid}",
            use_container_width=True,
            on_click=_on_remove_from_watchlist,
            args=(sid,),
        )
    else:
        st.button(
            "⭐ 加入關注",
            key=f"{button_key_prefix}_add_{sid}",
            use_container_width=True,
            on_click=_on_add_to_watchlist,
            args=(sid,),
        )


def _render_card_metadata(
    n_signals: int,
    risk_reward: float | None,
    ml_prob: float | None = None,
    analyst_target_mean: float | None = None,
    analyst_num: int | None = None,
) -> None:
    """卡片 row 3 右側 metadata — 信號數 · R:R · 🤖 ML 機率 · 📊 共識目標。

    ml_prob 染色(高 = 好,台股慣例好 = 紅):
    - >= 0.70 → 紅 #d62728
    - 0.60-0.70 → 灰 #888888
    - < 0.60 → 綠 #2ca02c
    None / NaN → **整段不渲**(跟 analyst_target_mean 同邏輯,避免 watchlist
    卡片永遠顯誤導性的「🤖 —」,主公會以為是「期待值」沒值)。

    analyst_target_mean:有資料才顯,沒有就不渲(避免空 badge)。

    用 markdown(unsafe_allow_html=True)而非 st.caption 才能上色。
    """
    parts: list[str] = []
    if n_signals > 0:
        # ≥2 策略共識命中標明顯,讓 confluence 過濾後的「優等生」一眼看出
        if n_signals >= 2:
            parts.append(f"<strong>📊 {n_signals} 策略命中</strong>")
        else:
            parts.append(f"{n_signals} 策略命中")
    if risk_reward is not None and risk_reward == risk_reward:
        try:
            parts.append(f"R:R {float(risk_reward):.1f}")
        except (TypeError, ValueError):
            pass

    # ML 機率(有值才顯,沒值整段不渲)
    ml_html = ""
    if ml_prob is not None and not (
        isinstance(ml_prob, float) and ml_prob != ml_prob  # NaN
    ):
        try:
            p = float(ml_prob)
            if p >= 0.70:
                ml_color = "#d62728"  # 紅(高,好)
            elif p >= 0.60:
                ml_color = "#888888"  # 灰
            else:
                ml_color = "#2ca02c"  # 綠(低,差)
            ml_html = (
                f"<span style='color:{ml_color}'>🤖 {p * 100:.0f}%</span>"
            )
        except (TypeError, ValueError):
            ml_html = ""

    # 法人共識目標價(有資料才顯)
    analyst_html = ""
    if analyst_target_mean is not None and not (
        isinstance(analyst_target_mean, float)
        and analyst_target_mean != analyst_target_mean  # NaN
    ):
        try:
            mean_val = float(analyst_target_mean)
            n_str = (
                str(int(analyst_num)) if analyst_num is not None
                and not (
                    isinstance(analyst_num, float) and analyst_num != analyst_num
                ) else "?"
            )
            analyst_html = (
                f"<span style='color:#1f77b4'>📊 共識 {mean_val:.0f} "
                f"({n_str} 家)</span>"
            )
        except (TypeError, ValueError):
            analyst_html = ""

    if parts or ml_html or analyst_html:
        text_parts = parts.copy()
        if ml_html:
            text_parts.append(ml_html)
        if analyst_html:
            text_parts.append(analyst_html)
        st.markdown(
            "<div style='font-size:11px;color:#888'>"
            + " · ".join(text_parts)
            + "</div>",
            unsafe_allow_html=True,
        )


def _render_lazy_detail_section_compact(sid: str, button_key_prefix: str) -> None:
    """row 3 的「展開詳細分析」按鈕(縮版)— 只渲 button,實際內容在卡片 below。"""
    flag_key = f"card_exp_{button_key_prefix}_{sid}"
    is_expanded = st.session_state.get(flag_key, False)
    label = "🔼 收起" if is_expanded else "📊 展開詳細"
    st.button(
        label,
        key=f"toggle_{flag_key}",
        use_container_width=True,
        help="技術分析 / 主力燈號 / 公司資訊",
        on_click=_toggle_expander,
        args=(flag_key,),
    )


def _render_lazy_detail_section_body(
    sid: str, button_key_prefix: str, flag_key: str,
) -> None:
    """展開狀態下的 5 個 helper section body(button 之外另起一塊)。"""
    from src.individual_sections import (
        _render_action_suggestion,
        _render_company_info_compact,
        _render_key_levels,
        _render_main_force_signal,
        _render_technical_summary,
    )
    with st.container(border=True):
        _render_main_force_signal(sid)
        _render_technical_summary(sid)
        _render_key_levels(sid)
        _render_action_suggestion(sid)
        _render_company_info_compact(sid, key_prefix=button_key_prefix)


def _toggle_expander(flag_key: str) -> None:
    """on_click callback — 翻轉 expander 開合狀態。

    用 callback 而非 `if st.button(): st.rerun()` 模式 — streamlit
    callback 會在 rerun 「之前」執行,所有 widget state(含其他 page
    selector)透過 widget key 自動 persist,不會 leak。
    `st.rerun()` 顯式呼叫沒這保證,且容易跟其他 button 的 edge-trigger
    互動產生 race(實測會把 _page_short 的 submit 狀態 reset)。
    """
    st.session_state[flag_key] = not st.session_state.get(flag_key, False)


# NOTE: 舊版 _render_lazy_detail_section(button + body 一體)被 3-row grid
# 改版拆成 _render_lazy_detail_section_compact(只 button)+
# _render_lazy_detail_section_body(展開狀態 render 完整 5 sections),分別
# 放進 row 3 button 列 + body 在卡片下方,維持「點才跑」的 lazy 行為不變。


def render_picks_cards(rows: list[dict], **kwargs: Any) -> None:
    """批次渲染多張卡片。"""
    for row in rows:
        render_pick_card(row, **kwargs)


def _load_more(shown_key: str, page_size: int) -> None:
    """on_click callback — 多顯示 page_size 張卡片(避免 st.rerun 衝撞 edge-triggered button)。"""
    st.session_state[shown_key] = (
        st.session_state.get(shown_key, page_size) + page_size
    )


def render_picks_cards_paginated(
    rows: list[dict],
    state_key: str,
    page_size: int = 10,
    **kwargs: Any,
) -> None:
    """分頁版:預設只 render 前 page_size 張,「載入更多」按鈕 +page_size。

    cold load 不跑滿 — 短線 138 picks 只 render 10 張,user 想看更多自己點。
    state_key 必須跨 caller 唯一(e.g. "short_全部" / "watchlist" / "long")
    避免 5 tabs / 多頁面 session_state 撞。

    「載入更多」走 on_click callback,不用 `if button(): rerun()` 模式
    (後者跟其他 edge-triggered button 互動時會把對方狀態吃掉)。

    其他 kwargs 直接傳給 render_pick_card。
    """
    if not rows:
        return
    total = len(rows)
    shown_key = f"{state_key}_shown"
    shown = st.session_state.get(shown_key, page_size)
    shown = min(shown, total)

    for row in rows[:shown]:
        render_pick_card(row, **kwargs)

    if shown < total:
        st.button(
            f"📜 載入更多({total - shown} 檔未顯示)",
            key=f"{state_key}_load_more",
            use_container_width=True,
            on_click=_load_more,
            args=(shown_key, page_size),
        )
    else:
        st.caption(f"✅ 已顯示全部 {total} 檔")


def add_to_watchlist_inline_button(stock_id: str, key: str) -> None:
    """單獨用的「加入關注」按鈕(用於表格 view 上方的多選操作 / 個股頁)。"""
    from src import database as db
    already = db.is_in_watchlist(stock_id)
    if st.button(
        "✅ 已關注" if already else f"⭐ 關注 {stock_id}",
        key=key, disabled=already,
    ):
        db.add_to_watchlist(stock_id)
        st.toast(f"已加入 {stock_id}", icon="⭐")
        st.rerun()


def view_mode_toggle(
    key: str,
    default: str = "🃏 卡片",
    label: str = "顯示方式",
) -> str:
    """渲染「📋 表格 / 🃏 卡片」segmented 選擇器,回傳目前選擇。

    手機預設卡片,桌機使用者可手動切表格。
    """
    return st.segmented_control(
        label, ["🃏 卡片", "📋 表格"],
        default=default, key=key, label_visibility="collapsed",
    )


__all__ = [
    "render_pick_card",
    "render_picks_cards",
    "render_picks_cards_paginated",
    "view_mode_toggle",
]
