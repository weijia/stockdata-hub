#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轮询缓存（设计 §6.4）单元测试：动态 TTL + 跨 TTL 去重合并 + fetcher 接入。"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pandas as pd

from stockdata_hub.cache import (  # noqa: E402
    IntradayCache,
    StockCacheManager,
    _merge_by_datetime,
    period_ttl,
)
from stockdata_hub.fetcher import StockDataFetcher  # noqa: E402


def _df(rows):
    return pd.DataFrame(
        rows, columns=["datetime", "open", "high", "low", "close", "volume"]
    )


# =========================================================================== #
# 1) StockCacheManager 动态 TTL
# =========================================================================== #
def test_stock_cache_manager_dynamic_ttl():
    mgr = StockCacheManager()
    mgr.set("k", "v", ttl=0.1)
    assert mgr.get("k") == "v"
    time.sleep(0.15)
    assert mgr.get("k") is None  # 超 TTL 失效


# =========================================================================== #
# 2) 跨 TTL 去重合并
# =========================================================================== #
def test_merge_by_datetime_keeps_latest_and_union():
    old = _df(
        [
            {"datetime": pd.Timestamp("2026-07-17 09:30"), "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10},
            {"datetime": pd.Timestamp("2026-07-17 09:31"), "open": 2, "high": 3, "low": 2, "close": 3, "volume": 20},
        ]
    )
    new = _df(
        [
            {"datetime": pd.Timestamp("2026-07-17 09:31"), "open": 99, "high": 99, "low": 99, "close": 99, "volume": 99},
            {"datetime": pd.Timestamp("2026-07-17 09:32"), "open": 3, "high": 4, "low": 3, "close": 4, "volume": 30},
        ]
    )
    merged = _merge_by_datetime(old, new)
    # 并集：09:30 / 09:31 / 09:32 共 3 根
    assert len(merged) == 3
    # 冲突的 09:31 保留 new（最新）的值
    row = merged[merged["datetime"] == pd.Timestamp("2026-07-17 09:31")].iloc[0]
    assert row["close"] == 99
    # 按时间排序
    assert list(merged["datetime"]) == sorted(merged["datetime"])


# =========================================================================== #
# 3) IntradayCache：TTL 命中 + 合并写入
# =========================================================================== #
def test_intraday_cache_ttl_and_merge():
    cache = IntradayCache(StockCacheManager())  # 独立 manager，隔离
    df1 = _df(
        [{"datetime": pd.Timestamp("2026-07-17 09:30"), "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10}]
    )
    cache.merge_and_set("600519", "1m", df1, "新浪分钟")
    got = cache.get("600519", "1m")
    assert got is not None and got["source"] == "新浪分钟"

    df2 = _df(
        [
            {"datetime": pd.Timestamp("2026-07-17 09:30"), "open": 1, "high": 2, "low": 1, "close": 5, "volume": 10},
            {"datetime": pd.Timestamp("2026-07-17 09:31"), "open": 2, "high": 3, "low": 2, "close": 3, "volume": 20},
        ]
    )
    merged2 = cache.merge_and_set("600519", "1m", df2, "新浪分钟")
    assert len(merged2) == 2
    row = merged2[merged2["datetime"] == pd.Timestamp("2026-07-17 09:30")].iloc[0]
    assert row["close"] == 5  # 保留最新


def test_period_ttl_values():
    assert period_ttl("1m") == 60
    assert period_ttl("5m") == 60
    assert period_ttl("15m") == 900
    assert period_ttl("60m") == 3600


# =========================================================================== #
# 4) fetcher 接入：缓存命中跳过下游 + use_cache 开关 + count 截断
# =========================================================================== #
def test_fetcher_cache_hit_skips_provider():
    f = StockDataFetcher(enable_name_resolution=False)
    f._intraday_cache = IntradayCache(StockCacheManager())
    calls = {"n": 0}
    sample = _df(
        [{"datetime": pd.Timestamp("2026-07-17 09:30"), "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10}]
    )

    def fake_get_intraday(resolved, period, days, count):
        calls["n"] += 1
        return sample, None

    f.provider_manager.get_intraday = fake_get_intraday
    # 第一次：未命中，调用下游
    df1, _, _ = f.fetch_intraday("600519", period="5m", days=2)
    assert calls["n"] == 1
    # 第二次：5m TTL=60s，命中，不再调用下游
    df2, _, _ = f.fetch_intraday("600519", period="5m", days=2)
    assert calls["n"] == 1
    assert df2 is not None and len(df2) == 1
    # use_cache=False：每次都调用下游
    f.fetch_intraday("600519", period="5m", days=2, use_cache=False)
    assert calls["n"] == 2


def test_fetcher_cache_count_truncate():
    f = StockDataFetcher(enable_name_resolution=False)
    f._intraday_cache = IntradayCache(StockCacheManager())
    sample = _df(
        [
            {"datetime": pd.Timestamp("2026-07-17 09:30"), "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10},
            {"datetime": pd.Timestamp("2026-07-17 09:31"), "open": 2, "high": 3, "low": 2, "close": 3, "volume": 20},
            {"datetime": pd.Timestamp("2026-07-17 09:32"), "open": 3, "high": 4, "low": 3, "close": 4, "volume": 30},
        ]
    )
    f.provider_manager.get_intraday = lambda *a, **k: (sample, None)
    df, _, _ = f.fetch_intraday("600519", period="5m", days=2, count=2)
    assert len(df) == 2
    assert df.iloc[-1]["datetime"] == pd.Timestamp("2026-07-17 09:32")


if __name__ == "__main__":
    test_stock_cache_manager_dynamic_ttl()
    test_merge_by_datetime_keeps_latest_and_union()
    test_intraday_cache_ttl_and_merge()
    test_period_ttl_values()
    test_fetcher_cache_hit_skips_provider()
    test_fetcher_cache_count_truncate()
    print("test_cache OK")
