# 设计文档：统一分时/分钟数据接口

- 文档版本：v0.1（草案）
- 日期：2026-07-19
- 关联需求文档：`intraday_requirements.md`
- 关联代码库：`stockdata-hub`

本文描述如何在**不破坏现有日线功能**的前提下，为 `stockdata-hub` 增加一套
「统一格式、多源兜底」的分时/分钟数据接口。设计严格复用现有
`DataProvider` / `DataProviderManager` / `StockDataFetcher` / `normalize_ohlcv` 的抽象。

---

## 1. 总体架构

沿用现有三层结构，仅做**参数化扩展**，不引入新范式：

```
调用方
  │
  ▼
StockDataFetcher  ── fetch_intraday(symbol, period, days, count?)
  │                    (新增门面方法，复用 get_default_manager)
  ▼
DataProviderManager.get_intraday(symbol, period, days, count?)
  │                    按 priority 升序尝试
  ▼
[MinuteProviderA, MinuteProviderB, ...]  (新增，独立类，不影响日线 Provider)
  │  fetch_data(symbol, days, period)  →  (raw_df, err)
  ▼
normalize_intraday(raw_df, period, days, count)  (新增纯函数，对标 normalize_ohlcv)
  │
  ▼
统一分钟契约 DataFrame (datetime, open, high, low, close, volume[, amount])
```

设计原则：
- **日线 Provider 完全不动**；分钟 Provider 是独立新增类。
- **`period` 向后兼容**：缺省 `"1d"` 时走现有日线路径，行为零变化。
- **归一化拆分**：新增 `normalize_intraday`，不复用/修改 `normalize_ohlcv`，避免回归。

---

## 2. 接口变更（签名）

### 2.1 `DataProvider`（基类，`core.py`）
新增可选参数，保持抽象方法签名兼容：

```python
@abstractmethod
def fetch_data(
    self, symbol: str, days: int = 30, period: str = "1d"
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    ...

def can_handle_request(
    self, symbol: str, days: int = 1, period: str = "1d"
) -> bool:
    # 默认调用 can_handle(symbol)；分钟 Provider 应额外校验 period 是否支持
    return self.can_handle(symbol)
```

> 现状 `can_handle_request` 已带 `days` 形参；本次加 `period`，默认值使现有子类零改动。

### 2.2 `DataProviderManager`（`core.py`）
新增 `get_intraday`，逻辑与 `get_data` 平行，仅透传 `period` 并在归一化时调用 `normalize_intraday`：

```python
def get_intraday(
    self, symbol: str, period: str = "1m", days: int = 1, count: Optional[int] = None
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    ...
    for provider in self.providers:
        if not provider.can_handle_request(symbol, days, period):
            continue
        raw, error = provider.fetch_data(symbol, days, period)
        ...
        df, err = normalize_intraday(raw, period, days, count)
        if df is not None and not df.empty:
            self._last_used_provider = provider.get_name()
            return df, None
    return None, "所有分钟 Provider 都无法获取数据"
```

`period` 合法性校验：非法 `period` 直接返回 `(None, "不支持的 period: ...")`，不进入兜底循环。

### 2.3 `StockDataFetcher`（`fetcher.py`）
新增门面方法，三元组语义与 `fetch_stock_data` 一致：

```python
def fetch_intraday(
    self, symbol: str, period: str = "1m", days: int = 1,
    count: Optional[int] = None, use_cache: bool = True
) -> Tuple[Optional[pd.DataFrame], Optional[str], Optional[str]]:
    """获取股票分时/分钟 K 线数据。"""
    if not symbol:
        return None, "无效的股票代码或名称", None
    resolved = self._resolve_symbol(symbol)
    df, reason = self.provider_manager.get_intraday(resolved, period, days, count)
    if df is not None and not df.empty:
        self._last_used_provider = self.provider_manager.get_last_used_provider()
        return df, None, resolved
    self._last_used_provider = None
    return None, reason or "无法获取分钟数据", None
```

`__init__.py` 顶层导出便捷函数（对标现有 `fetch_kline`）：

```python
def fetch_minute(symbol, period="1m", days=1, count=None):
    return get_default_manager().get_intraday(symbol, period, days, count)
```

---

## 3. 分钟 Provider 实现方案

每个分钟 Provider 是**独立类**（不修改现有日线 Provider）。内部把 `period` 映射为
源私有参数。统一 `period` 枚举：`"1m" | "5m" | "15m" | "30m" | "60m"`（外加 `"1d"` 仅供兼容，
分钟 Provider 对 `"1d"` 在 `can_handle_request` 返回 `False`）。

