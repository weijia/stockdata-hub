# 接口文档：日 K 线数据 API（API Reference）

> 本文档面向**调用者**，给出 `stockdata-hub` 日 K 线（日线）相关公开函数的完整参数、返回值、契约字段、多源兜底机制与已知限制。
> 配套文档：`intraday_api.md`（分钟级）、`add_provider.md`（新增数据源）、`data_sources.md`（数据源清单）。

---

## 1. 概述

日 K 线接口是 `stockdata-hub` 最基础的统一入口，负责返回某只标的的**按日 OHLCV** 数据。

- 主入口：`StockDataFetcher.fetch_stock_data(symbol, days)` —— 经内置多源管理器兜底，返回**统一契约** `DataFrame`（带 `date` 列）。
- 平行接口：`fetch_intraday(...)` / `fetch_minute(...)` 返回**分钟**数据（带 `datetime` 列）。二者契约字段共用 `OHLCV`，下游处理可复用。

> ✅ **命名已厘清**：`fetch_stock_data` 名字偏泛（"股票数据"可指任意数据），现已新增清晰命名的 **`fetch_daily_kline`（日 K 线）** 作为正式入口；`fetch_stock_data` 保留为**向后兼容别名**（二者完全等价）。新代码请用 `fetch_daily_kline`。区分日 K 与分时，靠**方法名**（`fetch_daily_kline` vs `fetch_intraday`/`fetch_minute`）+ **返回的时间列名**（`date` 日线 vs `datetime` 分时）。

### 1.1 三种调用方式（任选其一）

| 方式 | 适用场景 | 是否解析中文名 | 是否归一化到统一契约 |
|---|---|---|---|
| `StockDataFetcher.fetch_daily_kline(...)` | 已持有 fetcher 实例（**推荐，名字清晰**） | ✅ 是 | ✅ 是 |
| `StockDataFetcher.fetch_stock_data(...)` | 同上，**向后兼容别名**（专指日 K 线） | ✅ 是 | ✅ 是 |
| `DataProviderManager.get_data(...)` | 低级/编程接口，手动管理 manager | ❌ 否（接收已标准化代码） | ✅ 是 |
| 具体 Provider 直连（如 `AStockProvider` / `MootdxProvider`） | 锁定某一数据源/口径（raw 或 qfq） | ❌ 否 | ❌ 否（返回该源**原始** DataFrame） |

> 关键点：**门面（`fetch_stock_data` / `get_data`）返回归一化契约**；**Provider 直连返回该源原始 DataFrame**（如 akshare 的中文列）。需要 `振幅/涨跌幅/涨跌额/换手率` 等衍生列时，必须直连 akshare 系 Provider（见 §6）。

---

## 2. 公开 API

### 2.1 `StockDataFetcher.fetch_daily_kline`（日 K 线，推荐）

```python
fetcher.fetch_daily_kline(
    symbol: str,
    days: int = 30,
) -> Tuple[Optional[pd.DataFrame], Optional[str], Optional[str]]
```

> `fetch_stock_data(symbol, days)` 是其**向后兼容别名**，行为完全一致。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `symbol` | str | 必填 | 股票代码（如 `"600519"`）或中文名（如 `"贵州茅台"`，需 `enable_name_resolution`） |
| `days` | int | `30` | 取最近 N 个交易日；按时间升序截取最后 `days` 条 |

**返回值**（三元组）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `df` | `DataFrame \| None` | 统一契约日 K；失败为 `None` |
| `reason` | `str \| None` | 失败原因；成功为 `None` |
| `code` | `str \| None` | 实际命中的代码（中文名会被解析为代码）；失败为 `None` |

**行为**：
1. 若 `symbol` 非纯数字且启用名称解析，先用 `StockNameProvider` 解析为代码。
2. 交给 `DataProviderManager.get_data` 走多源兜底链（§5）。
3. 成功记录 `get_last_used_provider()`（如 `"通达信TCP(mootdx)"`）。

```python
from stockdata_hub import StockDataFetcher

fetcher = StockDataFetcher()
df, reason, code = fetcher.fetch_daily_kline("600519", days=30)
if df is not None:
    print(df.tail())
    print("实际命中源:", fetcher.get_last_used_provider())
else:
    print("失败原因:", reason)
```

### 2.2 `DataProviderManager.get_data`（低级接口）

```python
manager.get_data(
    symbol: str,
    days: int = 30,
) -> Tuple[Optional[pd.DataFrame], Optional[str]]
```

