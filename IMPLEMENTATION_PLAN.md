# 模型部署工具链 MVP — 实施计划

## 架构

该工具链是由 Agent 操作、以文件系统为先的 pipeline，包含强制阶段：

`INTAKE -> REQUIREMENT_GATE -> HOST_DISCOVERY -> RESEARCH -> PLAN -> PLAN_REVIEW -> EXECUTE -> VERIFY -> RECORD`

核心 contracts 与 policies 保持模型无关。模型专属事实、启动 recipes、兼容性、license 和验证内容位于 `models/<model-id>/`。调研产出精简证据 artifacts，绝不执行远程写入。执行只消费已审核的 `READY` deployment plan，且只有一个远程写入者。

## 阶段

1. **发现与设计** — 检查仓库；并行调研 MiniMax-H3 官方来源并设计机器可读 contracts。
2. **架构收敛** — 协调调研与 contracts；确定目录布局、schemas、policies、`AGENTS.md` 和模型 recipe 边界。
3. **实现** — 添加 requirement gate、只读主机探测、受控远程执行、preflight、registry、验证和知识沉淀辅助工具。
4. **集成与验证** — 添加 fixtures、mock SSH/API tests、schema checks 和对抗性 evals；运行全部已配置检查。
5. **独立审核** — 生成按严重性排名的审核报告，修复 critical/high 和影响架构的 medium 发现，并重新运行验证。

## Subagent 与文件所有权

| Subagent | 范围 | 分配期间拥有的路径 |
| --- | --- | --- |
| MiniMax-H3 调研 | 当前官方部署证据与开放问题 | 仅 `research/minimax-h3/**` |
| Contracts / Schemas | 数据 contracts、source policy、harness policy、契约文档 | `schemas/**`、`config/harness-policy.yaml`、`config/source-policy.yaml`、`docs/contracts.md` |
| Host Discovery / Execution | contracts 稳定后的只读发现与计划门禁远程辅助工具 | `scripts/probe_host.py`、`scripts/remote_exec.py`、`scripts/preflight.py`、相关 tests/fixtures |
| Eval / Reviewer | 集成后的对抗性审核；先报告，不改核心 | `docs/review-report.md`（以及明确分配的 eval/test 路径） |

主 Agent 负责架构决策、集成、`AGENTS.md`、`README.md`、模型 recipes、registry 与验证集成、冲突解决、修复和最终验收。

## 依赖

- Python 3.10+
- 用于 configuration/contracts 的 PyYAML 和 jsonschema
- 用于 SSH transport 的 Paramiko，优先使用 key，仅通过环境变量回退 password
- 用于 tests 的 pytest；mock 和 fixture 替代真实服务器
- 尽可能使用标准库；不使用 database、web framework、workflow engine 或 daemon

## 验收标准

- 仓库包含所需结构、policies、schemas、skills、MiniMax-H3 recipe、调研 artifacts、脚本、文档、tests 和 evals。
- 缺少必需用户意图字段时产生 `NEEDS_USER_INPUT`；不向用户索取可通过 SSH 发现的事实。
- 未经审核的 `READY` plan 不得执行远程写入；受保护/破坏性操作在获得明确批准前持续阻止；tests 绝不能连接真实服务器。
- Registry 输出区分历史已知状态与新鲜观测状态。
- 只有真实推理和输出验证等级通过后，部署状态才会成为 `VERIFIED`。
- 密钥只来自环境变量、从日志中脱敏、排除在 Git 外且不出现在 registry 中。
- 模型专属 MiniMax-H3 逻辑不进入 harness core。
- 所有 unit/schema/eval 检查通过，或报告剩余失败及其原因。
- 最终审核者未发现未解决的 critical 或 high 严重性问题。
