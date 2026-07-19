#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新浪（Sina）分钟 K线 Provider（A股 / ETF 最后兜底源）。

底层对标 akshare ``stock_zh_a_minute``：直连
``quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData``（akshare 同款
host；``money.finance.sina.com.cn`` 对 ``scale=1`` 会返回 null，故用此 host），
``symbol`` 带 ``sh/sz`` 前缀，``scale`` = 分钟数。仅依赖 ``requests``。

限流：令牌桶限流器（设计 §6.5，源名 ``新浪(min_kline)``，min_interval=0.5s +
抖动），封 IP 防护。仅作最后兜底（优先级最低）。

成交量单位：新浪该接口返回 ``volume`` 为「手」，符合统一分钟契约。
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import pandas as pd

from ..code_utils import StockCodeNormalizer
from ..core import DataProvider
from ..rate_limit import get_rate_limiter

logger = logging.getLogger(__name__)

try:  # requests 是核心依赖，但允许缺失时优雅降级
    import requests

    REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖可选
    requests = None  # type: ignore[assignment]
    REQUESTS_AVAILABLE = False
    logger.debug("requests 未安装，新浪分钟接口不可用。安装: pip install requests")

# period -> 新浪 scale（设计 §3.1，字符串）
_KTYPE = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
# 每交易日约根数（用于把「日历日 days」估算为 datalen 拉取条数）
_PER_DAY_BARS = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "60m": 4}

_SOURCE_NAME = "新浪(min_kline)"
_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)


class SinaMinuteProvider(DataProvider):
    """新浪分钟 K线 Provider（A股 / ETF），最后兜底源。"""

    supports_periods = {"1m", "5m", "15m", "30m", "60m"}

    def __init__(self) -> None:
        self.name = "新浪分钟"
        self.priority = 3  # 设计 §5：最后兜底
        self._limiter = get_rate_limiter(_SOURCE_NAME)
        if REQUESTS_AVAILABLE:
            self._session = requests.Session()
            self._session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36",
                    "Referer": "https://finance.sina.com.cn/",
                }
            )
        else:
            self._session = None

    @staticmethod
    def _format_symbol(symbol: str) -> str:
        """新浪分钟 symbol 前缀：沪市(6/5 开头) -> sh，深市(0/3/1 开头) -> sz。"""
        if symbol.startswith(("6", "5")):
            return f"sh{symbol}"
        if symbol.startswith(("0", "3", "1")):
            return f"sz{symbol}"
        return symbol

    def can_handle(self, symbol: str) -> bool:
        if not REQUESTS_AVAILABLE or self._session is None:
            return False
        if not (symbol.isdigit() and len(symbol) == 6):
            return False
        return StockCodeNormalizer.get_market_type(symbol) in ("A", "ETF")

    def can_handle_request(self, symbol: str, days: int = 1, period: str = "1m") -> bool:
        if period not in self.supports_periods:
            return False
        return self.can_handle(symbol)

    def fetch_data(
        self, symbol: str, days: int = 30, period: str = "1m"
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if not REQUESTS_AVAILABLE or self._session is None:
            return None, "requests 未安装"
        if period not in self.supports_periods:
            return None, f"不支持的 period: {period}"

        scale = _KTYPE.get(period, "1")
        per_day = _PER_DAY_BARS.get(period, 240)
        datalen = per_day * max(int(days), 1) + 10
        params = {
            "symbol": self._format_symbol(symbol),
            "scale": scale,
            "ma": "no",
            "datalen": datalen,
        }

        # 令牌桶限流（串行 + 抖动），封 IP 防护
        self._limiter.acquire()

        last_err: Optional[str] = None
        for attempt in range(2):  # 瞬时网络错误重试 1 次（设计 §6.3）
            try:
                resp = self._session.get(_KLINE_URL, params=params, timeout=10)
                if resp.status_code in (429, 403):
                    return None, f"新浪分钟被限流/拒绝: HTTP {resp.status_code}"
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:  # noqa: BLE001
                last_err = f"新浪分钟请求异常: {e}"
                logger.warning(f"新浪分钟请求失败（第 {attempt + 1} 次）: {symbol} - {e}")
        else:
            return None, last_err or "新浪分钟请求失败"

        if not data:
            return None, "新浪分钟接口无数据返回"
        if not isinstance(data, list):
            return None, "新浪分钟接口返回格式异常"

        records = []
        for item in data:
            try:
                rec = {
                    "datetime": item.get("day"),
                    "open": float(item.get("open") or 0),
                    "high": float(item.get("high") or 0),
                    "low": float(item.get("low") or 0),
                    "close": float(item.get("close") or 0),
                    "volume": float(item.get("volume") or 0),
                }
                records.append(rec)
            except (ValueError, TypeError):
                continue
        if not records:
            return None, "新浪分钟解析数据为空"
        df = pd.DataFrame(records)
        logger.info(f"新浪分钟获取成功: {symbol} {period} {len(df)} 条")
        return df, None

    def get_provider_info(self) -> dict:  # type: ignore[override]
        info = super().get_provider_info()
        info.update(
            {
                "available": REQUESTS_AVAILABLE and self._session is not None,
                "supports_periods": sorted(self.supports_periods),
                "rate_limit": _SOURCE_NAME,
            }
        )
        return info
