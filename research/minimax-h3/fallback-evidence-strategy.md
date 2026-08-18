# MiniMax-H3 非推荐硬件的降级证据策略

获取日期：**2026-08-18**（Asia/Shanghai）。范围：MiniMax-H3 的 ComfyUI、SGLang 与 vLLM-Omni 路线；未连接目标主机，未执行推理或部署。

## 结论

“不匹配官方推荐硬件”应产生 `UNVERIFIED_HARDWARE`，而不是直接产生 `BLOCKED`。工具链应继续只读检索替代拓扑、offload、量化、较小 workload 和已合并修复，将结果登记为候选；但候选本身不授予远程写权限，也不绕过 license、需求、主机发现、计划审核或 L5/L6 验证。

社区报告只能回答“下一步值得复测什么”。重要部署决策仍须引用 S/A 级来源。可接受的升级路径是：

```text
UNVERIFIED_HARDWARE
  -> COMMUNITY_LEAD
  -> OFFICIAL_MECHANISM_FOUND
  -> ISOLATED_CAPACITY_TRIAL_PLANNED
  -> TARGET_REPRODUCED
  -> PLAN_ELIGIBLE
```

- `COMMUNITY_LEAD`：Issue、论坛、Reddit、个人仓库或 CSDN 给出硬件、版本、命令和输出线索。
- `OFFICIAL_MECHANISM_FOUND`：在固定的框架源码、已合并 PR 或正式 recipe 中找到对应机制；这是 S/A 证据，不能由社区帖子替代。
- `ISOLATED_CAPACITY_TRIAL_PLANNED`：完整门禁通过后，另建只含实验 workload 的 `DeploymentPlan`；不得把试验计划伪装成生产部署。
- `TARGET_REPRODUCED`：在精确目标 GPU/topology 上记录版本、命令、峰值资源、L5 推理和 L6 媒体验证。
- `PLAN_ELIGIBLE`：计划仍需 `READY` 审核；复现成功不自动授权部署。

若只找到 C/D 线索而找不到固定上游机制，状态保持 `NEEDS_RESEARCH`。若 S/A 来源明确不支持目标架构、不可获得不可变制品、容量硬约束仍失败，或出现无法解释的黑帧/噪声/音频错误，则转为 `BLOCKED`，不得用“进程存活”降格通过。

## 检索与分级规则

1. 先查模型作者和框架的固定文档、release、merged PR 与源码（S/A），确认是否已有较小显存、offload、量化或替代拓扑。
2. 再查维护者 Issue/Discussion（维护者确认可为 B），并区分普通用户评论（仍为 C）。同一 GitHub Issue 中不同评论可以有不同等级。
3. 再查 Reddit、论坛、个人 GitHub 仓库和中文社区（C/D），只提取可复现参数。CSDN 按 `config/source-policy.yaml` 默认 D、最高 C。
4. 搜索结果必须保留失败案例。它们用于否定无效 workaround，例如“只加 swap”“只开 tiled retry”“只用 Ulysses 扩卡”。
5. 每条候选至少记录 URL、发布时间/更新时间、固定 commit 或版本、硬件和拓扑、RAM/磁盘、命令差异、workload、输出验证、风险和是否需要目标机复现。
6. 较低级来源不能覆盖较高级来源；同级冲突未解决时为 `BLOCKED`。社区测量必须在目标机复现。

## 证据台账

### A 级：可支撑候选机制与精确上游版本

