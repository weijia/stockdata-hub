#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stockdata_hub —— 统一的多源股票数据接口库。

把「akshare / mootdx / 腾讯 / 新浪 / 东财 / openstockdata / iTick」等参差不齐的
数据源，收敛为一套 **可插拔、按优先级兜底、统一返回契约** 的接口。

快速开始::

    from stockdata_hub import StockDataFetcher

    fetcher = StockDataFetcher()
    df, reason, code = fetcher.fetch_stock_data("600519", days=30)
    if df is not None:
        print(df.tail())          # 统一契约 DataFrame
        print(fetcher.get_last_used_provider())   # 例如 '通达信TCP(mootdx)'

想自定义数据源组合 / 新增 Provider？见 README 与文档 ``docs/add_provider.md``。
"""
from __future__ import annotations

__version__ = "0.1.1"

# ---- 核心 ----
from .core import (
    DataProvider,
    DataProviderManager,
    NoProviderError,
    ProviderFetchError,
    StockDataError,
    get_default_manager,
    create_provider_manager,
    retry_on_failure,
)

# ---- 代码工具 ----
from .code_utils import (
    StockCodeNormalizer,
    StockCodeValidator,
    clean_stock_code,
    validate_and_normalize_stock_code,
    validate_stock_code,
)

# ---- 归一化契约 ----
from .normalization import (
    CANONICAL_COLUMNS,
    VOLUME_SHARE_TO_LOT,
    normalize_ohlcv,
    to_lot,
)

# ---- 缓存 ----
from .cache import StockCacheManager, get_cache_manager

# ---- 门面 ----
from .fetcher import StockDataFetcher

# ---- Provider 类（便于直接 import）----
from .providers import (
    AStockProvider,
    EastMoneyAlternativeProvider,
    EastMoneyStockProvider,
    ETFProvider,
    FastTencentProvider,
    HKStockProvider,
    ItickProvider,
    MootdxProvider,
    OpenStockDataProvider,
    SinaStockProvider,
    TencentStockProvider,
    UniversalStockProvider,
    register_builtin_providers,
)
from .name_provider import StockNameProvider, get_name_provider

__all__ = [
    "__version__",
    # 核心
    "DataProvider",
    "DataProviderManager",
    "get_default_manager",
    "create_provider_manager",
    "retry_on_failure",
    "StockDataError",
    "NoProviderError",
    "ProviderFetchError",
    # 代码工具
    "StockCodeNormalizer",
    "StockCodeValidator",
    "validate_and_normalize_stock_code",
    "validate_stock_code",
    "clean_stock_code",
    # 归一化
    "normalize_ohlcv",
    "CANONICAL_COLUMNS",
    "VOLUME_SHARE_TO_LOT",
    "to_lot",
    # 缓存
    "StockCacheManager",
    "get_cache_manager",
    # 门面
    "StockDataFetcher",
    # Provider
    "AStockProvider",
    "ETFProvider",
    "HKStockProvider",
    "UniversalStockProvider",
    "FastTencentProvider",
    "SinaStockProvider",
    "TencentStockProvider",
    "EastMoneyStockProvider",
    "EastMoneyAlternativeProvider",
    "MootdxProvider",
    "OpenStockDataProvider",
    "ItickProvider",
    "register_builtin_providers",
    "StockNameProvider",
    "get_name_provider",
]
