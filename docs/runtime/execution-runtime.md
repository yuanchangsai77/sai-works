# Execution Runtime

状态：领域基线与当前实现。负责把动作提案变成受约束的现实操作，保持结果、观察和副作用边界可检查。

## 执行语义

```text
Action Proposal → 可见性与结构校验 → 资源预检
                → Policy / Permission → 必要审批
                → Executor / Target → Result / Observation
                → Evidence / Checkpoint
```

模型不直接获得宿主机操作权。Capability 描述可使用的操作；Executor 是具体执行适配；Execution Target 表达操作在哪个资源环境中发生。这些职责不能因为当前以工具函数实现就合并。

## 当前执行路径

ToolRegistry 管理工具定义与调用，预检校验参数 schema、工作空间范围和执行拦截条件。引擎执行风险判断与必要审批，工具调用后统一包装结果并进入状态与日志路径。内置文件、搜索、patch、测试、只读 Git、Shell 与激活的 MCP 工具复用现有边界。

工具结果的可见输出与结构化运行信息分开；单项结果先受输出预算约束，再进入总上下文预算。稳定字段和错误语义见[工具契约](../reference/tool-contract.md)。

## 副作用、重试与去重

当前根据规范化动作指纹避免同一工作区世代内重复执行已确认动作。可能修改环境的成功操作推进工作区世代，旧读取和去重结果不能继续当作当前事实。

这项机制不等于全局幂等、事务或 exactly-once。模型请求超时可以重试，不代表远程写入超时可以重复。动作是否生效不确定时，应先读取实际状态或依照目标提供的幂等、补偿机制协调。

## 执行目标边界

| 目标 | 当前范围 | 长期方向 |
| --- | --- | --- |
| 本地文件与软件项目 | 已有工具、路径约束与观察状态 | 资源环境与策略统一 |
| 本地 Shell | 串行持久 Bash、超时与进程组清理 | 受限环境、容量和生命周期明确 |
| MCP 远端能力 | 已有协议与工具适配 | 目标身份、能力、访问与执行范围进一步关联 |
| Remote / Cloud / Device | 尚无完整统一目标层 | 单独设计注册、访问、约束、租约和故障协调 |

MCP 的远端调用不能被描述为已完成通用 Remote Runtime。Shell 进程管理也不等于文件系统或网络沙盒。

## 约束与验证

Executor 不能自主批准动作，也不能把 Agent Loop 藏在内部扩大预算与权限。执行结果声明成功不自动满足任务完成要求；Evidence & Recovery 域决定哪些证据仍有效。

目标层演进见[执行目标设计](../design/execution-targets.md)，Shell 具体语义见[Shell 适配](shell.md)，授权见[Policy](policy.md)。

代码依据：src/saiworks/tools/registry.py、schema_validation.py、result_packager.py、orchestration/engine.py、mcp/adapter.py。
