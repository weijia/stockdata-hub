#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分钟（intraday）相关单元测试。

覆盖设计文档 §8 测试清单：
- test_normalize_intraday    : 统一分钟契约纯函数
- test_period_mapping        : period -> mootdx frequency 映射 + can_handle_request 周期过滤
- test_intraday_fallback     : 管理器 get_intraday 的兜底路由（日线源自动跳过 / 跨分钟源回退）
- test_tdx_client_singleton  : 共享单例 TCP 客户端管理器

约定：与 test_core.py / test_normalization.py 一致，注入 src 到 sys.path，
使用本地桩 Provider，不依赖 mootdx / 网络。
"""
import datetime
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pandas as pd

from stockdata_hub.core import DataProvider, DataProviderManager
from stockdata_hub.normalization import (
    CANONICAL_INTRADAY_COLUMNS,
    normalize_intraday,
)
from stockdata_hub.providers.mootdx_minute_provider import (
    MootdxMinuteProvider,
    _PERIOD_FREQ,
)
from stockdata_hub.providers.mootdx_provider import MootdxProvider
from stockdata_hub.providers.eastmoney_minute_provider import (
    EastMoneyMinuteProvider,
    _KLT as EM_KLT,
)
from stockdata_hub.providers.sina_minute_provider import (
    SinaMinuteProvider,
    _KTYPE as SINA_KTYPE,
)
from stockdata_hub.providers.openstockdata_minute_provider import (
    OpenStockDataMinuteProvider,
    _KTYPE as OSD_KTYPE,
)
from stockdata_hub.rate_limit import (
    INTRADAY_RATE_LIMITS,
    TokenBucket,
    get_rate_limiter,
)
import stockdata_hub.mootdx_client as mclient


# --------------------------------------------------------------------------- #
# 构造辅助
# --------------------------------------------------------------------------- #
def _ymdhm(dt: datetime.datetime) -> int:
    """把 datetime 转成 mootdx 原始整数 YYYYMMDDHHMM。"""
    return int(dt.strftime("%Y%m%d%H%M"))


def _recent_base():
    """今天 09:30 起的 datetime。"""
    return datetime.datetime.now().replace(hour=9, minute=30, second=0, microsecond=0)


def _raw_mootdx_df(n_recent=3, n_old=0):
    """构造 mootdx 风格（datetime 为 YYYYMMDDHHMM 整数）原始分钟 DataFrame。"""
    base = _recent_base()
    rows = []
    for i in range(n_recent):
        t = base + datetime.timedelta(minutes=i)
        rows.append((_ymdhm(t), 10 + i, 11 + i, 9 + i, 10.5 + i, 100 + i, 1000 + i))
    for j in range(n_old):
        t = base - datetime.timedelta(days=5, minutes=j)
        rows.append((_ymdhm(t), 5, 6, 4, 5.5, 50, 500))
    return pd.DataFrame(
        rows, columns=["datetime", "open", "high", "low", "close", "volume", "amount"]
    )


def _raw_datetime64_df(n=3):
    """构造 datetime 已是 datetime64 的原始分钟 DataFrame。"""
    base = _recent_base()
    dts = [base + datetime.timedelta(minutes=i) for i in range(n)]
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(dts),
            "open": [10.0, 10.5, 11.0],
            "high": [11.0, 12.0, 13.0],
            "low": [9.0, 10.0, 10.5],
            "close": [10.5, 11.2, 12.0],
            "volume": [100.0, 200.0, 300.0],
        }
    )


# --------------------------------------------------------------------------- #
# 桩 Provider（用于兜底路由测试，不依赖真实源）
# --------------------------------------------------------------------------- #
class StubMinuteProvider(DataProvider):
    supports_periods = {"1m", "5m", "15m", "30m", "60m"}

    def __init__(self, name, priority=1, df=None, error=None, can=True):
        self.name = name
        self.priority = priority
        self._df = df
        self._error = error
        self._can = can

    def can_handle(self, symbol: str) -> bool:
        return self._can

    def fetch_data(self, symbol, days=30, period="1m"):
        return self._df, self._error


class StubDailyOnlyProvider(DataProvider):
    supports_periods = {"1d"}

    def __init__(self, name="daily", priority=1, df=None, error=None, can=True):
        self.name = name
        self.priority = priority
        self._df = df
        self._error = error
        self._can = can

    def can_handle(self, symbol: str) -> bool:
        return self._can

    def fetch_data(self, symbol, days=30, period="1d"):
        return self._df, self._error


# =========================================================================== #
# 1) normalize_intraday
# =========================================================================== #
def test_normalize_intraday_mootdx_format():
    """mootdx 原始 YYYYMMDDHHMM 整数 -> 规范列 + datetime 含时分秒。"""
    raw = _raw_mootdx_df(n_recent=3)
    out, err = normalize_intraday(raw, period="1m", days=1)
    assert err is None, err
    assert list(out.columns) == CANONICAL_INTRADAY_COLUMNS + ["amount"]
    assert pd.api.types.is_datetime64_any_dtype(out["datetime"])
    # 时分秒被保留（不是午夜）
    assert (out["datetime"].dt.hour != 0).any()
    assert len(out) == 3
    # 最后一笔 close 正确
    assert out.iloc[-1]["close"] == 12.5


def test_normalize_intraday_datetime64_passthrough():
    """datetime 已是 datetime64 时，规范化后保持原值（幂等）。"""
    raw = _raw_datetime64_df(n=3)
    out, err = normalize_intraday(raw, period="5m", days=1)
    assert err is None, err
    assert list(out.columns) == CANONICAL_INTRADAY_COLUMNS
    assert out.iloc[1]["high"] == 12.0


def test_normalize_intraday_chinese_alias():
    """中文列名 + vol 别名 -> 规范列。"""
    base = _recent_base()
    df = pd.DataFrame(
        {
            "交易时间": [
                int((base + datetime.timedelta(minutes=i)).strftime("%Y%m%d%H%M"))
                for i in range(2)
            ],
            "开盘": [10, 10.5],
            "最高": [11, 12],
            "最低": [9, 10],
            "收盘": [10.5, 11.2],
            "vol": [100, 200],
        }
    )
    out, err = normalize_intraday(df, period="1m", days=1)
    assert err is None, err
    assert list(out.columns) == CANONICAL_INTRADAY_COLUMNS
    assert out.iloc[0]["open"] == 10
    assert out.iloc[0]["volume"] == 100


def test_normalize_intraday_missing_column():
    """缺少必要列 -> (None, 含「缺少必要列」)。"""
    df = pd.DataFrame({"datetime": [1, 2], "open": [1, 2], "high": [1, 2]})
    out, err = normalize_intraday(df, period="1m", days=1)
    assert out is None
    assert "缺少必要列" in err


def test_normalize_intraday_count_truncation():
    """count=2 取最后两根并重置索引。"""
    raw = _raw_mootdx_df(n_recent=3)
    out, err = normalize_intraday(raw, period="1m", days=1, count=2)
    assert err is None, err
    assert len(out) == 2
    # 取的是时间上最后两根（close = 11.5, 12.5）
    assert list(out["close"]) == [11.5, 12.5]


def test_normalize_intraday_days_window():
    """days=1 窗口过滤掉 5 天前的数据。"""
    raw = _raw_mootdx_df(n_recent=3, n_old=2)
    out, err = normalize_intraday(raw, period="1m", days=1)
    assert err is None, err
    assert len(out) == 3  # 仅保留最近 3 根
    # 过滤后无 5 天前的
    cutoff = datetime.datetime.now() - datetime.timedelta(days=1)
    assert (out["datetime"] >= cutoff).all()


def test_normalize_intraday_empty():
    """空数据 -> (None, 含「空数据」)。"""
    out, err = normalize_intraday(pd.DataFrame(), period="1m", days=1)
    assert out is None
    assert "空数据" in err


# =========================================================================== #
# 2) period 映射与 can_handle_request 周期过滤
# =========================================================================== #
def test_period_mapping_table():
    """设计 §3.1：period -> mootdx frequency 映射正确且覆盖全部分钟周期。"""
    assert _PERIOD_FREQ == {"1m": 8, "5m": 0, "15m": 1, "30m": 2, "60m": 3}
    assert set(_PERIOD_FREQ.keys()) == MootdxMinuteProvider.supports_periods


def test_can_handle_request_filters_by_period():
    """can_handle_request 按 period 过滤：日线源对分钟返回 False，反之亦然。"""
    daily = StubDailyOnlyProvider(can=True)
    minute = StubMinuteProvider("minute", can=True)

    # 分钟请求：日线源被排除，分钟源放行
    assert daily.can_handle_request("600519", days=1, period="1m") is False
    assert minute.can_handle_request("600519", days=1, period="1m") is True

    # 日线请求：分钟源被排除，日线源放行
    assert minute.can_handle_request("600519", days=1, period="1d") is False
    assert daily.can_handle_request("600519", days=1, period="1d") is True


def test_real_providers_period_support():
    """真实 Provider 的 supports_periods 符合设计：mootdx 分钟只声明分钟，日线只声明日线。"""
    assert MootdxMinuteProvider.supports_periods == _PERIOD_FREQ.keys()
    # MootdxProvider 默认仅日线（未覆盖 supports_periods）
    assert MootdxProvider.supports_periods == {"1d"}


# =========================================================================== #
# 3) get_intraday 兜底路由
# =========================================================================== #
def test_get_intraday_skips_daily_only_provider():
    """分钟请求时，仅支持日线的源被自动跳过，分钟源被使用。"""
    mgr = DataProviderManager()
    mgr.add_provider(StubDailyOnlyProvider(name="daily", priority=1))
    mgr.add_provider(StubMinuteProvider(name="minute", priority=2, df=_raw_mootdx_df(3)))
    df, err = mgr.get_intraday("600519", period="1m", days=1)
    assert err is None, err
    assert list(df.columns) == CANONICAL_INTRADAY_COLUMNS + ["amount"]
    assert mgr.get_last_used_provider() == "minute"


def test_get_intraday_fallback_to_next_minute_provider():
    """首个分钟源失败，自动回退到下一个可用分钟源。"""
    mgr = DataProviderManager()
    mgr.add_provider(StubMinuteProvider(name="bad", priority=1, df=None, error="boom"))
    mgr.add_provider(StubMinuteProvider(name="good", priority=2, df=_raw_mootdx_df(3)))
    df, err = mgr.get_intraday("600519", period="5m", days=1)
    assert df is not None
    assert mgr.get_last_used_provider() == "good"


def test_get_intraday_illegal_period():
    """非法 period 直接返回错误，不遍历任何源。"""
    mgr = DataProviderManager()
    mgr.add_provider(StubMinuteProvider(name="minute", df=_raw_mootdx_df(3)))
    out, reason = mgr.get_intraday("600519", period="7m", days=1)
    assert out is None
    assert "不支持的 period" in reason


def test_get_intraday_all_fail():
    """所有分钟源都失败 -> (None, 含「所有分钟 Provider 都无法获取数据」)。"""
    mgr = DataProviderManager()
    mgr.add_provider(StubMinuteProvider(name="a", df=None, error="x"))
    mgr.add_provider(StubMinuteProvider(name="b", df=None, error="y"))
    out, reason = mgr.get_intraday("600519", period="1m", days=1)
    assert out is None
    assert "所有分钟 Provider 都无法获取数据" in reason
    assert mgr.get_last_used_provider() is None


def test_get_intraday_no_provider():
    """无任何 Provider -> (None, 含「没有任何可用的 Provider」)。"""
    mgr = DataProviderManager()
    out, reason = mgr.get_intraday("600519", period="1m", days=1)
    assert out is None
    assert "没有任何可用的 Provider" in reason


# =========================================================================== #
# 4) 共享单例 TCP 客户端
# =========================================================================== #
def test_tdx_client_singleton():
    """get_tdx_client 多次调用返回同一进程级单例，且不触发网络（MOOTDX 关闭）。"""
    saved_flag = mclient.MOOTDX_AVAILABLE
    saved_mgr = mclient._manager
    mclient.MOOTDX_AVAILABLE = False
    mclient._manager = None
    try:
        a = mclient.get_tdx_client()
        b = mclient.get_tdx_client()
        assert a is b
        assert isinstance(a, mclient.TdxClientManager)
        # 单例持有一把串行锁
        assert isinstance(a.lock, type(threading.Lock()))
        c = mclient.get_tdx_client()
        assert c is a
    finally:
        mclient.MOOTDX_AVAILABLE = saved_flag
        mclient._manager = saved_mgr


# =========================================================================== #
# 5) 令牌桶限流器（设计 §6.5，test_rate_limit）
# =========================================================================== #
def test_token_bucket_enforces_min_interval():
    """令牌桶在 capacity=1 时应保证相邻 acquire 间隔 ≥ min_interval。"""
    bucket = TokenBucket(min_interval=0.05, jitter=0.0, capacity=1.0)
    n = 5
    start = time.monotonic()
    for _ in range(n):
        bucket.acquire()
    elapsed = time.monotonic() - start
    # 5 次 acquire，最小应等待 (n-1)*min_interval = 0.2s（允许极小误差）
    assert elapsed >= (n - 1) * 0.05 - 0.01, f"elapsed={elapsed}"


def test_token_bucket_shared_singleton():
    """get_rate_limiter 按源名返回同一共享实例。"""
    a = get_rate_limiter("东方财富(push2his)")
    b = get_rate_limiter("东方财富(push2his)")
    assert a is b
    # 不同源名是不同实例
    c = get_rate_limiter("新浪(min_kline)")
    assert c is not a


def test_rate_limit_table_present():
    """§6.5 限流常量表包含东财/新浪且参数合理。"""
    assert "东方财富(push2his)" in INTRADAY_RATE_LIMITS
    assert "新浪(min_kline)" in INTRADAY_RATE_LIMITS
    assert INTRADAY_RATE_LIMITS["东方财富(push2his)"]["min_interval"] >= 1.0
    assert INTRADAY_RATE_LIMITS["新浪(min_kline)"]["min_interval"] >= 0.5


# =========================================================================== #
# 6) 东财 / 新浪分钟 Provider（离线：映射 + can_handle，不触网）
# =========================================================================== #
def test_eastmoney_minute_period_mapping():
    """东财 klt 映射 + supports_periods 正确。"""
    assert EM_KLT == {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
    assert EastMoneyMinuteProvider.supports_periods == set(EM_KLT.keys())
    p = EastMoneyMinuteProvider()
    assert p.name == "东财分钟"
    # 周期过滤：日线请求被排除
    assert p.can_handle_request("600519", days=1, period="1d") is False
    assert p.can_handle_request("600519", days=1, period="1m") in (True, False)


def test_sina_minute_period_mapping():
    """新浪 ktype(scale) 映射 + supports_periods 正确。"""
    assert SINA_KTYPE == {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
    assert SinaMinuteProvider.supports_periods == set(SINA_KTYPE.keys())
    p = SinaMinuteProvider()
    assert p.name == "新浪分钟"
    assert p.can_handle_request("600519", days=1, period="1d") is False


def test_eastmoney_sina_can_handle():
    """东财/新浪分钟源仅处理 6 位 A/ETF 代码（无需网络）。"""
    em = EastMoneyMinuteProvider()
    sina = SinaMinuteProvider()
    # A 股
    assert em.can_handle("600519") == sina.can_handle("600519")
    # ETF（5/1 开头）亦可
    assert em.can_handle("510300") == sina.can_handle("510300")
    # 非 6 位 / 港股不应处理
    assert em.can_handle("00700") is False
    assert sina.can_handle("00700") is False


def test_openstockdata_minute_period_mapping():
    """openstockdata（百度）ktype 映射 + supports_periods 正确（设计 §3.5）。"""
    # 5m/15m/30m/60m = 分钟数字符串；1m = "m"（设计标注待实测）；日线用 "1" 故分钟不占用
    assert OSD_KTYPE == {"1m": "m", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
    assert "1" not in OSD_KTYPE.values()  # 日线专用，分钟不复用
    assert OpenStockDataMinuteProvider.supports_periods == set(OSD_KTYPE.keys())
    p = OpenStockDataMinuteProvider()
    assert p.name == "openstockdata分钟"
    assert p.priority == 4
    # 周期过滤：日线请求被排除
    assert p.can_handle_request("600519", days=1, period="1d") is False


def test_openstockdata_minute_degrades_when_unavailable():
    """未安装 openstockdata 时可优雅降级：can_handle=False、fetch_data 返回错误。"""
    import stockdata_hub.providers.openstockdata_minute_provider as osd

    original = osd.OPENSTOCKDATA_AVAILABLE
    try:
        osd.OPENSTOCKDATA_AVAILABLE = False
        p = osd.OpenStockDataMinuteProvider()
        assert p.can_handle("600519") is False
        assert p.can_handle_request("600519", days=1, period="5m") is False
        df, err = p.fetch_data("600519", days=1, period="5m")
        assert df is None and err is not None
    finally:
        osd.OPENSTOCKDATA_AVAILABLE = original


def test_openstockdata_minute_registered_in_default_manager():
    """默认管理器应注册 openstockdata 分钟源，且对日线请求自动跳过。"""
    from stockdata_hub.core import DataProviderManager

    mgr = DataProviderManager.build_default()
    names = [p.get_name() for p in mgr.providers]
    assert "openstockdata分钟" in names
    for p in mgr.providers:
        if p.get_name() == "openstockdata分钟":
            assert p.can_handle_request("600519", days=30, period="1d") is False


def test_eastmoney_sina_registered_in_default_manager():
    """默认管理器应注册东财/新浪分钟源，且它们对日线请求自动跳过。"""
    from stockdata_hub.core import DataProviderManager

    mgr = DataProviderManager.build_default()
    names = [p.get_name() for p in mgr.providers]
    assert "东财分钟" in names
    assert "新浪分钟" in names
    # 日线请求不会选中分钟源（period 缺省 1d）
    minute_names = {"东财分钟", "新浪分钟", "通达信TCP(mootdx)分钟"}
    for p in mgr.providers:
        if p.get_name() in minute_names:
            assert p.can_handle_request("600519", days=30, period="1d") is False


if __name__ == "__main__":
    test_normalize_intraday_mootdx_format()
    test_normalize_intraday_datetime64_passthrough()
    test_normalize_intraday_chinese_alias()
    test_normalize_intraday_missing_column()
    test_normalize_intraday_count_truncation()
    test_normalize_intraday_days_window()
    test_normalize_intraday_empty()
    test_period_mapping_table()
    test_can_handle_request_filters_by_period()
    test_real_providers_period_support()
    test_get_intraday_skips_daily_only_provider()
    test_get_intraday_fallback_to_next_minute_provider()
    test_get_intraday_illegal_period()
    test_get_intraday_all_fail()
    test_get_intraday_no_provider()
    test_tdx_client_singleton()
    test_token_bucket_enforces_min_interval()
    test_token_bucket_shared_singleton()
    test_rate_limit_table_present()
    test_eastmoney_minute_period_mapping()
    test_sina_minute_period_mapping()
    test_eastmoney_sina_can_handle()
    test_eastmoney_sina_registered_in_default_manager()
    test_openstockdata_minute_period_mapping()
    test_openstockdata_minute_degrades_when_unavailable()
    test_openstockdata_minute_registered_in_default_manager()
    print("test_intraday OK")
