# 贡献指南

感谢你考虑为 `stockdata-hub` 做贡献！

## 开发环境

```bash
git clone https://github.com/stockdata-hub/stockdata-hub
cd stockdata-hub
pip install -e .[all]
pip install pytest
```

## 代码风格

- 遵循 PEP 8；类型注解尽量完整（`from __future__ import annotations` 已开启）。
- 第三方依赖务必**延迟导入**，并在缺失时让 `can_handle` 返回 `False`，保证降级不崩。
- 新增 Provider 请参照 [docs/add_provider.md](docs/add_provider.md) 与检查清单。

## 测试

```bash
python -m pytest tests/ -q
```

- 纯逻辑测试（代码工具、归一化、管理器优先级）应离线可跑，不依赖网络。
- 联网/实盘测试建议在本地手动运行，避免写入 CI。

## 提交 PR

1. Fork 并新建分支（`feat/xxx` 或 `fix/xxx`）。
2. 确保测试通过、无新增 lint 错误。
3. PR 描述清楚：动机、改动点、如何验证。
4. 新增数据源请同时更新 README 的「内置数据源」表格与
   `providers/__init__.py` 的注册表（如适用）。

## 行为准则

请友好、专业地交流。我们欢迎任何能让库更好用的建议。