- 不解析中文名，调用方需先自行标准化代码。
- 其余兜底逻辑与 `fetch_stock_data` 一致。
- 适合需要自定义 manager（调整优先级 / 增删源）的高级场景：

```python
from stockdata_hub import DataProviderManager, StockDataFetcher

mgr = DataProviderManager.build_default()
mgr.set_provider_priority("openstockdata", 0)   # 调整优先级
fetcher = StockDataFetcher(manager=mgr)
```

### 2.3 `fetch_kline`（openstockdata 便捷函数）

```python
from stockdata_hub.providers.openstockdata_provider import fetch_kline
df, err = fetch_kline("600519", days=30)
```

- 仅用 `OpenStockDataProvider`（百度/腾讯 K 线，alpha）取日 K。
- 返回 `(df, err)`，签名兼容旧 `stock-cloud` 的 `openstockdata_adapter.fetch_kline`。
- 单源、无兜底，一般不直接用于生产。

---

## 3. 统一契约（返回值 DataFrame 列）

经门面返回的 `df` 满足统一契约（`normalization.CANONICAL_COLUMNS`）：

| 列 | 类型 | 说明 |
|---|---|---|
| `date` | datetime64 | 交易日期 |
| `open` / `high` / `low` / `close` | float | 开/高/低/收 |
| `volume` | float | **成交量，单位 = 手 (lot)**（A股/ETF 1 手 = 100 股） |
| `amount` | float（可选） | 成交额 |
| `ma5` / `ma10` / `ma20` | float（可选） | 均线（源提供时保留） |

> ⚠️ **衍生列会被丢弃**：`normalize_ohlcv` 只保留上述规范列（含可选 `ma*`），**`振幅 / 涨跌幅 / 涨跌额 / 换手率` 在归一化时被剥离**。若业务需要这 4 列，必须**直连 akshare 系 Provider**（见 §6），它们返回原始中文列 DataFrame。

---

## 4. 多源兜底机制

### 4.1 流程

`DataProviderManager.get_data`（`core.py`）按如下顺序取数：

1. 按 `priority` **升序**遍历所有已注册 Provider。
2. 对每个 Provider：`can_handle_request(symbol, days)` 过滤（按市场类型 / `supports_periods`）。
3. 调用 `fetch_data`；单源抛异常被 `try/except` 吞掉并记 `warning`，**不拖垮整体**，继续下一个源。
4. 第一个返回非空且归一化成功的 Provider 即命中，记录 `_last_used_provider` 并返回。
5. 全部失败返回 `(None, "所有 Provider 都无法获取数据")`。

### 4.2 内置日线源优先级（越小越优先）

来自 `providers/__init__.py` 的约定：

| 优先级 | Provider | 依赖 | 口径 | 能力 |
|---|---|---|---|---|
| 0 | 腾讯批量实时 | 零额外依赖 | raw | 当日快照（批量，**仅 days=1**） |
| 1 | 通达信TCP(mootdx) | `mootdx` | **raw（不复权）** | A股/ETF 日线，最快 |
| 2 | openstockdata | `cn-a-stock-data` | raw | 百度/腾讯 K 线（alpha） |
| 3 | iTick / 新浪A股 | `itick-sdk` / `akshare` | raw | 全球 / A股日线 |
| 4 | 腾讯A股 / ETF(akshare) | `akshare` | raw | A股/ETF 日线 |
| 5 | 港股(akshare) | `akshare` | qfq | 港股日线 |
| 6 | A股(akshare) / 东财A股 | `akshare` | **qfq** | A股日线 |
| 7 | 东财替代 | 零额外依赖 | raw | 直连东财 K 线 |
| 10 | 通用(akshare) | `akshare` | qfq | A股/ETF/港股最后兜底 |

> 依赖缺失时对应 Provider 的 `can_handle` 返回 `False`，自动跳过，不影响其它源（"缺依赖降级"）。

### 4.3 ⚠️ 关键坑：默认返回的是 **raw（不复权）**，不是 qfq

门面的兜底链是 **raw 与 qfq 混合**、且 **raw 优先**：

- `mootdx`（raw，优先级 1）排在所有 qfq 源（A股 qfq = 6、港股 qfq = 5、通用 qfq = 10）**之前**。
- 因此 `fetch_stock_data("600519")` 默认会先命中 mootdx，**返回不复权（raw）日 K**，根本到不了东财 qfq。

