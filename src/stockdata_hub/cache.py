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
        ttl = self.cache_config[cache_key]["ttl"] if cache_key in self.cache_config else 24 * 60 * 60
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

    def set(self, cache_key: str, data: Any) -> None:
        """通用写入：内存 + 磁盘双写。"""
        self._memory_cache[cache_key] = data
        self._cache_timestamps[cache_key] = time.time()
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
