#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP / akshare 直接调用的「裸」Provider 集合：

- :class:`SinaStockProvider`   —— 新浪 A股历史（akshare.stock_zh_a_daily）
- :class:`TencentStockProvider`—— 腾讯 A股历史（akshare.stock_zh_a_hist_tx）
- :class:`EastMoneyStockProvider` —— 东财 A股历史（akshare.stock_zh_a_hist）
- :class:`EastMoneyAlternativeProvider` —— 直连东方财富 K线 API（绕开 akshare bug）

前三个依赖 akshare（extra ``akshare``，延迟导入）；最后一个仅依赖 ``requests``
（随核心安装，无需额外依赖）。

成交量单位：上述源返回的成交量均为「手」，符合统一契约。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
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


class _AStockHttpProvider(DataProvider):
    """A股 HTTP Provider 的公共基类（6位 A股，排除 ETF）。"""

    def can_handle(self, symbol: str) -> bool:
        if _require_akshare() is None and not self._needs_akshare():
            return False
        if self._needs_akshare() and _require_akshare() is None:
            return False
        return StockCodeNormalizer.get_market_type(symbol) == "A"

    def _needs_akshare(self) -> bool:  # pragma: no cover - 子类覆盖
        return True


class SinaStockProvider(_AStockHttpProvider):
    """新浪 A股历史数据 Provider。"""

    def __init__(self) -> None:
        self.name = "新浪A股"
        self.priority = 3

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        ak = _require_akshare()
        if ak is None:
            return None, "akshare 未安装"

        formatted = self._format_symbol(symbol)
        try:
            df = ak.stock_zh_a_daily(symbol=formatted, adjust="qfq")
        except Exception as e:  # noqa: BLE001
            logger.error(f"新浪数据获取失败: {e}")
            return None, str(e)

        if df is None or df.empty:
            return None, "新浪数据为空"

        df.columns = df.columns.str.lower()
        if "date" not in df.columns and df.index.name:
            df = df.reset_index()
        elif "date" not in df.columns:
            df = df.reset_index()

        required = ["open", "close", "high", "low", "volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            return None, f"新浪数据缺少必要列: {missing}"

        data = df[["date", "open", "close", "high", "low", "volume"]].copy()
        data = data.dropna()
        if not pd.api.types.is_datetime64_any_dtype(data["date"]):
            data["date"] = pd.to_datetime(data["date"], errors="coerce")
            data = data.dropna(subset=["date"])
        data = data.sort_values("date").tail(days)
        if data.empty:
            return None, "处理后数据为空"
        logger.info(f"新浪数据获取成功: {len(data)} 条")
        return data, None

    @staticmethod
    def _format_symbol(symbol: str) -> str:
        if symbol.startswith(("000", "002", "300")):
            return f"sz{symbol}"
        if symbol.startswith(("600", "601", "603", "688")):
            return f"sh{symbol}"
        return symbol


class TencentStockProvider(_AStockHttpProvider):
    """腾讯 A股历史数据 Provider。"""

    def __init__(self) -> None:
        self.name = "腾讯A股"
        self.priority = 4

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        ak = _require_akshare()
        if ak is None:
            return None, "akshare 未安装"

        clean = self._format_symbol(symbol)
        try:
            df = ak.stock_zh_a_hist_tx(symbol=clean, adjust="qfq")
        except Exception as e:  # noqa: BLE001
            logger.error(f"腾讯数据获取失败: {e}")
            return None, str(e)

        if df is None or df.empty:
            return None, "腾讯数据为空"

        df.columns = df.columns.str.lower()
        if "amount" in df.columns and "volume" not in df.columns:
            df = df.rename(columns={"amount": "volume"})
        if "date" not in df.columns:
            df = df.reset_index()

        required = ["open", "close", "high", "low", "volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            return None, f"腾讯数据缺少必要列: {missing}"

        data = df[["date", "open", "close", "high", "low", "volume"]].copy()
        data = data.dropna()
        if not pd.api.types.is_datetime64_any_dtype(data["date"]):
            data["date"] = pd.to_datetime(data["date"], errors="coerce")
            data = data.dropna(subset=["date"])
        data = data.sort_values("date").tail(days)
        if data.empty:
            return None, "处理后数据为空"
        logger.info(f"腾讯数据获取成功: {len(data)} 条")
        return data, None

    @staticmethod
    def _format_symbol(symbol: str) -> str:
        if symbol.startswith(("000", "002", "300")):
            return f"sz{symbol}"
        return f"sh{symbol}"


class EastMoneyStockProvider(_AStockHttpProvider):
    """东财 A股历史数据 Provider。"""

    def __init__(self) -> None:
        self.name = "东财A股"
        self.priority = 6

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
                return None, "东财数据为空"
            return df, None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"东财数据获取失败: {e}")
            return None, f"东财数据获取失败: {str(e)}"


class EastMoneyAlternativeProvider(DataProvider):
    """
    直连东方财富 K线 API（绕开 akshare 的已知 bug）。

    仅依赖 ``requests``，返回 A股日线；成交量单位为「手」。
    """

    def __init__(self) -> None:
        self.name = "东财替代"
        self.priority = 7
        self.session = __import__("requests").Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://fund.eastmoney.com/",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }
        )

    def can_handle(self, symbol: str) -> bool:
        if len(symbol) != 6:
            return False
        return symbol.startswith(("00", "60", "30"))

    def _get_security_id(self, symbol: str) -> str:
        if symbol.startswith("6"):
            return f"1.{symbol}"
        if symbol.startswith(("0", "3")):
            return f"0.{symbol}"
        return f"1.{symbol}"

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=max(days, 30))).strftime("%Y-%m-%d")
        security_id = self._get_security_id(symbol)
        params = {
            "secid": security_id,
            "type": "RA",
            "fund_t": "all",
            "sort_type": "DEFAULT",
            "style": "all",
            "index": "all",
            "size": 3000,
            "sdate": start_date,
            "edate": end_date,
            "fields": "1,2,3,4,5,6",
        }
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("data"):
                return None, "东财替代接口无数据返回"
            klines = data["data"].get("klines", [])
            if not klines:
                return None, "东财替代接口无k线数据"

            records = []
            for kline in klines:
                parts = kline.split(",")
                if len(parts) < 6:
                    continue
                records.append(
                    {
                        "date": parts[0],
                        "open": float(parts[1] or 0),
                        "close": float(parts[2] or 0),
                        "high": float(parts[3] or 0),
                        "low": float(parts[4] or 0),
                        "volume": float(parts[5] or 0),
                    }
                )
            if not records:
                return None, "东财替代接口解析数据为空"
            df = pd.DataFrame(records)
            df = df.tail(days).reset_index(drop=True)
            logger.info(f"东财替代接口成功获取 {len(df)} 条: {symbol}")
            return df, None
        except Exception as e:  # noqa: BLE001
            logger.error(f"东财替代接口错误 - {str(e)}")
            return None, f"东财替代接口错误: {str(e)}"
