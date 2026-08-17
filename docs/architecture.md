# 架构

该工具链是一系列文件契约，而非工作流服务。每个阶段消费已验证的 artifact 并产出下一项；policy 控制状态转换，Agent 负责协调。

| 阶段 | 读取 | 写入 | 远程权限 |
| --- | --- | --- | --- |
| Intake / Gate | 用户请求、请求 schema/policy | 已验证请求或缺失字段 | 无 |
| Host Discovery | 已通过门禁的请求 | 当前主机档案 | 只读 |
| Research | 模型问题、来源 policy | 证据 artifact | 无 |
| Plan / Review | 请求、主机档案、recipe、证据 | 已审核部署计划 | 无 |
| Execute | 未改变的 `READY` 计划 | 步骤结果/日志元数据 | 单一写入者，受计划约束 |
| Verify | 计划、推理记录、服务/输出、语义审核 | L1–L6 结果 | 读取加计划内推理 |
| Record | 所有结果 | registry 和知识记录 | 本地文件系统 |

工具链核心了解如何验证、探测、规划、强制执行、验收与记录。模型 recipe 知道适用的 artifact 和框架、具备哪些兼容性证据、如何启动服务以及如何检查其生成输出。这是无需改变生命周期即可加入未来模型的分界面。

Registry 状态有意分为两层：

```text
known_state (historical expectation) + observed_state (timestamped live evidence)
```

两层互不覆盖。调用者可以看到过期与矛盾信息。
