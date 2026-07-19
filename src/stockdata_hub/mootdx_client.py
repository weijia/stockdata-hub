#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共享的单例 mootdx TCP 客户端管理。

mootdx 底层是**单条 TCP 长连接、非线程安全**，必须串行访问。日线
(:class:`MootdxProvider`) 与分钟 (:class:`MootdxMinuteProvider`) Provider 共用
**同一个** 客户端实例与连接锁，避免开两条 TDX 连接（服务器对并发连接数敏感），
也保证 daily+intraday 经同一把锁串行、不踩非线程安全（设计 §6.2）。

模块级 :func:`get_tdx_client` 返回进程级单例。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from mootdx.quotes import Quotes

    MOOTDX_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖可选
    MOOTDX_AVAILABLE = False
    Quotes = None  # type: ignore[assignment]
    logger.debug("mootdx 未安装，TCP 高速行情不可用。安装: pip install mootdx")


_DEFAULT_KNOWN_SERVERS = [
    ("202.108.253.131", 7709),
    ("114.80.149.92", 7709),
    ("123.125.108.90", 7709),
    ("221.194.181.81", 7709),
    ("202.108.253.139", 7709),
]


class TdxClientManager:
    """模块级单例容器：一个 mootdx Quotes 客户端 + 串行锁 + 测速/空闲重建。"""

    def __init__(self) -> None:
        self._client: Optional[object] = None
        self._best_server: Optional[Tuple[str, int]] = None
        self._conn_lock = threading.Lock()
        self._last_ok_ts = 0.0
        # 连接空闲超过此时长（秒）且无成功记录，空结果才视为失效并重建。
        self._IDLE_RECONNECT_SEC = 300
        # 取数总预算（秒）：超时类错误绝不重试，防止把 monitor 的 5s HTTP 超时顶爆。
        self._FETCH_BUDGET_SEC = 4.0
        # 连接锁获取上限：某次调用卡在死连接会长期持锁，拿不到锁立即放弃交上层回退。
        self._LOCK_ACQUIRE_SEC = 2.5

        if MOOTDX_AVAILABLE:
            self.quick_start()
            self.start_bench()

    # ---- 属性 ----
    @property
    def client(self):
        return self._client

    @property
    def lock(self) -> threading.Lock:
        return self._conn_lock

    @property
    def best_server(self) -> Optional[Tuple[str, int]]:
        return self._best_server

    @property
    def LOCK_ACQUIRE_SEC(self) -> float:
        return self._LOCK_ACQUIRE_SEC

    @property
    def FETCH_BUDGET_SEC(self) -> float:
        return self._FETCH_BUDGET_SEC

    @property
    def should_reconnect(self) -> bool:
        """连接曾成功过且距上次成功已超过空闲阈值。"""
        return self._last_ok_ts > 0 and (
            time.time() - self._last_ok_ts > self._IDLE_RECONNECT_SEC
        )

    def mark_ok(self) -> None:
        self._last_ok_ts = time.time()

    # ---- 连接管理 ----
    def quick_start(self) -> None:
        """（重建）启动一个 mootdx 客户端；失败则置 None。"""
        try:
            if self._best_server:
                self._client = Quotes.factory(
                    market="std", server=self._best_server, timeout=3
                )
            else:
                self._client = Quotes.factory(market="std", timeout=3)
            logger.info("mootdx TCP 客户端启动成功")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"mootdx 客户端启动失败: {e}")
            self._client = None

    def reconnect_if_idle(self) -> None:
        """调用方须持 ``lock``。空闲过久主动重建，避免死连接卡顿。"""
        if self.should_reconnect:
            logger.info(
                f"mootdx 连接空闲 {int(time.time() - self._last_ok_ts)}s，主动重建"
            )
            self.quick_start()

    def switch_to_best_server(self, server: Tuple[str, int]) -> None:
        """切换到已知最优服务器（外部缓存注入或测速选出）。"""
        if not server or not MOOTDX_AVAILABLE:
            return
        try:
            with self._conn_lock:
                new_client = Quotes.factory(market="std", server=server, timeout=3)
                new_client.bars(symbol="000001", category=4, offset=1)
                if self._client is not None:
                    try:
                        self._client.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._client = new_client
                self._best_server = server
        except Exception as e:  # noqa: BLE001
            logger.warning(f"mootdx 切换最优服务器失败: {e}")

    def start_bench(self) -> None:
        """异步测速选出最优服务器（不阻塞构造）。"""
        t = threading.Thread(
            target=self._async_find_best_server, daemon=True, name="mootdx-bestip"
        )
        t.start()

    def _async_find_best_server(self) -> None:  # pragma: no cover - 运行时测速
        try:
            from pytdx.util.best_ip import select_best_ip

            best = select_best_ip()
            if best and len(best) == 2:
                self.switch_to_best_server(best)
                return
        except Exception as e:  # noqa: BLE001
            logger.debug(f"select_best_ip 失败: {e}")

        best_server: Optional[Tuple[str, int]] = None
        for host, port in _DEFAULT_KNOWN_SERVERS:
            try:
                test_client = Quotes.factory(
                    market="std", server=(host, port), timeout=3
                )
                test_client.bars(symbol="000001", category=4, offset=1)
                best_server = (host, port)
                test_client.close()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"服务器 {host}:{port} 不可用: {e}")
                continue
        if best_server:
            self.switch_to_best_server(best_server)


_manager: Optional[TdxClientManager] = None
_manager_lock = threading.Lock()


def get_tdx_client() -> Optional[TdxClientManager]:
    """返回进程级共享的 mootdx 客户端管理器单例（懒构建，多次调用同一实例）。"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = TdxClientManager()
    return _manager
