# stockdata-hub

> 统一的多源股票数据接口库：可插拔、按优先级兜底，把 `akshare` / `mootdx` / 腾讯 / 新浪 / 东财 / `openstockdata` / `iTick` 收敛为一套**统一契约**。

你只管 `fetch("600519")`，底层用哪个源、怎么 fallback、返回什么格式，全部透明。

---

## 特性

- **统一契约**：所有源返回结构一致的 `DataFrame`（`date/open/high/low/close/volume`，可选 `amount`、`ma5/ma10/ma20`），`volume` 统一为「手」。
- **多源兜底**：内置 11 个数据源，按 `priority` 顺序尝试，命中第一个成功的源。
- **缺依赖降级**：`akshare` / `mootdx` / `openstockdata` / `itick-sdk` 都是**可选依赖**；缺失时对应源自动跳过，不影响其它源。
- **可插拔**：想加自己的源？继承 `DataProvider` 两个方法即可（见 [docs/add_provider.md](docs/add_provider.md)）。
- **零强制网络依赖**：核心只依赖 `pandas` + `requests`，其余按需安装。
- **分钟级数据**：`fetch_minute` / `fetch_intraday` 提供 `1m`~`60m` K 线，独立于日线兜底链、内置盘中轮询缓存（设计 §6.4）。

---

## 安装

```bash
# 仅核心（用默认兜底链路，但多数源需要下面 extra）
pip install stockdata-hub

# 安装全部数据源支持（推荐）
pip install stockdata-hub[all]

# 或按需安装单个源
pip install stockdata-hub[akshare]        # A股/ETF/港股
pip install stockdata-hub[mootdx]         # 通达信 TCP 高速 K线
pip install stockdata-hub[openstockdata]  # 百度/腾讯 K线（alpha）
pip install stockdata-hub[itick]          # 全球行情（需 Token）
```

---

## 30 秒上手

```python
from stockdata_hub import StockDataFetcher

fetcher = StockDataFetcher()
df, reason, code = fetcher.fetch_daily_kline("600519", days=30)  # 日 K 线（fetch_stock_data 为其兼容别名）

if df is not None:
    print(df.tail())
    print("实际命中源:", fetcher.get_last_used_provider())
else:
    print("失败原因:", reason)
```

返回三元组：`(DataFrame, 失败原因, 实际代码)`。成功时 `reason=None`。

> 日 K 线接口的完整参数、统一契约、多源兜底优先级、以及"如何取前复权(qfq)"专题，见 👉 [docs/daily_kline_api.md](docs/daily_kline_api.md)。

---

## 分钟数据（Intraday）

除日线外，本库提供与 `fetch_stock_data` 平行的**分钟级 K 线**入口，沿用同一套「多源兜底 + 统一格式 + 限流防封」范式。

```python
from stockdata_hub import fetch_minute

# 当日 1 分钟；symbol 支持代码 / 名称（如 "贵州茅台"）
df, reason, code = fetch_minute("600519", period="1m", days=1)
if df is not None:
    # 600519 240 ['datetime','open','high','low','close','volume']
    print(code, len(df), list(df.columns))
    print(df[["datetime", "close", "volume"]].tail())
```

- 支持的 `period`：`"1m"` / `"5m"` / `"15m"` / `"30m"` / `"60m"`（默认 `"1m"`）。
- 返回**统一分钟契约**：`datetime + OHLCV`（`volume` 单位 = 手，与日线一致），可选 `amount`。
- 多源兜底链（优先级 1→4）：`mootdx 分钟` → `东财分钟` → `新浪分钟` → `openstockdata分钟`（可选，百度，后置兜底）。
- 盘中轮询内置缓存（`use_cache=True`）：TTL 随周期（1m/5m=60s），跨 TTL 去重合并、不丢中间 bar。

> 与日线接口向后兼容：日线用 `fetch_stock_data`（返回 `date`），分钟用 `fetch_intraday` / `fetch_minute`（返回 `datetime`），下游消费代码可共享 OHLCV 处理。
>
> 完整 API / 参数 / 错误码 / 契约字段见 👉 [docs/intraday_api.md](docs/intraday_api.md)；
> 可运行示例见 👉 [examples/intraday_example.py](examples/intraday_example.py)。

---

## 统一契约

所有 Provider 最终返回的 DataFrame 满足：

| 列 | 类型 | 说明 |
|----|------|------|
| `date` | datetime64 | 交易日期 |
| `open` / `high` / `low` / `close` | float | OHLC |
| `volume` | float | **成交量，单位 = 手 (lot)**（A股/ETF 1 手 = 100 股） |
| `amount` | float（可选） | 成交额 |
| `ma5` / `ma10` / `ma20` | float（可选） | 均线（源提供时保留） |

