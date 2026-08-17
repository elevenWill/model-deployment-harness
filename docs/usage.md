# 使用说明

本文说明如何从零开始使用模型部署工具链。首次接入真实服务器前，请先通读
[操作契约](../AGENTS.md)；它定义了不可跳过的安全边界。

## 1. 开发环境安装

需要 Python 3.10 或更高版本。在项目根目录执行：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

最后一条命令应通过全部 mock 测试；它不会连接真实服务器。

## 2. 配置凭据与 SSH 信任

仅在已获授权进行真实检查或部署时创建 `.env`：

```bash
cp .env.example .env
chmod 600 .env
```

按实际认证方式填写，未使用的变量保持为空：

```dotenv
# 推荐：SSH 密钥认证
DEPLOY_SSH_KEY_PATH=/绝对路径/id_ed25519
DEPLOY_SSH_KEY_PASSPHRASE=

# 仅当密钥认证不可用时填写
DEPLOY_SSH_PASSWORD=

# Hugging Face 访问探测提示需要时再填写
HF_TOKEN=

# 仅当已审核计划中的步骤明确要求该变量时填写
MODEL_TOKEN=
```

`.env` 已被 Git 忽略，绝不可提交。工具会优先使用 SSH 密钥，只在密钥认证失败且提供
密码时回退到密码认证。

首次连接前，必须通过可信渠道核对主机 SSH 指纹，并写入本机的 `~/.ssh/known_hosts`。
工具使用严格的 known-host 校验，不会自动接受未知主机。

## 3. 创建部署请求

在项目根目录创建 `request.json`。它是用户意图，不应包含从主机探测得出的 CPU、内存、
GPU 占用或磁盘事实。最小示例如下：

```json
{
  "schema_version": "1.0",
  "request_id": "minimax-h3-test-001",
  "requested_at": "2026-08-17T10:00:00+08:00",
  "requested_by": "operator-id",
  "target": {
    "host": {
      "address": "10.0.0.25",
      "ssh_username": "deploy",
      "ssh_port": 22
    },
    "gpu_ids": [0, 1],
    "install_root": "/srv/model-runtime",
    "model_root": "/data/models"
  },
  "model": {"id": "minimax-h3", "variant": "fl2va"},
  "framework_preference": "ALLOW_RECIPE_SELECTION",
  "service": {
    "mode": "container",
    "bind_host": "127.0.0.1",
    "port": 30010
  },
  "existing_environment_policy": "PRESERVE_AND_ISOLATE",
  "intended_use": "内部评估",
  "deployment_region": "CN"
}
```

以 [部署请求 Schema](../schemas/deployment-request.schema.json) 为准。必填字段缺失时，
工具会返回 `NEEDS_USER_INPUT`，不会猜测端口、GPU 或目录。

MiniMax-H3 的 `fl2va` 与 `ref2va` 单分区各约 135 GiB；`both` 约 270 GiB，规划时还要
为缓存、临时文件、媒体输出和回滚预留空间。EU、UK、KR、US 部署需单独授权依据。

## 4. 需求门禁与只读主机侦察

先验证请求；该命令不会连接主机：

```bash
python scripts/preflight.py requirement --request request.json
```

得到通过结果后，才可在用户明确授权范围内进行只读 SSH 侦察：

```bash
python scripts/probe_host.py \
  --host 10.0.0.25 \
  --host-id minimax-test-host \
  --username deploy \
  --port 22 \
  --output host-profile.json
```

再对探测结果执行预检：

```bash
python scripts/preflight.py host \
  --request request.json \
  --host-profile host-profile.json \
  --environment-strategy container \
  --isolated
```

预检会阻止 GPU 被占用、端口被占用、发现不完整、CUDA/驱动不兼容或未隔离环境等情况。
它不会停止其他进程、升级驱动或修改系统环境。

## 5. 编写与审核部署计划

当前 MVP 不会自动猜测部署方案。根据 `request.json`、`host-profile.json`、模型 Recipe 和
高置信研究证据，编写符合 [部署计划 Schema](../schemas/deployment-plan.schema.json) 的
`deployment-plan.json`。

