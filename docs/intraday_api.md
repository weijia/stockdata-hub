# 接口文档：统一分时/分钟数据 API（API Reference）

> 配套文档：需求 `intraday_requirements.md`、设计 `intraday_design.md`。
> 本文档面向**调用者**，给出公开函数的完整参数/返回值/错误码/契约字段与示例。

## 1. 概述

本接口是 `stockdata-hub` 新增的**分钟级数据统一入口**，与现有日线接口 `fetch_stock_data` 平行，遵循同一套「多源兜底 + 统一格式 + 限流防封」范式（实现细节见设计 §6）。

三种调用方式（任选其一）：

| 方式 | 适用场景 | 是否解析中文名 |
|---|---|---|
| `StockDataFetcher.fetch_intraday(...)` | 已持有 fetcher 实例（推荐） | ✅ 是 |
| `fetch_minute(...)`（顶层便捷函数） | 一行调用，无需建实例 | ✅ 是 |
| `DataProviderManager.get_intraday(...)` | 高级/编程接口，需手动管理 manager | ❌ 否（接收已标准化代码） |

## 2. 公开 API

### 2.1 `StockDataFetcher.fetch_intraday`

```python
fetcher.fetch_intraday(
    symbol: str,
    period: str = "1m",
    days: int = 1,
    count: Optional[int] = None,
    use_cache: bool = True,
) -> Tuple[Optional[pd.DataFrame], Optional[str], Optional[str]]
```

**参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `symbol` | `str` | 必填 | 股票代码或名称；支持 `"600519"`、`"sh600519"`、`"贵州茅台"` 等，内部经 `_resolve_symbol` 解析 |
| `period` | `str` | `"1m"` | 周期枚举，见 §4：`"1m"/"5m"/"15m"/"30m"/"60m"`（分钟）；`"1d"` 不被分钟 Provider 处理 |
| `days` | `int` | `1` | 时间窗口（**日历日**）：保留 `datetime >= now - days` 的分钟 bar（见设计 §4 归一化窗口过滤） |
| `count` | `Optional[int]` | `None` | 若给定，窗口过滤后**仅取最后 `count` 根** bar；`None` 表示取窗口内全部 |
| `use_cache` | `bool` | `True` | 是否启用轮询缓存（键 `(symbol, period)`、TTL 随周期、去重合并，见设计 §6.4）；测试/强制刷新传 `False` |

**返回值**（三元组，与 `fetch_stock_data` 语义一致）

| 位置 | 名称 | 类型 | 说明 |
|---|---|---|---|
| `[0]` | `df` | `Optional[pd.DataFrame]` | 成功为统一分钟 DataFrame（列见 §3）；失败为 `None` |
| `[1]` | `reason` | `Optional[str]` | 成功为 `None`；失败为错误原因字符串（见 §5） |
| `[2]` | `code` | `Optional[str]` | 成功为**已解析的股票代码**（如 `"600519"`）；失败为 `None` |

**示例**

```python
from stockdata_hub import StockDataFetcher

fetcher = StockDataFetcher()
df, reason, code = fetcher.fetch_intraday("600519", period="1m", days=1)
if df is not None:
    print(code, len(df))        # 600519 240
    print(df[["datetime", "close", "volume"]].tail())
else:
    print("失败:", reason)
```

### 2.2 `fetch_minute`（顶层便捷函数）

```python
from stockdata_hub import fetch_minute

fetch_minute(
    symbol: str,
    period: str = "1m",
    days: int = 1,
    count: Optional[int] = None,
    use_cache: bool = True,
) -> Tuple[Optional[pd.DataFrame], Optional[str], Optional[str]]
```

等价于 `get_default_manager().get_intraday(已解析 symbol, ...)`，返回三元组同 §2.1。`symbol` 同样支持代码/名称解析。

```python
from stockdata_hub import fetch_minute

df, reason, code = fetch_minute("贵州茅台", period="5m", days=5)
```

### 2.3 `DataProviderManager.get_intraday`（高级接口）

```python
manager.get_intraday(
    symbol: str,
    period: str = "1m",
    days: int = 1,
    count: Optional[int] = None,
) -> Tuple[Optional[pd.DataFrame], Optional[str]]
```

- 接收**已标准化代码**（如 `"600519"`，不做中文名解析）。
- 返回二元组 `(df, reason)`，无 `code` 字段。
- 非法 `period` 在兜底循环**前**直接返回 `(None, "不支持的 period: ...")`。
- 仅遍历 `can_handle_request(symbol, days, period)` 为 `True` 的分钟 Provider（设计 §5）。

### 2.4 `DataProvider.fetch_data(symbol, days, period)`（契约扩展）

所有 Provider 的 `fetch_data` 抽象方法新增可选 `period` 参数（默认 `"1d"`）。现有日线 Provider 对分钟 `period` 在 `can_handle_request` 返回 `False`，**零改动**；分钟 Provider 据此映射源私有参数（设计 §3.1）。

## 3. 统一分钟契约（返回 DataFrame）

成功时 `df` 为 `pandas.DataFrame`，列如下：

| 列名 | 类型 | 单位 | 说明 |
|---|---|---|---|
| `datetime` | `datetime64[ns]` | — | 含**时分秒**的 K 线开始时间（如 `2026-07-17 09:31:00`） |
| `open` | `float` | 元 | 开盘价 |
| `high` | `float` | 元 | 最高价 |
| `low` | `float` | 元 | 最低价 |
| `close` | `float` | 元 | 收盘价 |
| `volume` | `float` | **手**（lot） | 成交量，A股/ETF 1 手 = 100 股 |
| `amount` | `float`（可选） | 元 | 成交额；部分源（如东财）提供，缺失时不强制补齐 |

