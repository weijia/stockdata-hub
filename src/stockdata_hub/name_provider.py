#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票名称 -> 代码 转换 Provider（可选，依赖 akshare）。

用于在 ``fetch("贵州茅台")`` 这种「按名称取数」的场景下把名称解析为代码。
数据通过 akshare 拉取并缓存（见 :mod:`stockdata_hub.cache`）。

覆盖市场：
- A 股：``ak.stock_info_a_code_name``
- ETF：``ak.fund_etf_category_sina``
- 港股：``ak.stock_hk_spot``

支持精确匹配与模糊匹配（``fuzzy_match``）。

依赖（可选 extra ``akshare``）：``akshare``。未安装时加载失败，相关查询返回 ``None``。
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from .cache import get_cache_manager

logger = logging.getLogger(__name__)


def _row_get(row, keys) -> Optional[str]:
    """从一行里按候选列名取第一个非空字符串值。"""
    for k in keys:
        if k in row:
            v = row[k]
            if v is None:
                continue
            s = str(v).strip()
            if s and s != "nan":
                return s
    return None


class StockNameProvider:
    """股票名称 <-> 代码 映射 Provider（A股 / ETF / 港股，akshare + 缓存）。"""

    def __init__(self) -> None:
        self.name = "股票名称转换"
        self._name_to_code: Dict[str, str] = {}
        self._code_to_name: Dict[str, str] = {}
        self._loaded = False
        try:
            self._cache = get_cache_manager()
        except Exception:  # noqa: BLE001
            self._cache = None

    # ---- 数据加载 ----
    def _load_a_stock_map(self) -> None:
        try:
            import akshare as ak

            df = self._cache.get_stock_list() if self._cache else None
            if df is None:
                df = ak.stock_info_a_code_name()
                if self._cache is not None and df is not None and not df.empty:
                    self._cache.set_stock_list(df)
            self._add_from_df(df, code_keys=("code", "symbol"),
                              name_keys=("name", "股票简称", "名称"), zfill=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载 A股名称映射失败: {e}")

    def _load_etf_map(self) -> None:
        try:
            import akshare as ak

            df = self._cache.get_etf_list() if self._cache else None
            if df is None:
                df = ak.fund_etf_category_sina()
                if self._cache is not None and df is not None and not df.empty:
                    self._cache.set_etf_list(df)
            self._add_from_df(df, code_keys=("symbol", "code"),
                              name_keys=("name", "名称"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载 ETF名称映射失败: {e}")

    def _load_hk_map(self) -> None:
        try:
            import akshare as ak

            df = self._cache.get_hk_stock_list() if self._cache else None
            if df is None:
                df = ak.stock_hk_spot()
                if self._cache is not None and df is not None and not df.empty:
                    self._cache.set_hk_stock_list(df)
            self._add_from_df(df, code_keys=("代码", "symbol", "code"),
                              name_keys=("名称", "name"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载 港股名称映射失败: {e}")

    def _add_from_df(self, df, code_keys, name_keys, zfill: bool = False) -> None:
        if df is None or getattr(df, "empty", True):
            return
        for _, row in df.iterrows():
            code = _row_get(row, code_keys)
            nm = _row_get(row, name_keys)
            if not code or not nm:
                continue
            if zfill and code.isdigit():
                code = code.zfill(6)
            self._name_to_code[nm] = code
            self._code_to_name[code] = nm
        logger.info(f"名称映射累计: {len(self._name_to_code)} 条")

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load_a_stock_map()
        self._load_etf_map()
        self._load_hk_map()
        self._loaded = True
        logger.info(f"股票名称映射初始化完成: {len(self._name_to_code)} 条")

    # ---- 查询 ----
    def get_stock_code_from_name(self, stock_name: str, fuzzy_match: bool = True) -> Optional[str]:
        """根据股票名称返回代码；找不到返回 ``None``。

        ``fuzzy_match=True`` 时，对名称做子串模糊匹配（兼容旧 ``stock-cloud`` 行为）。
        """
        self._ensure_loaded()
        name = (stock_name or "").strip()
        if not name:
            return None
        if name in self._name_to_code:
            return self._name_to_code[name]
        if fuzzy_match:
            for cached_name, code in self._name_to_code.items():
                if name in cached_name or cached_name in name:
                    logger.info(f"模糊匹配: {name} -> {code} ({cached_name})")
                    return code
        return None

    def get_name_from_code(self, code: str) -> Optional[str]:
        """根据代码返回名称；找不到返回 ``None``。"""
        self._ensure_loaded()
        if code is None:
            return None
        key = str(code).strip()
        if key in self._code_to_name:
            return self._code_to_name[key]
        # A股/ETF 代码可能为 6 位补齐格式
        zfilled = key.zfill(6)
        if zfilled in self._code_to_name:
            return self._code_to_name[zfilled]
        return None


# 模块级单例缓存
_name_provider_instance: Optional["StockNameProvider"] = None


def get_name_provider() -> "StockNameProvider":
    """
    便捷函数：获取股票名称转换 Provider 实例（进程级单例）。

    与旧 ``stock-cloud`` 中的 ``get_name_provider()`` 语义一致，并在其基础上
    扩展了 ETF / 港股映射与模糊匹配能力。
    """
    global _name_provider_instance
    if _name_provider_instance is None:
        _name_provider_instance = StockNameProvider()
    return _name_provider_instance