若业务需要前复权，门面**无法满足**（详见 §6）。这是日 K 接口与分钟接口在"口径"上最易被踩的坑。

### 4.4 跨源校验最后一根 bar

`mootdx` 等历史源偶发返回错误的当日/前一日收盘价。管理器对命中源为 `通达信TCP(mootdx)` 时，会用**腾讯实时快照**校验并修正最后 1~2 根 bar 的收盘价（相对差异超 5‰ 才修正），避免错误收盘污染下游。

---

## 5. 前复权（qfq）专题

### 5.1 qfq 没有独立的"qfq 兜底链"

`DataProviderManager` 的优先级兜底**不区分 raw / qfq**，是混在一起按 `priority` 跑的。qfq 源（A股/港股/通用）只是整体链里**较低优先级**的几个节点，且前面被 raw 源挡住。

### 5.2 各 qfq Provider 自身是单源

| Provider | 市场 | 底层 | 是否多源兜底 |
|---|---|---|---|
| `AStockProvider` | A股 | 东财 `stock_zh_a_hist(adjust="qfq")` | 单源，无内部兜底 |
| `ETFProvider` | ETF | 东财 `fund_etf_hist_em` → 新浪 `fund_etf_hist_sina` | ETF 内部两源兜底 |
| `HKStockProvider` | 港股 | 东财 `stock_hk_hist(adjust="qfq")` | 单源 |
| `UniversalStockProvider` | A股/ETF/港股 | 内部串起上述三者 | **跨市场**路由（非同标的多源） |

### 5.3 如何稳定取到 qfq

不要走门面，直接锁定 qfq Provider：

```python
from stockdata_hub.providers.akshare_provider import (
    AStockProvider, ETFProvider, HKStockProvider, UniversalStockProvider
)

# A股前复权（单源：东财 qfq）
df, err = AStockProvider().fetch_data("600519", days=30)

# 港股前复权
df, err = HKStockProvider().fetch_data("00700", days=30)

# 通用前复权入口：内部按市场自动选 A股/ETF/港股，且永不碰 TDX raw
df, err = UniversalStockProvider().fetch_data("600519", days=30)
```

> `UniversalStockProvider` 的"链条"是**跨市场**的（A股 → ETF → 港股 各选其一），**不是针对同一只 A股的多源兜底**。对单只 A股，它本质仍只用 `AStockProvider` 一个 qfq 源——东财 qfq 挂了不会自动换别的 qfq 源。

### 5.4 qfq 直连返回的是原始 DataFrame（保留衍生列）

`AStockProvider` 等返回 akshare 原始中文列 DataFrame，字段含：`日期 / 开盘 / 收盘 / 最高 / 最低 / 成交量 / 成交额 / 振幅 / 涨跌幅 / 涨跌额 / 换手率`。其中后 4 列为归一化契约**不包含**的衍生列，需业务侧自行映射（参考 `stock_price_server._qfq_df_to_records`）。

### 5.5 raw 与 qfq 对照

| 维度 | raw（默认门面） | qfq（Provider 直连） |
|---|---|---|
| 入口 | `fetch_stock_data` / `get_data` | `AStockProvider` / `ETFProvider` / `HKStockProvider` / `UniversalStockProvider` |
| 优先源 | 通达信TCP(mootdx) 优先级 1 | 东财 qfq 等（优先级 5~10） |
| 是否复权 | 否（实际成交价） | 前复权 |
| 衍生列 | 归一化后无 | 原始 df 保留（振幅/涨跌幅/涨跌额/换手率） |
| 多源兜底 | 有（raw 链） | 仅跨市场（UniversalStockProvider） |

---

## 6. 错误与失败

- 全链失败：`fetch_stock_data` 返回 `(None, reason, None)`，常见 `reason`：
  - `"没有任何可用的 Provider"`（manager 为空）
  - `"所有 Provider 都无法获取数据"`（全部源失败）
  - `"无效的股票代码或名称"`（`symbol` 为空）
- 单源失败：被管理器吞掉并记录 `warning`，继续尝试下一源，**不会**向上抛异常。
- Provider 直连（如 `AStockProvider().fetch_data`）返回 `(df, err)`，失败为 `(None, "东财...")`, 调用方需自行判空。

---

## 7. 注意事项 / 易踩的坑