计划至少需要：

- 对应的请求 ID、主机 ID、精确 GPU ID、目录、模型变体和服务参数；
- 固定的框架 commit、受该 checkout 约束的绝对可执行路径与 CUDA 要求；
- 每一个远程命令的受限执行步骤、回滚条件和验证要求；
- 许可证门禁、S/A 级研究证据、完整生命周期制品和批准审核；
- 状态 `READY` 及匹配的 canonical SHA-256。

请以 [契约说明](contracts.md) 和 `schemas/` 为准。没有审核完成的 `READY` 计划，
执行器将拒绝任何远程写入。

## 6. 执行已审核计划

以下命令会在远程主机上执行计划允许的写操作。执行前请确认计划已审核，并已获得本次
真实部署的明确授权：

```bash
python scripts/remote_exec.py \
  --plan deployment-plan.json \
  --request request.json \
  --host-profile host-profile.json \
  --host 10.0.0.25 \
  --username deploy \
  --port 22 \
  --env-file .env
```

执行器会重新侦察主机、核对实际 runtime commit、确认精确 GPU 未被占用，并使用远程
原子锁避免两个控制器同时写入。它不会自动执行受保护操作，例如驱动/CUDA 变更、系统
升级、删除模型、停止无关进程、重启或防火墙修改。

## 7. 真实推理与验证

执行完成不表示部署成功。先让工具自己提交真实请求、轮询作业并下载媒体：

```bash
python scripts/run_inference.py \
  --plan deployment-plan.json \
  --recipe models/minimax-h3/verify.yaml \
  --endpoint http://127.0.0.1:30010 \
  --request-payload smoke-request.json \
  --media-output output.mp4 \
  --response-output inference-response.json \
  --proof-output inference-proof.json
```

`--endpoint` 的主机和端口必须符合已审核计划。若服务只绑定远端 `127.0.0.1`，请在目标
主机上运行该命令，或使用已获批准的 SSH 端口转发。

检查输出画面和音频后，创建绑定输出哈希的 `semantic-review.json`，然后完成 L1–L6 验证：

```bash
python scripts/verify_service.py \
  --plan deployment-plan.json \
  --observations verification-observations.json \
  --recipe models/minimax-h3/verify.yaml \
  --media output.mp4 \
  --inference-proof inference-proof.json \
  --semantic-review semantic-review.json \
  --output verification-result.json
```

只有 L5 真实推理和 L6 媒体/语义验证都通过时，结果才会是 `VERIFIED`。

## 8. 查询与知识记录

读取已登记信息：

```bash
python scripts/registry.py show-host --host-id minimax-test-host --live --username deploy
python scripts/registry.py show-deployment --deployment-id minimax-h3-test-001 --live --username deploy
```

`--live` 会触发已授权的只读 SSH 探测；没有实时探测时，历史记录不会被伪装成当前状态。

记录事故、经验或基准结果：

```bash
python scripts/registry.py record-incident --file incident.json
python scripts/registry.py record-lesson --file lesson.json
python scripts/registry.py record-benchmark --file benchmark.json
```

## 常见阻断原因

| 现象 | 处理方式 |
| --- | --- |
| `NEEDS_USER_INPUT` | 补齐部署请求中的必填意图字段。 |
| SSH host key 被拒绝 | 通过可信渠道核对指纹后更新 `known_hosts`，不要关闭严格校验。 |
| GPU 或端口已占用 | 修改请求并重新审核计划，或选择用户明确指定的其他资源；不要杀死无关任务。 |
| CUDA/驱动不兼容 | 选择兼容主机或重新审核可行的隔离 runtime；容器不能修复过旧主机驱动。 |
| License 门禁阻断 | 核对地区和用途；受限地区需要单独授权记录。 |
| `VERIFICATION_FAILED` | 保留证据，检查作业响应、媒体解码和语义复核，不要把进程存活视为成功。 |
