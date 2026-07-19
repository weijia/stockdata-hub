#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openstockdata（cn-a-stock-data）分钟 K线 Provider（设计 §3.5，可选、后置）。

复用 :func:`openstockdata.baidu_kline_with_ma`（百度股市通 K线，内部含腾讯
fallback），把 ``period`` 映射到百度 ``ktype`` 后取分钟 K线，成交量由「股」换算
为统一契约「手」。归一化交由管理器统一跑 :func:`normalization.normalize_intraday`。

依赖（可选 extra ``openstockdata``）：``cn-a-stock-data``（导入名 ``openstockdata``）。
未安装时 ``can_handle`` 返回 ``False``，管理器跳过（与其余可选源一致的降级策略）。

优先级 4：仅作分钟兜底（mootdx / 东财 / 新浪 之后，设计 §5）。

.. note::
   ``ktype`` 的分钟取值以设计 §3.1 映射为准：``5m/15m/30m/60m`` = 分钟数字符串，
   ``1m`` = ``"m"``（设计标注「待实测」；本机因百度接口反爬 + 包未安装无法联网
   实测，1m 主力仍为本地 mootdx / 新浪，此源仅作补充）。
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import pandas as pd

from ..code_utils import StockCodeNormalizer
from ..core import DataProvider
from ..normalization import VOLUME_SHARE_TO_LOT
from ..rate_limit import get_rate_limiter

logger = logging.getLogger(__name__)

try:
    from openstockdata import baidu_kline_with_ma

    OPENSTOCKDATA_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖可选
    baidu_kline_with_ma = None  # type: ignore[assignment]
    OPENSTOCKDATA_AVAILABLE = False
    logger.debug(
        "openstockdata 未安装，分钟 Provider 不可用。安装: pip install cn-a-stock-data"
    )

# period -> 百度 ktype（设计 §3.1）。日线用 "1"，故分钟不复用 "1"。
# 1m 取值设计标注「待实测」，此处按设计给出的 "m"（无法本机联网确认）。
_KTYPE = {"1m": "m", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}

_SOURCE_NAME = "openstockdata(百度)"


class OpenStockDataMinuteProvider(DataProvider):
    """openstockdata（百度）分钟 K线 Provider（A股 / ETF），可选后置兜底源。"""

    supports_periods = {"1m", "5m", "15m", "30m", "60m"}

    def __init__(self) -> None:
        self.name = "openstockdata分钟"
        self.priority = 4  # 设计 §5：分钟兜底，位于新浪之后
        self._limiter = get_rate_limiter(_SOURCE_NAME)

    def can_handle(self, symbol: str) -> bool:
        if not OPENSTOCKDATA_AVAILABLE:
            return False
        if not (symbol.isdigit() and len(symbol) == 6):
            return False
        return StockCodeNormalizer.get_market_type(symbol) in ("A", "ETF")

    def can_handle_request(
        self, symbol: str, days: int = 1, period: str = "1m"
    ) -> bool:
        if period not in self.supports_periods:
            return False
        return self.can_handle(symbol)

    def fetch_data(
        self, symbol: str, days: int = 30, period: str = "1m"
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if not OPENSTOCKDATA_AVAILABLE:
            return None, "openstockdata 未安装（pip install cn-a-stock-data）"
        if period not in self.supports_periods:
            return None, f"不支持的 period: {period}"

        ktype = _KTYPE.get(period, "5")

        # 令牌桶限流（串行 + 抖动），封 IP 防护
        self._limiter.acquire()

        try:
            raw = baidu_kline_with_ma(symbol, ktype=ktype)
        except Exception as e:  # noqa: BLE001
            logger.error(f"openstockdata 分钟调用失败: {e}")
            return None, f"openstockdata 分钟调用失败: {e}"

        if raw is None:
            return None, "openstockdata 分钟返回 None"

        df = raw.copy() if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if df.empty:
            return None, "openstockdata 分钟返回空数据"

        # 成交量单位换算：openstockdata 返回「股」 -> 统一契约「手」
        if "volume" in df.columns:
            df["volume"] = (
                pd.to_numeric(df["volume"], errors="coerce") / VOLUME_SHARE_TO_LOT
            )
        logger.info(f"openstockdata 分钟获取成功: {symbol} {period} {len(df)} 条")
        return df, None

    def get_provider_info(self) -> dict:  # type: ignore[override]
        info = super().get_provider_info()
        info.update(
            {
                "available": OPENSTOCKDATA_AVAILABLE,
                "supports_periods": sorted(self.supports_periods),
                "rate_limit": _SOURCE_NAME,
            }
        )
        return info
