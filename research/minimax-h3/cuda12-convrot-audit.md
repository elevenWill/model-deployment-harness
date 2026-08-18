# CUDA 12.2 与 MiniMax H3 `pruned_int8_convrot` 复核

获取日期：2026-08-17。范围是 ComfyUI 固定提交 `0d80858061b511bd38c8cef4c235ef8e01040822`、其 `comfy-kitchen==0.2.31` 依赖，以及 121.30 的 RTX 3090 / NVIDIA 535.104（CUDA 12.2 compatibility）。本文件不声称完成了目标主机推理测试。

## 可直接确认的事实

- `pruned_int8_convrot` checkpoint 本身不是 CUDA 版本绑定的二进制；但 H3 的 ComfyUI 工作流还需要 `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` 文本编码器，不能只评估 diffusion checkpoint。
- 固定提交的 [ComfyUI `quant_ops.py`](https://github.com/Comfy-Org/ComfyUI/blob/0d80858061b511bd38c8cef4c235ef8e01040822/comfy/quant_ops.py) 在 PyTorch CUDA 小于 13 时会禁用 `comfy-kitchen` 的 CUDA backend，并记录 `cu130` 警告；默认也关闭 NVIDIA 上的 Triton backend。因此它会走 eager，而不是自动改走 Triton；该代码并不在这个分支直接抛出异常。
- 同一版本的 [comfy-kitchen 0.2.31](https://github.com/Comfy-Org/comfy-kitchen/tree/7c6ca3a5b63857d42c2d49777d6afb69de23f13f) 功能矩阵列出了 `int8_convrot` 的 eager 实现，并为 `NVFP4` 也列出了 eager 实现。它另有纯 Python wheel（eager/Triton）和 CUDA wheel；后者要求 CUDA runtime >= 13 和 NVIDIA r580+ 驱动。
- 因此，CUDA 12.x 上可以设计 **eager 回退实验**；但默认安装当前的 Linux x86_64 CUDA wheel 并不适合 535.104 驱动。必须显式选择可审计的 CUDA 12.x PyTorch 和 pure-Python `comfy-kitchen`，而不是执行未固定的 `pip install -r requirements.txt`。
- [NVIDIA CUDA 13.0 release notes](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html) 给出 Linux CUDA 13.0 的最低驱动 `580.65.06`；121.30 的 535.104 无法运行 cu130 runtime。
- PyTorch 的 [历史安装说明](https://docs.pytorch.org/get-started/previous-versions/) 提供 CUDA 12.1 wheels（例如 `torch==2.5.1`）。121.30 的 535.104 高于 CUDA 12.2 对 Linux 的最低驱动 535.54.03，故 cu121/cu122 是可构造的基础环境；这不是 H3 已验证配置。

## 对待核实材料的判定

| 主张 | 判定 | 原因 |
|---|---|---|
| cu130 仅是 ConvRot CUDA 加速条件 | 部分成立 | 对 `quant_ops.py` 的 backend 禁用行为成立；但当前 ComfyUI 系统要求仍称 NVIDIA 20 系及以上需 cu130+，且 H3 还涉及 NVFP4。 |
| CUDA 12.2 100% 无报错、FL2VA/I2V/Ref2VA 全功能 | 未证实 | 未找到 Comfy-Org maintainer 对 CUDA 12.2 + RTX 3090 + H3 两变体的成功证明。已找到的 H3 eager 成功报告为 cu128，不能外推到 cu122。 |
| 画质完全无差异 | 未证实 | checkpoint 不因 CUDA runtime 改变，但 eager、CUDA kernel 与内存/offload 路径不是一个已证明 bitwise-identical 的 H3 输出实现。没有上述精确配置的对照测量。 |
| 30 系升级 cu130 收益很小，慢 15%--30% | 未证实 | 未找到对应固定 workflow、分辨率、时长、seed 和 3090 的一手 benchmark。不可将其当成容量或 SLA 结论。 |
| 仅 NVFP4/FP8 有版本限制，INT8 没有 | 不完整 | `comfy-kitchen` 的 NVFP4 同样具有 eager 支持；严格的 CUDA 13/r580 条件来自该项目的 **CUDA wheel**，并非只针对两种格式。 |
| CSDN 多篇实测已证明 | 无法审计 | 未提供文章 URL、作者、版本、命令、权重哈希或输出比较，不能作为关键部署依据。 |

## 结论与安全决策

“CUDA 12.2 不一定不能运行”是合理判断；“当前默认 ComfyUI 安装即可保证无报错且画质无差异”没有证据支持。若继续，应作为受控实验：固定 CUDA 12.1/12.x PyTorch、强制 pure-Python `comfy-kitchen`、禁止默认 CUDA wheel、单并发低分辨率，并分别完成 FL2VA 与 Ref2VA 的 L5/L6 输出验证。未通过时必须停止自身服务，不把结果报告为 VERIFIED。
