import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pandas as pd

from stockdata_hub.normalization import (
    CANONICAL_COLUMNS,
    normalize_ohlcv,
)


def _make_df(columns, rows):
    return pd.DataFrame(rows, columns=columns)


def test_normalize_canonical_columns():
    df = _make_df(
        ["date", "open", "high", "low", "close", "volume"],
        [
            ("2024-01-01", 10, 11, 9, 10.5, 1000),
            ("2024-01-02", 10.5, 12, 10, 11.2, 1200),
            ("2024-01-03", 11, 13, 10.5, 12.0, 900),
        ],
    )
    out, err = normalize_ohlcv(df, days=2)
    assert err is None, err
    assert list(out.columns) == CANONICAL_COLUMNS
    assert len(out) == 2
    assert out.iloc[-1]["close"] == 12.0


def test_normalize_chinese_and_alias_columns():
    # 中文列名 + time 别名
    df = _make_df(
        ["time", "开盘", "最高", "最低", "收盘", "成交量"],
        [
            ("2024-01-01", 10, 11, 9, 10.5, 1000),
            ("2024-01-02", 10.5, 12, 10, 11.2, 1200),
        ],
    )
    out, err = normalize_ohlcv(df, days=10)
    assert err is None, err
    assert list(out.columns)[:6] == CANONICAL_COLUMNS
    assert out.iloc[0]["open"] == 10


def test_normalize_keeps_ma():
    df = _make_df(
        ["date", "open", "high", "low", "close", "volume", "ma5avgprice"],
        [("2024-01-03", 11, 13, 10.5, 12.0, 900, 11.5)],
    )
    out, err = normalize_ohlcv(df, days=10)
    assert err is None, err
    assert "ma5" in out.columns
    assert out.iloc[0]["ma5"] == 11.5


def test_normalize_missing_column_errors():
    df = _make_df(["date", "open", "high", "low"], [("2024-01-01", 1, 2, 1)])
    out, err = normalize_ohlcv(df, days=10)
    assert out is None
    assert "缺少必要列" in err


def test_normalize_drops_extra_columns():
    df = _make_df(
        ["date", "open", "high", "low", "close", "volume", "source"],
        [("2024-01-01", 10, 11, 9, 10.5, 1000, "baidu")],
    )
    out, err = normalize_ohlcv(df, days=10)
    assert err is None
    assert "source" not in out.columns


if __name__ == "__main__":
    test_normalize_canonical_columns()
    test_normalize_chinese_and_alias_columns()
    test_normalize_keeps_ma()
    test_normalize_missing_column_errors()
    test_normalize_drops_extra_columns()
    print("test_normalization OK")
