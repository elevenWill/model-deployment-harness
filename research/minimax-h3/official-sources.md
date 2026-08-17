# MiniMax-H3 官方来源台账

获取日期：**2026-08-17**。`S` 表示 MiniMax；`A` 表示框架/vendor 的一手来源。“已验证”仅表示来源报告过真实运行，并不表示本工具链已复现。URL 在可行处已 pin。

| ID | 来源（版本） | 主张 / 适用性 | 权威性 | 置信度 | 状态 |
|---|---|---|---|---|---|
| MM-GH | [MiniMax 官方 README](https://github.com/MiniMax-AI/MiniMax-H3/blob/d21241f0a4b3acbb34c97dae47fa417b7065e438/README.md)（commit `d21241f`，2026-08-15） | H3 系统边界、两个 Base checkpoint、下载形式、推荐框架、SGLang 示例、本地 768p 和托管辅助 2K 工作流。 | S | 高 | 官方文档；命令是示例，并非最低硬件主张。 |
| MM-HF | [官方 Hugging Face 模型](https://huggingface.co/MiniMaxAI/MiniMax-H3/tree/42ed227ee7df40d41602854ae760620d6eb651fe)（revision `42ed227e`，元数据最后修改于 2026-08-13） | 公共权重包含 `FL2VA/` 和 `Ref2VA/`；BF16；模型 API 当前显示 `gated=false`、`private=false`、license `other`、region tag `us`。 | S | 高 | 当前仓库元数据。region tag 是仓库元数据，**不是** license 授予。 |
| MM-LIC | [MiniMax H3 Community License](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/42ed227ee7df40d41602854ae760620d6eb651fe/LICENSE)（日期 2026-08-02） | 除非另行获得 license，使用仅限 Applicable Territory；EU、UK、Republic of Korea 和 USA 被排除。还适用其他实质义务。 | S | 高 | 有约束力的上游条款；法律审核仍由部署者负责。 |
| SGL-DOC | [SGLang H3 cookbook](https://github.com/sgl-project/sglang/blob/a54de989c8ba817ebb603c5443e694e5fcf7edb1/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx)（文档 commit `a54de98`，2026-08-16） | 原生 SGLang Diffusion 支持、root-ID 加载、硬件选择器、请求 schema、已验证拓扑与 benchmark。 | A | 高 | 具有精确 GPU 运行的上游框架证据。 |
| SGL-MATRIX | [SGLang 部署矩阵](https://github.com/sgl-project/sglang/blob/a54de989c8ba817ebb603c5443e694e5fcf7edb1/docs/src/snippets/configs/MiniMaxAI/minimax-h3.jsx)（相同文档 revision） | B200/B300/H200/H100/RTX 5090 和 AMD 的精确已验证 flags；区分 resident/FSDP/offload/cross-node recipes。 | A | 高 | 机器可读的上游文档；不是通用兼容性矩阵。 |
| VLO-REC | [vLLM-Omni H3 recipe](https://github.com/vllm-project/vllm-omni/blob/d1e230c95ba12aec7664ee6fd18c0b2b2d0d6187/recipes/MiniMaxAI/MiniMax-H3.md)（recipe commit `d1e230c`，2026-08-15） | 原生 `MiniMaxH3Pipeline`、下载布局、storage/RAM 指引、offload/parallel recipes、API、测量输出与限制。维护者字段为 “Community”。 | A | 有精确记录场景时为高；未测量起点为中 | 官方项目 recipe；部分证据特定于 development commit。 |
| VLO-SUP | [vLLM-Omni 支持模型](https://github.com/vllm-project/vllm-omni/blob/5d09cf27a98bb104506ee842ca81e0e76e47dc92/docs/models/supported_models.md)（main 观测于 `5d09cf2`，2026-08-17） | 为 T2VA、FL2VA 和 Ref2VA 注册 `MiniMaxH3Pipeline`，包括 NVIDIA 与已验证 AMD 说明。 | A | 高 | 直接的项目支持声明。 |
| VLLM-CORE | [vLLM 仓库](https://github.com/vllm-project/vllm/tree/5fd7a888386cff800f32de6b5a33d1dd3ca1e397)（main 观测于 `5fd7a88`，2026-08-17） | 递归 source-path 检查发现 MiniMax-M2/M3 text-model 支持，但没有 H3 audio-video pipeline。 | A | 中高 | **来自仓库检查的推断：** H3 应使用 vLLM-Omni，而不是仅 core vLLM。 |
| MM-API | [Global MiniMax H3 API](https://platform.minimax.io/docs/api-reference/video-generation-v2-create) / [CN API](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-create) | 托管 create endpoint；官方 README 也链接 Context-IR 和 Regenerate-2K API 用于完整 2K 工作流。 | S | 高 | 托管服务；credentials/terms/availability 随平台区域而异。 |

## 高置信模型事实

- 开放版本为 **H3-Base**，分为 `FL2VA` 和 `Ref2VA`。`FL2VA` 提供 text-to-audio-video (`t2va`) 以及零/一/两个端点图像条件的 (`fl2va`)；`Ref2VA` 提供多模态参考 (`ref2va`) 与作为 Ref2VA 用例的视频到视频。二者均在 BF16 推理时输出视频和音频。
- 完整产品 pipeline 并非完全本地：截至此 revision，H3-Context-IR 和 H3-Regenerate-2K 为托管/未开源。local Base 验证目标为 768-pixel short-edge 输出；复现官方质量的 2K 需要围绕本地 H3-Base 调用托管 API。
- MiniMax/framework 来源记录的输出 contract：4–15 秒、24 FPS、32 kHz stereo audio；发布的本地质量 recipe 使用 768-pixel short edge。MiniMax 报告稳定支持 11 种 dialog language。
- H3-Base 包含 33B dense Omni Transformer，并使用完整 Qwen3-VL-32B encoder 权重。约 13B transformer 参数为 AdaLN 相关 branch，其输出可针对固定 schedule cache；但公共 checkpoint 提供原始 branch，不能从 storage/RAM 规划中扣除。
- MiniMax 当前推荐 SGLang、vLLM（链接 recipe 实际为 vLLM-Omni）、Diffusers 和 ComfyUI。本 MVP 仅应暴露 **SGLang** 与 **vLLM-Omni**。

## 证据注意事项

- 上游 main branch 在最新 release 后仍有变动：观察到 SGLang 最新 release 为 `v0.5.17`（2026-08-08），而引用 H3 文档改于 2026-08-16；观察到 vLLM-Omni 最新 release 为 `v0.26.0`（2026-08-03），目标证据包含更晚的 development commit。实际 preflight 后，recipe 必须 pin 已知具备 H3 能力的 commit/image digest，不能仅使用 “latest”。
- Hugging Face 元数据目前显示 ungated，与 vLLM-Omni recipe 的 “requires access approval” 矛盾。应将 authentication 视为待探测条件：执行只读 model-file access test，并仅在需要时提供 `HF_TOKEN`。
- “Official” framework evidence 不等于 MiniMax 已验证每项 framework 主张。应单独记录来源 authority 与 `officially_verified_by_vendor`。
