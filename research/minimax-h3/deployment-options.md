# MiniMax-H3 部署选项

证据基线和获取日期见 [official-sources.md](official-sources.md)。下列任何命令均不构成远程执行授权。

## 决策摘要

| 选项 | 选择时机 | Model path 语义 | Service/API | 置信度 |
|---|---|---|---|---|
| SGLang Diffusion | 首选 recipe：精确 NVIDIA topology matrix 最强，MiniMax README 以它作为部署示例。 | 传入 root Hub ID `MiniMaxAI/MiniMax-H3`（或 ModelScope `MiniMax/MiniMax-H3`）加 `--model-variant fl2va|ref2va`；**不要**指向已下载的 task subdirectory。 | 异步 OpenAI-compatible `POST /v1/videos`，poll `GET /v1/videos/{id}`，获取 `.../{id}/content`。 | 引用的精确 topology 为高。 |
| vLLM-Omni | 需要原生 H3 pipeline、combined FL2VA+Ref2VA service、consumer-GPU DLO 或 vLLM-Omni 操作标准化时。 | Root ID 可加载 combined service；`--task-type fl2va|ref2va` 限制下载/service。本地示例可指向 `${MODEL_ROOT}/FL2VA` 且保留 sibling layout。 | 异步 `/v1/videos` 和同步 multipart `/v1/videos/sync`。 | 实现支持为高；精确硬件主张各异。 |
| Core vLLM | 不要单独为 H3 选择。 | 当前 core tree 未发现 H3 audio-video pipeline。 | N/A | 中高，仓库检查推断。 |
| Hosted-assisted full 2K | 仅在用户明确接受 MiniMax API 使用、credentials、data transfer、cost 与 region/terms 时。 | 本地 H3-Base 加托管 H3-Context-IR 与 H3-Regenerate-2K。 | MiniMax Global 或 CN endpoint。 | 高，MiniMax 官方。 |

## Checkpoint 与下载计划

MiniMax 记录的 original-format 下载命令：

```bash
# SGLang/vLLM-Omni 风格布局的两个任务族
hf download MiniMaxAI/MiniMax-H3 \
  --include "model_index.json" "FL2VA/*" "Ref2VA/*" \
  --local-dir MiniMax-H3

# 仅 FL2VA
hf download MiniMaxAI/MiniMax-H3 \
  --include "model_index.json" "FL2VA/*" \
  --local-dir MiniMax-H3
```

计划规则：

- 下载前解析并记录精确 Hub revision；不要在未记录解析 SHA 的情况下部署可变 `main`。
- 从用户意图选择 `fl2va`、`ref2va` 或两者。每个 partition 约 135 GiB disk；不加区分地下载 root 也可能拉取 Diffusers-format weight，几乎重复 byte。
- 除非明确需要 shared model root，否则优先 framework-managed snapshot download。验证 filesystem free space、inode availability、cache location、proxy reachability 和 checksum/snapshot completion。
- 当前 HF metadata 是 public/ungated。不要默认要求 token，应先 probe access；secret 只来自 environment。

## SGLang 启动形式

从已验证具有 H3 能力的 pin 安装；当前上游 docs 显示：

```bash
uv pip install "sglang[diffusion]" --prerelease=allow
```

代表性官方命令（仅从审核 intent 替换 bind/port）：

```bash
# 4× H200 resident
sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va \
  --num-gpus 4 --ulysses-degree 4 --performance-mode speed --port 30010

# 4× H100 80 GB，测得最快 topology
sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va \
  --num-gpus 4 --tp-size 2 --ulysses-degree 2 \
  --performance-mode speed --port 30010

# 2× RTX 5090 32 GB，无损 layerwise-offload 路径
sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va \
  --num-gpus 2 --tp-size 2 --ulysses-degree 1 --performance-mode memory \
  --layerwise-offload-components dit,text_encoder,vae \
  --dit-offload-prefetch-size 1 --dit-layerwise-resident-layers 20 \
  --enable-torch-compile false --port 30010
```

