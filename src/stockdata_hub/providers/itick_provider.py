#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iTick 全球金融数据 Provider（覆盖 A股 / 港股 / 美股 / 外汇 / 加密货币）。

特点：
- 覆盖全球 50+ 交易所。
- 支持实时报价、历史 K线、分钟数据。
- 需要 API Token（环境变量 ``ITICK_API_TOKEN`` 或构造参数 ``api_token``）。
- 免费套餐有频率限制（约 5 次/分钟）。

依赖（可选 extra ``itick``）：``itick-sdk``。未安装或缺少 Token 时
``can_handle`` 返回 ``False``，管理器跳过。
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import pandas as pd

from ..core import DataProvider

logger = logging.getLogger(__name__)

try:
    from itick.sdk import Client

    ITICK_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖可选
    ITICK_AVAILABLE = False
    Client = None  # type: ignore[assignment]
    logger.debug("itick-sdk 未安装，iTick Provider 不可用。安装: pip install itick-sdk")


class ItickProvider(DataProvider):
    """iTick 全球行情 Provider。"""

    MARKET_MAP = {
        "sh": "SH", "SH": "SH", "上海": "SH",
        "sz": "SZ", "SZ": "SZ", "深圳": "SZ",
        "bj": "BJ", "BJ": "BJ", "北交所": "BJ",
        "hk": "HK", "HK": "HK", "港股": "HK",
        "us": "US", "US": "US", "美股": "US",
    }

    def __init__(self, api_token: Optional[str] = None) -> None:
        self.name = "iTick全球行情"
        self.priority = 3
        self._client = None
        self._can_handle_cache: set = set()
        self._api_token = api_token or os.environ.get("ITICK_API_TOKEN", "")

        if ITICK_AVAILABLE and self._api_token:
            try:
                self._client = Client(self._api_token)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"iTick 客户端初始化失败: {e}")

    def _get_region(self, symbol: str) -> Tuple[str, str]:
        symbol = symbol.strip().upper()
        if symbol.startswith("SH"):
            return "SH", symbol[2:]
        if symbol.startswith("SZ"):
            return "SZ", symbol[2:]
        if symbol.startswith("BJ"):
            return "BJ", symbol[2:]
        if symbol.startswith("HK"):
            return "HK", symbol[2:]
        if symbol.isdigit():
            if len(symbol) == 5:
                return "HK", symbol
            if len(symbol) == 6:
                if symbol.startswith(("6", "9")):
                    return "SH", symbol
                if symbol.startswith("8"):
                    return "BJ", symbol
                return "SZ", symbol
            if len(symbol) <= 4:
                return "US", symbol
        return "US", symbol

    def can_handle(self, symbol: str) -> bool:
        if not ITICK_AVAILABLE or self._client is None:
            return False
        if symbol in self._can_handle_cache:
            return True
        region, _ = self._get_region(symbol)
        if region in ("SH", "SZ", "BJ", "HK", "US"):
            self._can_handle_cache.add(symbol)
            return True
        return False

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if not ITICK_AVAILABLE:
            return None, "itick-sdk 未安装"
        if self._client is None:
            return None, "iTick 客户端未初始化（缺少 API Token）"

        region, code = self._get_region(symbol)
        try:
            kline_data = self._client.get_stock_kline(region, code, 2, days)
            if not kline_data:
                return None, "iTick 返回空数据"
            records = [
                {
                    "date": item.get("time", ""),
                    "open": float(item.get("open", 0)),
                    "high": float(item.get("high", 0)),
                    "low": float(item.get("low", 0)),
                    "close": float(item.get("close", 0)),
                    "volume": float(item.get("volume", 0)),
                }
                for item in kline_data
            ]
            df = pd.DataFrame(records)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            logger.info(f"iTick 数据获取成功: {symbol} ({region}/{code}) {len(df)} 条")
            return df, None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"iTick 获取失败: {symbol} - {e}")
            return None, f"iTick 获取失败: {e}"

    def get_provider_info(self) -> dict:  # type: ignore[override]
        info = super().get_provider_info()
        info.update(
            {
                "available": ITICK_AVAILABLE and self._client is not None,
                "token_configured": bool(self._api_token),
            }
        )
        return info
