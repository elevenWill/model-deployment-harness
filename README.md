# 模型部署工具链

一套文件系统优先的工具链，帮助编码 Agent 将明确的模型部署请求转化为经过调研、审核、安全执行、真实验证和记录的部署。MiniMax-H3 是用于验证设计的第一个模型 recipe；它不内嵌于工具链核心。

## 解决的问题

临时部署脚本会模糊用户意图、主机事实、互联网调研和远程修改之间的界限。本项目将这些边界明确化并机器可读。它回答：用户请求了什么、缺少哪些必需选择、主机当前真实状态、哪些官方来源支持计划、将发生什么变更、真实推理是否成功、历史上登记了什么，以及下次可以安全复用什么。

当前不提供 web UI、daemon、GPU scheduler、cloud control plane、Kubernetes 或 MLOps layer、database、RAG/vector search、自动 driver/CUDA 升级或自动重启。MVP 支持经 SSH 访问远程 Linux、NVIDIA GPU、MiniMax-H3，以及有证据支撑的 SGLang 或 vLLM-Omni recipes。

## 核心生命周期

```text
INTAKE -> REQUIREMENT_GATE -> HOST_DISCOVERY -> RESEARCH -> PLAN
       -> PLAN_REVIEW -> EXECUTE -> VERIFY -> RECORD
```

阶段不可跳过。调研和主机发现均为只读。只有一个执行者可以进行远程写入，且只能来自状态为 `READY` 的已审核计划。完整操作规则见 [`AGENTS.md`](AGENTS.md)；schema 和 policy 是持久化数据的权威定义。

## 仓库地图

- `config/` — 来源分级、执行/意图 policy 与安全操作默认值。
- `schemas/` — 请求、主机事实、计划、步骤、验证、registry、事故、决策和经验的 JSON Schema contracts。
- `skills/` — 每个生命周期阶段的精简 Agent 说明。
- `models/minimax-h3/` — 模型专属 manifest、兼容性、framework recipes、验证、license 信息和已知问题。
- `research/minimax-h3/` — 从 primary sources 提取的精简、带日期证据。
- `scripts/` — requirement/preflight、主机探测、计划门禁执行、验证和 registry 辅助工具。
- `inventory/hosts/` — 被忽略的 runtime 主机记录；持久身份是 `host_id`，而非 IP。
- `deployments/` — 被忽略的 runtime 计划和部署记录。
- `knowledge/` — 决策、事故、已验证经验和 benchmarks。
- `tests/` 与 `evals/` — 单元测试、fixture 驱动的远程测试和安全攻击。

## 使用说明

从安装、`.env`、部署请求到主机侦察、计划审核、执行、真实推理验证、登记与排错，请阅读独立的[使用说明](docs/usage.md)。
