# 如何新增一个数据 Provider

`stockdata-hub` 的设计目标之一，就是让「接一个新的数据源」变成一件小事：
**只要实现两个方法，剩下的（兜底、归一、合并）库都替你做了。**

---

## 1. 最小实现

继承 `DataProvider`，实现 `can_handle` 与 `fetch_data`：

```python
from typing import Optional, Tuple
import pandas as pd
from stockdata_hub import DataProvider
from stockdata_hub.normalization import VOLUME_SHARE_TO_LOT


class MyAwesomeProvider(DataProvider):
    def __init__(self):
        self.name = "我的数据源"
        self.priority = 5   # 越小越优先

    def can_handle(self, symbol: str) -> bool:
        # 返回该源能否处理此 symbol（例如：只支持 6 位 A股）
        return symbol.isdigit() and len(symbol) == 6

    def fetch_data(self, symbol: str, days: int = 30) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        # 1) 调用你的数据源拿到原始 DataFrame
        raw = my_api_get_kline(symbol, days)   # 假设返回含 open/high/low/close/volume 的 DataFrame
        if raw is None or raw.empty:
            return None, "无数据"

        # 2) 关键：volume 必须是「手」。如果你的源返回的是「股」，先换算：
        if "volume" in raw.columns:
            raw["volume"] = raw["volume"] / VOLUME_SHARE_TO_LOT

        # 3) 直接返回原始 DataFrame 即可 —— 管理器会统一跑 normalize_ohlcv
        #    （列别名、数值化、日期解析、排序、截取 days、去多余列）。
        return raw, None
```

> 返回约定：`(数据, 错误信息)`。成功时错误为 `None`；失败时数据为 `None`。
> 你**不需要**自己把列名改成规范名——`normalize_ohlcv` 会自动处理
> `time→date`、`开盘→open`、`成交量→volume` 等别名。但你返回的列最好已包含
> `date/open/high/low/close/volume` 之一组，否则归一化会报错。

---

## 2. 注册进管理器

```python
from stockdata_hub import DataProviderManager, StockDataFetcher
from my_lib import MyAwesomeProvider

mgr = DataProviderManager.build_default()   # 先拿到内置全部源
mgr.add_provider(MyAwesomeProvider())        # 追加你的源
mgr.set_provider_priority("我的数据源", 0)  # 想让它最优先就调小 priority

fetcher = StockDataFetcher(manager=mgr)
df, reason, code = fetcher.fetch_stock_data("600519", days=30)
```

你也可以把依赖做成**可选**，让缺失时优雅跳过（和内置源一致）：

```python
try:
    from my_sdk import Client
    MY_SDK_AVAILABLE = True
except ImportError:
    MY_SDK_AVAILABLE = False

class MyAwesomeProvider(DataProvider):
    def can_handle(self, symbol: str) -> bool:
        if not MY_SDK_AVAILABLE:
            return False
        return symbol.isdigit() and len(symbol) == 6
```

---

## 3. （可选）接入内置注册表

如果想让 `DataProviderManager.build_default()` 默认就带上你的源，
把类加进 `src/stockdata_hub/providers/__init__.py` 的 `_PROVIDER_FACTORIES` 列表，
并在该文件里 `from .your_module import MyAwesomeProvider` 即可。

---

## 4. 检查清单

- [ ] `can_handle` 在依赖缺失时返回 `False`（避免管理器报错）。
- [ ] `fetch_data` 返回的 `volume` 单位是「手」（股需 `÷100`）。
- [ ] `fetch_data` 单源异常应被捕获并返回 `(None, reason)`，而不是抛出——管理器会
      继续尝试下一个源。
- [ ] 返回的 DataFrame 至少包含 `open/high/low/close/volume` 这些列（可带 `date`）。
- [ ] `priority` 合理：高速/稳定源给小值，兜底源给大值。
