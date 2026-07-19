#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mootdx（通达信）TCP 分钟 K线 Provider（A股 / ETF）。

复用 :mod:`stockdata_hub.mootdx_client` 的共享单例 TCP 连接（与日线 Provider
共用同一条连接 + 串行锁），实时、不封 IP、最快。频率映射见设计 §3.1：

    period -> mootdx frequency
    1m -> 8   5m -> 0   15m -> 1   30m -> 2   60m -> 3

注：mootdx ``frequency=7`` 是「1 分钟除权口径」，本接口统一用 ``8``（标准 1 分钟）。
成交量单位：mootdx 返回 ``volume`` 为「手」，符合统一分钟契约。
"""
from __future__ import annotations

import logging
import socket
import time
from typing import Optional, Tuple

import pandas as pd

from ..code_utils import StockCodeNormalizer
from ..core import DataProvider
from ..mootdx_client import MOOTDX_AVAILABLE, get_tdx_client

logger = logging.getLogger(__name__)

# period -> mootdx frequency（与 a-stock-data SKILL.md 实测一致）
_PERIOD_FREQ = {"1m": 8, "5m": 0, "15m": 1, "30m": 2, "60m": 3}
# 每交易日约根数（用于把「日历日 days」估算为 mootdx 的 offset 根数）
_PER_DAY_BARS = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "60m": 4}


class MootdxMinuteProvider(DataProvider):
    """通达信 TCP 分钟 K线 Provider（A股 / ETF），日线 Provider 的分钟版。"""

    # 仅声明支持分钟周期；管理器据此在日线请求时自动跳过本源（设计 §5）。
    supports_periods = {"1m", "5m", "15m", "30m", "60m"}

    def __init__(self) -> None:
        self.name = "通达信TCP(mootdx)分钟"
        self.priority = 1  # 与日线 mootdx 同优先级；分钟请求仅本源通过 can_handle_request
        self._mgr = get_tdx_client()

    @staticmethod
    def _is_timeout_err(e: Exception) -> bool:
        if isinstance(e, (socket.timeout, TimeoutError)):
            return True
        msg = str(e).lower()
        return "timed out" in msg or "timeout" in msg

    def can_handle(self, symbol: str) -> bool:
        if not MOOTDX_AVAILABLE or self._mgr.client is None:
            return False
        if not (symbol.isdigit() and len(symbol) == 6):
            return False
        mt = StockCodeNormalizer.get_market_type(symbol)
        return mt in ("A", "ETF")

    def can_handle_request(self, symbol: str, days: int = 1, period: str = "1m") -> bool:
        if period not in self.supports_periods:
            return False
        return self.can_handle(symbol)

    def fetch_data(
        self, symbol: str, days: int = 30, period: str = "1m"
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if not MOOTDX_AVAILABLE:
            return None, "mootdx 未安装"
        if period not in self.supports_periods:
            return None, f"不支持的 period: {period}"

        mgr = self._mgr
        if mgr.client is None:
            return None, "mootdx 客户端未初始化"

        freq = _PERIOD_FREQ.get(period, 8)
        per_day = _PER_DAY_BARS.get(period, 240)
        offset = max(per_day * max(int(days), 1), per_day)

        df = None
        last_err = None
        started = time.time()
        for attempt in range(2):
            try:
                if not mgr.lock.acquire(timeout=mgr.LOCK_ACQUIRE_SEC):
                    logger.warning(f"mootdx 分钟连接锁获取超时，放弃 {symbol}")
                    return None, "mootdx 连接繁忙"
                try:
                    mgr.reconnect_if_idle()
                    client = mgr.client
                    if client is None:
                        return None, "mootdx 客户端未初始化"
                    df = client.bars(symbol=symbol, frequency=freq, offset=offset)
                finally:
                    mgr.lock.release()
            except Exception as e:  # noqa: BLE001
                last_err = e
                elapsed = time.time() - started
                logger.warning(f"mootdx 分钟请求异常（第 {attempt + 1} 次）: {symbol} - {e}")
                # 超时 / 预算耗尽：立即失败回退，不重建重试（保护调用方）
                if self._is_timeout_err(e) or elapsed >= mgr.FETCH_BUDGET_SEC:
                    return None, f"mootdx 请求超时: {e}"
                df = None

            if df is not None and not df.empty:
                mgr.mark_ok()
                break

            # 空结果（非异常）：连接未报错但无数据
            if not (bool(last_err) or mgr.should_reconnect):
                logger.debug(f"mootdx 分钟 {symbol} 返回空（连接健康，视为无数据）")
                return None, "mootdx 返回空数据"
            if attempt == 0:
                logger.info(f"mootdx 分钟 {symbol} 连接异常/可能失效，重建连接重试")
                if not mgr.lock.acquire(timeout=mgr.LOCK_ACQUIRE_SEC):
                    return None, "mootdx 连接繁忙"
                try:
                    mgr.quick_start()
                finally:
                    mgr.lock.release()
                continue
            return (
                None,
                f"mootdx 请求失败: {last_err}" if last_err else "mootdx 返回空数据",
            )

        if df is None or df.empty:
            return None, "mootdx 返回空数据"

        try:
            # 列名归一：mootdx 分钟使用 'datetime' 列；个别版本可能用 'date'
            if "datetime" not in df.columns and "date" in df.columns:
                df = df.rename(columns={"date": "datetime"})
            if "vol" in df.columns and "volume" not in df.columns:
                df = df.rename(columns={"vol": "volume"})
            # datetime 解析（mootdx 分钟为 YYYYMMDDHHMM 整数/字符串，含时分秒）
            if "datetime" in df.columns:
                dt = pd.to_datetime(df["datetime"], errors="coerce")
                if dt.isna().all():
                    dt = pd.to_datetime(
                        df["datetime"].astype(str), format="%Y%m%d%H%M", errors="coerce"
                    )
                df["datetime"] = dt
            logger.info(f"mootdx 分钟获取成功: {symbol} {period} {len(df)} 条")
            return df, None
        except Exception as e:  # noqa: BLE001
            logger.error(f"mootdx 分钟获取失败: {symbol} - {e}")
            return None, f"mootdx 分钟获取失败: {e}"

    def get_provider_info(self) -> dict:  # type: ignore[override]
        info = super().get_provider_info()
        info.update(
            {
                "available": MOOTDX_AVAILABLE and self._mgr.client is not None,
                "supports_periods": sorted(self.supports_periods),
            }
        )
        return info