### 3.1 周期映射表（集中管理）

| period | mootdx `frequency` | 东财 `klt` | 新浪 `ktype` | openstockdata `ktype` |
|---|---|---|---|---|
| `"1m"` | `8` | `1` | `"1"` | `"m"` (待实测) |
| `"5m"` | `0` | `5` | `"5"` | `"5"` |
| `"15m"` | `1` | `15` | `"15"` | `"15"` |
| `"30m"` | `2` | `30` | `"30"` | `"30"` |
| `"60m"` | `3` | `60` | `"60"` | `"60"` |

> 注意：mootdx `frequency=7` 是「1 分钟**除权**口径」，一般不使用；`8` 为标准 1 分钟。
> 该表与 a-stock-data SKILL.md 实测值一致。

### 3.2 `MootdxMinuteProvider`（优先级最高，实时，不封 IP）
- 复用现有 `mootdx_provider.py` 的连接管理（`_conn_lock`/`_reconnect_if_idle`/测速），
  建议**抽取为共享基类或 helper**，避免复制粘贴。
- `fetch_data`：`client.bars(symbol=symbol, frequency=<映射>, offset=<days 交易日对应根数>)`。
- 连接锁、超时预算（`_FETCH_BUDGET_SEC`）、空闲重建逻辑原样复用。
- **不做**最后一根腾讯实时修正（分钟 bar 实时刷新，修正意义有限，见需求 Q2）。
- `can_handle_request`：仅 `symbol` 为 A/ETF 且 `period in 分钟集合` 时返回 `True`。

### 3.3 `EastMoneyMinuteProvider`（历史深、需限流，优先级中）
- 底层复用 `http_provider.py` 的东财 kline URL（`push2his.eastmoney.com/api/qt/stock/kline/get`），
  追加 `klt` 参数 + `period`→`klt` 映射。
- **必须**经限流入口（对标 `em_get()`：串行、间隔 ≥1s + 随机抖动、会话复用）。
- `klines` 文本行拆 `,` 解析为 `datetime/open/high/low/close/volume/amount`。
- `can_handle_request`：A/ETF + 分钟 period；腾讯/新浪 fallback 同文件已有。
- 限流与封 IP 防护是**硬性需求（FR-8 / NFR-无新增风险）**。

### 3.4 `SinaMinuteProvider`（兜底，优先级低）
- 底层对标 akshare `stock_zh_a_minute`：新浪 `money.finance.sina.com.cn` 的
  `min_kline` 接口，`symbol` 带 `sh/sz` 前缀，`ktype` 映射同上。
- 直接 HTTP 抓取 + 限流（新浪也限频）。
- 仅作最后兜底。

### 3.5 `OpenStockDataMinuteProvider`（可选，后置）——**已实现**
- 复用 `openstockdata.baidu_kline_with_ma(symbol, ktype=<映射>)`；成交量「股」→「手」换算；
  令牌桶限流源名 `openstockdata(百度)`；优先级 4（新浪之后，分钟最末兜底）。
- 依赖 `cn-a-stock-data` 为**可选**，未安装时 `can_handle` 返回 `False`，管理器自动跳过。
- `ktype` 映射：`5m/15m/30m/60m` = 分钟数字符串，`1m` = `"m"`（设计标注「待实测」）。
  日线用 `ktype="1"`，故分钟不复用 `"1"`。
- **实测限制**：本机 `openstockdata` 未安装 + 百度 `finance.pae.baidu.com` 反爬（各
  `ktype` 均返回空 `Result` 列表），无法联网验证分钟数据；已按设计 §3.1 映射落地并
  以离线单测覆盖（映射/`supports_periods`/降级/默认管理器注册）。1m 主力仍为本地
  mootdx / 新浪，此源仅作补充兜底。

---

## 4. 归一化：`normalize_intraday`

新增纯函数（不改动 `normalize_ohlcv`），契约见需求 §6：

```python
CANONICAL_INTRADAY_COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]

def normalize_intraday(
    df: pd.DataFrame,
    period: str = "1m",
    days: int = 1,
    count: Optional[int] = None,
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    # 1. 列别名归并： datetime/time/date/日期 -> datetime
    #    open/high/low/close/volume 同 normalize_ohlcv
    # 2. 校验必要列（缺则返回错误）
    # 3. 数值化 open/high/low/close/volume/amount
    # 4. datetime 解析为 datetime64（保留时分秒）
    # 5. 排序（升序）
    # 6. 窗口过滤：保留 datetime >= (now - days 个日历日) 的行
    #    （分钟数据按时间窗口而非「N 根」，更贴近 "days" 语义）
    # 7. 可选 count 截断：若 count 给定，取最后 count 根
    # 8. 丢弃多余列，仅留 CANONICAL_INTRADAY_COLUMNS (+ amount)
```

