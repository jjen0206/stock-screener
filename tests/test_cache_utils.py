"""src/cache_utils.py 測試:確認 clear_all_caches() 真的清空各 module cache。"""
from __future__ import annotations

import pandas as pd
import pytest

from src import (
    cache_utils,
    data_fetcher,
    financial_fetcher_free as ff,
    market_sentiment as ms,
)


def test_clear_all_caches_resets_data_fetcher():
    # 灌髒 cache
    data_fetcher._stock_info_cache = {"2330": {"name": "old"}}
    data_fetcher._stock_info_cache_time = 9999.0
    data_fetcher._bulk_cache["test"] = pd.DataFrame([{"x": 1}])

    cache_utils.clear_all_caches()

    assert data_fetcher._stock_info_cache is None
    assert data_fetcher._bulk_cache == {}


def test_clear_all_caches_resets_financial_fetcher_free():
    ff._metrics_cache = {"2330": {"pe": 99}}
    ff._metrics_cache_time = 9999.0
    ff._eps_cache = {"2330": {"eps": 99}}

    cache_utils.clear_all_caches()

    assert ff._metrics_cache is None
    assert ff._eps_cache is None


def test_clear_all_caches_resets_market_sentiment():
    ms._CACHE["test_key"] = (9999.0, pd.DataFrame([{"x": 1}]))
    cache_utils.clear_all_caches()
    assert ms._CACHE == {}


def test_clear_all_caches_no_streamlit_context_does_not_raise():
    """非 streamlit context(例如 cron 腳本)呼叫該 swallow 例外不爆。"""
    # 此測試本身就是非 streamlit context,呼叫該成功
    cache_utils.clear_all_caches()  # 不該 raise
