#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票数据缓存管理器（可选组件）。

提供基于内存 + 磁盘 pickle 的轻量缓存，用于缓存「股票列表 / ETF 列表 / 港股列表」
等相对静态、获取较慢的数据，避免频繁网络请求。默认缓存目录为
``<用户缓存目录>/stockdata_hub``，可用 ``cache_dir`` 参数覆盖。

与抓取逻辑解耦：管理器只负责 *存取*，真正的数据获取由调用方负责（未命中时返回
``None``，由调用方补数后 ``set_*`` 写回）。
"""
from __future__ import annotations

import logging
import os
import pickle
import threading
import time
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 30 * 24 * 60 * 60  # 30 天


class StockCacheManager:
    """股票数据缓存管理器（支持多缓存键、内存 + 磁盘双存）。"""

    def __init__(self, cache_dir: Optional[str] = None) -> None:
        if cache_dir is None:
            cache_dir = os.path.join(
                os.path.expanduser("~"), ".cache", "stockdata_hub"
            )
        self.cache_dir = cache_dir
        self._ensure_cache_dir()

        self.cache_config: Dict[str, Dict[str, Any]] = {
            "stock_list": {
                "ttl": _DEFAULT_TTL,
                "filename": "stock_list_cache.pkl",
            },
            "etf_list": {
                "ttl": _DEFAULT_TTL,
                "filename": "etf_list_cache.pkl",
            },
            "hk_stock_list": {
                "ttl": _DEFAULT_TTL,
                "filename": "hk_stock_list_cache.pkl",
            },
        }

        self._memory_cache: Dict[str, Any] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._cache_ttls: Dict[str, float] = {}

    # ----- 目录 -----

    def _ensure_cache_dir(self) -> None:
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except Exception as e:  # pragma: no cover - 环境相关
            logger.warning(f"创建缓存目录失败 {self.cache_dir}: {e}")

    # ----- 校验 -----

    def _is_cache_valid(self, cache_key: str) -> bool:
        if cache_key not in self._cache_timestamps:
            return False
        ttl = self._cache_ttls.get(cache_key)
        if ttl is None:
            ttl = self.cache_config[cache_key]["ttl"] if cache_key in self.cache_config else None
        if ttl is None:
            return False  # 未声明 TTL 的任意 key（如分钟缓存未传 ttl）不视为有效
        return (time.time() - self._cache_timestamps[cache_key]) < ttl

    def _load_from_disk(self, cache_key: str) -> Optional[Any]:
        if cache_key not in self.cache_config:
            return None
        filepath = os.path.join(self.cache_dir, self.cache_config[cache_key]["filename"])
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "rb") as f:
                return pickle.load(f)
        except Exception as e:  # pragma: no cover - 损坏文件
            logger.warning(f"从磁盘加载缓存失败 {cache_key}: {e}")
            return None

    def _save_to_disk(self, cache_key: str, data: Any) -> None:
        if cache_key not in self.cache_config:
            return
        filepath = os.path.join(self.cache_dir, self.cache_config[cache_key]["filename"])
        try:
            with open(filepath, "wb") as f:
                pickle.dump(data, f)
        except Exception as e:  # pragma: no cover - 环境相关
            logger.warning(f"保存缓存到磁盘失败 {cache_key}: {e}")

    # ----- 通用存取 -----

    def get(self, cache_key: str, refresh: bool = False) -> Optional[Any]:
        """通用读取：先内存、再磁盘，均未命中返回 ``None``。"""
        if refresh:
            return None
        if cache_key in self._memory_cache and self._is_cache_valid(cache_key):
            return self._memory_cache[cache_key]
        disk_data = self._load_from_disk(cache_key)
        if disk_data is not None:
            self._memory_cache[cache_key] = disk_data
            self._cache_timestamps[cache_key] = time.time()
            return disk_data
        return None

    def set(self, cache_key: str, data: Any, ttl: Optional[float] = None) -> None:
        """通用写入：内存 + 磁盘双写；``ttl`` 为可选过期秒数（分钟缓存使用动态 TTL）。"""
        self._memory_cache[cache_key] = data
        self._cache_timestamps[cache_key] = time.time()
        if ttl is not None:
            self._cache_ttls[cache_key] = ttl
        self._save_to_disk(cache_key, data)

    # ----- 便捷封装 -----

    def get_stock_list(self, refresh: bool = False) -> Optional[pd.DataFrame]:
        return self.get("stock_list", refresh)

    def set_stock_list(self, data: pd.DataFrame) -> None:
        self.set("stock_list", data)

    def get_etf_list(self, refresh: bool = False) -> Optional[pd.DataFrame]:
        return self.get("etf_list", refresh)

    def set_etf_list(self, data: pd.DataFrame) -> None:
        self.set("etf_list", data)

    def get_hk_stock_list(self, refresh: bool = False) -> Optional[pd.DataFrame]:
        return self.get("hk_stock_list", refresh)

    def set_hk_stock_list(self, data: pd.DataFrame) -> None:
        self.set("hk_stock_list", data)

    # ----- 维护 -----

    def clear_cache(self, cache_type: Optional[str] = None) -> None:
        """清除缓存；``cache_type=None`` 时清除全部。"""
        if cache_type:
            self._memory_cache.pop(cache_type, None)
            self._cache_timestamps.pop(cache_type, None)
            if cache_type in self.cache_config:
                filepath = os.path.join(self.cache_dir, self.cache_config[cache_type]["filename"])
                if os.path.exists(filepath):
                    os.remove(filepath)
            logger.info(f"已清除缓存: {cache_type}")
        else:
            self._memory_cache.clear()
            self._cache_timestamps.clear()
            for config in self.cache_config.values():
                filepath = os.path.join(self.cache_dir, config["filename"])
                if os.path.exists(filepath):
                    os.remove(filepath)
            logger.info("已清除所有缓存")

    def get_cache_info(self) -> Dict[str, Any]:
        """返回缓存元信息（调试用）。"""
        info: Dict[str, Any] = {"cache_dir": self.cache_dir, "caches": {}}
        for cache_key, config in self.cache_config.items():
            cache_info = {
                "ttl_hours": config["ttl"] / 3600,
                "filename": config["filename"],
                "in_memory": cache_key in self._memory_cache,
                "valid": self._is_cache_valid(cache_key),
            }
            filepath = os.path.join(self.cache_dir, config["filename"])
            if os.path.exists(filepath):
                cache_info["file_size_mb"] = round(os.path.getsize(filepath) / (1024 * 1024), 2)
            info["caches"][cache_key] = cache_info
        return info


_manager: Optional[StockCacheManager] = None


def get_cache_manager(cache_dir: Optional[str] = None) -> StockCacheManager:
    """返回进程级缓存管理器单例（``cache_dir`` 仅在首次生效）。"""
    global _manager
    if _manager is None:
        _manager = StockCacheManager(cache_dir=cache_dir)
    return _manager


# =========================================================================== #
# 分钟轮询缓存（设计 §6.4）
# =========================================================================== #
# 不同周期的合理 TTL（秒）：1m/5m 盘中变化快取 60s；其余取约一个周期长度。
_PERIOD_TTL_SECONDS = {
    "1m": 60,
    "5m": 60,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
    "1d": 0,  # 日线不入分钟轮询缓存
}


def period_ttl(period: str) -> float:
    """返回某周期对应的轮询缓存 TTL（秒）；未知周期默认 60s。"""
    return _PERIOD_TTL_SECONDS.get(period, 60)


def _merge_by_datetime(old_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
    """按 ``datetime`` 去重合并两段分钟数据，冲突时保留``new_df``（最新）的值。

    用于跨 TTL 拼接：新拉取结果与缓存按时间并集，避免轮询丢中间 bar。
    """
    frames = [f for f in (old_df, new_df) if f is not None and not f.empty]
    if not frames:
        return new_df if new_df is not None else old_df
    if len(frames) == 1:
        return frames[0].copy()
    combined = pd.concat(frames, ignore_index=True)
    if "datetime" not in combined.columns:
        return frames[-1].copy()
    combined = combined.drop_duplicates(subset=["datetime"], keep="last")
    combined = combined.sort_values("datetime").reset_index(drop=True)
    return combined


class IntradayCache:
    """分钟 K线轮询缓存（设计 §6.4）。

    键为 ``(symbol, period)``（不含 ``days``/``count``，轮询场景窗口固定）；
    值在 TTL 内直接返回，跨 TTL 重新拉取时与旧缓存按 ``datetime`` 去重合并，
    避免轮询丢中间 bar。底层复用 :class:`StockCacheManager`（仅内存，不落盘）。
    """

    def __init__(self, manager: Optional[StockCacheManager] = None) -> None:
        self._manager = manager or get_cache_manager()
        self._lock = threading.Lock()

    @staticmethod
    def _key(symbol: str, period: str) -> str:
        return f"intraday:{symbol}:{period}"

    def get(self, symbol: str, period: str) -> Optional[Dict[str, Any]]:
        """命中且在 TTL 内返回 ``{"df": DataFrame, "source": str}``，否则 ``None``。"""
        return self._manager.get(self._key(symbol, period))

    def set(self, symbol: str, period: str, df: pd.DataFrame, source: str) -> None:
        """直接写入（覆盖）缓存。"""
        self._manager.set(
            self._key(symbol, period),
            {"df": df, "source": source},
            ttl=period_ttl(period),
        )

    def merge_and_set(
        self, symbol: str, period: str, new_df: pd.DataFrame, source: str
    ) -> pd.DataFrame:
        """与既有缓存按 ``datetime`` 去重合并后写入，返回合并后的 DataFrame。"""
        key = self._key(symbol, period)
        ttl = period_ttl(period)
        with self._lock:
            old = self._manager.get(key)
            old_df = old.get("df") if isinstance(old, dict) else None
            merged = _merge_by_datetime(old_df, new_df)
            self._manager.set(key, {"df": merged, "source": source}, ttl=ttl)
        return merged

    def clear(self, symbol: Optional[str] = None, period: Optional[str] = None) -> None:
        """清除缓存；``symbol``/``period`` 为 ``None`` 时按维度通配。"""
        if symbol is None and period is None:
            self._manager.clear_cache()  # 清除全部（含列表缓存，调用方知晓）
            return
        # 精确键清除
        if symbol is not None and period is not None:
            self._manager.clear_cache(self._key(symbol, period))
