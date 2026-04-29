"""集中所有 in-memory cache 的 reset helper。

各 module 都有自己的 module-level dict / 變數當 cache(避免重複打網路 / SQL),
本模組統一暴露 clear_all_caches() 給 UI 重新載入按鈕呼叫。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def clear_all_caches() -> None:
    """清空所有 in-memory cache + Streamlit cache,配合 st.rerun() 強制重抓。

    覆蓋:
    - data_fetcher: stock_info cache + bulk cache
    - financial_fetcher_free: metrics / eps / industry / session
    - market_sentiment: TAIEX / 法人 / 融資券 / VIX 60 秒 cache
    - Streamlit: cache_data / cache_resource(若有用)

    任一 reset 失敗不擋整體(log warn 繼續)。
    """
    cleared = []
    failed = []

    # 1. data_fetcher
    try:
        from src import data_fetcher
        if hasattr(data_fetcher, "_reset_stock_info_cache"):
            data_fetcher._reset_stock_info_cache()
            cleared.append("data_fetcher.stock_info")
        if hasattr(data_fetcher, "_reset_bulk_cache"):
            data_fetcher._reset_bulk_cache()
            cleared.append("data_fetcher.bulk")
    except Exception as e:  # noqa: BLE001
        failed.append(f"data_fetcher: {e}")

    # 2. financial_fetcher_free
    try:
        from src import financial_fetcher_free
        if hasattr(financial_fetcher_free, "_reset_caches"):
            financial_fetcher_free._reset_caches()
            cleared.append("financial_fetcher_free")
    except Exception as e:  # noqa: BLE001
        failed.append(f"financial_fetcher_free: {e}")

    # 3. market_sentiment
    try:
        from src import market_sentiment
        if hasattr(market_sentiment, "_reset_cache"):
            market_sentiment._reset_cache()
            cleared.append("market_sentiment")
    except Exception as e:  # noqa: BLE001
        failed.append(f"market_sentiment: {e}")

    # 4. Streamlit cache(若不在 streamlit context 會 raise,吞掉)
    try:
        import streamlit as st
        st.cache_data.clear()
        cleared.append("streamlit.cache_data")
    except Exception:  # noqa: BLE001 — 非 streamlit context 預期
        pass
    try:
        import streamlit as st
        st.cache_resource.clear()
        cleared.append("streamlit.cache_resource")
    except Exception:
        pass

    if failed:
        logger.warning("[CACHE] 部分 reset 失敗: %s", failed)
    logger.info("[CACHE] 清空: %s", cleared)


__all__ = ["clear_all_caches"]
