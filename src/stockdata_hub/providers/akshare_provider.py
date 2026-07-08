#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于 akshare 的内置 Provider（A股 / ETF / 港股 / 通用聚合）。

akshare 为本库的**可选依赖**（extra ``akshare``）。本模块全程延迟导入 akshare：
未安装时 Provider 的 ``can_handle`` 直接返回 ``False``，管理器会跳过它，
不影响其它数据源。

成交量单位：akshare 返回的 ``成交量`` 均为「手」，符合统一契约，无需换算。
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import pandas as pd

from ..code_utils import StockCodeNormalizer
from ..core import DataProvider

logger = logging.getLogger(__name__)


def _require_akshare():
    try:
        import akshare as ak  # noqa: F401
        return ak
    except ImportError:  # pragma: no cover - 依赖可选
        return None


class AStockProvider(DataProvider):
    """A股数据 Provider（akshare.stock_zh_a_hist，前复权）。"""

    def __init__(self) -> None:
        self.name = "A股(akshare)"
        self.priority = 6

    def can_handle(self, symbol: str) -> bool:
        if _require_akshare() is None:
            return False
        mt = StockCodeNormalizer.get_market_type(symbol)
        return mt == "A"

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        ak = _require_akshare()
        if ak is None:
            return None, "akshare 未安装"
        try:
            start = (pd.Timestamp.now() - pd.Timedelta(days=days + 60)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="qfq", start_date=start)
            if df is None or df.empty:
                return None, "未找到匹配的A股数据"
            return df, None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"A股数据获取失败: {e}")
            return None, f"A股数据获取失败: {str(e)}"


class ETFProvider(DataProvider):
    """ETF 数据 Provider（优先 fund_etf_hist_em，回退 fund_etf_hist_sina）。"""

    def __init__(self) -> None:
        self.name = "ETF(akshare)"
        self.priority = 4

    def can_handle(self, symbol: str) -> bool:
        if _require_akshare() is None:
            return False
        mt = StockCodeNormalizer.get_market_type(symbol)
        return mt == "ETF"

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        ak = _require_akshare()
        if ak is None:
            return None, "akshare 未安装"
        try:
            df = ak.fund_etf_hist_em(symbol=symbol)
            if df is not None and not df.empty:
                return df, None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"fund_etf_hist_em 失败: {e}")

        for symbol_format in (f"sz{symbol}", f"sh{symbol}"):
            try:
                df = ak.fund_etf_hist_sina(symbol=symbol_format)
                if df is not None and not df.empty:
                    return df, None
            except Exception as e:  # noqa: BLE001
                logger.debug(f"{symbol_format} 获取失败: {e}")
        return None, "未找到匹配的ETF数据"


class HKStockProvider(DataProvider):
    """港股数据 Provider（akshare.stock_hk_hist，前复权）。"""

    def __init__(self) -> None:
        self.name = "港股(akshare)"
        self.priority = 5

    def can_handle(self, symbol: str) -> bool:
        if _require_akshare() is None:
            return False
        mt = StockCodeNormalizer.get_market_type(symbol)
        return mt == "HK"

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        ak = _require_akshare()
        if ak is None:
            return None, "akshare 未安装"
        try:
            start = (pd.Timestamp.now() - pd.Timedelta(days=days + 60)).strftime("%Y%m%d")
            df = ak.stock_hk_hist(symbol=symbol, period="daily", adjust="qfq", start_date=start)
            if df is None or df.empty:
                return None, "未找到匹配的港股数据"
            return df, None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"港股数据获取失败: {e}")
            return None, f"港股数据获取失败: {str(e)}"


class UniversalStockProvider(DataProvider):
    """
    通用兜底 Provider：不区分市场，依次尝试 A股 / ETF / 港股。

    作为所有内置源都无法命中时的最后兜底（优先级最低）。
    """

    def __init__(self) -> None:
        self.name = "通用(akshare)"
        self.priority = 10
        self._cached = {
            "a_stock": AStockProvider(),
            "etf": ETFProvider(),
            "hk_stock": HKStockProvider(),
        }

    def can_handle(self, symbol: str) -> bool:
        if _require_akshare() is None:
            return False
        return StockCodeNormalizer.get_market_type(symbol) is not None

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        for key in ("a_stock", "etf", "hk_stock"):
            provider = self._cached[key]
            if not provider.can_handle(symbol):
                continue
            data, error = provider.fetch_data(symbol, days)
            if data is not None:
                return data, error
        return None, "无法找到匹配的数据Provider或数据不存在"
