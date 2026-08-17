# MiniMax-H3 硬件与容量说明

获取日期：2026-08-17。以下是**上游观察到的运行点**，不是最低要求。相似产品名、VRAM 容量或 interconnect 不会自动继承验证结论。

## 容量事实

- vLLM-Omni 报告每个任务分区约 **134 GiB BF16 safetensors / 135 GiB disk**，两个分区约 **270 GiB**。还必须预留 download staging/cache、framework/container、生成 media 和 rollback 空间；270 GiB 文件系统不是足够的操作容量。
- CPU/layerwise offload 改变 residency，并不会降低 checkpoint 字节数。vLLM-Omni 的 two-GPU DLO recipe 至少需要 **200 GiB available host RAM**，推荐 **384 GiB host**。SGLang 的 2×5090 运行使用 377 GiB host，并推荐同一量级。
- H3 使用完整 Qwen3-VL-32B encoder 权重和 33B dense DiT。activation memory 会随 resolution、frame count、prompt/reference sequence length，尤其是 multiple reference video 显著增加。preflight 必须使用目标 workload，而不只是权重大小。
- Host RAM、GPU HBM、model disk、cache/temp disk 和 media disk 是独立门禁。RAM 规划要包含 pinned-memory headroom 与 filesystem cache。

## SGLang 精确来源矩阵

来源：上游 [cookbook](https://github.com/sgl-project/sglang/blob/a54de989c8ba817ebb603c5443e694e5fcf7edb1/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx) 与 [selector](https://github.com/sgl-project/sglang/blob/a54de989c8ba817ebb603c5443e694e5fcf7edb1/docs/src/snippets/configs/MiniMaxAI/minimax-h3.jsx)。

| 硬件 | 已验证 placement/topology | 报告的容量/性能证据 | 适用性 |
|---|---|---|---|
| 8× B300 | Resident Ulysses8；FSDP Ulysses8；BF16/FP8 resident | 文档 sweep 中 BF16 peak 随 variant/encoder 变化；例如 Ref2VA replicate 为 124,490 MB/GPU，FP8 auto 为 52,816 MB/GPU。 | 8 GPU 是 benchmark topology，明确不是最低主张。 |
| 8× B200 | Resident Ulysses8；4× B200 FSDP Ulysses4 | FSDP 无损但慢于 8-GPU resident recipe。 | 仅精确 B200。 |
| 4× H200 (141 GB) | Resident Ulysses4（latency default）；FSDP Ulysses4；TP2+U2 memory trade | 1344×768/5 s/50 steps 时，warm U4 peak 94,290 MB/GPU，TP2+U2 为 63,490 MB/GPU；U4 更快。 | 单机 high-bandwidth topology。 |
| 2 nodes × 8× H200 | node 内 Ulysses8 + node 间 Ring2，encoder `replicate` | 已验证 long-sequence denoise scaling 和确定性的 repeated cross-node output。 | 要求已验证 fabric/NCCL 和明确 node rank/address；不是 MVP default。 |
| 4× H100 80 GB | TP2+U2 resident 最快；TP4+U1 resident memory 最低；FSDP+U4 容量 | 报告的 pipeline peak 分别为 66.04 GB、49.80 GB 和 57.01 GB/GPU。纯 U4 无法保持完整 pipeline resident。 | 将 80 GB 与 topology 视为硬匹配。 |
| 2× RTX 5090 32 GB | TP2 + layerwise offload，20 DiT block resident，384 GiB 级 host | 1344×768/5 s/50 steps：559.67 s，采样 peak 26.3 GiB/GPU。 | 精确验证的 consumer recipe；PCIe/offload 慢且 RAM 消耗高。 |

SGLang 也记录 AMD MI300X/MI355X AITER run，但 AMD 在本 Harness MVP 的 NVIDIA 边界之外。

## vLLM-Omni 精确来源矩阵

来源：上游 [recipe](https://github.com/vllm-project/vllm-omni/blob/d1e230c95ba12aec7664ee6fd18c0b2b2d0d6187/recipes/MiniMaxAI/MiniMax-H3.md)。

| 硬件/档案 | 证据 | 置信度 / 注意事项 |
|---|---|---|
| 1 GPU + model-level CPU offload | 官方项目 recipe；没有声明通用 HBM 或 GPU SKU minimum。active H3 component 必须能放入 GPU，host RAM 必须容纳 offloaded component。 | 中；容量必须以目标 task/shape 探测。 |
| 2× RTX 5090 32 GB, TP2+DLO | 一个 1344×768、124-frame、50-step T2VA 在 8m38s 完成；vLLM 0.26.0、vLLM-Omni dev `ae6577ea`、PyTorch 2.11.0+cu130 上的采样 peak 约 22.6 GiB/GPU。 | 对该精确运行置信度高；采样 `nvidia-smi` 不是 allocator high-water。 |
| 2× RTX 4090 24 GB, TP2+DLO | 1024×576、12 resident layer 标为 capacity-proxy starting point；proxy measurement 在 B300 rank 上发生。 | 中低；**未在目标硬件验证**，也没有 PCIe latency 主张。 |
| 4× B300 combined service | 无 offload、Ulysses4、VAE patch parallel4；上游测量 FL2VA 和 two-video Ref2VA。 | 精确 shape 时为高；multi-video Ref2VA 因更长 encoder/DiT sequence 慢得多。 |
| 1× B300 96 GB FP8 | 无 offload capacity check；global FP8 在记录对比中降低 resident peak。 | approximate path；应重新验证 video/audio quality。 |

上游提供 ROCm gfx942/gfx950 支持，但它不在 MVP 内。不得将 AMD evidence 映射到 NVIDIA。

## 拓扑与卸载 preflight

- 记录 `nvidia-smi -L`、每 GPU total/free/used memory、active process、`nvidia-smi topo -m`、PCIe link state、driver、CUDA compatibility、NUMA layout、CPU count、available RAM/swap 以及 model/cache filesystem free space。
- 要求用户选择精确 GPU ID；绝不替换为空闲 GPU。无关 GPU 正在使用时应 abort，而不是 kill。
- TP/FSDP/Ulysses/Ring 选择相互耦合。Ulysses sharding sequence/attention 但可能 replicate weight；TP sharding weight；FSDP 通过 per-block collective 降低 residency；offload 用 host RAM/bus traffic 交换 HBM。只选择有匹配上游 evidence 的 topology，或标为 experimental 并要求审核 capacity trial。
- Multi-GPU P2P/fabric 很重要。在将 SXM/NVLink 结果视为可迁移至 PCIe card 前，验证 peer access/NCCL。Cross-node execution 需要明确 fabric validation 和同步 model/cache access。
- 探测选定 partition 加至少操作 headroom 的 disk；主机仅容纳一个时，不要下载两个。下载后记录实际 snapshot byte。
- Warmup 和 peak capture 必须采用目标 resolution、frame/duration、task 与代表性 reference。reference-video count/length 可主导 memory 和 latency。
