#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mootdx（通达信）TCP 高速行情 Provider。

核心优势：
- TCP 二进制协议直连通达信服务器，延迟 < 50ms，不封 IP，无需注册。
- K线获取速度比 HTTP 接口快 50–100 倍。

依赖（可选 extra ``mootdx``）：``mootdx``（以及可选的 ``pytdx`` 用于服务器测速）。
未安装时 ``can_handle`` 返回 ``False``，管理器自动跳过。

成交量单位：mootdx 返回 ``volume`` 为「手」，符合统一契约。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional, Tuple

import pandas as pd

from ..code_utils import StockCodeNormalizer
from ..core import DataProvider

logger = logging.getLogger(__name__)

try:
    from mootdx.quotes import Quotes

    MOOTDX_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖可选
    MOOTDX_AVAILABLE = False
    Quotes = None  # type: ignore[assignment]
    logger.debug("mootdx 未安装，TCP 高速行情不可用。安装: pip install mootdx")


class MootdxProvider(DataProvider):
    """通达信 TCP 高速行情 Provider（A股 / ETF 日线）。"""

    def __init__(self, best_server: Optional[Tuple[str, int]] = None) -> None:
        self.name = "通达信TCP(mootdx)"
        self.priority = 1  # 第二优先级：K线速度最快
        self._client = None
        self._best_server = None
        self._can_handle_cache: set = set()

        if MOOTDX_AVAILABLE:
            self._quick_start()
            if best_server:
                # 已知最快服务器（外部缓存注入）：直接连接，跳过 select_best_ip
                # 测速（耗时且每次进程启动都要重跑），显著加快冷启动。
                self._best_server = best_server
                self._switch_to_best_server()
            else:
                self._start_async_bench()

    def _quick_start(self) -> None:
        try:
            self._client = Quotes.factory(market="std", timeout=5)
            logger.info("mootdx TCP 客户端快速启动成功（A股/ETF）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"mootdx A股客户端快速启动失败: {e}")
            self._client = None

    def _start_async_bench(self) -> None:
        t = threading.Thread(target=self._async_find_best_server, daemon=True, name="mootdx-bestip")
        t.start()

    def _async_find_best_server(self) -> None:  # pragma: no cover - 运行时测速
        try:
            from pytdx.util.best_ip import select_best_ip

            best = select_best_ip()
            if best and len(best) == 2:
                self._best_server = best
                self._switch_to_best_server()
                return
        except Exception as e:  # noqa: BLE001
            logger.debug(f"select_best_ip 失败: {e}")

        known_servers = [
            ("202.108.253.131", 7709),
            ("114.80.149.92", 7709),
            ("123.125.108.90", 7709),
            ("221.194.181.81", 7709),
            ("202.108.253.139", 7709),
        ]
        best_latency = float("inf")
        best_server = None
        for host, port in known_servers:
            try:
                test_client = Quotes.factory(market="std", server=(host, port), timeout=3)
                test_client.bars(symbol="000001", category=4, offset=1)
                latency = time.time() - time.time()  # 占位，真实测速在调用时
                if latency < best_latency:
                    best_latency = latency
                    best_server = (host, port)
                test_client.close()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"服务器 {host}:{port} 不可用: {e}")
                continue
        if best_server:
            self._best_server = best_server
            self._switch_to_best_server()

    def _switch_to_best_server(self) -> None:  # pragma: no cover - 运行时
        if not self._best_server or not MOOTDX_AVAILABLE:
            return
        try:
            new_client = Quotes.factory(market="std", server=self._best_server, timeout=5)
            new_client.bars(symbol="000001", category=4, offset=1)
            if self._client:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
            self._client = new_client
        except Exception as e:  # noqa: BLE001
            logger.warning(f"mootdx 切换最优服务器失败: {e}")

    def can_handle(self, symbol: str) -> bool:
        if not MOOTDX_AVAILABLE or self._client is None:
            return False
        if not (symbol.isdigit() and len(symbol) == 6):
            return False
        # 仅支持 A股/ETF（港股扩展市场接口已失效）
        mt = StockCodeNormalizer.get_market_type(symbol)
        return mt in ("A", "ETF")

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if not MOOTDX_AVAILABLE:
            return None, "mootdx 未安装"
        if self._client is None:
            return None, "mootdx 客户端未初始化"

        # 长驻 TCP 连接空闲后，首次请求可能偶发返回空（连接已失效但未被标记断开）。
        # 遇到空结果/异常时重建连接重试一次，避免单点抖动直接降级到其它数据源。
        df = None
        for attempt in range(2):
            try:
                client = self._client
                # frequency=9 表示日线
                df = client.bars(symbol=symbol, frequency=9, offset=days)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"mootdx 请求异常（第 {attempt + 1} 次）: {symbol} - {e}")
                df = None

            if df is not None and not df.empty:
                break
            if attempt == 0:
                logger.info(f"mootdx {symbol} 返回空，重建连接重试")
                self._quick_start()
            else:
                return None, "mootdx 返回空数据"

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
                "available": MOOTDX_AVAILABLE and self._client is not None,
                "best_server": f"{self._best_server[0]}:{self._best_server[1]}"
                if self._best_server
                else None,
            }
        )
        return info
