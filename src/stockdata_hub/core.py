#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stockdata_hub —— 统一股票数据接口核心。

本模块定义「统一接口」的抽象契约：

- :class:`DataProvider` 抽象基类：任何数据源（akshare / mootdx / 腾讯 / 新浪 /
  openstockdata / iTick ...）都实现 ``can_handle`` 与 ``fetch_data`` 两个方法，
  对外返回 *统一契约* 的 DataFrame（见 :mod:`stockdata_hub.normalization`）。
- :func:`retry_on_failure`：网络请求重试装饰器（指数退避 + 抖动）。
- :class:`DataProviderManager`：可插拔数据源管理器，按优先级顺序尝试各 Provider，
  命中第一个成功返回的数据源。支持动态增删、调整优先级、列出可用源。

设计目标：调用方只需面对 ``fetch(symbol)``，底层用哪个源、怎么 fallback 全部透明。
"""
from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple, Callable

import pandas as pd

logger = logging.getLogger(__name__)


class StockDataError(Exception):
    """库的统一异常基类。"""


class NoProviderError(StockDataError):
    """没有任何 Provider 能处理该 symbol。"""


class ProviderFetchError(StockDataError):
    """所有 Provider 都获取失败。"""


def _rel_diff(a: float, b: float) -> float:
    """返回 a、b 的相对差异绝对值 |a-b|/max(|a|,|b|)。"""
    a, b = float(a), float(b)
    denom = max(abs(a), abs(b))
    if denom == 0:
        return 0.0
    return abs(a - b) / denom


# 这些历史源偶发返回错误的当日/前一日收盘，需对其最后一根 bar 做跨源校验
_LAST_BAR_VALIDATE_PROVIDERS = {"通达信TCP(mootdx)"}


def validate_last_bar_against_realtime(
    df: "pd.DataFrame", symbol: str, threshold: float = 0.005
) -> Tuple["pd.DataFrame", bool]:
    """用腾讯实时快照校验并修正 df 最后 1~2 根 bar 的收盘价。

    腾讯实时快照提供权威的「当前价」与「昨收」，可纠正 mootdx 等历史源偶发的
    当日/前一日收盘错误（如 002027 的 07-08 收盘被错返回 4.91，真实应为 4.72）。
    仅当相对差异超过 ``threshold`` 时才修正，避免正常小幅波动被误改。

    Returns:
        (可能已修正的 df, 是否做过修正)
    """
    if df is None or len(df) == 0 or "close" not in df.columns:
        return df, False
    try:
        from .providers.fast_tencent_provider import tencent_quote_batch
    except Exception as e:  # noqa: BLE001
        logger.debug(f"导入腾讯实时接口失败，跳过最后一根 bar 校验: {e}")
        return df, False
    try:
        res = tencent_quote_batch([symbol])
    except Exception as e:  # noqa: BLE001
        logger.debug(f"腾讯实时校验调用失败，跳过: {e}")
        return df, False
    if not res or symbol not in res:
        return df, False
    q = res[symbol]
    price = q.get("price")
    last_close = q.get("last_close")
    if price is None and last_close is None:
        return df, False

    df = df.copy()
    close_pos = df.columns.get_loc("close")
    corrected = False

    # 修正最后一根（今日）收盘 -> 腾讯当前价
    if price is not None and len(df) >= 1:
        m_val = float(df.iloc[-1, close_pos])
        if _rel_diff(m_val, price) > threshold:
            logger.warning(
                f"[{symbol}] mootdx 最后一根收盘 {m_val} 与腾讯实时 {price} 差异超阈值，已修正"
            )
            df.iloc[-1, close_pos] = price
            corrected = True

    # 修正倒数第二根（前一日）收盘 -> 腾讯昨收（前一日涨跌基准）
    if last_close is not None and len(df) >= 2:
        m_prev = float(df.iloc[-2, close_pos])
        if _rel_diff(m_prev, last_close) > threshold:
            logger.warning(
                f"[{symbol}] mootdx 前一根收盘 {m_prev} 与腾讯昨收 {last_close} "
                f"差异超阈值，已修正"
            )
            df.iloc[-2, close_pos] = last_close
            corrected = True

    return df, corrected


def retry_on_failure(
    max_retries: int = 3,
    retry_delay: float = 2.0,
    backoff_factor: float = 1.5,
    retry_exceptions: Tuple[type, ...] = (Exception,),
):
    """
    重试装饰器，用于处理网络请求失败的情况。

    Args:
        max_retries: 最大重试次数。
        retry_delay: 初始重试延迟（秒）。
        backoff_factor: 延迟增长因子。
        retry_exceptions: 需要重试的异常类型。
    """

    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            attempts = 0
            current_delay = retry_delay
            while attempts < max_retries:
                try:
                    return func(*args, **kwargs)
                except retry_exceptions as e:  # noqa: PERF203
                    attempts += 1
                    if attempts >= max_retries:
                        logger.error(
                            f"请求失败，已达到最大重试次数 {max_retries}，最终错误: {e}"
                        )
                        raise
                    logger.warning(
                        f"请求失败: {e}，第 {attempts} 次重试，等待 {current_delay:.2f} 秒"
                    )
                    time.sleep(current_delay)
                    current_delay = current_delay * backoff_factor + random.uniform(0.1, 1.0)

        return wrapper

    return decorator


class DataProvider(ABC):
    """
    数据 Provider 抽象基类。

    子类必须实现：

    - :meth:`can_handle` —— 判断本 Provider 能否处理某 symbol。
    - :meth:`fetch_data` —— 实际抓取，返回 ``(DataFrame | None, error | None)``。

    统一契约（返回给管理器的 DataFrame 应满足，最终由管理器统一规范化）：
        - 列：``date``(datetime64)、``open``/``high``/``low``/``close``(float)、
          ``volume``(float，单位=手/lot)；可选 ``amount``、``ma5``/``ma10``/``ma20``。
        - ``volume`` 单位统一为「手」(lot)：A股/ETF 1 手 = 100 股。返回「股」的源
          （如 openstockdata）必须在 ``fetch_data`` 内先 ``÷100``。

    便捷属性 ``name`` / ``priority`` 可由子类在 ``__init__`` 中设置；管理器按
    ``priority`` 升序尝试（越小越优先）。
    """

    #: Provider 显示名（子类覆盖）
    name: str = "base"
    #: 优先级，越小越优先（子类覆盖）
    priority: int = 100

    #: 支持的周期集合；默认仅日线。分钟 Provider 应覆盖为分钟集合，管理器据此在
    #: 兜底前自动跳过不支持该 period 的源（设计 §5）。
    supports_periods: set = {"1d"}

    @abstractmethod
    def can_handle(self, symbol: str) -> bool:
        """判断此 Provider 是否能处理指定的股票代码 / 名称。"""
        raise NotImplementedError

    def can_handle_request(
        self, symbol: str, days: int = 1, period: str = "1d"
    ) -> bool:
        """
        判断此 Provider 是否能满足「指定代码 + 历史天数 + 周期」的请求。

        默认等价于 :meth:`can_handle`，但会先按 ``period`` 过滤：若 ``period``
        不在本 Provider 的 ``supports_periods`` 中，直接返回 ``False``，使管理器在
        分钟请求时自动跳过仅支持日线的源、在日线请求时跳过仅支持分钟的源
        （设计 §5）。
        """
        if period not in getattr(self, "supports_periods", {"1d"}):
            return False
        return self.can_handle(symbol)

    @abstractmethod
    def fetch_data(
        self, symbol: str, days: int = 30, period: str = "1d"
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """
        获取股票数据。

        Returns:
            ``(数据, 错误信息)``。成功时错误为 ``None``；失败时数据为 ``None``。
        """
        raise NotImplementedError

    def get_name(self) -> str:
        """获取 Provider 名称。"""
        return self.name

    def get_priority(self) -> int:
        """获取 Provider 优先级。"""
        return self.priority

    def get_provider_info(self) -> Dict[str, object]:
        """获取 Provider 元信息（便于调试 / 展示）。"""
        return {
            "name": self.name,
            "priority": self.priority,
            "description": (self.__doc__ or "").strip().splitlines()[0]
            if self.__doc__
            else "",
            "available": True,
        }


class DataProviderManager:
    """
    数据 Provider 管理器：按优先级顺序尝试所有可用 Provider。

    典型用法::

        from stockdata_hub import DataProviderManager

        manager = DataProviderManager.build_default()
        df, error = manager.get_data("600519", days=30)
        if df is not None:
            print(df.tail())

    也可用单例::

        from stockdata_hub import get_default_manager

        manager = get_default_manager()
    """

    def __init__(self) -> None:
        self.providers: List[DataProvider] = []
        self._last_used_provider: Optional[str] = None

    # ----- 构建 -----

    @classmethod
    def build_default(cls) -> "DataProviderManager":
        """
        构建一个带有全部内置 Provider 的管理器。

        各 Provider 的底层依赖（akshare / mootdx / openstockdata / itick-sdk ...）
        均为**延迟导入（可选）**：缺失时该 Provider 自动跳过，不影响其它源。
        这正是「多源兜底、缺依赖降级」的设计。
        """
        manager = cls()
        from .providers import register_builtin_providers

        register_builtin_providers(manager)
        return manager

    # ----- Provider 管理 -----

    def add_provider(self, provider: DataProvider) -> None:
        """添加一个 Provider（按 priority 自动排序插入）。"""
        if not isinstance(provider, DataProvider):
            raise TypeError("provider 必须是 DataProvider 的实例")
        self.providers.append(provider)
        self.providers.sort(key=lambda p: p.get_priority())
        logger.info(
            f"添加 Provider: {provider.get_name()} (优先级: {provider.get_priority()})"
        )

    def remove_provider(self, name: str) -> None:
        """按名称移除 Provider。"""
        self.providers = [p for p in self.providers if p.get_name() != name]
        logger.info(f"移除 Provider: {name}")

    def set_provider_priority(self, name: str, priority: int) -> None:
        """调整某 Provider 的优先级并重新排序。"""
        for provider in self.providers:
            if provider.get_name() == name:
                provider.priority = priority
                self.providers.sort(key=lambda p: p.get_priority())
                logger.info(f"更新 Provider {name} 优先级为: {priority}")
                break

    # ----- 数据获取 -----

    def get_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """
        获取股票数据：依次尝试所有能 ``can_handle`` 的 Provider。

        Returns:
            ``(统一契约 DataFrame, 错误信息)``。全部失败则数据为 ``None``。
        """
        logger.info(f"开始获取股票数据: {symbol}")

        if not self.providers:
            return None, "没有任何可用的 Provider"

        from .normalization import normalize_ohlcv

        for provider in self.providers:
            if not provider.can_handle_request(symbol, days):
                logger.debug(f"Provider {provider.get_name()} 不能处理 {symbol} (days={days})")
                continue

            logger.info(f"尝试使用 Provider: {provider.get_name()}")
            try:
                raw, error = provider.fetch_data(symbol, days)
            except Exception as e:  # 单源异常不应拖垮整体
                logger.warning(f"Provider {provider.get_name()} 抛异常: {e}")
                continue

            if raw is None or (isinstance(raw, pd.DataFrame) and raw.empty):
                if error:
                    logger.warning(f"Provider {provider.get_name()} 返回错误: {error}")
                continue

            # 统一契约规范化（列名别名、数值化、日期、排序、截取、去多余列）
            df, err = normalize_ohlcv(raw, days)
            if df is not None and not df.empty:
                self._last_used_provider = provider.get_name()
                # mootdx 等源偶发返回错误的当日/前一日收盘，跨源校验修正最后一根 bar
                if provider.get_name() in _LAST_BAR_VALIDATE_PROVIDERS:
                    df, corrected = validate_last_bar_against_realtime(df, symbol)
                    if corrected:
                        logger.info(
                            f"数据获取成功: {provider.get_name()}（最后一根 bar 已跨源校验修正）"
                        )
                        return df, None
                logger.info(f"数据获取成功: {provider.get_name()}")
                return df, None
            if err:
                logger.warning(f"Provider {provider.get_name()} 规范化失败: {err}")

        self._last_used_provider = None
        return None, "所有 Provider 都无法获取数据"

    def get_intraday(
        self,
        symbol: str,
        period: str = "1m",
        days: int = 1,
        count: Optional[int] = None,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """
        获取分钟级 K线：依次尝试所有 ``can_handle_request(symbol, days, period)``
        为 True 的分钟 Provider（与 :meth:`get_data` 平行的分钟版）。

        仅遍历声明支持该 ``period`` 的源（日线 Provider 对分钟 period 返回 False
        被自动排除），归一化后返回统一分钟契约 DataFrame。

        Returns:
            ``(统一分钟契约 DataFrame, 错误信息)``。全部失败则数据为 ``None``。
        """
        if period not in {"1m", "5m", "15m", "30m", "60m"}:
            return None, f"不支持的 period: {period}"

        if not self.providers:
            return None, "没有任何可用的 Provider"

        from .normalization import normalize_intraday

        for provider in self.providers:
            try:
                ok = provider.can_handle_request(symbol, days, period)
            except TypeError:
                # 旧 Provider 未实现 period 形参：视为非分钟源，跳过（防御性）
                logger.debug(
                    f"Provider {provider.get_name()} 不支持 period 参数，分钟请求跳过"
                )
                ok = False
            if not ok:
                logger.debug(
                    f"Provider {provider.get_name()} 不能处理 {symbol} (period={period})"
                )
                continue

            logger.info(f"尝试使用分钟 Provider: {provider.get_name()}")
            try:
                raw, error = provider.fetch_data(symbol, days, period)
            except Exception as e:  # 单源异常不应拖垮整体
                logger.warning(f"分钟 Provider {provider.get_name()} 抛异常: {e}")
                continue

            if raw is None or (isinstance(raw, pd.DataFrame) and raw.empty):
                if error:
                    logger.warning(
                        f"分钟 Provider {provider.get_name()} 返回错误: {error}"
                    )
                continue

            df, err = normalize_intraday(raw, period, days, count)
            if df is not None and not df.empty:
                self._last_used_provider = provider.get_name()
                logger.info(f"分钟数据获取成功: {provider.get_name()}")
                return df, None
            if err:
                logger.warning(
                    f"分钟 Provider {provider.get_name()} 规范化失败: {err}"
                )

        self._last_used_provider = None
        return None, "所有分钟 Provider 都无法获取数据"

    # ----- 查询 -----

    def get_provider_list(self) -> List[Dict[str, object]]:
        """返回所有 Provider 的元信息。"""
        return [p.get_provider_info() for p in self.providers]

    def get_last_used_provider(self) -> Optional[str]:
        """返回上一次成功命中的 Provider 名称。"""
        return self._last_used_provider

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        names = ", ".join(f"{p.get_name()}({p.get_priority()})" for p in self.providers)
        return f"DataProviderManager[{names}]"


# 模块级单例缓存（懒构建）
_default_manager: Optional[DataProviderManager] = None


def get_default_manager() -> DataProviderManager:
    """返回进程级默认 Provider 管理器（懒构建，多次调用返回同一实例）。"""
    global _default_manager
    if _default_manager is None:
        _default_manager = DataProviderManager.build_default()
    return _default_manager


def create_provider_manager() -> DataProviderManager:
    """
    便捷函数：获取默认的 Provider 管理器（进程级单例，已注册全部内置源）。

    与旧 ``stock-cloud`` 中的 ``create_provider_manager()`` 语义一致：多次调用
    返回同一实例，且该实例已按优先级注册好内置多源 Provider。
    """
    return get_default_manager()
