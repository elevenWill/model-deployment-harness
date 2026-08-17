# 工具链契约

所有契约使用 JSON Schema draft 2020-12，拒绝未知属性，并携带稳定的本地 `$id`，以便从 `schemas/` 目录解析引用。JSON 和 YAML 实例在 YAML 解析后均可作为有效输入。持久化 artifact 不得包含密钥。

## 契约清单

| 契约 | 文件 | 边界 |
| --- | --- | --- |
| IntakeDraft | `schemas/intake-draft.schema.json` | 可跨多轮合并的部分意图与自然偏好；不能授权写入 |
| DeploymentRequest | `schemas/deployment-request.schema.json` | 完整、明确的用户意图 |
| HostProfile | `schemas/host-profile.schema.json` | 来自只读探测的带时间戳事实 |
| ResearchEvidence | `schemas/research-evidence.schema.json` | 可追溯主张及来源质量 |
| ExecutionStep | `schemas/execution-step.schema.json` | 单个有界操作、成功标准和回滚链接 |
| DeploymentPlan | `schemas/deployment-plan.schema.json` | 已审核执行输入、证据、license、验证和回滚门禁 |
| VerificationResult | `schemas/verification-result.schema.json` | L1-L6 观测与真实推理结果 |
| DeploymentRecord | `schemas/deployment-record.schema.json` | 历史部署事实与可选的新鲜观测 |
| HostRegistry | `schemas/host-registry.schema.json` | 稳定主机身份以及 known/observed 状态指针 |
| Incident | `schemas/incident.schema.json` | 失败症状、原因、修复和证据 |
| Lesson | `schemas/lesson.schema.json` | 假设或已验证的可复用知识 |
| DecisionRecord | `schemas/decision-record.schema.json` | 上下文、选择、备选项、证据和后果 |
| Benchmark | `schemas/benchmark.schema.json` | 与部署关联的已验证工作负载/环境测量 |
| InferenceProof | `schemas/inference-proof.schema.json` | 与已审核计划绑定的请求、完成任务响应、endpoint 和输出 |
| SemanticOutputReview | `schemas/semantic-review.schema.json` | 绑定精确生成输出的具名语义审核 |
| LifecycleArtifact | `schemas/lifecycle-artifact.schema.json` | 与请求和部署绑定的、仅 PASS 的带类型阶段证据 |
| DeploymentArchive | `schemas/deployment-archive.schema.json` | 按时间顺序保存全过程事件和带哈希制品引用 |
| ExecutionRecord | `schemas/execution-record.schema.json` | 每个远程步骤的起止时间、退出码和脱敏输出 |

`schemas/common.schema.json` 包含共享 scalar 和 artifact-reference 定义。

## 用户意图与主机事实

`IntakeDraft` 位于用户对话和最终 `DeploymentRequest` 之间。它允许字段暂时不完整，每一轮结构化更新都递归合并，因此新一轮没有重述的 GPU、目录、模型变体、监听地址、地区和用途不会丢失。草稿还可记录“两个变体”、ModelScope 下载源、独立 uv/venv、框架选择委托以及“优先默认端口，占用后从探测结果选择”的端口策略。密码、私钥和访问令牌不能进入草稿。

门禁分为三层：

1. 只读连接门禁只检查主机选择器、SSH 用户名和端口；通过后允许执行严格 `known_hosts` 校验下的主机侦察。
2. 完整需求门禁要求 `DeploymentRequest` 中的部署意图全部落成精确值，才允许进入规划。
3. 远程执行还必须有许可门禁通过且审核状态为 `READY` 的精确 `DeploymentPlan`。

`DeploymentRequest` 保存发现阶段无法安全推断的最终意图。必需 target 字段包括请求者身份、主机定位信息及 SSH 用户名/端口、GPU ID、安装根目录、模型根目录、模型 ID 与变体、已解析的精确框架、服务模式、绑定主机和端口、允许如何处理现有环境、预期用途及部署区域。它们均没有 schema 默认值。完整门禁缺少任一字段时必须输出 `NEEDS_USER_INPUT`，并阻止规划与执行；不得填充习惯端口、选择空闲 GPU 或部署到 `/root`。但只要连接门禁通过，它不再阻止安全的只读主机侦察。

