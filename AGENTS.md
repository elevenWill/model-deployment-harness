# 模型部署工具链 — Agent 操作契约

本文件是所有在此仓库中工作的编码 Agent 的权威操作规范。

## 目标与边界

本仓库是一套文件系统优先的工具链，用于规划、执行、验证和记录模型部署。MVP 支持通过 SSH 访问远程 Linux、NVIDIA GPU、MiniMax-H3 配方以及 SGLang/vLLM-Omni 证据。它不是通用云管理器、GPU 调度器、MLOps 平台、守护进程或权限系统。

除非用户明确提出并指明真实主机的部署或检查请求，否则绝不能连接真实主机。测试只能使用 mock 和 fixture。

## 强制状态机

每次部署生命周期必须按以下顺序执行，不得跳过阶段：

```text
INTAKE
  -> REQUIREMENT_GATE
  -> HOST_DISCOVERY
  -> RESEARCH
  -> PLAN
  -> PLAN_REVIEW
  -> EXECUTE
  -> VERIFY
  -> RECORD
```

禁止的转换包括 `INTAKE -> EXECUTE`、`RESEARCH -> EXECUTE` 与 `HOST_DISCOVERY -> EXECUTE`。门禁失败时必须以 `NEEDS_USER_INPUT` 或 `BLOCKED` 停止对应的后续操作。`REQUIREMENT_GATE` 分两层：只读主机侦察前只检查连接意图；进入 `PLAN` 前检查完整部署意图。这不是跳过阶段，也绝不授权从 `HOST_DISCOVERY` 直接进入 `EXECUTE`。

## 需求门禁

用户意图与主机事实是不同的数据类别。

- 多轮对话先合并进 `IntakeDraft`。新一轮未提及的已确认字段必须保留；回复应先用白话列出已记住的内容，再只询问真正需要用户决定且尚未提供的项目。不得把内部枚举值原样当作用户说明。
- 只读连接门禁只需要目标主机选择器、SSH 用户名和 SSH 端口。通过后可以先探测 GPU、内存、磁盘、端口和运行环境。SSH 客户端仍必须使用外部严格 `known_hosts` 校验；未知主机指纹必须停止。
- 完整部署意图包括精确 GPU ID、安装根目录、模型根目录、模型及变体、已解析的精确框架、服务模式、绑定主机、精确端口，以及是否可修改现有环境。以 schema 和 policy 中的规范清单为准。端口“优先默认、占用后选择可用端口”和框架委托选择可以先记录为偏好；主机侦察后必须解析成精确框架与端口，完整门禁通过后才可进入 `PLAN`。
- 绝不能猜测或默认填充缺失的必需意图。完整门禁失败时输出 `NEEDS_USER_INPUT`，但只阻止规划和执行，不应阻止已经通过连接门禁的安全只读侦察。
- 主机事实包括 OS、内核、CPU、RAM、GPU/VRAM/驱动/拓扑/进程、CUDA 兼容性、文件系统/挂载/可用空间、Docker/runtime、Python/uv/conda、端口、服务与连通性。应通过 SSH 只读发现；可以检查时，不应要求用户手工提供。

IP 地址是查找地址，不是持久身份。Registry 身份使用 `host_id`；别名、主机名和网络地址只是选择器。

## 调研与证据

调研是只读活动，且必须与执行分离。使用 `config/source-policy.yaml` 为来源分级。关键部署决策需要 S/A 级证据。B 级来源可用于解决边缘问题；C 级只提供线索；D 级不能支撑关键决策。记录 URL、获取时间、已知版本/commit、主张、适用性、权威性、置信度、官方验证和推断状态。

工具链核心保持模型无关。模型专属的 checkpoint、命令、兼容性、license、已知问题和验证内容位于 `models/<model-id>/`。不要向核心脚本添加按模型名分支的逻辑。

## 规划与审核

每次远程写操作都需要已序列化的 `DeploymentPlan`。只有在满足以下条件时才允许执行：

1. 需求和 license 门禁通过；
2. 主机发现结果最新且兼容；
3. 重要决策引用可接受的证据；
4. 每条拟执行命令都有对应的执行步骤；
5. 风险、必要变更、回滚和验证均已明确；
6. 计划审核将状态设为 `READY`。

需求草稿、连接门禁通过或完成只读侦察均不授予远程写入能力。

创建和审核计划不授予远程写入能力。

## 远程执行与单一写入者

一次部署最多只能有一个 Agent 持有远程写入权限。调研、发现、规划和审核 Agent 均没有该权限。执行者只能运行已审核的 `READY` 计划，并且必须在命令不匹配、前置条件失败、SSH 失败、GPU 被占用或出现受保护操作时停止。

只读检查不需要额外审批。若这些精确步骤已写入计划，ready 计划可以创建目标目录、创建隔离 venv、拉取容器、下载请求的模型、创建服务配置并启动自身服务。

以下操作受保护，需用户单独明确审批；未获审批时一律阻止：驱动或系统 CUDA 变更、`apt upgrade`、停止/杀死无关任务、删除现有模型、防火墙/SSH 变更、重启、磁盘格式化/挂载或修改系统 Python。优先使用容器、venv 与独立 runtime，避免修改现有环境。

## 密钥

密钥只能来自环境变量或被忽略的 `.env`，包括 SSH key/password 和模型 token。绝不能将密钥写入落盘命令、日志、计划、registry、测试 fixture、shell history 或 Git。诊断信息中必须脱敏敏感环境变量值。GPU ID、端口和路径属于部署配置，不是密钥。

## 验证

安装完成或进程存活并不代表部署成功。独立记录每个等级：

- L1 环境
- L2 进程
- L3 端口
- L4 API
- L5 真实推理
- L6 输出验证

只有 L5 和 L6 为 `PASS` 才能产生 `VERIFIED` 部署状态。验证应检查请求完成、输出存在且可解码、相关媒体元数据/时长和 runtime 错误。可用时记录耗时、版本、命令/配置、拓扑和资源使用情况。

## Registry 与知识

历史 registry 数据是 `known_state`；实时探测是带有 `checked_at` 的 `observed_state`。绝不能将已知状态报告为当前状态。如果 SSH 可用，在回答当前状态问题前刷新观测，并明确展示不一致。

使用结构化 JSON/YAML/Markdown 记录事实、决策、事故、经验和 benchmark。假设不是已验证的经验；只有可复现的修复经过复测并记录证据后，才能提升为已验证经验。

## 工作约定

- Python 应具备类型标注、小巧、明确且可测试。
- JSON Schema draft 2020-12 定义持久化契约；YAML 用于人工编写的 policy/recipe。
- 保留用户已有工作。不要提交 runtime 清单、部署记录、凭证或大型模型输出。
- 测试中使用 mock SSH、伪造的 `nvidia-smi`、伪造文件系统和伪造 API。
- 保持架构简单：此 MVP 不使用 database、Redis、queue、LangGraph、Kubernetes、web backend/UI、vector database、RAG、自定义 agent framework 或长期运行的 daemon。
