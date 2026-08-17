# MiniMax-H3 调研摘要

证据获取于 2026-08-17。已发布的 H3-Base 有两个约 135 GiB 的 BF16 分区：用于文本/端点帧音视频生成的 FL2VA，以及用于多模态参考生成的 Ref2VA。本地开源版本的目标短边为 768 像素；MiniMax 的完整 2K 工作流还使用托管的 Context-IR 和 Regenerate-2K 服务。

SGLang 是默认首选的规划候选，因为其官方 cookbook 提供了最清晰的精确 NVIDIA 拓扑证据。vLLM-Omni 也受支持，且是相关的 vLLM 音视频 pipeline；core vLLM 本身不被视为支持 H3。文档发布时间晚于观察到的稳定发行版，因此每个可执行计划都必须 pin 并 preflight 一个不可变、具备 H3 能力的 commit 或 image。

MiniMax H3 Community License 在未另行授权的情况下将 EU、UK、Republic of Korea 和 USA 排除在 Applicable Territory 之外，并包含实质性的使用/分发条件。区域、预期用途、适用时的业务授权以及 license 接受均是强制计划门禁。完整来源台账、硬件证据和未解决事项请见 `../../research/minimax-h3/`。
