#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内置 Provider 注册表。

:func:`register_builtin_providers` 会把所有内置数据源加入管理器。每个 Provider 的
底层依赖都是**可选**的（akshare / mootdx / openstockdata / itick-sdk），缺失时该
Provider 的 ``can_handle`` 返回 ``False``，由管理器自动跳过 —— 这正是「多源兜底、
缺依赖降级」的设计。

优先级约定（越小越优先）：
    0  腾讯批量实时（仅当日快照，days=1 才生效）
    1  通达信TCP(mootdx)  —— K线最快
    2  openstockdata      —— 百度/腾讯 K线（alpha）
    3  iTick / 新浪A股
    4  腾讯A股 / ETF(akshare)
    5  港股(akshare)
    6  A股(akshare) / 东财A股
    7  东财替代
    10 通用(akshare) 兜底
"""
from __future__ import annotations

import logging
from typing import List

from ..core import DataProvider, DataProviderManager
from .akshare_provider import (
    AStockProvider,
    ETFProvider,
    HKStockProvider,
    UniversalStockProvider,
)
from .fast_tencent_provider import FastTencentProvider
from .http_provider import (
    EastMoneyAlternativeProvider,
    EastMoneyStockProvider,
    SinaStockProvider,
    TencentStockProvider,
)
from .itick_provider import ItickProvider
from .mootdx_provider import MootdxProvider
from .openstockdata_provider import (
    OpenStockDataProvider,
    OPENSTOCKDATA_AVAILABLE,
    fetch_kline,
)
from .tushare_provider import TushareProvider

logger = logging.getLogger(__name__)

# 旧 stock-cloud 兼容别名（同名不同层级，指向库内统一类）
EastMoneyDataProvider = EastMoneyStockProvider
SinaDataProvider = SinaStockProvider
TencentDataProvider = TencentStockProvider

# 默认注册顺序（含构造失败的容错）
_PROVIDER_FACTORIES = [
    FastTencentProvider,
    MootdxProvider,
    OpenStockDataProvider,
    ItickProvider,
    SinaStockProvider,
    TencentStockProvider,
    TushareProvider,
    ETFProvider,
    HKStockProvider,
    EastMoneyStockProvider,
    EastMoneyAlternativeProvider,
    UniversalStockProvider,
]


def register_builtin_providers(manager: DataProviderManager) -> None:
    """把所有内置 Provider 注册进 ``manager``（构造异常会被忽略并告警）。"""
    for factory in _PROVIDER_FACTORIES:
        try:
            provider = factory()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"构造 Provider {factory.__name__} 失败，已跳过: {e}")
            continue
        manager.add_provider(provider)


__all__: List[str] = [
    "register_builtin_providers",
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
    "OPENSTOCKDATA_AVAILABLE",
    "fetch_kline",
    "TushareProvider",
    "EastMoneyDataProvider",
    "SinaDataProvider",
    "TencentDataProvider",
]