偏好不是执行值。框架委托和动态端口策略先保存在草稿中；侦察完成后，工具必须将它们解析为已支持的精确框架和未占用的精确端口。最终请求和计划仍记录精确框架、端口与命令。

ModelScope 可以作为已确认下载源保存在草稿中，但当前执行器的下载动作尚未支持它。摘要必须说明“等待计划阶段适配和审核”，不得假装能够下载，也不得再次让用户选择下载源。

OS、kernel、CPU、RAM、GPU 与 VRAM、driver 兼容性、Docker、Python、storage、mount、进程、被占用端口、topology 和 connectivity 不是请求字段。只读探测在 `HostProfile` 中以 `observed_at` 和探测完整度记录它们。未能发现必需事实会阻止规划；这不构成要求用户猜测的许可。

请求的 `host` 是定位信息，可包含稳定 `host_id`、hostname、address 或 alias。IP 地址永远不是主机身份。`HostProfile` 和 `HostRegistry` 使用 `host_id` 作为稳定 key，并仅将地址保存为属性。

## 计划与执行边界

计划包含精确的请求和主机观测引用、目标、recipe、选择的框架、隔离环境策略、有序步骤、风险、必要变更、证据、服务模式/暴露方式、验证契约、回滚步骤、license 门禁、审核和状态。它还记录带 artifact hash 的精确已完成生命周期前缀。`READY` 计划必须且只能按序出现一次 `INTAKE` 至 `PLAN_REVIEW`。每次转换均使用独立的带类型 artifact，系统验证其阶段、PASS 状态、请求 ID、部署 ID、路径和 SHA-256。

计划生命周期取值为 `DRAFT`、`NEEDS_USER_INPUT`、`BLOCKED`、`READY`、`EXECUTING`、`EXECUTED`、`VERIFICATION_FAILED`、`VERIFIED` 和 `ROLLED_BACK`。`READY` 只有在以下条件下才 schema-valid：

- 已批准的审核包含审核者、时间和审核计划 SHA-256；
- license 门禁通过；
- 不存在 protected 步骤（MVP 可记录但绝不自动执行）；以及
- 至少存在一条 research-evidence 记录。

执行器只能接受 `READY`，重新计算不可变计划的 hash 并与 `review.plan_sha256` 匹配，同时只允许一个远程写入者。调研、发现、规划和审核绝不能远程写入。它还重新验证并绑定 request 和 host profile：在重新计算 preflight 前，request ID、target、model、framework permission、service 字段、host ID 和精确 `observed_at` 必须匹配审核计划。调用者无法提供未绑定的 “PASS” 断言。

不信任写操作 label：执行器检查 executable、argv 形态、不可变 download 或 image pin，以及位于审核根目录下的路径。它同时获取控制器本地锁和远程主机原子锁；精确 acquire/release argv 序列化在计划中，并在每次写入前重复只读发现。获取锁之前，它还探测实际 framework source checkout，并要求其 Git revision 等于 recipe 与 plan 的 pin。

计划 digest 是 UTF-8 canonical JSON 的 SHA-256（key 排序、紧凑 separator），计算前将 `review.plan_sha256` 替换为空字符串。审核和执行使用同一算法；这避免自引用 digest，同时仍覆盖其他所有审核与计划字段。schema 将每步分类为 `READ_ONLY`、`PLAN_ALLOWED_WRITE` 或 `PROTECTED`。harness policy 枚举允许与 protected action。即使存在 approval record，protected action 在 MVP 执行器中仍保持 manual 且 blocked。rollback 本身也是明确的有界 ExecutionStep 列表，受同样分类和审批约束。

