#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
各源的显式限流（设计 §6.5）。

把限流参数从「文字约定」提升为可配置常量表，并为每个 HTTP 源提供线程安全的
令牌桶限流器（token bucket）：并发调用也保证相邻请求间隔 ≥ ``min_interval``，
并叠加 ``jitter`` 随机抖动，规避固定节奏被风控识别。

限流器按源名挂到模块级单例，供同进程内所有该源调用共享。
"""
from __future__ import annotations

import random
import threading
import time
from typing import Dict, Optional

# 各源显式限流参数（设计 §6.5）。
#   min_interval : 相邻请求最小间隔（秒）
#   jitter       : 额外随机抖动上限（秒），规避固定节奏
#   capacity     : 令牌桶容量（允许短时突发数）；默认 1（严格串行）
INTRADAY_RATE_LIMITS: Dict[str, Dict[str, float]] = {
    "东方财富(push2his)": {"min_interval": 1.0, "jitter": 0.5, "capacity": 1.0},
    "新浪(min_kline)": {"min_interval": 0.5, "jitter": 0.3, "capacity": 1.0},
    "openstockdata(百度)": {"min_interval": 0.3, "jitter": 0.2, "capacity": 1.0},
    # mootdx TCP 无硬性 QPS 限制，靠单连接串行 + 锁即可（不在令牌桶管理范围）
}

# 未知源时的默认限流（避免无限制高频）
_DEFAULT_LIMIT = {"min_interval": 0.5, "jitter": 0.2, "capacity": 1.0}


class TokenBucket:
    """
    线程安全令牌桶限流器。

    以 ``rate = 1 / min_interval``（令牌/秒）的速率补充令牌，``acquire`` 在令牌
    不足时阻塞等待，并叠加随机 ``jitter`` 抖动。``capacity=1`` 时等价于「严格最小
    间隔串行」；``capacity>1`` 允许短时突发。
    """

    def __init__(
        self, min_interval: float, jitter: float = 0.0, capacity: float = 1.0
    ) -> None:
        self.min_interval = float(min_interval)
        self.rate = 1.0 / self.min_interval if self.min_interval > 0 else float("inf")
        self.jitter = float(jitter)
        self.capacity = float(capacity)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    def acquire(self, tokens: float = 1.0) -> None:
        """获取 ``tokens`` 个令牌；不足则阻塞直到可获取（含抖动等待）。"""
        with self._lock:
            self._refill()
            wait = 0.0
            if self._tokens < tokens:
                wait = (tokens - self._tokens) / self.rate
            if self.jitter:
                wait += random.uniform(0.0, self.jitter)
            if wait > 0:
                time.sleep(wait)
            self._refill()
            self._tokens = max(0.0, self._tokens - tokens)

    def get_name(self) -> str:  # pragma: no cover - 调试用
        return f"TokenBucket(min_interval={self.min_interval}, jitter={self.jitter})"


# 模块级共享限流器注册表：源名 -> TokenBucket
_LIMITERS: Dict[str, TokenBucket] = {}
_LIMITERS_LOCK = threading.Lock()


def get_rate_limiter(source_name: str) -> TokenBucket:
    """
    返回指定源的共享令牌桶限流器（按源名缓存，模块级单例）。

    Args:
        source_name: 限流源名（对应 :data:`INTRADAY_RATE_LIMITS` 的键；未知则用默认）。
    """
    if source_name in _LIMITERS:
        return _LIMITERS[source_name]
    with _LIMITERS_LOCK:
        if source_name in _LIMITERS:  # 双重检查
            return _LIMITERS[source_name]
        cfg = INTRADAY_RATE_LIMITS.get(source_name, _DEFAULT_LIMIT)
        _LIMITERS[source_name] = TokenBucket(
            min_interval=cfg["min_interval"],
            jitter=cfg["jitter"],
            capacity=cfg.get("capacity", 1.0),
        )
        return _LIMITERS[source_name]
