#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东方财富（EastMoney）分钟 K线 Provider（A股 / ETF 兜底源）。

底层直连 ``push2his.eastmoney.com`` 的 K线接口（与 :class:`EastMoneyAlternativeProvider`
同域，但追加 ``klt`` 周期参数），仅依赖 ``requests``（随核心安装），**不经 akshare**。

限流：所有请求经令牌桶限流器（设计 §6.5，源名 ``东方财富(push2his)``，
min_interval=1.0s + 抖动），单进程共享，封 IP 防护。

成交量单位：东财该接口返回 ``volume`` 为「手」，符合统一分钟契约。不复权（fqt=0）。
"""
from __future__ import annotations

import logging
from datetime import datetime
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
    logger.debug("requests 未安装，东财分钟接口不可用。安装: pip install requests")

# period -> 东财 klt（设计 §3.1）
_KLT = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
# 每交易日约根数（用于把「日历日 days」估算为 lmt 拉取条数）
_PER_DAY_BARS = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "60m": 4}

_SOURCE_NAME = "东方财富(push2his)"
_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


class EastMoneyMinuteProvider(DataProvider):
    """东方财富分钟 K线 Provider（A股 / ETF），mootdx 之后的历史深兜底源。"""

    supports_periods = {"1m", "5m", "15m", "30m", "60m"}

    def __init__(self) -> None:
        self.name = "东财分钟"
        self.priority = 2  # 设计 §5：mootdx(1) 之后，新浪(3) 之前
        self._limiter = get_rate_limiter(_SOURCE_NAME)
        if REQUESTS_AVAILABLE:
            self._session = requests.Session()
            self._session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36",
                    "Referer": "https://quote.eastmoney.com/",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                }
            )
        else:
            self._session = None

    @staticmethod
    def _get_security_id(symbol: str) -> str:
        """东财 secid：沪市(6 开头) -> 1.{code}，深市(0/3 开头) -> 0.{code}。"""
        if symbol.startswith("6"):
            return f"1.{symbol}"
        return f"0.{symbol}"

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

        klt = _KLT.get(period, 1)
        per_day = _PER_DAY_BARS.get(period, 240)
        lmt = per_day * max(int(days), 1) + 10
        secid = self._get_security_id(symbol)
        params = {
            "secid": secid,
            "klt": klt,
            "fqt": 0,  # 不复权
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "beg": "0",
            "end": "20500101",  # 取到最新
            "lmt": lmt,
            "ut": "fa5fd1943c7b386f172d6893dbfba1a9",
        }

        # 令牌桶限流（串行 + 抖动），封 IP 防护
        self._limiter.acquire()

        last_err: Optional[str] = None
        for attempt in range(2):  # 瞬时网络错误重试 1 次（设计 §6.3）
            try:
                resp = self._session.get(_KLINE_URL, params=params, timeout=10)
                if resp.status_code in (429, 403):
                    # 限流类响应不重试，直接交上层回退（设计 §6.3）
                    return None, f"东财分钟被限流/拒绝: HTTP {resp.status_code}"
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:  # noqa: BLE001
                last_err = f"东财分钟请求异常: {e}"
                logger.warning(f"东财分钟请求失败（第 {attempt + 1} 次）: {symbol} - {e}")
        else:
            return None, last_err or "东财分钟请求失败"

        if not data.get("data"):
            return None, "东财分钟接口无数据返回"
        klines = data["data"].get("klines", [])
        if not klines:
            return None, "东财分钟接口无 k 线数据"

        records = []
        for kline in klines:
            parts = kline.split(",")
            if len(parts) < 6:
                continue
            try:
                rec = {
                    "datetime": parts[0],
                    "open": float(parts[1] or 0),
                    "close": float(parts[2] or 0),
                    "high": float(parts[3] or 0),
                    "low": float(parts[4] or 0),
                    "volume": float(parts[5] or 0),
                }
                if len(parts) >= 7:
                    rec["amount"] = float(parts[6] or 0)
                records.append(rec)
            except ValueError:
                continue
        if not records:
            return None, "东财分钟解析数据为空"
        df = pd.DataFrame(records)
        logger.info(f"东财分钟获取成功: {symbol} {period} {len(df)} 条")
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
