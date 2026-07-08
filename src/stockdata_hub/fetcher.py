#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高层门面（Facade）：``StockDataFetcher``。

对调用方屏蔽「用哪个源 / 怎么 fallback」的细节，提供最简接口：

    from stockdata_hub import StockDataFetcher

    fetcher = StockDataFetcher()
    df, reason, code = fetcher.fetch_stock_data("600519", days=30)
    if df is not None:
        print(df.tail())

返回的三元组：
    - ``df``     : 满足统一契约的 DataFrame（见 :mod:`stockdata_hub.normalization`）；
                  失败为 ``None``。
    - ``reason`` : 失败原因（成功为 ``None``）。
    - ``code``   : 实际命中的股票代码（名称解析后可能为规范化代码）。

语义：内部使用 :class:`~stockdata_hub.core.DataProviderManager` 的多源兜底链；
可选地支持「按名称取数」（依赖 akshare）。
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import pandas as pd

from .core import DataProviderManager, get_default_manager

logger = logging.getLogger(__name__)


class StockDataFetcher:
    """统一股票数据获取门面。"""

    def __init__(
        self,
        manager: Optional[DataProviderManager] = None,
        enable_name_resolution: bool = True,
    ) -> None:
        """
        Args:
            manager: 自定义 Provider 管理器；``None`` 时使用默认内置多源管理器。
            enable_name_resolution: 是否启用「股票名称 -> 代码」解析（需 akshare）。
        """
        self.provider_manager = manager or get_default_manager()
        self.enable_name_resolution = enable_name_resolution
        self.stock_name_provider = None
        self._last_used_provider: Optional[str] = None

        if enable_name_resolution:
            try:
                from .name_provider import StockNameProvider

                self.stock_name_provider = StockNameProvider()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"初始化名称提供者失败（将仅支持代码取数）: {e}")
                self.stock_name_provider = None

    def _resolve_symbol(self, symbol: str) -> str:
        """若传入的是名称，尝试解析为代码；解析失败则原样返回。"""
        if self.stock_name_provider and not symbol.isdigit():
            code = self.stock_name_provider.get_stock_code_from_name(symbol)
            if code:
                logger.info(f"名称解析: {symbol} -> {code}")
                return code
        return symbol

    def fetch_stock_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], Optional[str]]:
        """
        获取股票日 K线数据。

        Returns:
            ``(DataFrame, 失败原因, 实际代码)``。
        """
        if not symbol:
            return None, "无效的股票代码或名称", None

        resolved = self._resolve_symbol(symbol)

        df, reason = self.provider_manager.get_data(resolved, days)
        if df is not None and not df.empty:
            self._last_used_provider = self.provider_manager.get_last_used_provider()
            return df, None, resolved

        self._last_used_provider = None
        return None, reason or "无法获取股票数据", None

    def get_last_used_provider(self) -> Optional[str]:
        """返回上一次成功命中的 Provider 名称。"""
        return self._last_used_provider

    def list_providers(self):
        """列出当前所有可用 Provider 元信息。"""
        return self.provider_manager.get_provider_list()