1. **口径默认是 raw**：`fetch_stock_data` 默认返回不复权日 K（见 §4.3）。要 qfq 必须直连 qfq Provider（§5.3）。
2. **成交量单位 = 手**：A股/ETF 1 手 = 100 股；返回"股"的源已在 `fetch_data` 内 `÷100`。
3. **衍生列在归一化后被丢弃**：需要 振幅/涨跌幅/涨跌额/换手率 必须直连 akshare 系 Provider（§3、§5.4）。
4. **中文名解析**：默认开启（`enable_name_resolution`），失败则原样按代码处理；`get_data` 级不解析，需先标准化。
5. **日线无内置缓存**：与分钟接口 `fetch_intraday` 带 `IntradayCache` 轮询缓存不同，`get_data` 每次实时打源、无 TTL 缓存。
6. **排序与截断**：返回按 `date` **升序**，仅保留最近 `days` 条。
7. **腾讯批量实时仅 days=1**：优先级 0 的腾讯源只能服务当日快照，历史天数请求会自动跳过它。

---

## 8. 与分钟接口对比

| 维度 | 日 K（`fetch_stock_data`） | 分钟（`fetch_intraday` / `fetch_minute`） |
|---|---|---|
| 时间列 | `date`（datetime64，无时分秒） | `datetime`（含时分秒） |
| 入口方法 | `fetch_stock_data` | `fetch_intraday` |
| 底层管理器方法 | `get_data` | `get_intraday` |
| 周期参数 | 无（固定日线） | `period` ∈ `1m/5m/15m/30m/60m` |
| 内置缓存 | 无 | 有（`IntradayCache`，TTL 随周期） |
| 跨源校验最后 bar | 仅 mootdx 命中时 | 类似机制 |
| 默认口径 | raw（mootdx 优先） | 按分钟源优先级 |

> 二者共用 `OHLCV` 处理，下游代码可统一解析；区别仅在时间粒度列名（`date` vs `datetime`）与缓存策略。

---

## 9. 已知限制 / 后续改进方向

1. **命名不清**：`fetch_stock_data` 未体现"日 K"。建议新增 `fetch_daily_kline` 作为正式名，`fetch_stock_data` 保留为兼容别名。
2. **无 `adjust` 参数**：门面无法声明"只取 qfq 并多源兜底"。建议给 `fetch_stock_data` / `get_data` 增加 `adjust: str = "raw" | "qfq"` 参数，让管理器仅在匹配口径的源内按优先级兜底——这是让 qfq 获得真正多源兜底、且不再被 raw 抢占的前提。
3. **qfq 单标的无多源兜底**：即便有了 `adjust="qfq"`，也需确保 qfq 源之间存在优先级梯度（如 东财 qfq → 东财替代 qfq → 新浪 qfq），目前库内 qfq 源多为同一东财后端，需补充异源 qfq。
4. **衍生列被归一化剥离**：若希望门面也能带回 振幅/涨跌幅 等列，需在 `normalize_ohlcv` 增加"保留衍生列"选项。

---

## 10. 完整示例

### 10.1 日 K（默认 raw，带兜底）

```python
from stockdata_hub import StockDataFetcher

fetcher = StockDataFetcher()
df, reason, code = fetcher.fetch_daily_kline("600519", days=60)
if df is not None:
    print(f"命中源={fetcher.get_last_used_provider()} 行数={len(df)}")
    print(df[["date", "open", "high", "low", "close", "volume"]].tail())
```

### 10.2 A股前复权（qfq，保留衍生列）

```python
from stockdata_hub.providers.akshare_provider import AStockProvider

df, err = AStockProvider().fetch_data("600519", days=30)
if df is not None:
    # df 为中文列：日期/开盘/收盘/最高/最低/成交量/成交额/振幅/涨跌幅/涨跌额/换手率
    print(df[["日期", "收盘", "涨跌幅", "换手率"]].tail())
```

### 10.3 通用前复权入口（A股/ETF/港股自动路由）

```python
from stockdata_hub.providers.akshare_provider import UniversalStockProvider

df, err = UniversalStockProvider().fetch_data("600519", days=30)   # A股
df, err = UniversalStockProvider().fetch_data("00700", days=30)    # 港股
```

### 10.4 自定义优先级 + qfq 优先

```python
from stockdata_hub import DataProviderManager, StockDataFetcher

mgr = DataProviderManager.build_default()
mgr.set_provider_priority("通达信TCP(mootdx)", 99)   # 把 raw 源降权
fetcher = StockDataFetcher(manager=mgr)
# 此时 qfq 源会先于 mootdx 被尝试
df, reason, code = fetcher.fetch_stock_data("600519", days=30)
```