> ⚠️ **新增 Provider 的硬规则**：返回的 `volume` 必须是「手」。返回「股」的源（如
> `openstockdata`）必须在 `fetch_data` 内先 `÷ VOLUME_SHARE_TO_LOT`（100）。管理器会
> 再做一次统一规范化（列别名、数值化、日期、排序、截取、去多余列）。

---

## 内置数据源与优先级（越小越优先）

| 优先级 | Provider | 依赖 | 能力 |
|-------|----------|------|------|
| 0 | 腾讯批量实时 | （零额外依赖） | 当日快照（批量，days=1） |
| 1 | 通达信TCP(mootdx) | `mootdx` | A股/ETF 日线（<50ms） |
| 2 | openstockdata | `cn-a-stock-data` | 百度/腾讯 K线（alpha） |
| 3 | iTick 全球行情 | `itick-sdk` + Token | 全球多市场 |
| 3 | 新浪A股 | `akshare` | A股日线 |
| 4 | 腾讯A股 / ETF(akshare) | `akshare` | A股/ETF 日线 |
| 5 | 港股(akshare) | `akshare` | 港股日线 |
| 6 | A股(akshare) / 东财A股 | `akshare` | A股日线 |
| 7 | 东财替代 | （零额外依赖） | 直连东财 K线 |
| 10 | 通用(akshare) | `akshare` | 最后兜底 |

### 分钟源优先级（仅处理 `period` ∈ `1m/5m/15m/30m/60m`）

| 优先级 | Provider | 依赖 | 源 |
|-------|----------|------|-----|
| 1 | 通达信TCP(mootdx)分钟 | `mootdx` | 通达信 TCP，实时、最快 |
| 2 | 东财分钟 | （零额外依赖） | 东财 HTTP，历史深、需限流 |
| 3 | 新浪分钟 | （零额外依赖） | 新浪 HTTP，兜底 |
| 4（可选） | openstockdata分钟 | `cn-a-stock-data` | 百度 K线，后置兜底 |

> 未安装 `mootdx` / `cn-a-stock-data` 时对应源自动跳过，不影响其余源（见 [docs/intraday_api.md §4](docs/intraday_api.md)）。

---

## 自定义：管理器 / 新增源

```python
from stockdata_hub import DataProviderManager, StockDataFetcher

# 只看 Provider 列表
mgr = DataProviderManager.build_default()
for info in mgr.get_provider_list():
    print(info["name"], info["priority"])

# 调整优先级 / 移除 / 新增
mgr.set_provider_priority("openstockdata", 0)
mgr.remove_provider("东财替代")

fetcher = StockDataFetcher(manager=mgr)
```

**如何新增一个数据源？** 见 👉 [docs/add_provider.md](docs/add_provider.md)（含完整可运行示例）。

---

## 项目结构

```
stockdata-hub/
├── pyproject.toml
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── docs/
│   ├── add_provider.md         # 如何新增 Provider
│   ├── daily_kline_api.md      # 日 K 线 API 参考（参数/契约/兜底/前复权专题）
│   ├── intraday_api.md         # 分钟数据 API 参考（参数/契约/错误码）
│   ├── intraday_design.md      # 分钟数据设计文档
│   └── intraday_requirements.md
├── examples/
│   └── intraday_example.py     # 分钟数据可运行示例
├── src/stockdata_hub/
│   ├── __init__.py             # 公共 API（含 fetch_minute 顶层函数）
│   ├── core.py                 # DataProvider / DataProviderManager / 重试
│   ├── code_utils.py           # 股票代码标准化
│   ├── normalization.py        # 统一契约规范化
│   ├── cache.py                # 可选缓存 + 分钟轮询缓存
│   ├── fetcher.py              # StockDataFetcher 门面
│   ├── name_provider.py        # 名称->代码（可选）
│   └── providers/              # 各数据源实现
│       ├── akshare_provider.py
│       ├── mootdx_provider.py
│       ├── mootdx_minute_provider.py
│       ├── eastmoney_minute_provider.py
│       ├── sina_minute_provider.py
│       ├── http_provider.py    # 新浪/腾讯/东财
│       ├── fast_tencent_provider.py
│       ├── openstockdata_provider.py
│       ├── openstockdata_minute_provider.py
│       └── itick_provider.py
└── tests/
```

---

## 开发 / 测试

```bash
git clone https://github.com/stockdata-hub/stockdata-hub
cd stockdata-hub
pip install -e .[all]
pip install pytest

python -m pytest tests/ -q
```

---

## 许可证

[MIT](LICENSE)
