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


class TrackedLock:
    """带持有者追踪的锁，用于诊断「连接锁获取超时」时锁被谁长期占据。

    普通 ``threading.Lock`` 不暴露持有者；这里在 acquire 成功后记录持有线程名
    与获取时刻，release 时清除。超时的调用方可读取 ``holder()`` 得知「锁被哪个
    线程持有了多久」，从而区分两类卡死：

    - 切换线程（mootdx-bestip）卡死在重建/测速 → 死锁式无法切换；
    - 取数线程（如 StockServer-*/worker）卡死在底层 mootdx socket 调用 → 网络/服务器不可用。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: Optional[str] = None
        self._since = 0.0
        self._meta = threading.Lock()

    def acquire(self, *args, **kwargs):
        ok = self._lock.acquire(*args, **kwargs)
        if ok:
            with self._meta:
                self._owner = threading.current_thread().name
                self._since = time.time()
        return ok

    def release(self):
        with self._meta:
            self._owner = None
            self._since = 0.0
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    def holder(self) -> str:
        with self._meta:
            if self._owner is None:
                return "空闲"
            return f"{self._owner}（已持 {time.time() - self._since:.1f}s）"


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
        self._conn_lock = TrackedLock()
        self._last_ok_ts = 0.0
        # 连接空闲超过此时长（秒）且无成功记录，空结果才视为失效并重建。
        self._IDLE_RECONNECT_SEC = 300
        # 取数总预算（秒）：超时类错误绝不重试，防止把 monitor 的 5s HTTP 超时顶爆。
        self._FETCH_BUDGET_SEC = 4.0
        # 连接锁获取上限：某次调用卡在死连接会长期持锁，拿不到锁立即放弃交上层回退。
        self._LOCK_ACQUIRE_SEC = 2.5
        # 连续取数失败计数：达到阈值（默认 5 次）触发一次后台重测速切换服务器，
        # 根治「所选 TDX 服务器不提供分钟数据/已不可达却永久卡在 503」的问题。
        self._fail_count = 0
        self._FAIL_SWITCH_THRESHOLD = 5
        # 诊断信息：即使 best_server 为 None（受限网络选不出可用服务器），也能从
        # 日志/接口看到「当前实际连的是哪台、测速结果如何」，定位分钟数据不可用根因。
        self._current_server_desc = "未连接"
        self._bench_done = False
        self._bench_status = ""

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
    def lock_holder(self) -> str:
        """当前连接锁持有者信息（线程名 + 已持有时长），用于诊断超时根因。"""
        return self._conn_lock.holder()

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

    def note_result(self, ok: bool) -> None:
        """记录一次取数成败，供运行时自动切换服务器。

        连续 ``_FAIL_SWITCH_THRESHOLD`` 次失败（连接锁超时 / 调用抛异常 /
        bars 等返回 None 或空 DataFrame）后，触发一次后台重测速
        （``start_bench``），由 ``_async_find_best_server`` 选出「日线+分钟均可用」
        的服务器并 ``switch_to_best_server``。成功取数即清零计数。

        注意：本方法在持有 ``_conn_lock`` 时也可能被调用（如 _guard 异常分支），
        但 ``start_bench`` 仅启动守护线程便立即返回，不会同步等待锁，故不会死锁。
        """
        if ok:
            self._fail_count = 0
            return
        self._fail_count += 1
        if self._fail_count >= self._FAIL_SWITCH_THRESHOLD:
            self._fail_count = 0
            logger.warning(
                f"mootdx 连续 {self._FAIL_SWITCH_THRESHOLD} 次取数失败，"
                f"触发后台重测速切换 TDX 服务器"
            )
            self.start_bench()

    # ---- 连接管理 ----
    def quick_start(self) -> None:
        """（重建）启动一个 mootdx 客户端；失败则置 None。"""
        try:
            if self._best_server:
                self._client = Quotes.factory(
                    market="std", server=self._best_server, timeout=3
                )
                self._current_server_desc = (
                    f"{self._best_server[0]}:{self._best_server[1]}"
                )
            else:
                self._client = Quotes.factory(market="std", timeout=3)
                self._current_server_desc = (
                    "mootdx 内置默认 TDX 服务器（未测速择优）"
                )
                logger.info(self._current_server_desc)
            self._bench_done = False
            self._bench_status = ""
            logger.info("mootdx TCP 客户端启动成功")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"mootdx 客户端启动失败: {e}")
            self._client = None
            self._current_server_desc = f"启动失败: {e}"

    def reconnect_if_idle(self) -> None:
        """调用方须持 ``lock``。空闲过久主动重建，避免死连接卡顿。"""
        if self.should_reconnect:
            logger.info(
                f"mootdx 连接空闲 {int(time.time() - self._last_ok_ts)}s，主动重建"
            )
            self.quick_start()

    def switch_to_best_server(self, server: Tuple[str, int]) -> None:
        """切换到已知最优服务器（外部缓存注入或测速选出）。

        切换成功时打 info 日志，便于从日志直接看出现网连的是哪台 TDX 服务器；
        并额外校验分钟数据（frequency=7）可用——部分服务器只提供日线不提供分钟，
        仅校验日线会误切到一台仍无法返回分钟数据的服务器。

        关键修正：构建 + 校验新客户端使用**独立临时连接**，全程**不持有**生产
        连接锁；仅在「替换 self._client」这一瞬间（毫秒级）加锁。否则后台测速
        线程（mootdx-bestip）会在锁内做 factory + 多次 bars 探测，单台慢服务器
        即可让生产连接锁被占用数十秒，导致所有实时取数请求超时（见 2026-07-23 日志）。
        """
        if not server or not MOOTDX_AVAILABLE:
            return
        # 1) 锁外：用临时连接构建并校验新客户端（慢/卡死不影响生产流量）
        try:
            new_client = Quotes.factory(market="std", server=server, timeout=3)
            new_client.bars(symbol="000001", category=4, offset=1)
            # 分钟数据可用性校验：用户场景为「日线有、分钟无」，必须显式验证
            new_client.bars(symbol="000001", frequency=7, offset=5)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"mootdx 切换最优服务器校验失败 {server}: {e}")
            return
        # 2) 锁内：仅做「关旧 + 换新」这一瞬间的原子替换
        with self._conn_lock:
            old = self._best_server
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
            self._client = new_client
            self._best_server = server
            self._current_server_desc = f"{server[0]}:{server[1]}"
        logger.info(f"mootdx 切换 TDX 服务器: {old} -> {server}")

    def start_bench(self) -> None:
        """异步测速选出最优服务器（不阻塞构造）。"""
        t = threading.Thread(
            target=self._async_find_best_server, daemon=True, name="mootdx-bestip"
        )
        t.start()

    def _verify_server(self, server: Tuple[str, int]) -> bool:
        """探测服务器是否同时提供日线(category=4)与分钟(frequency=7)数据。

        用户场景痛点：部分公共 TDX 服务器只返回日线、不返回分钟（导致永久
        「无分钟数据」）。仅校验日线会误选，故切换前必须显式验证分钟可用。
        """
        try:
            c = Quotes.factory(market="std", server=server, timeout=3)
            c.bars(symbol="000001", category=4, offset=1)
            c.bars(symbol="000001", frequency=7, offset=5)
            c.close()
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug(f"服务器 {server} 验证失败(日线/分钟): {e}")
            return False

    def _async_find_best_server(self) -> None:  # pragma: no cover - 运行时测速
        chosen: Optional[Tuple[str, int]] = None
        try:
            from pytdx.util.best_ip import select_best_ip

            best = select_best_ip()
            if best and len(best) == 2:
                chosen = best
        except Exception as e:  # noqa: BLE001
            logger.debug(f"select_best_ip 失败: {e}")

        # 候选（测速最优）服务器需同时支持日线+分钟，否则落入已知列表兜底
        if chosen and self._verify_server(chosen):
            self.switch_to_best_server(chosen)
            self._bench_done = True
            self._bench_status = f"已选中测速最优服务器 {chosen[0]}:{chosen[1]}"
            return

        # 回退：遍历已知服务器，挑第一台「日线+分钟均可用」的
        for host, port in _DEFAULT_KNOWN_SERVERS:
            if self._verify_server((host, port)):
                self.switch_to_best_server((host, port))
                self._bench_done = True
                self._bench_status = f"已选中已知服务器 {host}:{port}"
                return
        self._bench_done = True
        self._bench_status = (
            "未找到同时支持日线/分钟的 TDX 服务器（分钟数据可能持续不可用）"
        )
        logger.warning(
            "mootdx 未发现同时支持日线/分钟的 TDX 服务器，分钟数据可能持续不可用"
        )

    def _client_host(self) -> Optional[str]:
        """best-effort 提取当前 mootdx 客户端实际连接的 host:port。

        mootdx 的 Quotes 包装 pytdx API，pytdx 连接成功后保存 ``self.ip`` /
        ``self.port``；不同封装层级下属性位置略有差异，逐级尝试，拿不到则返回 None。
        """
        c = self._client
        if c is None:
            return None
        for obj in (c, getattr(c, "client", None)):
            if not obj:
                continue
            ip = getattr(obj, "ip", None)
            if ip:
                port = getattr(obj, "port", None)
                return f"{ip}:{port}" if port else str(ip)
        return None

    def diagnostic(self) -> dict:
        """返回当前 TDX 连接诊断，供 ``/api/config`` 暴露（无需翻日志即可定位）。

        关键字段：

        - ``best_server``：测速选中的最优服务器（受限网络选不出时为 ``None``）；
        - ``current_host``：当前**实际**连接的地址（优先从 client 提取，否则用描述）；
        - ``bench_done`` / ``bench_status``：测速是否完成及结果（为何没选中）；
        - ``connected``：底层 client 是否建立；``fail_count``：连续失败计数。
        """
        best = (
            f"{self._best_server[0]}:{self._best_server[1]}"
            if self._best_server
            else None
        )
        host = self._client_host()
        return {
            "connected": self._client is not None,
            "best_server": best,
            "current_host": host or self._current_server_desc,
            "bench_done": self._bench_done,
            "bench_status": self._bench_status,
            "fail_count": self._fail_count,
        }


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
