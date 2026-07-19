# 需求文档：统一分时/分钟数据接口

- 文档版本：v0.1（草案）
- 日期：2026-07-19
- 关联设计文档：`intraday_design.md`
- 关联代码库：`stockdata-hub`（统一多源股票数据接口库）

---

## 1. 背景

`stockdata-hub` 当前只提供**日 K 线**的统一接口（见 `core.py` 的 `fetch_stock_data(symbol, days)`、
各 Provider 的 `fetch_data(symbol, days)`、`normalize_ohlcv` 契约）。所有对外入口的 `period` 都被
写死为日线：

- `mootdx_provider.py:215` `client.bars(symbol=symbol, frequency=9, offset=days)` —— `frequency=9` = 日线。
- `openstockdata_provider.py:60` `baidu_kline_with_ma(symbol, ktype="1")` —— `ktype="1"` = 日线。
- `itick_provider.py:105` `get_stock_kline(region, code, 2, days)` —— kline 类型写死。

实际下游（`stock` 项目）需要分时/分钟数据灌入 `cn_stock_minute`，但现有库无法满足。
用户希望像日线一样「**一行调用、多源兜底、统一格式**」地拿到分时数据。

经调研（见对话），可选的分时源分散在各处：
- **mootdx（通达信 TCP）**：`bars(frequency=8/0/1/2/3)` 可拿 1/5/15/30/60 分 K 线 + 逐笔成交，
  不封 IP、实时，但目前库里只接了日线。
- **东方财富 push2his**：`klt` 参数支持 1/5/15/30/60 分钟（akshare `stock_zh_a_hist_min_em` 同款）。
- **新浪**：`stock_zh_a_minute`，分钟 K 线。
- **openstockdata（百度 K 线）**：`baidu_kline_with_ma` 支持多 `ktype`（含分钟）。

因此目标是在 `stockdata-hub` 内新增一套**平行于日线、但 period 可选**的统一分时接口。

---

## 2. 目标

1. 提供统一的「分时/分钟」数据获取入口，调用方无需关心底层用哪个源。
2. 多源兜底：按优先级尝试，命中第一个成功源；缺依赖/被封时自动降级。
3. 统一返回契约：分钟级 `datetime`（含时分秒）+ OHLCV，与现有日线契约保持兼容。
4. 支持多种周期：1 分、5 分、15 分、30 分、60 分（及沿用日线 `"1d"`）。
5. 与现有日线接口**向后兼容**——不破坏 `fetch_stock_data`、各 Provider、`normalize_ohlcv` 的现有行为。

## 3. 非目标（Out of Scope）

- **不**提供逐笔成交（tick）的统一接口（mootdx `transaction()` 可后续单列需求）。
- **不**提供实时推送（WebSocket / 长轮询）；本接口为「拉取最近 N 根分钟 K 线」。
- **不**做复权处理：分钟 K 线默认不复权（与 mootdx `bars` 行为一致），跨除权日需调用方自行复权。
- **不**改变现有日线 Provider 的任何逻辑。
- 历史分钟深度不受本接口约束（受各源滚动窗口限制，在线源通常仅近期数日~数周）。

---

## 4. 功能需求（FR）

| 编号 | 需求 | 说明 |
|---|---|---|
| FR-1 | 统一入口获取分时数据 | `StockDataFetcher` 新增 `fetch_intraday(symbol, period, days)`，返回 `(df, reason, code)` 三元组，语义与 `fetch_stock_data` 一致。 |
| FR-2 | period 参数化 | 所有 Provider 的 `fetch_data` 增加可选 `period` 参数（默认 `"1d"`），支持 `"1m"/"5m"/"15m"/"30m"/"60m"`。 |
| FR-3 | 多源兜底 | `DataProviderManager` 新增 `get_intraday(symbol, period, days)`，按优先级尝试；仅 `can_handle_request(symbol, days, period)` 返回 `True` 的源参与。 |
| FR-4 | 分钟级 Provider 实现 | 至少实现 3 个分钟 Provider：mootdx（实时优先）、东方财富（历史深）、新浪（兜底）。openstockdata 分钟作为可选第 4 源。 |
| FR-5 | 统一分钟契约 | 分钟 DataFrame 列：`datetime(datetime64, 含时分秒)`、`open`、`high`、`low`、`close`、`volume(手)`；可选 `amount`。 |
| FR-6 | 周期映射 | 各源内部 frequency/klt/ktype 映射集中在一张表，由 Provider 自行转换，对调用方透明。 |
| FR-7 | 切片语义 | 分钟数据按「最近 `days` 个交易日窗口」过滤 + 可选「最多 `count` 根」截断，区别于日线的「最近 N 天」。 |
| FR-8 | 限流/防封 | 东方财富等 HTTP 源分钟接口须复用现有 `em_get()` 式串行限流（≥1s + 抖动），不新增封 IP 风险。 |
| FR-9 | 能力声明 | 仅支持日线的 Provider（如现有 mootdx 日线 Provider）在 `period != "1d"` 时 `can_handle_request` 返回 `False`，被优雅跳过。 |
| FR-10 | 元信息暴露 | `get_provider_list()` / `get_last_used_provider()` 对分钟请求同样有效；新增字段标明各源支持的 period。 |

