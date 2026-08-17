# MiniMax-H3 部署阻塞项与开放问题

获取日期：2026-08-17。适用的 **P0** 项必须在计划进入 `READY` 前解决。

| 优先级 | 问题 / 冲突 | 重要性 | 解决门禁 |
|---|---|---|---|
| P0 | 部署主机、每个服务用户、输出用途和任意跨境访问是否完全位于 license 的 Applicable Territory 内？ | MiniMax 将 EU、UK、Republic of Korea 和 USA 排除；若未获单独 license，禁止在 Applicable Territory 外使用/输出。 | 收集部署/用户/数据区域事实与 license 接受记录；需要时获取 MiniMax 单独授权。不得从 HF 的 `region:us` tag 推断资格。 |
| P0 | 商业年收入是否超过 USD 20M，以及是否会实现必需 attribution/user terms/safeguards？ | License 对超过门槛的情况要求书面授权，并要求显著 “MiniMax H3”、下游保护条款、safeguards/reporting 和 distribution notice。 | 由 legal/business owner 批准；适用时联系 `api@minimax.io`。 |
| P0 | 目标 checkpoint family 是 FL2VA、Ref2VA 还是二者？本地 768p 是否足够，还是需要托管辅助 2K？ | 这改变约 135 GiB 下载、服务数量/residency、API/data exposure、成本和验证。H3-Context-IR 与 Regenerate-2K 未本地发布。 | 明确用户意图，以及托管 API 的 data-transfer approval。 |
| P0 | 哪个精确 framework revision/container digest 可部署？ | H3 docs/recipes 晚于观察到的 stable release；“latest”不可复现。 | 在可丢弃的 local/target-compatible 环境中验证 package 暴露 H3 flags/pipeline；pin commit/wheel/image digest 和 dependency lock。 |
| P0 | 精确 GPU/RAM/topology 是否适合目标任务、时长、分辨率与参考输入范围？ | 没有通用最低值。消费级 recipe 依赖 200 GiB 可用 / 384 GiB 级 RAM；多视频 Ref2VA 扩大 sequence。 | 只读 host profile 加已审核 warmup/capacity trial。 |
| P1 | Hugging Face authentication 实际是否需要？ | HF API 显示 `gated=false`；vLLM-Omni recipe 表示需要 access approval。 | 匿名探测 revision/file access；仅在访问失败时注入 `HF_TOKEN`，且绝不持久化。 |
| P1 | 所选 vLLM-Omni revision 是否支持完整目标 Ref2VA 组合？ | recipe 称模型范围最多 9 images/3 videos/3 audios/12 total，但 known-limit 缩窄 serving 组合。 | 对精确组合运行 contract test；否则在 planning 时拒绝不支持的组合。 |
| P1 | 是否允许 audio-only Ref2VA？ | 当前 MiniMax model table 列出 audio count，却未明确要求 visual reference；vLLM-Omni 明确拒绝 audio-only。 | 在 MiniMax 与所选 framework 一致、且真实 test 通过前，一律视为不支持。 |
| P1 | 哪个 SGLang image 可复现？ | selector 当前生成可变 `lmsysorg/sglang:dev`；tag 不是不可变部署 artifact。 | 在 plan review 前解析 digest 并确认 H3 dependencies/flags。 |
| P1 | 预期 ports、bind address、authentication/TLS boundary、media URI policy、output retention 和 concurrency 是什么？ | 示例会广泛 bind 并接受 local/remote media。裸露的 `0.0.0.0` generation API 默认不安全；当前 diffusion batching 有限。 | 明确用户意图加 network/security review；默认阻止 external exposure。 |
| P1 | 如何限制 remote reference URLs 和 uploaded media？ | SSRF、数据泄漏、license/consent、temp-file 生命周期和磁盘耗尽风险。 | 定义 allowlists/size-duration limits、local read-only media mount、cleanup 与 audit 行为。 |
| P2 | 是否可接受 approximate acceleration？ | FP8、Cache-DiT/TeaCache、compile 和 cache 可能改变输出或依赖任务/topology。 | 默认 lossless BF16/FP32；启用前要求代表性的 fixed-seed video+audio A/B acceptance。 |
| P2 | 什么构成语义输出 acceptance？ | codec-valid video 仍可能冻结、静音、不同步或忽略参考。 | 按任务定义 human/automated checks、参考保持预期及可接受时长/audio sync 容差。 |

## 工具链不得隐瞒的 License 事实

这是一份技术解读，不是法律建议。[license](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/42ed227ee7df40d41602854ae760620d6eb651fe/LICENSE) 还限制用 H3 works/outputs 改进另一 AI model（H3 derivative 除外），要求遵循 Acceptable Use Policy，并规定托管服务 safeguards 与 downstream terms。非托管服务的 distribution 需要指定 `NOTICE`；modified files 需要 notice。license termination 要求停止使用并销毁副本。这些条件说明需要阻塞性 license gate 和已记录 acceptance，而不应只在 README 中警告。

## 必须保留为未知的证据缺口

- 没有官方通用最低 GPU 数、VRAM、RAM、driver、CUDA 或适用于每个 H3 task/shape 的 disk-free 阈值。
- 本次收集的证据未显示 stable SGLang `v0.5.17` 本身包含引用的 post-release H3 recipe；也未显示 vLLM-Omni `v0.26.0` 本身包含每项更晚 recipe feature。
- 不主张 core vLLM 能够在没有 vLLM-Omni 的情况下提供 H3。
- 上游 benchmark latency 不是 SLA，不得外推至其他 GPU、interconnect、reference count 或 approximate acceleration mode。
- 本调研任务未运行 local 或 remote inference；所有“已验证”标签均指上游 evidence。
