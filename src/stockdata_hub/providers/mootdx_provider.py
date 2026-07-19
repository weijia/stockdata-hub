#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mootdx（通达信）TCP 高速行情 Provider（A股 / ETF 日线）。

连接管理已抽取到 :mod:`stockdata_hub.mootdx_client` 的共享单例，与分钟 Provider
共用同一条 TCP（单连接串行、非线程安全安全）。本文件只保留日线（frequency=9）
的抓取逻辑。

依赖（可选 extra ``mootdx``）：``mootdx``（以及可选的 ``pytdx`` 用于服务器测速）。
未安装时 ``can_handle`` 返回 ``False``，管理器自动跳过。

成交量单位：mootdx 返回 ``volume`` 为「手」，符合统一契约。
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


class MootdxProvider(DataProvider):
    """通达信 TCP 高速行情 Provider（A股 / ETF 日线）。"""

    def __init__(self, best_server: Optional[Tuple[str, int]] = None) -> None:
        self.name = "通达信TCP(mootdx)"
        self.priority = 1  # 第二优先级：K线速度最快
        self._mgr = get_tdx_client()
        if best_server:
            # 已知最快服务器（外部缓存注入）：直接连接，跳过 select_best_ip 测速
            self._mgr.switch_to_best_server(best_server)

    @staticmethod
    def _is_timeout_err(e: Exception) -> bool:
        """判断异常是否为网络超时（mootdx/pytdx 底层为 socket.timeout）。"""
        if isinstance(e, (socket.timeout, TimeoutError)):
            return True
        msg = str(e).lower()
        return "timed out" in msg or "timeout" in msg

    def can_handle(self, symbol: str) -> bool:
        if not MOOTDX_AVAILABLE or self._mgr.client is None:
            return False
        if not (symbol.isdigit() and len(symbol) == 6):
            return False
        # 仅支持 A股/ETF（港股扩展市场接口已失效）
        mt = StockCodeNormalizer.get_market_type(symbol)
        return mt in ("A", "ETF")

    def fetch_data(
        self, symbol: str, days: int = 30, period: str = "1d"
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if not MOOTDX_AVAILABLE:
            return None, "mootdx 未安装"
        if period != "1d":
            # 本 Provider 仅处理日线；分钟由 MootdxMinuteProvider 负责。
            return None, f"mootdx 日线 Provider 不支持 period: {period}"
        mgr = self._mgr
        if mgr.client is None:
            return None, "mootdx 客户端未初始化"

        # 单 TCP 串行 + 重建均由 mgr.lock 保护；节流由调用方（server）负责。
        # 取数总预算：超时类错误绝不重试，立即失败回退（设计 §6.1/§6.3）。
        df = None
        last_err = None
        started = time.time()
        for attempt in range(2):
            try:
                if not mgr.lock.acquire(timeout=mgr.LOCK_ACQUIRE_SEC):
                    logger.warning(f"mootdx 连接锁获取超时（连接可能卡死），放弃 {symbol}")
                    return None, "mootdx 连接繁忙"
                try:
                    mgr.reconnect_if_idle()  # 空闲过久先重建，避免死连接卡顿
                    client = mgr.client
                    if client is None:
                        return None, "mootdx 客户端未初始化"
                    # frequency=9 表示日线
                    df = client.bars(symbol=symbol, frequency=9, offset=days)
                finally:
                    mgr.lock.release()
            except Exception as e:  # noqa: BLE001
                last_err = e
                elapsed = time.time() - started
                logger.warning(f"mootdx 请求异常（第 {attempt + 1} 次）: {symbol} - {e}")
                if self._is_timeout_err(e) or elapsed >= mgr.FETCH_BUDGET_SEC:
                    df = None
                    return None, f"mootdx 请求超时: {e}"
                df = None

            if df is not None and not df.empty:
                mgr.mark_ok()
                break

            # 空结果（非异常）：连接未报错但无数据
            need_reconnect = bool(last_err) or mgr.should_reconnect
            if not need_reconnect:
                logger.debug(f"mootdx {symbol} 返回空（连接健康，视为无数据，不重建）")
                return None, "mootdx 返回空数据"
            if attempt == 0:
                logger.info(f"mootdx {symbol} 连接异常/可能失效，重建连接重试")
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
            rename_map = {}
            if "datetime" in df.columns and "date" not in df.columns:
                rename_map["datetime"] = "date"
            if "vol" in df.columns and "volume" not in df.columns:
                rename_map["vol"] = "volume"
            if rename_map:
                df = df.rename(columns=rename_map)

            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            # 仅保留标准列（防御重复列）
            standard_cols = ["date", "open", "high", "low", "close", "volume"]
            available_cols = [c for c in standard_cols if c in df.columns]
            df = df[available_cols].copy()

            logger.info(f"mootdx 获取成功: {symbol} {len(df)} 条")
            return df, None
        except Exception as e:  # noqa: BLE001
            logger.error(f"mootdx 获取失败: {symbol} - {e}")
            return None, f"mootdx 获取失败: {e}"

    def get_provider_info(self) -> dict:  # type: ignore[override]
        info = super().get_provider_info()
        info.update(
            {
                "available": MOOTDX_AVAILABLE and self._mgr.client is not None,
                "best_server": f"{self._mgr.best_server[0]}:{self._mgr.best_server[1]}"
                if self._mgr.best_server
                else None,
            }
        )
        return info