---

## 5. 非功能需求（NFR）

| 编号 | 需求 | 说明 |
|---|---|---|
| NFR-1 | 向后兼容 | 现有日线调用（`period` 缺省）行为零变化；`fetch_data` 新增参数为可选。 |
| NFR-2 | 可选依赖 | 任一分钟 Provider 的底层依赖（mootdx / akshare / requests）缺失时，该 Provider 自动跳过，不影响其余源与日线功能。 |
| NFR-3 | 超时预算 | 复用现有 `_FETCH_BUDGET_SEC` 思路，分钟请求不得拖垮调用方（如 monitor 的 5s HTTP 超时）。 |
| NFR-4 | 可观测 | 每次命中/跳过/失败均打日志（info/debug/warning），与现有风格一致。 |
| NFR-5 | 可测试 | 归一化与周期映射为纯函数，可单测；新增 Provider 不应降低现有测试覆盖率。 |

---

## 6. 统一分钟契约

返回 DataFrame 满足：

```
规范列: datetime(datetime64, 含时分秒), open, high, low, close, volume(均为 float)
可选列: amount
volume 单位: 「手」(lot)，A股/ETF 1手 = 100股
```

与日线契约的差异：
- 日线用 `date` 列（仅日期）；分钟用 `datetime` 列（日期+时间）。归一化层对 `date/time/datetime`
  别名统一，下游按 `period` 判断是否含时间分量。
- 其余列（`open/high/low/close/volume`）语义完全一致，便于同一套下游消费代码。

---

## 7. period 枚举与源覆盖矩阵

| period | 含义 | mootdx | 东方财富 | 新浪 | openstockdata | 备注 |
|---|---|---|---|---|---|---|
| `"1d"` | 日线（现状） | ✅ freq=9 | ✅ klt=101 | — | ✅ ktype=1 | 已有，不改动 |
| `"1m"` | 1 分钟 | ✅ freq=8 | ✅ klt=1 | ✅ | ✅ | 实时优先 mootdx |
| `"5m"` | 5 分钟 | ✅ freq=0 | ✅ klt=5 | ✅ | ✅ | |
| `"15m"` | 15 分钟 | ✅ freq=1 | ✅ klt=15 | ✅ | ✅ | |
| `"30m"` | 30 分钟 | ✅ freq=2 | ✅ klt=30 | ✅ | ✅ | |
| `"60m"` | 60 分钟 | ✅ freq=3 | ✅ klt=60 | ✅ | ✅ | |

> 注：mootdx `frequency` 实测值表（来自 a-stock-data SKILL.md，已校验）：
> `0=5分 1=15分 2=30分 3=60分 4=日线 8=1分 9=日线(默认)`。
> 东方财富 klt：`1=1分 5=5分 15 30 60 101=日线`。

---

## 8. 验收标准

1. `fetcher.fetch_intraday("600519", period="1m", days=1)` 返回非空的分钟 DataFrame，列含 `datetime/open/high/low/close/volume`。
2. 当 mootdx 可用时，分钟请求优先命中 mootdx；mootdx 不可用时自动降级到东方财富/新浪。
3. `period` 缺省（`"1d"`）时，行为与改动前完全一致（回归测试通过）。
4. 某个分钟 Provider 依赖缺失时，该源被跳过且不影响其他源与日线功能。
5. 东方财富分钟请求触发 `em_get()` 式限流，连续高频调用不报 403/429 雪崩。
6. `get_last_used_provider()` 在分钟请求下正确返回实际命中源名。

---

## 9. 待确认问题

- Q1：`days` 对分钟应解释为「交易日窗口」还是「根数 count」？倾向：分钟接口新增可选 `count` 参数（默认按 `days` 交易日窗口过滤后全取）。
- Q2：是否需要为分钟数据也做「最后一根实时修正」（复用腾讯快照）？分钟 bar 实时刷新，修正意义有限，倾向**不做**，除非明确需要。
- Q3：openstockdata 是否纳入首批？其 `baidu_kline_with_ma` 分钟 `ktype` 值需实测确认，可后置。
