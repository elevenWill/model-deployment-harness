# 部署调研

调研与执行相互分离。使用 `config/source-policy.yaml`，优先采用当前官方模型/框架/vendor 来源，并持久化精简的 `ResearchEvidence`。调研绝不能写入部署目标，也不能依据低权威来源选择不受支持的选项。

调研 ComfyUI 时必须同时检查四类入口：官方 Changelog/Release 与固定 commit 的核心模块、Comfy-Org GitHub Issue/PR、PyPI `comfy-kitchen` 官方包元数据，以及带版本与原始观测的 CSDN 技术实测。具体入口和当前 pin 见 `research/<model-id>/comfyui-evidence.yaml`。CSDN 和普通开放 Issue 只能提出复测线索；目标主机复现前不得据此扩大兼容性或性能结论。已合并 PR 仍须绑定 merge commit，PyPI 安装仍须固定版本和实际 wheel SHA-256。
