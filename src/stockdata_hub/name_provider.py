#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票名称 -> 代码 转换 Provider（可选，依赖 akshare）。

用于在 ``fetch("贵州茅台")`` 这种「按名称取数」的场景下把名称解析为代码。
数据通过 akshare 拉取并缓存（见 :mod:`stockdata_hub.cache`）。

依赖（可选 extra ``akshare``）：``akshare``。未安装时
:meth:`StockNameProvider.get_stock_code_from_name` 返回 ``None``。
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from .cache import get_cache_manager

logger = logging.getLogger(__name__)


class StockNameProvider:
    """股票名称 <-> 代码 映射 Provider（akshare + 缓存）。"""

    def __init__(self) -> None:
        self.name = "股票名称转换"
        self._name_to_code: Dict[str, str] = {}
        self._code_to_name: Dict[str, str] = {}
        try:
            self._cache = get_cache_manager()
        except Exception:  # noqa: BLE001
            self._cache = None

    def _load_a_stock_map(self) -> None:
        try:
            import akshare as ak

            df = self._cache.get_stock_list() if self._cache else None
            if df is None:
                df = ak.stock_info_a_code_name()
                if self._cache is not None and df is not None and not df.empty:
                    self._cache.set_stock_list(df)
            if df is None or df.empty:
                return
            for _, row in df.iterrows():
                code = str(row["code"]).zfill(6)
                nm = str(row["name"]).strip()
                if nm and nm != "nan":
                    self._name_to_code[nm] = code
                    self._code_to_name[code] = nm
            logger.info(f"已建立 A股名称映射: {len(self._name_to_code)} 条")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载 A股名称映射失败: {e}")

    def get_stock_code_from_name(self, stock_name: str) -> Optional[str]:
        """根据股票名称返回 6 位代码；找不到返回 ``None``。"""
        if not self._name_to_code:
            self._load_a_stock_map()
        return self._name_to_code.get(stock_name.strip())

    def get_name_from_code(self, code: str) -> Optional[str]:
        """根据代码返回名称；找不到返回 ``None``。"""
        if not self._code_to_name:
            self._load_a_stock_map()
        return self._code_to_name.get(code)
