#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票代码验证与标准化工具。

只接受「纯数字代码」(可带 .SZ/.SH/.HK 后缀)，并据此判断市场类型：

- ``A``    : 6 位，以 0/3/6 开头（沪深 A 股）
- ``ETF``  : 6 位，以 1/5 开头
- ``HK``   : 4–5 位数字（港股）

本模块为纯标准库实现，无任何第三方依赖，可被任意 Provider 复用。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple


class StockCodeNormalizer:
    """股票代码标准化工具类。"""

    @staticmethod
    def validate_stock_code(code: str) -> Tuple[bool, Optional[str]]:
        """验证股票代码是否为有效的纯数字代码。"""
        if not code:
            return False, "股票代码不能为空"

        clean_code = code.replace(".SZ", "").replace(".SH", "").replace(".HK", "")
        if not clean_code.isdigit():
            return False, f"股票代码必须为纯数字，当前包含非数字字符: {clean_code}"

        if len(clean_code) < 4 or len(clean_code) > 6:
            return False, f"股票代码长度必须在4-6位之间，当前长度: {len(clean_code)}"

        return True, None

    @staticmethod
    def normalize_stock_code(code: str) -> Optional[str]:
        """标准化股票代码为纯数字格式，验证失败返回 ``None``。"""
        clean_code = re.sub(r"[^0-9]", "", code)
        is_valid, _ = StockCodeNormalizer.validate_stock_code(clean_code)
        return clean_code if is_valid else None

    @staticmethod
    def format_a_stock_code(code: str) -> Optional[str]:
        """格式化为 6 位 A 股代码。"""
        clean_code = StockCodeNormalizer.normalize_stock_code(code)
        return clean_code if clean_code and len(clean_code) == 6 else None

    @staticmethod
    def format_hk_stock_code(code: str) -> Optional[str]:
        """格式化为港股代码（4–6 位）。"""
        clean_code = StockCodeNormalizer.normalize_stock_code(code)
        return clean_code if clean_code and len(clean_code) >= 4 else None

    @staticmethod
    def get_market_type(code: str) -> Optional[str]:
        """
        判断市场类型：``'A'`` / ``'ETF'`` / ``'HK'`` / ``None``。
        """
        clean_code = StockCodeNormalizer.normalize_stock_code(code)
        if not clean_code:
            return None

        if len(clean_code) == 6 and clean_code[0] in ("1", "5"):
            return "ETF"
        if len(clean_code) == 6 and clean_code[0] in ("0", "3", "6"):
            return "A"
        if 4 <= len(clean_code) <= 5:
            return "HK"
        return None


class StockCodeValidator:
    """股票代码验证装饰器工厂：确保方法只接收纯数字代码。"""

    @staticmethod
    def validate_method_input(method):
        """装饰器：在调用前校验并标准化 symbol。"""

        def wrapper(self, symbol: str, *args, **kwargs):
            is_valid, error = StockCodeNormalizer.validate_stock_code(symbol)
            if not is_valid:
                if hasattr(self, "fetch_data"):
                    return None, error
                return False

            clean_symbol = StockCodeNormalizer.normalize_stock_code(symbol)
            if clean_symbol is None:
                if hasattr(self, "fetch_data"):
                    return None, "代码标准化失败"
                return False

            return method(self, clean_symbol, *args, **kwargs)

        return wrapper


def validate_and_normalize_stock_code(
    code: str,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """统一入口：``(is_valid, normalized_code, error_message)``。"""
    if not code:
        return False, None, "股票代码不能为空"
    normalized_code = StockCodeNormalizer.normalize_stock_code(code)
    if normalized_code:
        return True, normalized_code, None
    return False, None, f"无效的股票代码格式: {code}"


def clean_stock_code(code: str) -> Optional[str]:
    """清理股票代码为标准格式。"""
    return StockCodeNormalizer.normalize_stock_code(code)


def validate_stock_code(code: str) -> bool:
    """验证股票代码是否有效。"""
    is_valid, _ = StockCodeNormalizer.validate_stock_code(code)
    return is_valid


def get_clean_stock_code(code: str) -> Optional[str]:
    """获取清理后的股票代码，失败返回 ``None``。"""
    return StockCodeNormalizer.normalize_stock_code(code)
