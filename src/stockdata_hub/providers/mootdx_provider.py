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
import socket
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
        # 单 TCP 连接锁：串行化 bars/quotes 调用与重建，保护非线程安全的单一连接。
        # 锁归属放在库内（连接真正所在处），而非调用方，避免多路径并发访问同一条 TCP。
        self._conn_lock = threading.Lock()
        self._last_ok_ts = 0.0  # 上次成功取到数据的时间，用于判断空闲连接是否失效
        # 连接空闲超过此时长（秒）且无成功记录，空结果才视为失效并重建；
        # 短于此时长则信任连接、把空结果当作「该票无数据」，不拆连接。
        self._IDLE_RECONNECT_SEC = 300
        # 取数总预算（秒）：超时类错误绝不重试，详见 fetch_data——防止把 monitor
        # 的 5s HTTP 超时顶爆（单只 K线偶发超时的根因）。
        self._FETCH_BUDGET_SEC = 4.0
        # 连接锁获取上限：mootdx 单 TCP 非线程安全必须串行，但某次调用卡在死连接
        #（socket 超时未触发，Windows TCP 重传可达 ~21s）会长期持锁，导致后续所有
        # 请求排队级联超时。拿不到锁立即放弃，交上层回退，不炸雪球。
        self._LOCK_ACQUIRE_SEC = 2.5

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
            # 重建时优先复用已测速选出的最优服务器，避免每次重建都退回到 pytdx 默认节点
            if self._best_server:
                self._client = Quotes.factory(
                    market="std", server=self._best_server, timeout=3)
            else:
                self._client = Quotes.factory(market="std", timeout=3)
            logger.info("mootdx TCP 客户端快速启动成功（A股/ETF）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"mootdx A股客户端快速启动失败: {e}")
            self._client = None

    def _reconnect_if_idle(self) -> None:
        """调用方须持 ``_conn_lock``。

        若连接曾成功取过数、且距上次成功已超过 ``_IDLE_RECONNECT_SEC``，说明长驻
        TCP 很可能已被 TDX 静默断开（空闲过久）。在真正发起请求**前**主动重建，
        避免先卡在死连接上等 TCP 重传超时（Windows 下可达 ~10-21s）才报错——这正是
        收盘后空闲数小时、monitor 的 5s/10s HTTP 超时先到而记 ``timed out`` 的根因。
        """
        if self._last_ok_ts <= 0:
            return  # 尚未成功取过数，沿用启动时的连接，不重复重建
        if self._client is not None and (
            time.time() - self._last_ok_ts > self._IDLE_RECONNECT_SEC
        ):
            logger.info(
                f"mootdx 连接空闲 {int(time.time() - self._last_ok_ts)}s，"
                "主动重建避免死连接卡顿"
            )
            self._quick_start()

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
            with self._conn_lock:
                new_client = Quotes.factory(market="std", server=self._best_server, timeout=3)
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

    @staticmethod
    def _is_timeout_err(e: Exception) -> bool:
        """判断异常是否为网络超时（mootdx/pytdx 底层为 socket.timeout）。"""
        if isinstance(e, (socket.timeout, TimeoutError)):
            return True
        msg = str(e).lower()
        return "timed out" in msg or "timeout" in msg

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if not MOOTDX_AVAILABLE:
            return None, "mootdx 未安装"
        if self._client is None:
            return None, "mootdx 客户端未初始化"

        # 单 TCP 串行 + 重建均由 _conn_lock 保护；节流由调用方（server）负责。
        # 长驻 TCP 连接空闲后，首次请求可能偶发返回空（连接已失效但未被标记断开）。
        # 区分两类情况，避免无谓重建：
        #   - 连接异常（except）→ 连接可能已死，重建重试一次；
        #   - 空结果但连接近期健康 → 视为该票确无数据，不重建（避免拆掉好连接）；
        #   - 空结果且连接长时间空闲（>_IDLE_RECONNECT_SEC）→ 可能空闲连接静默失效，重建重试。
        # 取数总预算：monitor 侧 K线 HTTP 超时仅 _ps_timeout(=5s)，批量 10s。
        # mootdx 单次 bars 已受 Quotes 的 socket timeout(=3s) 约束；本预算用于防止
        # 「超时后还重建再重试」把总耗时撑到 ~15s 把 monitor 的 5s 顶爆——正是空闲/
        # 偶发超时后单只 K线仍记 timed out 的根因。超时类错误绝不重试，立即失败回退。
        df = None
        last_err = None
        started = time.time()
        for attempt in range(2):
            try:
                # 连接锁获取限时：mootdx 单 TCP 非线程安全必须串行，但某次调用卡在死连接
                #（socket 超时未触发，Windows TCP 重传可达 ~21s）会长期持锁，导致后续所有
                # 请求排队级联超时。拿不到锁立即放弃，交上层回退，不炸雪球。
                if not self._conn_lock.acquire(timeout=self._LOCK_ACQUIRE_SEC):
                    logger.warning(f"mootdx 连接锁获取超时（连接可能卡死），放弃 {symbol}")
                    return None, "mootdx 连接繁忙"
                try:
                    self._reconnect_if_idle()  # 空闲过久先重建，避免死连接卡顿
                    client = self._client
                    if client is None:
                        return None, "mootdx 客户端未初始化"
                    # frequency=9 表示日线
                    df = client.bars(symbol=symbol, frequency=9, offset=days)
                finally:
                    self._conn_lock.release()
            except Exception as e:  # noqa: BLE001
                last_err = e
                elapsed = time.time() - started
                logger.warning(f"mootdx 请求异常（第 {attempt + 1} 次）: {symbol} - {e}")
                # 超时 / 预算耗尽：连接已慢或死，重建再重试只会再耗 ~2 倍超时，
                # 必然超过 monitor 的 5s HTTP 超时。直接失败，交给上层回退。
                if self._is_timeout_err(e) or elapsed >= self._FETCH_BUDGET_SEC:
                    df = None
                    return None, f"mootdx 请求超时: {e}"
                df = None

            if df is not None and not df.empty:
                self._last_ok_ts = time.time()
                break

            # 空结果（非异常）：连接未报错但无数据
            need_reconnect = bool(last_err) or (
                time.time() - self._last_ok_ts > self._IDLE_RECONNECT_SEC
            )
            if not need_reconnect:
                logger.debug(f"mootdx {symbol} 返回空（连接健康，视为无数据，不重建）")
                return None, "mootdx 返回空数据"
            if attempt == 0:
                logger.info(f"mootdx {symbol} 连接异常/可能失效，重建连接重试")
                if not self._conn_lock.acquire(timeout=self._LOCK_ACQUIRE_SEC):
                    return None, "mootdx 连接繁忙"
                try:
                    self._quick_start()
                finally:
                    self._conn_lock.release()
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
                "available": MOOTDX_AVAILABLE and self._client is not None,
                "best_server": f"{self._best_server[0]}:{self._best_server[1]}"
                if self._best_server
                else None,
            }
        )
        return info
