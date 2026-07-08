import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stockdata_hub.code_utils import (
    StockCodeNormalizer,
    validate_and_normalize_stock_code,
    validate_stock_code,
)

import pandas as pd


def test_normalize_stock_code():
    assert StockCodeNormalizer.normalize_stock_code("000001.SZ") == "000001"
    assert StockCodeNormalizer.normalize_stock_code("600036.SH") == "600036"
    assert StockCodeNormalizer.normalize_stock_code("abc123") is None
    assert StockCodeNormalizer.normalize_stock_code("") is None
    assert StockCodeNormalizer.normalize_stock_code("123") is None  # 太短
    assert StockCodeNormalizer.normalize_stock_code("1234567") is None  # 太长


def test_get_market_type():
    assert StockCodeNormalizer.get_market_type("600519") == "A"
    assert StockCodeNormalizer.get_market_type("000001") == "A"
    assert StockCodeNormalizer.get_market_type("300750") == "A"
    assert StockCodeNormalizer.get_market_type("510050") == "ETF"
    assert StockCodeNormalizer.get_market_type("518880") == "ETF"
    assert StockCodeNormalizer.get_market_type("00700") == "HK"
    assert StockCodeNormalizer.get_market_type("12345") == "HK"
    assert StockCodeNormalizer.get_market_type("123") is None


def test_validate_and_normalize():
    ok, code, err = validate_and_normalize_stock_code("600036.SH")
    assert ok and code == "600036" and err is None
    ok2, code2, err2 = validate_and_normalize_stock_code("xyz")
    assert not ok2 and code2 is None and err2 is not None


def test_validate_stock_code():
    assert validate_stock_code("000001")
    assert not validate_stock_code("12")


if __name__ == "__main__":
    test_normalize_stock_code()
    test_get_market_type()
    test_validate_and_normalize()
    test_validate_stock_code()
    print("test_code_utils OK")
