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
                arrow = "▲" if change_pct >= 0 else "▼"
                st.metric(
                    "收盤", close_str,
                    delta=f"{arrow} {abs(change_pct):.2f}%",
                    delta_color="normal" if change_pct >= 0 else "inverse",
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


def render_picks_cards(rows: list[dict], **kwargs: Any) -> None:
    """批次渲染多張卡片。"""
    for row in rows:
        render_pick_card(row, **kwargs)


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
    "view_mode_toggle",
]