**与日线契约的差异**：仅把日线的 `date`（纯日期）升格为 `open/high/low/close/volume` 语义完全一致，下游消费代码可共享（设计 §4）。

**不复权**：分钟 K 线默认不复权，与 mootdx `bars` 行为一致；跨除权日需调用方自行复权（需求 FR-4）。

## 4. period 枚举与源覆盖

**周期映射表**（源私有参数，设计 §3.1；与 a-stock-data SKILL.md 实测一致）

| `period` | mootdx `frequency` | 东财 `klt` | 新浪 `ktype` | openstockdata `ktype` |
|---|---|---|---|---|
| `"1m"` | `8` | `1` | `"1"` | `"m"`（待实测） |
| `"5m"` | `0` | `5` | `"5"` | `"5"` |
| `"15m"` | `1` | `15` | `"15"` | `"15"` |
| `"30m"` | `2` | `30` | `"30"` | `"30"` |
| `"60m"` | `3` | `60` | `"60"` | `"60"` |

> 注：mootdx `frequency=7` 是「1 分钟除权口径」，不用于本接口；标准 1 分钟用 `8`。

**兜底优先级**（数值越小越优先，设计 §5）

| 优先级 | Provider | 源 | 特点 |
|---|---|---|---|
| 1 | `MootdxMinuteProvider` | 通达信 TCP | 实时、不封 IP、最快 |
| 2 | `EastMoneyMinuteProvider` | 东财 HTTP | 历史深、需限流（≥1s + 抖动） |
| 3 | `SinaMinuteProvider` | 新浪 HTTP | 兜底 |
| 4（可选） | `OpenStockDataMinuteProvider` | 百度 | 后置，`ktype` 待实测 |

## 5. 错误与异常

- **不抛出未捕获异常**：所有错误经兜底后仍失败，以返回值 `reason` 表达，调用方无需 `try/except` 库内部异常。
- **单源异常隔离**：某一源抛异常仅记 warning 并 `continue` 到下一源（设计 §6.1）。
- **限流透明**：403/429 由库内部令牌桶限流器处理，调用方无需感知（设计 §6.5）。

**`reason` 错误原因字符串枚举**

| reason | 触发条件 |
|---|---|
| `"无效的股票代码或名称"` | `symbol` 为空 |
| `"不支持的 period: <p>"` | `period` 不在枚举内（前置拦截，不进入兜底） |
| `"所有分钟 Provider 都无法获取数据"` | 兜底链全部失败/被 `can_handle_request` 排除 |
| `"无法获取分钟数据"` | 兜底耗尽且上游未给出具体 reason |
| 源内部错误（透传） | 如 `"TDX 连接超时"`、`"东财请求失败: ..."` 等，原样透传用于排查 |

## 6. 完整示例

### 6.1 基础：取当日 1 分钟

```python
df, reason, code = fetcher.fetch_intraday("600519", period="1m", days=1)
# df.columns -> ['datetime','open','high','low','close','volume'] (+'amount' 若源提供)
```

### 6.2 指定根数：取最近 30 根 5 分钟

```python
df, _, _ = fetcher.fetch_intraday("000001", period="5m", days=5, count=30)
```

### 6.3 盘中轮询 + 缓存

```python
# 默认 use_cache=True：同一 (symbol, period) 在 TTL 内返回缓存（1m/5m 为 60s），去重合并不丢中间 bar
while trading:
    df, _, _ = fetcher.fetch_intraday("600519", period="1m", days=1)
    time.sleep(60)
```

### 6.4 查实际命中的源

```python
_, _, _ = fetcher.fetch_intraday("600519", period="1m")
print(fetcher.get_last_used_provider())   # 如 "mootdx" / "eastmoney"
```

### 6.5 错误处理

```python
df, reason, code = fetcher.fetch_intraday("600519", period="7m")  # 非法 period
# df is None, reason == "不支持的 period: 7m"
```

### 6.6 与日线混用（向后兼容）

```python
daily_df, _, _ = fetcher.fetch_stock_data("600519", days=30)      # 现有日线接口，行为不变
min_df,   _, _ = fetcher.fetch_intraday("600519", period="1m")    # 新增分钟接口
```

## 7. 性能与限流备注

- **mootdx（优先源）**：单 TCP 连接、非线程安全，库内部用共享单例 client + 连接锁串行访问；不封 IP，适合盘中实时轮询（设计 §6.2）。
- **东财 / 新浪（HTTP 源）**：每源独立令牌桶限流器（东财 `min_interval≥1.0s` + 抖动、新浪 `0.5s`），并发也超不过下限，规避封 IP（设计 §6.5）。
- **重试**：HTTP 源对瞬时网络错误做 2 次指数退避重试；超时类错误立即回退、不重试以保调用方（设计 §6.3）。
- **缓存**：轮询场景启用 `use_cache` 可显著降低重复拉取与封 IP 风险（设计 §6.4）。

## 8. 与现有日线接口对照

| 维度 | `fetch_stock_data`（日线，已有） | `fetch_intraday` / `fetch_minute`（分钟，新增） |
|---|---|---|
| 返回列 | `date` + OHLCV | `datetime` + OHLCV（+`amount`） |
| 周期 | 仅日线 | `1m/5m/15m/30m/60m` |
| 时间窗口 | `days`（交易日） | `days`（日历日）+ 可选 `count` |
| 缓存 | 原逻辑 | 新增 `use_cache` 轮询缓存 |
| 兜底源 | mootdx/东财/新浪/openstockdata（日线） | 同名分钟 Provider（独立类，互不干扰） |
| 复权 | 源相关 | 默认不复权 |
