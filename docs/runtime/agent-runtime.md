# Agent Runtime

状态：领域基线与当前实现。Agent Runtime 负责持续工作，不负责推理资源调度、终端展示或绕过执行门禁。

## 领域对象与所有权

| 对象 | 语义 | 当前实现 |
| --- | --- | --- |
| Agent | 在约束下推进任务的运行主体 | 以引擎及独立子会话运行体现，尚无完整通用 Agent 管理对象 |
| Task | 目标、完成要求、进度和阻塞 | TaskCheckpoint 保存有界投影，复杂依赖图仍待设计 |
| Session | 持续交互、工作空间关系与授权的容器 | 已有持久化会话和恢复状态 |
| Run | 一次推进任务的运行过程 | 请求、预算、事件、终态与结果汇总 |
| Turn / Step | 模型交互轮次与执行步骤 | 引擎循环及逐轮轨迹；不是独立服务 |
| Action / Observation | 候选操作及其观察结果 | ToolAction / ToolResult；实际执行归 Execution Runtime |

Session、Task 和 Run 的生命周期不能互相替代。一次 Run 结束不意味着 Session 结束，也不必然意味着 Task 完成。模型消息是建议或观察，不是权威状态。

## 当前推进机制

运行开始时加载规则和明确上下文，恢复已保存的能力激活，打包有界工作集。模型返回文本与动作后，Runtime 解析提案，经执行路径获得结果，再更新任务检查点和下一轮上下文。

模型声明完成后仍需通过完成门禁。运行时结合必要证据、阻塞和执行结果计算终态，不能以最后一句回复覆盖失败。能力本轮激活后，从下一模型轮次开始可见。

当前 TaskStatus 包含 pending、in_progress、blocked、verified、done；Run outcome 另行区分 completed、blocked、stalled、runtime_error、interrupted、exhausted。两者是不同层级，不能按名称互换。任务状态迁移由 Runtime 校验，允许的迁移见[运行状态参考](../reference/runtime-state.md)。

## 预算与干预

当前运行限制模型轮数、请求尝试数、连续超时和总运行时间。模型传输重试增加尝试数，不等于一个现实动作已经安全重试。用户取消会触发引擎取消与工具状态清理；当前交互输入和干预仍有壳侧协调。

长任务、后台任务与预算应该沿用同一状态模型。未来的调度器不能通过创建新 Run 重置任务约束或绕过用户取消。

## 子代理与上下文

子代理是 Runtime 管理的独立会话，不是工具内部的隐藏 Agent。委派合同限制资源、副作用和交付要求；成员协调与证据交付见[子代理](subagents.md)。

上下文是权威状态的有界投影，不是状态数据库。完整材料与恢复所需事实分开保存，模型只能消费当前有效的工作集。见[上下文生命周期](context.md)。

## 目标与差距

目标是统一任务提交、运行控制、状态查询、干预与恢复，供不同壳使用。已提取无终端 Runtime 装配与会话协调，并实现同实例单执行槽、提交去重及任务级执行上下文。常驻 worker、可恢复异步审批、复杂任务图和跨进程自动续跑尚未实现。

验收重点是终态一致、预算不可绕过、取消不被迟到结果覆盖、恢复保留约束；不能用可演示的聊天过程替代故障验收。

代码依据：src/saiworks/orchestration/engine.py、control.py、session.py、src/saiworks/types.py、sessions/store.py。总体事实见[实现对照](../implementation.md)。