要点：
- 与日线契约共享 `open/high/low/close/volume` 语义；仅把 `date` 升格为 `datetime`。
- `normalize_ohlcv` **保持不变**，确保日线回归零风险。
- 窗口过滤用「日历日」而非「交易日」，因分钟数据需要含当日盘中的实时部分；
  下游如需严格交易日，可在 `days` 上传更大值后自行裁剪。

---

## 5. 兜底与优先级

分钟 Provider 在 `register_builtin_providers` 中按以下优先级注册（数值越小越优先）：

| 优先级 | Provider | 源 | 特点 |
|---|---|---|---|
| 1 | `MootdxMinuteProvider` | 通达信 TCP | 实时、不封 IP、最快 |
| 2 | `EastMoneyMinuteProvider` | 东财 HTTP | 历史深、需限流 |
| 3 | `SinaMinuteProvider` | 新浪 HTTP | 兜底 |
| 4（可选） | `OpenStockDataMinuteProvider` | 百度 | 后置、ktype 待实测 |

管理器对分钟请求**只遍历 `can_handle_request(symbol, days, period)` 为 True 的源**，
因此现有日线 Provider（对分钟 period 返回 False）自动被排除，互不干扰。

---

## 6. 错误处理、限流与并发健壮性

> 本节把「流控/防封」从口头要求落实为可照做的规格，覆盖 4 个此前仅轻描淡写的点：
> mootdx 单 TCP 连接共享、重试策略、轮询缓存、各源显式限流常量。

### 6.1 单源异常隔离与超时预算
- **异常隔离**：`get_intraday` 内对每个 Provider 包 `try/except`，某源抛异常仅记 warning 并 `continue`，不拖垮整体兜底（沿用 `get_data` 现有做法）。
- **超时预算**：mootdx 分钟沿用 `_FETCH_BUDGET_SEC`（默认 4.0s），超时类错误（`socket.timeout`/`TimeoutError`/`"timed out"`）**立即失败回退、不重建重试**，避免把 monitor 的 5s HTTP 超时顶爆（与现有日线逻辑一致）。
- **非法 period**：管理器层在兜底循环**前**校验，非法值直接返回 `(None, "不支持的 period: ...")`，不进入循环。

### 6.2 mootdx 单 TCP 连接的共享与串行
mootdx 底层是**单条 TCP 长连接、非线程安全**，必须串行访问。决策如下：
- **日线与分钟共用同一单例客户端与锁**：新增 `src/stockdata_hub/mootdx_client.py`，提供模块级 `get_tdx_client()` 单例（懒构建 + 测速选优 + 空闲重建 + 连接锁 `_conn_lock`）。`MootdxProvider`(日线) 与 `MootdxMinuteProvider`(分钟) 均从此获取**同一个** client + lock，避免开两条 TDX 连接（服务器对并发连接数敏感），也保证 daily+intraday 经同一把锁串行、不踩非线程安全。
- **锁获取限时**：复用现有 `_LOCK_ACQUIRE_SEC`（2.5s）；拿不到锁（某次调用卡在死连接）立即放弃并交上层回退，防止排队级联超时。
- **备选**：若日后日线批量与分钟轮询争锁严重，可改为「日线/分钟各持独立 client + 各自 lock」（2 条连接），但默认走共用单例。

### 6.3 重试策略
- **HTTP 源（东财/新浪）**：套用 `core.py` 已有的 `retry_on_failure` 装饰器，针对**瞬时网络错误**（连接重置、临时 5xx）做 2 次指数退避重试（初始 1s ×1.5 + 抖动）。限流类响应（403/429）**不重试**，直接交限流器退避。
- **mootdx 分钟**：沿用 §6.1 的超时预算逻辑，**不重试**（超时即回退，保护调用方）；其「空结果 vs 连接失效」的重建重试逻辑保持现有实现。
- 重试仅作用于 `fetch_data` 内部网络层，不影响管理器层的多源兜底。

