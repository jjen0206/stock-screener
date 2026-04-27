"""
Stock Screener — Streamlit 入口
這是最小骨架,後續任務會逐步擴充功能。
"""
import streamlit as st


def main() -> None:
    """主程式入口。"""
    st.set_page_config(
        page_title="個人選股工具",
        page_icon="📈",
        layout="wide",
    )

    st.title("📈 個人選股工具")
    st.caption("台股 / 美股 · 短線 + 長線")

    st.info(
        "這是專案骨架,功能尚未實作。\n\n"
        "請依照 `docs/TASKS.md` 的階段逐步完成開發。"
    )

    # 風險警語
    st.markdown("---")
    st.caption(
        "⚠️ 本工具僅供個人研究使用,不構成任何投資建議。"
        "投資請自行評估風險。"
    )


if __name__ == "__main__":
    main()
