# ModelScope 1.31.0 与 `pkg_resources` 兼容性复核

获取日期：2026-08-17。范围仅限隔离的 Python 3.11 环境中
`modelscope==1.31.0` 与 `setuptools==80.9.0` 的包元数据和发布 wheel；不代表已在
目标主机完成下载或推理验证。

## 可确认的事实

- 官方 [ModelScope 1.31.0 PyPI JSON 元数据](https://pypi.org/pypi/modelscope/1.31.0/json)
  的基础 `Requires-Dist` 包含无条件的 `setuptools`，并发布了通用的
  `modelscope-1.31.0-py3-none-any.whl`。该 wheel 的官方 SHA-256 为
  `16fc2d4209d508cd8b511c07ef74d92531aa0ee1f4c303a19e0234f60a7371a9`。
- 对上述官方 wheel 的源码检查显示，
  [`modelscope/utils/plugins.py`](https://files.pythonhosted.org/packages/bb/a8/26bda5fdbcb9ad18c52d81961520b3b55bc082969a46dc8b18495ef7726b/modelscope-1.31.0-py3-none-any.whl)
  在模块顶层执行 `import pkg_resources`，并使用 `working_set` 与
  `parse_version`；`models/nlp/glm_130b/kernels/__init__.py` 也顶层导入它。
  因而 `pkg_resources` 不是可以安全假定不存在的可选实现细节，至少插件和该模型
  路径会依赖它。
- 官方 [setuptools 80.9.0 PyPI JSON 元数据](https://pypi.org/pypi/setuptools/80.9.0/json)
  声明 `Requires-Python: >=3.9`，故 Python 3.11 在声明支持范围内；发布的通用
  wheel `setuptools-80.9.0-py3-none-any.whl` 的 SHA-256 是
  `062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922`。
  该官方 wheel 实际包含 `pkg_resources/__init__.py`。其源码的版本门槛为
  Python 3.9，未排除 Python 3.11。
- 补充的本地隔离 CPython 3.11.15 检查已确认，安装该 exact wheel 后
  `import pkg_resources` 成功（仅有弃用警告）。反向检查发现本机的
  `setuptools==82.0.1` 中该导入为 `ModuleNotFoundError`。这说明不能把
  ModelScope 未设上限的 `setuptools` 依赖留给解析器任意升级；后者是本地复现，
  不是 PyPI 的兼容性承诺。

## 部署结论

在新建的、可能未预装 setuptools 的 uv 虚拟环境里，将
`setuptools==80.9.0` 作为与 `modelscope==1.31.0` 同一次隔离安装的精确依赖是
有依据的兼容性固定：它满足 ModelScope 的声明依赖，也提供其源码导入的
`pkg_resources`。这只改变 H3 的 `.venv`，不触及系统 Python 或已有服务。

`pkg_resources` 在 setuptools 源码中已标为 deprecated；本 pin 是为兼容旧调用，
不是推荐新代码继续采用该 API。该结论不替代目标机上的实际 `import modelscope`
与 ModelScope 下载验证。
