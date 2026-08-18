# 部署调研

调研与执行相互分离。使用 `config/source-policy.yaml`，优先采用当前官方模型/框架/vendor 来源，并持久化精简的 `ResearchEvidence`。调研绝不能写入部署目标，也不能依据低权威来源选择不受支持的选项。

调研 ComfyUI 时必须同时检查四类入口：官方 Changelog/Release 与固定 commit 的核心模块、Comfy-Org GitHub Issue/PR、PyPI `comfy-kitchen` 官方包元数据，以及带版本与原始观测的 CSDN 技术实测。具体入口和当前 pin 见 `research/<model-id>/comfyui-evidence.yaml`。CSDN 和普通开放 Issue 只能提出复测线索；目标主机复现前不得据此扩大兼容性或性能结论。已合并 PR 仍须绑定 merge commit，PyPI 安装仍须固定版本和实际 wheel SHA-256。

遇到非推荐硬件或兼容性缺口时，默认把资料检索、候选机制整理和证据关联交给独立 subagent。主会话
只接收结构化 `CompatibilityAssessment`、精简证据、候选机制和待验证条件，不回填搜索过程、长篇网页
内容或试错对话。subagent 只能调研和生成制品，不能把论坛结论写成执行授权，也没有远程写入权限。

纯社区证据只能保持 `COMMUNITY_LEAD`/`RESEARCH_NEEDED`。只有每个兼容性缺口都由 S/A 上游来源通过
`supports_gap_ids` 与 `supports_mechanism_ids` 直接关联具体缓解机制，候选方案才可进入容量试跑。

尚未复现的候选方案应标为 `READY_FOR_TRIAL`，而不是 `BLOCKED`。容量试跑必须使用独立序列化且经
审核的 `purpose=CAPACITY_TRIAL`、`status=READY` 计划；仍走正常执行和 L5/L6 验证。只有试跑计划、
完整步骤执行记录、InferenceProof、原始请求/响应/任务/输出、SemanticReview、验证结果、输出制品
及当前 HostProfile 的哈希和交叉引用全部一致，并通过可信媒体校验重建 L5/L6，适配状态才能成为
`VALIDATED`。这只表示适配证据可作为正式计划输入；完整需求、许可、S/A 关键证据和正式计划审核仍需
分别通过。
