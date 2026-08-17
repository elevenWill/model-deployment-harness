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

## 开发安装

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

仅在经明确授权的真实检查或部署需要凭证时，才将 `.env.example` 复制为 `.env`。`.env` 被忽略。SSH 优先使用 key，仅通过环境变量回退 password。绝不要将凭证、token、GPU ID、端口或路径放进 source code；后三者是非密钥配置，应放在 deployment request/plan 中。

## 创建部署请求

编写符合 `schemas/deployment-request.schema.json` 的 JSON 文档。必需用户意图包括主机选择器、精确 GPU ID、目标根目录、模型/变体、服务暴露与端口、环境策略和允许的环境变更范围。这些均为有意选择：工具链不发明它们，也不将其隐藏在默认值中。

Conceptually:

```json
{
  "schema_version": "1.0",
  "request_id": "request-example",
  "requested_at": "2026-08-17T10:00:00+08:00",
  "requested_by": "operator-id",
  "target": {
    "host": {"address": "10.0.0.25", "ssh_username": "deploy", "ssh_port": 22},
    "gpu_ids": [0, 1],
    "install_root": "/srv/model-runtime",
    "model_root": "/data/models"
  },
  "model": {"id": "minimax-h3", "variant": "explicit-variant"},
  "framework_preference": "ALLOW_RECIPE_SELECTION",
  "service": {"mode": "foreground", "bind_host": "127.0.0.1", "port": 30010},
  "existing_environment_policy": "PRESERVE_AND_ISOLATE",
  "intended_use": "internal evaluation",
  "deployment_region": "CN"
}
```

以实际 schema 为准；该示例用于说明意图，可能随 contract 演进。主机发现前运行 preflight/requirement gate。若必需意图缺失，它将以缺失字段路径返回 `NEEDS_USER_INPUT`，且不执行部署。

## 检查与规划

OS、RAM、GPU 型号/VRAM/topology、driver、磁盘、Docker、Python、进程和端口等服务器事实应被发现，而非向用户询问：

```bash
python scripts/preflight.py requirement --request request.json
python scripts/probe_host.py --host 10.0.0.25 --host-id knode25 --username deploy \
  --output host-profile.json
python scripts/preflight.py host --request request.json --host-profile host-profile.json \
  --environment-strategy container --isolated
```

主机检查为只读。没有用户明确请求时，不要对真实服务器运行它。随后 Coding Agent 编写符合 `schemas/deployment-plan.schema.json` 的计划。计划引用可接受证据、列出精确变更与命令、识别冲突、描述回滚和 L1–L6 验证；审核将其设为 `READY` 前始终不可执行。这个 MVP 有意不提供猜测选择的自动 planner。

## 部署与验证

```bash
python scripts/remote_exec.py --plan deployment-plan.json --request request.json \
  --host-profile host-profile.json --host 10.0.0.25 --username deploy
python scripts/run_inference.py --plan deployment-plan.json \
  --recipe models/minimax-h3/verify.yaml --endpoint http://127.0.0.1:30010 \
  --request-payload smoke-request.json --media-output output.mp4 \
  --response-output inference-response.json --proof-output inference-proof.json
python scripts/verify_service.py --plan deployment-plan.json \
  --observations verification-observations.json \
  --recipe models/minimax-h3/verify.yaml --media output.mp4 \
  --inference-proof inference-proof.json --semantic-review semantic-review.json \
  --output verification-result.json
```

SSH 失败、计划漂移、GPU 被占用、受保护操作或前置条件失败时，执行会停止。它绝不会杀死无关工作、升级 driver/system CUDA、改变 firewall/SSH、重启、修改 system Python、删除模型或变更磁盘；即使另行获批，受保护操作也不属于自动化 MVP。现有环境与推荐环境冲突时，优先使用隔离 container 或 venv。

验证记录 L1 environment、L2 process、L3 port、L4 API、L5 real inference 和 L6 output validation。仅安装、进程健康或 API 健康均不代表成功。只有通过 L5 和 L6 才能创建 `VERIFIED` 部署状态。`run_inference.py` 自行向审核 HTTP endpoint 提交请求、轮询并下载任务输出；`verify_service.py` 接受其带类型 transcript 与具名 semantic review，绝不接受调用者自写的 L5/L6 PASS Boolean。

## 查询主机与部署

```bash
python scripts/registry.py show-host --host-id knode25 --live --username deploy
python scripts/registry.py show-deployment --deployment-id minimax-h3-example \
  --live --username deploy
```

Registry 数据是历史 `known_state`。当前查询会通过 `probe_host.py` 尝试经授权的只读探测，然后将带时间戳的观测提供给查询/更新流程。输出直接说明不一致，例如“registry 预期正在运行；实时 process/port 失败”，而不是将历史当作当前状态。

## 记录事故与经验

```bash
python scripts/registry.py record-incident --file incident.json
python scripts/registry.py record-lesson --file lesson.json
python scripts/registry.py record-benchmark --file benchmark.json
```

事实说明部署了什么；决策保留理由；事故关联症状、环境、原因、修复和复测；benchmark 保留测量结果。未经测试的 workaround 仍是假设，不能提升为已验证经验。

## 添加主机

不要手工编写主机能力。从部署请求中的明确主机选择器开始，运行只读发现，分配/确认稳定 `host_id`，并在 `inventory/hosts/` 下存储已验证档案。地址和 alias 是可能变化的查找 key。

## 添加模型

创建 `models/<model-id>/`，其中包括 manifest、compatibility 数据、framework recipe(s)、verification recipe、README 以及有来源支撑的 research。核心脚本通用地加载这些 contracts。添加模型不得需要在 harness core 中增加 `if model == ...` 分支。

## Agent 必须询问或停止的情形

仅就缺失的必需用户意图、受保护操作的手工处理或无法安全确定的 license/use 决策提问。不要询问可通过只读 SSH 发现的事实。

意图缺失时以 `NEEDS_USER_INPUT` 停止。门禁后 SSH 不可达、硬件不兼容或被占用、高置信证据不足、未解决 license 风险、计划审核失败、执行漂移、任何受保护变更或真实 inference/output validation 失败时，以 `BLOCKED` 停止。绝不要悄然弱化验证并宣称部署成功。