### 6.4 轮询缓存（降低实时刷新开销）
实时场景是「盘中每分钟轮询」，重复拉取同一窗口既浪费配额又加剧封 IP 风险。接入现有 `cache.py` 的 `StockCacheManager`：
- 缓存键：`(symbol, period)`；值：最近一次成功返回的分钟 DataFrame。
- TTL：随周期变化——`1m/5m` 取 60s，`15m/30m/60m` 取对应周期秒数，`1d` 不缓存（走原日线逻辑）。
- 命中且在 TTL 内：直接返回缓存（仍更新 `_last_used_provider` 为缓存源）。
- 跨 TTL 合并：新拉取结果与缓存按 `datetime` 去重拼接（保留最新一根），避免轮询丢中间 bar。
- 提供 `fetch_intraday(..., use_cache: bool = True)` 开关，便于强制刷新或测试。

### 6.5 各源显式限流常量（集中配置）
限流参数从「文字约定」提升为**可配置常量表**，避免各 Provider 硬编码散落：

```python
# src/stockdata_hub/rate_limit.py（新增）
INTRADAY_RATE_LIMITS = {
    "东方财富(push2his)": {"min_interval": 1.0, "jitter": 0.5, "limiter": "token_bucket"},
    "新浪(min_kline)":     {"min_interval": 0.5, "jitter": 0.3, "limiter": "token_bucket"},
    "openstockdata(百度)": {"min_interval": 0.3, "jitter": 0.2, "limiter": "token_bucket"},
    # mootdx TCP 无硬性 QPS 限制，靠单连接串行 + 锁即可
}
```
- 每个 HTTP 源持有一个 `threading.Lock` + **令牌桶限流器**（token bucket），并发调用也保证不超过 `min_interval`；`jitter` 随机抖动进一步规避固定节奏被风控。
- 限流器在 Provider 构造时按源名创建，挂到模块级单例，供同进程内所有该源调用共享。
- 东财严格复用现有 `em_get()` 的串行限流（≥1s + 抖动 + 会话复用）语义，本表仅将其参数显式化。

### 6.6 并发安全总结
| 关注点 | 方案 |
|---|---|
| mootdx 单 TCP 非线程安全 | 共用单例 client + `_conn_lock` 串行（§6.2） |
| HTTP 源高频限流 | 每源令牌桶 + 抖动（§6.5） |
| 瞬时网络错误 | `retry_on_failure` 退避重试（§6.3） |
| 超时拖垮调用方 | 超时预算内立即回退、不重试（§6.1） |
| 轮询重复拉取 | `StockCacheManager` TTL 缓存 + 去重合并（§6.4） |
| 单源崩溃影响整体 | `try/except` 隔离 + 多源兜底（§6.1） |

---

## 7. 向后兼容清单

| 现有项 | 改动 | 风险 |
|---|---|---|
| `DataProvider.fetch_data` | 加可选 `period="1d"` | 子类零改动（抽象方法签名加默认参） |
| `DataProvider.can_handle_request` | 加可选 `period="1d"` | 默认实现不变 |
| `get_data` / `fetch_stock_data` | 不改动 | 无 |
| `normalize_ohlcv` | 不改动 | 无 |
| 现有日线 Provider | 不改动 | 无 |
| `register_builtin_providers` | 末尾追加分钟 Provider | 仅新增，不影响已有顺序 |

---

## 8. 预计文件改动清单

