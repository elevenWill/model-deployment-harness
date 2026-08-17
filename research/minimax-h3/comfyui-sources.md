# ComfyUI 检索来源

获取日期：**2026-08-17**。机器可读证据位于 [comfyui-evidence.yaml](comfyui-evidence.yaml)。

| 来源族 | 当前入口 | 用法与边界 |
| --- | --- | --- |
| ComfyUI 官方更新与核心逻辑 | [官方 Changelog](https://docs.comfy.org/changelog)、[GitHub Releases](https://github.com/Comfy-Org/ComfyUI/releases)、固定 commit `0d80858061b511bd38c8cef4c235ef8e01040822` 下的 `nodes_minimax_h3.py`、`comfy/ldm/minimax/`、`quant_ops.py`、`model_management.py` 与 `execution.py` | A 级。用于确认已发布能力和实际实现；部署必须固定 Release/commit，不能跟随滚动 `master`。 |
| GitHub 社区 Issue/PR | [MiniMax-H3 Issues](https://github.com/Comfy-Org/ComfyUI/issues?q=is%3Aissue+MiniMax+H3)、[MiniMax-H3 PR](https://github.com/Comfy-Org/ComfyUI/pulls?q=is%3Apr+MiniMax+H3)、[comfy-kitchen Issues](https://github.com/Comfy-Org/ComfyUI/issues?q=is%3Aissue+comfy-kitchen) | 已合并且固定 merge commit 的上游代码可作为 A 级；维护者确认是 B 级；普通开放 Issue 默认 C 级。未合并结论不能单独决定部署。 |
| PyPI `comfy-kitchen` | [PyPI 0.2.31](https://pypi.org/project/comfy-kitchen/0.2.31/)、[Comfy-Org/comfy-kitchen](https://github.com/Comfy-Org/comfy-kitchen/tree/cfcc843b6e8ec1e119b8fe8f7f8f6a46dad8599e) | A 级包元数据。当前 ComfyUI 固定 commit 的 `requirements.txt` 要求 `comfy-kitchen==0.2.31`；实际安装还要按平台记录 wheel SHA-256。 |
| CSDN 技术实测 | [RTX 5090 / INT8 ConvRot 部署记录](https://blog.csdn.net/gitblog_00670/article/details/159141415) | 默认 D 级，最多只能作为复测线索。必须提取硬件、驱动、PyTorch/CUDA、ComfyUI commit、包版本、命令和原始输出，并在目标主机复现后才能形成工具链自己的验证证据。 |

## 当前核验结果

- ComfyUI 最新已观察 Release 为 `v0.33.1`（2026-08-13），检索时 `master` 为 `0d80858061b511bd38c8cef4c235ef8e01040822`；二者不能混称为同一版本。
- 官方 Changelog 明确列出 MiniMax-H3 原生支持及后续 `int8_convrot` VAE、comfy-kitchen attention、峰值内存和音频修复。
- 固定 `master` commit 的 `requirements.txt` 使用 `comfy-kitchen==0.2.31`；PyPI 页面链接至 Comfy-Org 官方仓库，并提供 Python 3.10+ 的多平台 wheel。
- GitHub Issue/PR 反映出对 CUDA 版本、权重完整性、attention 对齐、动态显存和不同 GPU 的强环境依赖。它们适合构造预检与复测项目，不足以直接扩大兼容性声明。
