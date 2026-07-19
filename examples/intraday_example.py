#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stockdata-hub 分钟数据（Intraday）使用示例
=========================================

演示 ``fetch_minute`` / ``StockDataFetcher.fetch_intraday`` 的常见用法：
基础取数、多周期、查命中源、盘中轮询缓存、错误处理、openstockdata 可选源状态。

运行::

    cd stockdata-hub
    uv run python examples/intraday_example.py
    # 或： python examples/intraday_example.py

注意：示例需要可直连对应数据源的网络环境；取不到数据时仅打印 reason，不抛异常。
完整 API 参考见 ``docs/intraday_api.md``。
"""
from __future__ import annotations

import time

from stockdata_hub import (
    OPENSTOCKDATA_AVAILABLE,
    StockDataFetcher,
    fetch_minute,
)


def demo_basic() -> None:
    """基础：取当日 1 分钟 K 线。"""
    print("=== 1) 基础：当日 1 分钟 ===")
    df, reason, code = fetch_minute("600519", period="1m", days=1)
    if df is None:
        print("  失败:", reason)
        return
    print(f"  代码={code} 行数={len(df)} 列={list(df.columns)}")
    print(df[["datetime", "close", "volume"]].tail(3).to_string(index=False))


def demo_multi_period() -> None:
    """多周期 + 指定根数。"""
    print("\n=== 2) 多周期 + 取最近 30 根 ===")
    for period in ("5m", "15m", "30m", "60m"):
        df, reason, code = fetch_minute("000001", period=period, days=5, count=30)
        if df is None:
            print(f"  {period}: 失败 {reason}")
        else:
            print(f"  {period}: 行数={len(df)} 末根={df['datetime'].iloc[-1]}")


def demo_last_provider() -> None:
    """查看实际命中的分钟源（多源兜底透明）。"""
    print("\n=== 3) 查看命中源 ===")
    fetcher = StockDataFetcher()
    df, reason, code = fetcher.fetch_intraday("600519", period="1m", days=1)
    if df is None:
        print("  失败:", reason)
        return
    print(f"  命中源: {fetcher.get_last_used_provider()}  行数={len(df)}")


def demo_polling() -> None:
    """盘中轮询：use_cache 默认开启，TTL 内返回缓存、跨 TTL 去重合并。"""
    print("\n=== 4) 盘中轮询（演示 3 轮，避免死循环）===")
    fetcher = StockDataFetcher()
    for i in range(3):
        df, reason, code = fetcher.fetch_intraday("600519", period="1m", days=1)
        if df is None:
            print(f"  第{i + 1}轮 失败: {reason}")
        else:
            print(f"  第{i + 1}轮 行数={len(df)} 最近收盘={df['close'].iloc[-1]}")
        time.sleep(2)  # 真实场景按周期 TTL 设间隔（1m/5m=60s）


def demo_error_handling() -> None:
    """错误处理：reason 表达，无需 try/except 库内部异常。"""
    print("\n=== 5) 错误处理 ===")
    df, reason, code = fetch_minute("600519", period="7m")  # 非法 period
    print(f"  非法 period -> df={df} reason={reason!r}")
    assert df is None and reason == "不支持的 period: 7m"


def demo_openstockdata_status() -> None:
    """openstockdata（百度）为可选源：未安装时自动降级、不参与兜底。"""
    print("\n=== 6) openstockdata 可选源状态 ===")
    if OPENSTOCKDATA_AVAILABLE:
        print("  openstockdata 已安装：'openstockdata分钟' 源可用（优先级 4，分钟兜底）")
    else:
        print("  openstockdata 未安装：'openstockdata分钟' 自动跳过（不影响其它源）")
        print("  安装： uv add --extra openstockdata   # 即 pip install cn-a-stock-data")


def main() -> None:
    demo_basic()
    demo_multi_period()
    demo_last_provider()
    demo_polling()
    demo_error_handling()
    demo_openstockdata_status()
    print("\n全部示例结束。取数失败多为网络/依赖问题，参考 docs/intraday_api.md。")


if __name__ == "__main__":
    main()
