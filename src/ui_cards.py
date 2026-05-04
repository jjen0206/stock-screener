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


def _fire_emoji(n_signals: int) -> str:
    """信號數 → 🔥 視覺。"""
    if n_signals <= 0:
        return ""
    if n_signals == 1:
        return "🔥"
    if n_signals == 2:
        return "🔥🔥"
    return "🔥🔥🔥"  # 3+


def render_pick_card(
    row: dict,
    show_signal: bool = True,
    show_targets: bool = True,
    show_change: bool = False,
    show_add_button: bool = False,
    button_key_prefix: str = "card",
) -> None:
    """渲染單檔股票卡片。

    row 必含:stock_id, name, close
    可選:信號數, 信號, target_low/high, stop_loss, risk_reward, atr14,
          change_pct (漲跌%), volume, ma5

    show_add_button=True 會在卡片下方加 ☆ 加關注按鈕(短線 / 長線推薦頁用,
    我的關注頁不用 — 自己加自己沒意義)。
    """
    sid = row.get("stock_id", "?")
    name = row.get("name") or row.get("名稱") or "—"
    close = row.get("close")
    if close is None and "收盤" in row:
        close = row["收盤"]

    n_signals = int(row.get("信號數") or 0)
    signal_text = row.get("信號") or ""
    change_pct = row.get("change_pct")
    if change_pct is None and "漲跌%" in row:
        # 字串型如 "+1.23%" → 嘗試 parse
        try:
            change_pct = float(str(row["漲跌%"]).rstrip("%"))
        except (TypeError, ValueError):
            change_pct = None

    with st.container(border=True):
        col1, col2 = st.columns([3, 2])
        with col1:
            fire = _fire_emoji(n_signals) if show_signal else ""
            title = f"**{sid} {name}**"
            if fire:
                title += f"  {fire}"
            st.markdown(title)
            if show_signal and signal_text:
                st.caption(f"📌 {signal_text}")
        with col2:
            close_str = _fmt_num(close, "{:.2f}")
            if show_change and change_pct is not None:
                # 台股慣例:漲紅跌綠 — st.metric 用 inverse 反轉預設(預設正
                # 是綠,跟台股顛倒)。format_change 統一箭頭 + 顏色字串
                from src.ui_format import arrow_for
                st.metric(
                    "收盤", close_str,
                    delta=f"{arrow_for(change_pct)} {abs(change_pct):.2f}%",
                    delta_color="inverse",
                )
            else:
                st.metric("收盤", close_str)

        if show_targets:
            tl = row.get("target_low")
            th = row.get("target_high")
            sl = row.get("stop_loss")
            rr = row.get("risk_reward")
            if tl is not None and th is not None and sl is not None:
                rr_str = (
                    f" (R:R {rr:.1f}:1)" if rr is not None and rr == rr else ""
                )
                st.markdown(
                    f"🎯 {_fmt_num(tl)} / 🚀 {_fmt_num(th)} / "
                    f"🛑 {_fmt_num(sl)}{rr_str}"
                )

        # P&L 行(if 該股在 trades 表有持倉)— 漲紅跌綠(台股慣例,via ui_format)
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

        if show_add_button:
            from src import database as db
            already = db.is_in_watchlist(sid)
            label = "✅ 已關注" if already else "⭐ 加入關注"
            st.button(
                label,
                key=f"{button_key_prefix}_add_{sid}",
                disabled=already,
                use_container_width=True,
                on_click=_on_add_to_watchlist,
                args=(sid,),
            )

        # 詳細分析(真 lazy — 點按鈕才 render 5 helper)
        #
        # 為什麼不用 st.expander(expanded=False):streamlit 的 expander
        # body **永遠執行**(只是 CSS 收起 UI),138 picks × 8 SQL =
        # ~1100 queries 打 SQLite,cold load 8-15 秒。
        # 改用 session_state flag + 條件 render — 收起時完全不跑 helper。
        _render_lazy_detail_section(sid, button_key_prefix)


def _toggle_expander(flag_key: str) -> None:
    """on_click callback — 翻轉 expander 開合狀態。

    用 callback 而非 `if st.button(): st.rerun()` 模式 — streamlit
    callback 會在 rerun 「之前」執行,所有 widget state(含其他 page
    selector)透過 widget key 自動 persist,不會 leak。
    `st.rerun()` 顯式呼叫沒這保證,且容易跟其他 button 的 edge-trigger
    互動產生 race(實測會把 _page_short 的 submit 狀態 reset)。
    """
    st.session_state[flag_key] = not st.session_state.get(flag_key, False)


def _render_lazy_detail_section(sid: str, button_key_prefix: str) -> None:
    """卡片底部「詳細分析」區塊 — 真 lazy(session_state flag + 條件 render)。

    收起狀態:只渲染「📊 展開詳細分析」按鈕(0 SQL)
    展開狀態:渲染 5 個 helper section + 「🔼 收起」按鈕

    flag key 帶 button_key_prefix + sid → 5 tabs UI 同 sid 可在
    「全部」/「趨勢」分別開合,互不干擾。

    按鈕走 on_click callback 而非 `if st.button(): rerun()` — 後者會把
    其他 widget 的 edge-trigger button(e.g. 短線頁的「執行選股」)的
    True 狀態吃掉,造成 page reset 假象。
    """
    flag_key = f"card_exp_{button_key_prefix}_{sid}"
    is_expanded = st.session_state.get(flag_key, False)

    if not is_expanded:
        st.button(
            "📊 展開詳細分析",
            key=f"open_{flag_key}",
            use_container_width=True,
            help="點開才會跑技術分析 / 主力燈號 / 公司資訊(避免清單 cold load 慢)",
            on_click=_toggle_expander,
            args=(flag_key,),
        )
        return

    # 展開:render 5 sections
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
        # button_key_prefix 帶進 helper — 5 tabs UI 同 sid 可能出現在
        # 「全部」+ 「趨勢」兩個 tab,各自 prefix 區隔避免 key 撞
        _render_company_info_compact(sid, key_prefix=button_key_prefix)
        st.button(
            "🔼 收起",
            key=f"close_{flag_key}",
            use_container_width=True,
            on_click=_toggle_expander,
            args=(flag_key,),
        )


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
