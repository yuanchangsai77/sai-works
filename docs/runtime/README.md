# 执行运行时领域

六个领域是职责与状态所有权边界，不要求六个服务、六个类或六组 API。当前实现与目标差距见[实现对照](../implementation.md)。

| 核心领域 | 权威文档 | 核心问题 |
| --- | --- | --- |
| 智能体运行时 | [持续运行](agent-runtime.md) | 任务如何持续推进、被干预和恢复 |
| 受控执行运行时 | [受控执行](execution-runtime.md) | 动作如何在约束下作用于现实资源 |
| 能力 | [能力](capability.md) | 能力如何描述、发现、激活与释放 |
| 工作区 | [资源环境](workspace.md) | Agent 在什么环境和范围内工作 |
| 策略与授权 | [执行权限](policy.md) | 谁可以执行什么操作及其约束 |
| 证据与恢复 | [证据与恢复](evidence-recovery.md) | 如何判断完成、检查过程和继续任务 |

子机制：[上下文生命周期](context.md)、[子代理与委派](subagents.md)、[Shell 执行适配](shell.md)。外部来源见[接入](../integrations/README.md)，界面见[交互边界](../interfaces/README.md)。

每份领域文档分开说明目标语义、当前机制和未闭合边界，不按工具或框架产品组织核心责任。已有字段见 reference；未实现的消息与部署设计见 design。