每个 variant 使用独立 server/port，除非 framework 明确支持 combined routing。SGLang 的 `speed` 路径保持 eager，因为当前 `torch.compile` 会改变数值输出。FSDP 是容量 policy，并非自动更快。`quality=high`、FP8、Cache-DiT 和 AdaLN cache 均为 opt-in approximate/experimental 路径，需要特定任务的 A/B output validation。

## vLLM-Omni 启动形式

从包含 H3 support 的 checkout 安装；当前 recipe 使用 editable source。`ffmpeg` 与 `ffprobe` 必须位于 `PATH`。

```bash
uv venv && source .venv/bin/activate
uv pip install -e .
```

文档中的 single-GPU path 使用 model-level CPU offload，但并未给出通用 GPU minimum：

```bash
vllm serve MiniMaxAI/MiniMax-H3 --omni --trust-remote-code \
  --num-gpus 1 --enable-cpu-offload \
  --diffusion-attention-backend FLASH_ATTN
```

文档中 2× RTX 5090/4090 family 使用 TP2 加 distributed layerwise offload；只有 2×5090、1344×768 是 target-hardware validated。4090 行是保守的 capacity-proxy 起点，不是已验证 latency/support：

```bash
vllm serve MiniMaxAI/MiniMax-H3 --omni --trust-remote-code \
  --task-type fl2va --num-gpus 2 --tensor-parallel-size 2 \
  --text-encoder-tp-size 2 --vae-patch-parallel-size 2 \
  --vae-parallel-mode tile --vae-use-tiling \
  --enable-distributed-layerwise-offload --dlo-no-use-allgather \
  --dlo-resident-layers 20 --enforce-eager \
  --diffusion-attention-backend CUDNN_ATTN
```

对于 four-high-memory-GPU combined service，上游推荐 Ulysses4 和 tiled VAE patch parallelism；除非添加 CPU offload，否则两个 DiT 均为 resident。测量前先 warm 一次，因为 performance path 会 regionally compile DiT block。

## 真实验证契约

在真实生成完成且 media 通过验证前，部署不是 `VERIFIED`。

1. 记录解析 model revision、framework package/commit、PyTorch/CUDA/driver、精确 GPU ID/topology、command/config 和 free/peak GPU+host memory。
2. L4：提交请求并验证 structured status/error 行为。对 SGLang，poll job 至 `completed`；`failed` 或 timeout 都是失败。
3. L5：在 FL2VA 上生成至少一个 fixed-seed 4–5 s、768-short-edge T2VA request。部署任意 Ref2VA service 时，也运行一个代表性的、已许可本地 image/video reference；验证 FL2VA capability 时至少运行一个 endpoint-frame case。
4. L6：要求非空 MP4，使用 `ffmpeg -v error -i output.mp4 -f null -` 完整 decode，并通过 `ffprobe` 证明 24 FPS H.264 video、32 kHz AAC stereo audio；duration 必须在请求容差内且为 4–15 s。检查代表帧并聆听/检查非静音 audio；仅 container/codec metadata 不足以证明 semantic validity。
5. 不要求不同 topology/attention/compile setting 间 byte identity。fixed-seed consistency 仅与同一 pinned stack/topology 比较。将上游 reference script/result 作为 test vector，而不是本地运行成功的证据。

## 已知框架限制

- 两条 pipeline 均为 CFG-distilled：CFG parallel size 必须为 1。
- SGLang：发布路径使用 full attention；native sparse attention 已承诺但未发布。`torch.compile` 不适合作为 consistency ground-truth。引用的 cross-node H200 要求 node 内 Ulysses、node 间 Ring 和明确 encoder replication。
- vLLM-Omni：一个 generation request 对应一个 diffusion batch；第一次 compile request 是 warmup；pure Ulysses 会 replicate 完整 DiT，因此不是小 GPU 的容量方案；TeaCache 仅为 FL2VA 校准，且与 Cache-DiT 互斥；多种 Ref2VA serving combination 仍窄于模型公布的 input envelope。
- Ref2VA 是 semantic reference generation，而非 pixel-aligned editing。它可能 crop、recompose、reorder motion/cut，且不提供 denoising-strength control。
