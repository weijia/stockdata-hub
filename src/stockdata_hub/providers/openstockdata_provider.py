#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openstockdata（cn-a-stock-data）Provider。

openstockdata 只是「多个按源划分的抓取函数 + 共享 HTTP 会话」，*不* 提供跨源统一
Schema；其返回的列名、单位随源变化。本 Provider 在此之上做两件事：

1. 调用 ``baidu_kline_with_ma``（百度主路径 + 内部腾讯 fallback）取日 K线；
2. 把成交量由「股」换算为统一契约的「手」(``÷ VOLUME_SHARE_TO_LOT``)。

之后由管理器统一跑 :func:`stockdata_hub.normalization.normalize_ohlcv` 完成列别名、
日期、排序、MA 归并。

依赖（可选 extra ``openstockdata``）：``cn-a-stock-data``（导入名 ``openstockdata``）。
未安装时 ``can_handle`` 返回 ``False``，管理器跳过。
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import pandas as pd

from ..code_utils import StockCodeNormalizer
from ..core import DataProvider
from ..normalization import VOLUME_SHARE_TO_LOT

logger = logging.getLogger(__name__)

try:
    from openstockdata import baidu_kline_with_ma

    OPENSTOCKDATA_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖可选
    baidu_kline_with_ma = None  # type: ignore[assignment]
    OPENSTOCKDATA_AVAILABLE = False
    logger.debug("openstockdata 未安装，Provider 不可用。安装: pip install cn-a-stock-data")


class OpenStockDataProvider(DataProvider):
    """openstockdata 日 K线 Provider（百度主路径，成交量已归一到「手」）。"""

    def __init__(self) -> None:
        self.name = "openstockdata"
        self.priority = 2  # 高优先级，但在 mootdx 之后

    def can_handle(self, symbol: str) -> bool:
        if not OPENSTOCKDATA_AVAILABLE:
            return False
        mt = StockCodeNormalizer.get_market_type(symbol)
        return mt in ("A", "ETF")

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if not OPENSTOCKDATA_AVAILABLE:
            return None, "openstockdata 未安装（pip install cn-a-stock-data）"
        try:
            raw = baidu_kline_with_ma(symbol, ktype="1")
        except Exception as e:  # noqa: BLE001
            logger.error(f"openstockdata 调用失败: {e}")
            return None, f"openstockdata 调用失败: {e}"

        if raw is None:
            return None, "openstockdata 返回 None"

        df = raw.copy()
        # 成交量单位换算：openstockdata 返回「股」 -> 统一契约「手」
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce") / VOLUME_SHARE_TO_LOT
        return df, None

    def get_provider_info(self) -> dict:  # type: ignore[override]
        info = super().get_provider_info()
        info["available"] = OPENSTOCKDATA_AVAILABLE
        return info


def fetch_kline(
    symbol: str, days: int = 30
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """便捷函数：用 :class:`OpenStockDataProvider` 取日 K线，返回统一契约 ``(df, err)``。

    与旧 ``stock-cloud`` 的 ``openstockdata_adapter.fetch_kline`` 签名保持一致。
    """
    return OpenStockDataProvider().fetch_data(symbol, days)
