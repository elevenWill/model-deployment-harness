# 对抗性审核报告

初始审核基线：commit `089b6f1` 加当时未提交的 working tree。审核日期：2026-08-17。当时 39 项测试与 Ruff 均通过，但这些检查没有覆盖以下 exploit。本节保留为历史 finding 台账；最终处置见后文。

## 标准维度

### CRITICAL

1. **Action-label command smuggling。** `scripts/remote_exec.py` 按声明的 `action` 分类步骤，却执行不受限 argv。因此 `create_target_directory` 步骤可在审核后携带 `reboot`、`rm` 或其他 protected command。每个允许 write action 都应绑定严格 argv/target validator，并添加错误标记命令攻击。
2. **SSH endpoint 未绑定用户 selector。** CLI 可接受 request 或 profile 中的 host。对地址 A 的 request 可能针对另行提供的 profile/host B 执行。要求 live endpoint 同时匹配用户 selector 和由同一连接产生的 profile。

### HIGH

1. **Stale preflight。** 文件间 timestamp 相等并不强制 freshness，live drift check 又是可选的。应在 write 前立即经 execution transport 重新探测。
2. **可伪造验证。** 调用者编写的 L5/L6 PASS field、duration 与 artifact reference 可在没有 inference、response evidence、文件存在或 hash 检查的情况下产生 `VERIFIED`。
3. **仅控制器本地的 writer lock。** `/tmp` `flock` 无法协调不同 controller machine 的 executor。应增加 remote atomic host lease，且 stale-lock 必须 fail-closed。
4. **误导性的 live status。** 所有 `NOT_CHECKED` observation 都被汇总为 `OK`。健康状态应要求 PASS，并明确报告 incomplete/stale check。
5. **仅声明式 lifecycle。** 没有 runtime transition evidence 强制状态序列。执行前应添加并验证有序 lifecycle transition。

### MEDIUM

1. **Secret-name drift。** SSH/environment loading 接受 `MODEL_TOKEN`，但 persisted-artifact scanner 漏扫。应使用统一的 canonical secret-name set。

### LOW

无发现。

## 规格维度

### CRITICAL

1. **Action-label command smuggling。** fake transport 执行了审核为 `create_target_directory` 但被改为 `rm -rf /` 的步骤，违反 §§15–16 及 plan-bypass attack。
2. **没有 inference/media 的 `VERIFIED`。** 声称的 PASS levels 加不存在的 path 仍产生 `VERIFIED`，违反 §19。
3. **License input 未绑定。** 通过的 CN/research plan 可对变更为 US/commercial 的 request 执行，因为未比较 region/use，PASS 也不要求记录 acceptance。

### HIGH

1. **Stale preflight 与 local-only writer lock。** 新近被占用的 GPU 与第二个 controller 不一定会被可靠阻止（§16）。
2. **未强制 source quality 和 immutable pins。** 悬空 evidence ID、C/D evidence、mutable version 和 null recipe pin 可以通过 plan validation（§§12、14）。
3. **没有真实 live-status query。** 尽管 §11 要求在可达时 fresh probe，registry command 仅输出 stored 或 caller-supplied state。

### MEDIUM

1. **Eval/review 完整性缺口。** 12 个 safety eval 未直接覆盖全部 15 个命名 attack，特别是 plan bypass、cross-controller writer、structured incident 和 second-host reuse。
2. **缺少 Benchmark capture。** 有 fact/decision/incident/lesson，但没有 Benchmark schema 或 registry command（§20）。

### LOW

无发现。

## 修复处置

在初始审核时，所有 CRITICAL 和 HIGH finding 都是 release blocker。最终处置记录如下，而不是改写原始 finding。

## 最终修复与复审

最终本地基线：**63 项测试通过**、Ruff 通过、所有 JSON schema 可解析、六条 CLI help path 通过，并且 `git diff --check` 通过。未尝试 SSH 连接或真实模型 inference。

已解决的控制措施：

- action label 现映射到严格 command grammar；Docker 绑定精确 GPU ID 和 immutable image digest，native launch 使用 pinned checkout 的 absolute venv executable 与 exact argv；
- request、审核 profile 与 fresh live hostname/address 独立绑定；historical alias 从不注入 observed identity；
- write 前重复 discovery，runtime revision 在 remote lease 内重复，且精确 atomic remote lock command 序列化进审核 plan；
- recipe/source pin、S/A evidence、compatibility/CUDA requirement、license region/use、lifecycle stage envelope、typed source artifact、cross-stage ID 及两个 artifact hash 都作为 fail-closed plan input；
- `run_inference.py` 执行审核的 POST/poll/content-download workflow 并产生 typed request/response/output proof；最终化独立检查 hash、media decode 及具名 semantic review，忽略调用者自写的 L5/L6 PASS；
- mock-only test 覆盖 live registry status、benchmark capture、全部 18 个命名 safety theme、incident/lesson integrity 以及 `.env`/environment secret scan。

剩余操作边界不构成成功部署证据：

- MiniMax-H3 recipe 尚未在用户指定服务器上运行；
- semantic task alignment 仍依赖可归属的人工审核，而不是通用自动 vision/audio judge；
- 已实现 source-checkout runtime；额外 container-runtime provenance profile 在使用前需要独立审核 contract。
