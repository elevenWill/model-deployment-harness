# MiniMax-H3 模型 recipe

此目录包含 MiniMax-H3 专属知识。核心脚本应将其作为数据加载，且不得按模型 ID 分支。

- `manifest.yaml` 标识 checkpoint、来源 revision、下载语义、输出契约与 license 门禁。
- `compatibility.yaml` 仅记录上游实际观察到的精确硬件档案及必需 preflight 事实；它不宣称通用最低硬件要求。
- `recipes/` 描述 SGLang 和 vLLM-Omni 的启动/API 边界，并记录不可变上游 commit。`READY` 计划必须将该 pin 绑定到主机上实际探测的 source checkout。
- `verify.yaml` 定义最小真实 T2VA smoke test 与媒体验证契约。
- `research.md` 总结当前高置信结论并链接带日期的证据台账。

本地 Base 验证不能证明托管辅助 2K 行为。Ref2VA 部署还需要具有代表性、已获授权的本地参考测试。绝不能将上游性能主张迁移到不匹配的 GPU、RAM 容量、互连、工作负载形态或框架 revision。