| 文件 | 改动 |
|---|---|
| `src/stockdata_hub/core.py` | `DataProvider` 抽象方法加 `period`；新增 `get_intraday`；`can_handle_request` 加 `period` |
| `src/stockdata_hub/normalization.py` | 新增 `CANONICAL_INTRADAY_COLUMNS`、`normalize_intraday` |
| `src/stockdata_hub/fetcher.py` | `StockDataFetcher.fetch_intraday` 接入轮询缓存：命中即返回、跨 TTL 去重合并；`count` 在两分支一致截断；`use_cache` 开关（§6.4） |
| `src/stockdata_hub/mootdx_client.py` | **新增** 共享单例 TCP client + 连接锁（日线/分钟共用，§6.2） |
| `src/stockdata_hub/rate_limit.py` | **新增（已实现）** `INTRADAY_RATE_LIMITS` 常量表 + 线程安全 `TokenBucket` 令牌桶限流器 + `get_rate_limiter()` 模块级共享（§6.5） |
| `src/stockdata_hub/providers/mootdx_provider.py` | **改动** 日线 Provider 改为从 `mootdx_client.get_tdx_client()` 取共享 client（§6.2） |
| `src/stockdata_hub/providers/mootdx_minute_provider.py` | **新增** 分钟 Provider（复用共享 client + 锁） |
| `src/stockdata_hub/providers/eastmoney_minute_provider.py` | **新增（已实现）** 直连 `push2his` 东财 kline（`klt` 映射 + `secid` + `fqt=0`），仅依赖 `requests`，令牌桶限流，优先级 2（§3.3/§6.5） |
| `src/stockdata_hub/providers/sina_minute_provider.py` | **新增（已实现）** 直连新浪 `quotes.sina.cn/.../CN_MarketData.getKLineData`（`scale`/`ktype` 映射 + `sh/sz` 前缀），仅依赖 `requests`，令牌桶限流，优先级 3（§3.4/§6.5）。**注意**：原 `money.finance.sina.com.cn` host 对 `scale=1` 返回 `null`（1m 缺失），已改用 akshare 同款 `quotes.sina.cn` 修复，1m/5m/15m/30m/60m 均已实测可用 |
| `src/stockdata_hub/providers/openstockdata_minute_provider.py` | **新增（已实现，可选依赖）** 复用 `baidu_kline_with_ma`（`ktype` 映射 + 「股」→「手」换算），令牌桶限流、优先级 4；缺 `cn-a-stock-data` 时自动降级跳过。1m ktype=`"m"`（待实测），本机因反爬 + 包未装未能联网验证，离线单测覆盖映射/降级/注册（§3.5） |
| `src/stockdata_hub/providers/__init__.py` | 分钟 Provider 加入 `_PROVIDER_FACTORIES` |
| `src/stockdata_hub/__init__.py` | 导出 `fetch_minute` 及新 Provider 类 |
| `src/stockdata_hub/cache.py` | **新增（已实现）** `IntradayCache` 专用类：键 `(symbol, period)`、动态 TTL（1m/5m=60s，其余=周期秒数）、跨 TTL 按 `datetime` 去重合并；`StockCacheManager` 扩展支持动态 `ttl` 参数（§6.4） |
| `tests/test_intraday.py` | **新增** 单元测试（已全部通过）：`test_normalize_intraday*`（含 mootdx `YYYYMMDDHHMM` 整数解析、中文/vol 别名、缺失列、count 截断、days 窗口过滤、空数据）、`test_period_mapping*`（period→frequency 映射 + `can_handle_request` 周期过滤）、`test_intraday_fallback*`（日线源自动跳过 / 跨分钟源回退 / 非法 period / 全失败 / 无 Provider）、`test_tdx_client_singleton`（共享单例，monkeypatch 关闭 mootdx 不触网）、`test_rate_limit*`（令牌桶 min_interval 强制 + 模块级共享单例 + §6.5 常量表）、`test_eastmoney_sina_minute*`（klt/scale 映射、supports_periods、can_handle、默认管理器注册与日线请求自动跳过） |
| `docs/intraday_api.md` | **新增** 面向调用者的接口参考（API Reference）：公开函数参数表、返回值三元组、统一分钟契约字段、错误码枚举、完整示例 |

---

## 9. 使用示例（目标态）

```python
from stockdata_hub import StockDataFetcher

fetcher = StockDataFetcher()

# 1 分钟实时（当日，盘中轮询）
df, reason, code = fetcher.fetch_intraday("600519", period="1m", days=1)
# df 列: datetime, open, high, low, close, volume

# 5 分钟，最近 5 个交易日
df2, _, _ = fetcher.fetch_intraday("000001", period="5m", days=5)

# 顶层便捷函数
from stockdata_hub import fetch_minute
df3, err = fetch_minute("600519", period="1m", days=1)

# 日线仍照旧（零变化）
df_daily, _, _ = fetcher.fetch_stock_data("600519", days=30)
```

---

## 10. 风险与未决项

- **R1 历史深度**：在线源分钟仅近期窗口；深历史需本地 TDX `Reader` 或盘中轮询落库（不在本需求范围）。
- **R2 openstockdata 分钟 ktype**：需实测确认，建议后置。
- **R3 复权**：分钟默认不复权，跨除权日需调用方处理（需求 §3 已声明为非目标）。
- **U1 `days` vs `count` 语义**：采用「交易日窗口过滤 + 可选 count 截断」（需求 Q1 倾向方案）。
- **U2 最后一根实时修正**：分钟不做（需求 Q2 倾向不做）。
- **U3 实时性**：本接口为拉取式，非推送；盘中刷新需调用方自行定时轮询。