`config/harness-policy.yaml` 是操作 allow/block policy。未知 action 默认失败关闭。它还要求通用 license 门禁在 `READY` 前通过；已知冲突与实质性不确定限制会阻止执行。此门禁识别风险，并非法律建议。

## 证据边界

每项重要计划决策都指向 evidence ID。每条 evidence 记录来源 URL、发布者、authority tier、获取时间、主张、适用性、置信度、官方验证状态以及是否为推断。`config/source-policy.yaml` 允许 S/A 来源支持部署决策，B 来源仅作补充的边缘案例证据，C/D 来源仅作线索。较低级别来源不能覆盖较高级别来源。

ComfyUI 调研进一步区分官方更新/固定核心源码、GitHub Issue/PR、PyPI 官方包和 CSDN 技术实测。合并且固定 commit 的上游代码可以是 A 级，维护者确认通常是 B 级，普通 Issue 与 CSDN 报告只能生成复测线索。PyPI 元数据可确认发布版本与平台制品，但实际部署仍需固定并记录所选 wheel 的 SHA-256。

## 验证边界

验证与执行分离。`VerificationResult` 记录 L1 环境、L2 进程、L3 端口、L4 API、L5 真实推理和 L6 输出验证。只有 L5 与 L6 均为 `PASS`，结果才能为 `VERIFIED`。启动进程、打开端口或完成 package 安装均不满足此契约。模型专属请求和输出检查属于 model recipe，而通用结果记录 artifact 与资源/耗时 metrics。

调用者提供的 L5/L6 断言会被丢弃。L5 从带类型的 `InferenceProof` 重建：endpoint 和端口必须匹配审核服务，且请求 payload、完成任务响应和输出文件均存在并具有匹配 hash。L6 需要完整 decode/codec 检查，以及一个绑定同一输出 hash 的具名 `SemanticOutputReview`；裸 Boolean acceptance 无效。

`DeploymentPlan.status` 为 `VERIFIED` 仅是生命周期元数据，必须始终有独立的 `VerificationResult` 支撑。同样，标为 `VERIFIED` 的 `DeploymentRecord` 需要 verification reference 和完整验证时间。

## 已知与观测状态

known state 是历史预期。observed state 是带时间戳的实时检查。registry 和 deployment record 将两者保存在独立对象中，任何一方都不暗示另一方。当主机可达时，状态展示必须刷新 `observed_state` 并明确报告差异，例如：预期运行、进程检查失败、尚未检查推理。它还必须显示最近一次完整验证时间。

## 知识完整性

Incident 区分假设、已确认或未知原因。`RESOLVED` 需要已确认原因、已验证修复和 verification reference。Lesson 从 `HYPOTHESIS` 开始；提升为 `VERIFIED` 需要时间戳、验证者、证据和一个或多个 verification reference。Decision record 保留被拒绝的替代方案和证据，确保结论可审计。

所有生命周期命令通过 `DeploymentArchive.record()` 追加到 `deployments/<deployment-id>/archive.json`。需求草稿和主机观测在生成时归档；执行器在再次校验 `READY` 计划后，补齐需求门禁、研究、计划与审核阶段的已哈希制品；每个远程步骤另存执行记录。执行失败自动生成 `Incident`，验证结果自动更新 `DeploymentRecord`，只有 `VERIFIED` 才自动生成 `Benchmark`。

自动沉淀不自动编造结论。`Lesson` 与 `DecisionRecord` 只有在存在明确来源、证据引用或具名人工判断时才可写入；普通失败不会自动晋升为经验，未经完整验证的性能数据也不会成为基准。

## 验证方式

将每个 schema 加载到一个 draft-2020-12 registry，运行 `check_schema`，再启用 format checking 验证 artifact。相对于 `schemas/` 目录解析引用。schema validity 不能替代如下 policy 检查：比较 plan hash、拒绝未知 action、强制阶段顺序、确保引用 ID 存在或保证单一远程写入者。
