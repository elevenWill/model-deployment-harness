# ModelScope MiniMax-H3 revision 可复现性复核

获取日期：2026-08-17。范围为 `modelscope==1.31.0` 的 `modelscope download`
路径和 ModelScope 公开只读 API；没有连接或写入目标主机。

## 结论

当前 `master` 的后端 commit 是
`b443a9325ea31c1020aa56bc061fe3a0db82601e`，并且 ModelScope 的 repo-files API
能以该完整 SHA 成功返回文件树，因此它是可用于固定产物清单的不可变 commit。
仓库没有 tag。

但 `modelscope==1.31.0` 的旧客户端会先只允许 API 列出的 branch/tag，故在发起
下载前错误地拒绝这个 commit；这解释了目标机的 CLI 报错。不能因该客户端限制而把
`master` 误称为不可变，也不能声称旧 `modelscope download` 可接受该 SHA。

## 一手证据

- ModelScope 的公开 revisions API 在本次观测中返回
  `Branches: [{"Revision":"master","CreatedAt":1786968958}]`，`Tags: []`：
  [API response](https://www.modelscope.cn/api/v1/models/Comfy-Org/MiniMax-H3/revisions)。
  同一模型信息端点报告 `Revision: "master"`、`ModelRevisions: null`：
  [model API](https://www.modelscope.cn/api/v1/models/Comfy-Org/MiniMax-H3)。
- `modelscope==1.31.0` 的官方
  [wheel](https://files.pythonhosted.org/packages/bb/a8/26bda5fdbcb9ad18c52d81961520b3b55bc082969a46dc8b18495ef7726b/modelscope-1.31.0-py3-none-any.whl)
  （SHA-256 `16fc2d4209d508cd8b511c07ef74d92531aa0ee1f4c303a19e0234f60a7371a9`）中，
  `HubApi.get_valid_revision_detail()` 先取得该 branch/tag 列表，再拒绝不在两者
  中的 revision；`file_download.py` 在下载前调用该校验。这是客户端限制，而不是
  后端 revision 不存在的证明。
- 对同一官方 wheel 的本地只读调用可复现：
  `get_valid_revision_detail('Comfy-Org/MiniMax-H3', 'master')` 返回上述 `master`
  条目；传入 `b443a9325ea31c1020aa56bc061fe3a0db82601e` 抛出
  `NotExistError: The model ... has no revision ...`。这与目标机的 CLI 拒绝一致。
- 与之相对，ModelScope 的官方 repo-files API 以该完整 SHA 成功返回 25 个文件及
  `LatestCommitter.ShortId: "b443a932"：
  [pinned API response](https://www.modelscope.cn/api/v1/models/Comfy-Org/MiniMax-H3/repo/files?Revision=b443a9325ea31c1020aa56bc061fe3a0db82601e&Recursive=True)。
  该端点的 `master` 响应也报告同一 `LatestCommitter`，故本次观测时 `master` 指向
  此 commit。

## 部署处理

应保留该 SHA 和每个文件 checksum 作为不可变目标；但若继续使用
`modelscope==1.31.0`，需要升级到支持 commit revision 的官方客户端下载器，或使用
经审核的等价 API 客户端。临时使用 `--revision master` 只能在下载后以 manifest
hash 校验，且必须明确其 TOCTOU 风险，不能替代 commit pin。

## 当前 master 的五个白名单资产 manifest

读取端点：
[`GET /api/v1/models/Comfy-Org/MiniMax-H3/repo/files?Revision=master&Recursive=True`](https://www.modelscope.cn/api/v1/models/Comfy-Org/MiniMax-H3/repo/files?Revision=master&Recursive=True)。
获取时间：2026-08-17；HTTP 200；`LatestCommitter.ShortId` 为 `b443a932`。

| 相对路径 | SHA-256 | size (bytes) |
|---|---|---:|
| `diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a` | 20,970,379,616 |
| `diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors` | `9255f52b6677845ad238f20dfaafa94727053694127ab7f255c048f0f9365779` | 20,970,379,616 |
| `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6` | 15,687,142,551 |
| `vae/minimax_h3_audio_vae_fp32.safetensors` | `8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48` | 605,254,808 |
| `vae/minimax_h3_video_vae_fp16.safetensors` | `7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522` | 5,207,808,496 |