| ID | 来源与固定版本 | 主张和适用条件 | 风险 / 复现要求 |
|---|---|---|---|
| VLO-PR-5850 | [vLLM-Omni PR #5850](https://github.com/vllm-project/vllm-omni/pull/5850)，2026-08-06 创建、2026-08-11 合并，merge commit `6d4e4ffc7cf68f68b6d30076e04113737e35f567` | 已合并的官方项目 recipe 在 vLLM-Omni `0.26.1.dev55+g81b48e83e`、vLLM `0.26.0`、PyTorch `2.11.0+cu130`、driver `580.126.09` 上验证 2×/4× RTX 4090 24GiB。TP2+DLO、12 resident DiT blocks、CUDNN attention、1024×576/124 frames；T2VA/Ref2VA 均有真实 MP4，2 卡峰值约 15GiB/GPU。 | A 级仅覆盖该固定 recipe/shape。要求至少 200GiB available RAM，推荐 384GiB；每次只启一个 partition。4 卡 Ref2VA 只有一次完整运行且后续遇到异步输出 timeout。目标机仍需 L5/L6 复现。 |
| VLO-PR-5764 | [vLLM-Omni PR #5764](https://github.com/vllm-project/vllm-omni/pull/5764)，2026-08-04 创建、2026-08-06 合并，merge commit `1c2a81f6d84aea4fff53bd2f894c2a287c237245` | 官方合并 DLO：TP-local streaming、`--dlo-no-use-allgather`、text encoder/VAE staging 和 tiled VAE。2×RTX 5090 32GiB 在 commit `ae6577ea` 完成 1344×768/124 frames/50 steps，约 22.6GiB sampled peak/GPU。 | 5090 是一次目标硬件运行；PR 当时的 4090 行只是 B300 capacity proxy，4090 的后续目标实测应改引 VLO-PR-5850。采样 `nvidia-smi` 不是 allocator high-water。 |
| VLO-PR-6213 | [vLLM-Omni PR #6213](https://github.com/vllm-project/vllm-omni/pull/6213)，2026-08-15 创建、2026-08-16 合并，merge commit `9d2bb23ff676746e1cea69f2366a32f925c39f50` | 官方合并 loader-owned host-weight plan 与 bounded staging。TP1/no-AllGather 可 mmap checkpoint 并共享 page cache；2-GPU E2E 峰值 13,226MiB，L20X 对比中两 worker PSS 从 283.56GiB 降到 150.08GiB。 | 直接 mmap 的已声明边界为 TP1；TP>1 会退回 ordinary loader，某 4×B300 TP2/no-AllGather smoke 的 PSS 反而约 314GiB。不能把低 HBM误写成低 host RAM。 |
| SGL-PR-33864 | [SGLang PR #33864](https://github.com/sgl-project/sglang/pull/33864)，2026-08-06 创建、2026-08-07 合并，merge commit `a79340dedd4780a7678c1bce64351f717994a56d` | 修复 H3 `--text-encoder-cpu-offload` 的 CPU/CUDA device mismatch。1×RTX PRO 5000 72GB 上与 DiT layerwise offload 组合完成 1344×768、124 frames，并验证视频和音频轨。 | 仅 model-wise DiT+encoder+VAE offload 在同一卡仍 OOM；不能从“flag 已修复”推断 24/32GB 单卡可用。必须固定包含该 merge 的 revision。 |
| SGL-PR-34294 | [SGLang PR #34294](https://github.com/sgl-project/sglang/pull/34294)，2026-08-10 合并，merge commit `ec9babe36cc172cb5d7f3882547718e99ddb2e0c` | 修复 H3 rank-local FSDP 绕过 grouped-QKV layout transform 导致的静默错误；4×H200 FSDP 输出与 resident 路径 byte-identical。 | 这是强制版本下限线索：旧 FSDP 路径可能“服务成功但影音损坏”。任何 FSDP fallback 必须包含此修复并做 L6。 |
| SGL-PR-33327 | [SGLang PR #33327](https://github.com/sgl-project/sglang/pull/33327)，2026-08-03 创建、2026-08-08 合并，merge commit `a25c330eb1514deff1fa39dc620ccd9b0ccb64ef` | 官方验证 2 nodes × 8×H200：node 内 Ulysses8、node 间 Ring2、`--encoder-parallel replicate`；4/4 Ref2VA/V2V 请求成功。 | 只覆盖该高速互连拓扑。`encoder-parallel auto` 会跨 node fold 并崩溃；跨节点不是消费卡 workaround，也不属于当前 MVP 默认。 |
| COMFY-PR-15446 | [ComfyUI PR #15446](https://github.com/Comfy-Org/ComfyUI/pull/15446)，2026-08-09 创建并合并，merge commit `2a68ce33b4c9ea6ee4283e618a74560cefb32694` | 上游用 chunked I/O 将 H3 VAE decode 临时 VRAM 从约 4,150MB 降到 944MB、encode 从约 6,145MB 降到 2,775MB；端到端 pixel output 声称 bitwise identical。 | 只解决 VAE encode/decode 峰值，不解决 DiT/text encoder residency。低显存 ComfyUI 候选必须固定包含该 merge 的 revision。 |
| COMFY-MEMORY-FLAGS | [ComfyUI 固定源码 `cli_args.py`](https://github.com/Comfy-Org/ComfyUI/blob/0d80858061b511bd38c8cef4c235ef8e01040822/comfy/cli_args.py) 与 [`model_management.py`](https://github.com/Comfy-Org/ComfyUI/blob/0d80858061b511bd38c8cef4c235ef8e01040822/comfy/model_management.py)，commit `0d80858061b511bd38c8cef4c235ef8e01040822` | 官方代码提供 `--disable-pinned-memory`、`--disable-async-offload` 和实验性 `--fp16-intermediates`；Linux pinned-memory 上限可达到 RAM 的 90%（仍受其他约束）。这是社区 16/24GB workaround 所依赖的正式机制。 | 代码存在不证明目标硬件成功；关闭 pinning/offload 可能降低吞吐。`--fp16-intermediates` 明确是实验性，需影音质量复测。 |

### B/C/D 级：只能生成复测候选或否定项

| ID / 等级 | 来源与版本 | 可提取线索 | 风险 / 所需复现 |
|---|---|---|---|
| COMFY-ISSUE-15337 / C | [ComfyUI Issue #15337](https://github.com/Comfy-Org/ComfyUI/issues/15337)，2026-08-06，ComfyUI `0.30.0` commit `6f7cd7f`、RTX 5070 Ti 16GB、Windows 11、PyTorch `2.13.0+cu130` | 官方 I2V template 默认参数在 pinned memory/async offload 下 native crash；同时使用 `--disable-async-offload --disable-pinned-memory` 后，同一 workflow 155s 完成。 | 普通用户开放 Issue，不是维护者确认；安全软件/WDDM 可能参与。只能作为精确 Windows 复测候选。 |
| COMMUNITY-3090 / C | [tonyd2wild/minimax-h3-local](https://github.com/tonyd2wild/minimax-h3-local/tree/76abed188f3e7ef210a223ee23a2ce1b005d5c9a)，commit `76abed188f3e7ef210a223ee23a2ce1b005d5c9a`，2026-08-04 | 报告单 RTX 3090 24GB + 31,997MB RAM、ComfyUI 0.30.1、PyTorch `2.11.0+cu130`，使用量化权重、`--disable-pinned-memory --fp16-intermediates` 完成 832×480、362 frames、15.08s，约 19.8GB peak VRAM；记录 H.264/AAC、帧/亮度/音量校验。 | 个人仓库，缺少独立复核与制品哈希。它否定“只加 swap”和“第二张 GPU 自动解决 host pinning”；只能用于隔离单卡 capacity trial。新增 swap 属系统变更，若计划包含必须单独审核，不应默认执行。 |
| VLO-L40S-COMMENT / C | [vLLM-Omni roadmap #5700 用户评论](https://github.com/vllm-project/vllm-omni/issues/5700#issuecomment-5187762935)，2026-08-05，author association `NONE` | 报告 4×L40S 46GB、vLLM 0.26.0 + source vLLM-Omni，TP4+CPU offload、CUDNN attention，480×256/4s/50 steps，约 24GB/GPU，H.264 + 32kHz stereo AAC。关键 flags 为 `--cpu-offload-gb 50 --offload-group-size 1`；移除会 OOM。 | 非维护者评论且未固定 vLLM-Omni commit。还报告 `--vae-patch-parallel-size 4` 会 empty-task error。必须先找到相同 flags 的固定上游代码，再在 4×L40S 复现；不得据此宣布 L40S 支持。 |
| COMFY-ISSUE-15453 / C | [ComfyUI Issue #15453](https://github.com/Comfy-Org/ComfyUI/issues/15453)，2026-08-09，ComfyUI `0.30.2` commit `dec5d945`、RTX Pro 2000 16GB | 报告 243-frame sampling 后 VAE decode OOM；当时 `decode_tiled()` 实际重跑同一 decode，`--reserve-vram` 与提高 estimate 未释放动态 resident weight。显式卸载后相同请求可完成。 | 这是重要的否定证据：不能把“自动 tiled retry”当 fallback。优先固定 COMFY-PR-15446；若仍需显式 unload，必须使用上游支持的节点/机制，不能把社区临时 core patch写入生产计划。 |
| COMFY-ISSUE-15663 / C | [ComfyUI Issue #15663](https://github.com/Comfy-Org/ComfyUI/issues/15663)，2026-08-16，ComfyUI `0.33.0`、RTX 4090 24GB、PyTorch `2.13.0+cu130` | 报告短片显存预算反而更乐观：2s 保留 16GB weights 后 OOM，而 10s 完全 offload 可稳定；同一 graph 的决策疑似缓存。 | 开放 Issue 且原因未验证。短 smoke 不能天然视为最小内存 workload；capacity trial 必须覆盖短片和计划最大 workload，并记录实际 residency。 |
| CSDN-H3-RTX5090 / D | [CSDN 文章](https://blog.csdn.net/gitblog_00670/article/details/159141415)，2026-08-05，声称 ComfyUI `14b05228`、comfy-kitchen `0.2.26`、PyTorch `2.8.0+cu128` | 给出第三方 INT8 ConvRot text encoder、文件大小与约 24.7/26.1GiB allocation/reservation，可作为检查量化文件布局和 checksum 的线索。 | 文章混合 `cu128` 与“推荐 CUDA 13”，只展示 encoder 级检查，没有完整 H3 L5/L6、原始日志或产物哈希；40GB storage 也不代表完整 pipeline 容量。不得支撑部署或质量主张。 |
| REDDIT-3090 / C | [r/StableDiffusion 帖子](https://www.reddit.com/r/StableDiffusion/comments/1vhloyz/walter_white_and_the_minimax_h3_official/)，2026-08-07 | 发帖者称 RTX 3090 使用最新 ComfyUI 默认 H3 template 与 Sage Attention 完成 1MP、14s、约 40min 视频。 | 缺少精确 framework commit、依赖、峰值资源和可审计命令；只证明值得询问/复测，不进入 recipe。 |

## 候选队列

| 优先级 | 目标硬件 | 候选 | 当前状态 | 晋级条件 |
|---|---|---|---|---|
| P0 | 2×/4× RTX 4090 24GB | vLLM-Omni TP2+DLO，12 resident layers，CUDNN，1024×576 | `OFFICIAL_MECHANISM_FOUND` | 固定包含 `6d4e4ff` 的可安装 commit/image；host available RAM ≥200GiB；精确 target L5/L6 与峰值记录。 |
| P0 | ComfyUI 16–24GB | 固定包含 VAE chunked-I/O 修复；按主机事实试验关闭 pinned/async offload | `OFFICIAL_MECHANISM_FOUND + COMMUNITY_LEAD` | 先分别做 baseline 与单变量试验；不得同时改多个 flag 后归因。验证 kernel OOM、host OOM、黑帧、冻结帧和静音。 |
| P1 | 1×RTX 3090 24GB + 32GB RAM | 量化 ComfyUI、832×480、关闭 pinned memory | `COMMUNITY_LEAD` | 验证量化权重 provenance/hash、官方加载支持、5s 与 15s 两个 workload、host RSS/swap、L5/L6。 |
| P1 | 4×L40S 46GB | vLLM-Omni TP4 + group-size-1 CPU offload，单 FL2VA partition | `COMMUNITY_LEAD` | 固定 framework commit；核对 flags 在该 revision 的语义；复现 API、peak HBM/host RAM/latency；确认不启用已知失败的 VAE patch parallel。 |
| P1 | 1×RTX PRO 5000 72GB | SGLang DiT layerwise + text encoder CPU offload | `OFFICIAL_MECHANISM_FOUND` | 固定包含 `a79340d` 的 revision，并确保不选择已知 OOM 的纯 model-wise 组合。 |
| P2 | 非推荐多节点 | SGLang Ulysses(node-local)+Ring(cross-node) | `OFFICIAL_MECHANISM_FOUND`, MVP 默认外 | 只有 2×8 H200、fabric/NCCL 与 `encoder-parallel replicate` 精确匹配时才可另审 recipe；其他 GPU 不外推。 |

## 安全与复现门禁

候选进入 capacity trial 前，除既有 license/需求门禁外，至少满足：

- 主机只读发现记录精确 GPU ID、空闲 HBM、进程、`nvidia-smi topo -m`、P2P/NCCL、driver/CUDA、available RAM/swap 和分区磁盘；不能用产品名近似匹配。
- 固定 model revision、framework commit/container digest、PyTorch/CUDA 和量化 checkpoint hash。第三方量化权重还需 license/provenance 审核。
- 隔离 venv/container、独立 model/cache/output 路径、单并发、禁止停止无关任务；不得为 workaround 修改驱动、系统 CUDA、系统 Python、防火墙或现有服务。
- workload 同时覆盖最小 smoke 与计划上界。H3 的短片预算可能反常，不能只测“更小”的 shape 就宣布容量安全。
- 采集 GPU allocator high-water、`nvidia-smi`、host RSS/PSS/available、swap、OOM killer/TDR、PCIe/fabric 和 wall time。offload 的 HBM 降低不能掩盖 host RAM 或总线压力。
- L5 必须完成真实请求；L6 必须完整解码 MP4，验证非空/非冻结/非全黑视频、时长/FPS、AAC 32kHz stereo、非静音以及 runtime 无错误。FSDP/量化/SageAttention 尤其要检查噪声和静默错误。
- 任一 OOM、native crash、TDR、黑帧、噪声、静音、timeout、输出不完整或无法解释的同 seed 漂移，均为试验失败。失败后恢复隔离环境并记录，不自动尝试更多侵入性系统修改。

## 对当前模型文档的影响

- 现有“官方配置外不得外推”仍成立，但应改读为“不得直接作为生产兼容配置”；它不应阻止继续收集 fallback 候选。
- vLLM-Omni 的 2×RTX 4090 已从 capacity proxy 晋级为官方项目固定 recipe 的目标硬件实测；仍只覆盖精确版本、shape 和 host-memory 条件。
- ComfyUI 单 3090 路线仍是实验性。上游 VAE 修复和官方 memory flags 提供 A 级机制，社区 3090 结果只提供目标试验参数。
- 多 GPU 数量本身不是容量保证：Ulysses 可复制完整 DiT，ComfyUI 第二张卡不自动消除 host pinning，TP/DLO/FSDP 必须与固定上游 topology 匹配。

## 检索说明

本次使用 Agent Reach：`doctor --json` 确认 Exa、GitHub CLI、Reddit OpenCLI 与 Jina Reader 可用；使用 Exa 做跨站技术搜索、GitHub CLI 读取 Issue/PR/固定源码、Reddit OpenCLI 读取社区讨论、Jina Reader 核对 CSDN 页面。Twitter CLI 搜索按重试链直接重试后仍无结果，OpenCLI 兜底也未返回可用材料，因此未将 Twitter 内容纳入证据。
