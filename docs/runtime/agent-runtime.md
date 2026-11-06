# 智能体运行时

状态：领域基线与当前实现。Agent Runtime 负责持续工作，不负责推理资源调度、终端展示或绕过执行门禁。

## 领域对象与所有权

| 对象 | 语义 | 当前实现 |
| --- | --- | --- |
| 智能体（Agent） | 在约束下推进任务的运行主体 | 以引擎及独立子会话运行体现，尚无完整通用 Agent 管理对象 |
| 任务（Task） | 目标、完成要求、进度和阻塞 | TaskCheckpoint 保存有界投影，复杂依赖图仍待设计 |
| 会话（Session） | 持续交互、工作空间关系与授权的容器 | 已有持久化会话和恢复状态 |
| 运行（Run） | 一次推进任务的运行过程 | 请求、预算、事件、终态与结果汇总 |
| 轮次与步骤（Turn / Step） | 模型交互轮次与执行步骤 | 引擎循环及逐轮轨迹；不是独立服务 |
| 动作与观察（Action / Observation） | 候选操作及其观察结果 | ToolAction / ToolResult；实际执行归 Execution Runtime |

Session、Task 和 Run 的生命周期不能互相替代。一次 Run 结束不意味着 Session 结束，也不必然意味着 Task 完成。模型消息是建议或观察，不是权威状态。

## 当前推进机制

运行开始时加载规则和明确上下文，恢复已保存的能力激活，打包有界工作集。模型返回文本与动作后，Runtime 解析提案，经执行路径获得结果，再更新任务检查点和下一轮上下文。

模型声明完成后仍需通过完成门禁。运行时结合必要证据、阻塞和执行结果计算终态，不能以最后一句回复覆盖失败。能力本轮激活后，从下一模型轮次开始可见。

任务检查点阶段、一次运行的终态及持久化提交状态是不同层级，不能按名称互换。实际名称、已有字段和迁移规则只维护在[运行状态参考](../reference/runtime-state.md)与[任务接受参考](../reference/runtime-acceptance.md)，本页只说明各自责任。

## 内部职责与状态所有权

| 所有者 | 当前职责 | 不承担 |
| --- | --- | --- |
| Runtime / TaskStore | 接受、提交、取消决定、审批记录、revision 与持久化通知排序 | 不把展示回调当权威状态 |
| Task 级 ExecutionContext | 绑定的取消、工具与动作账本 | 不跨 Task 共用具体批准和工具状态 |
| RunState / model_turn | 每次 Run 的预算、尝试、去重、进度及模型请求 | 不新建提交来绕过取消或任务预算 |
| ActionFlow / ToolRegistry | 准入、执行、可信阶段、观察归档及有序结果 | 不从远端 metadata 推导执行资格 |
| ResultRecorder / turn_decision / finalize | 一次性会话投影、轮次裁决和引擎终态 | 不覆盖 Runtime 的持久化与会话交付顺序 |

协调器保留装配和轮次推进；当前可见操作说明由同一内部选择器按轮装载。Run 临时计数与 Task 共享绑定分离；SessionContext 是模型工作集，不是第二套策略或任务数据库。源码见 [engine](../../src/saiworks/orchestration/engine.py)、[actions](../../src/saiworks/orchestration/actions.py)、[results](../../src/saiworks/orchestration/results.py) 和 [finalize](../../src/saiworks/orchestration/finalize.py)。

Task 取消接受与动作启动在 TaskStore 中按事务排序；已经启动的工具登记结果后再报告中断。清理只能作用于所属上下文，失败保留待重试事实，不覆盖已确定结果；单独的文件现状或进程退出不能解除未知副作用门禁。可信阶段、未知动作及数据升级分别以[工具执行](../reference/tool-execution.md)和[运行控制](../reference/runtime-control.md)为准，不重复定义。

## 预算与干预

当前运行限制模型轮数、请求尝试数和连续超时；总运行时间默认不设上限，可显式配置。模型传输重试增加尝试数，不等于一个现实动作已经安全重试。用户取消会触发引擎取消与工具状态清理；当前交互输入和干预仍有界面侧协调。

显式启用总运行时间上限时，同步等待用户审批的时间单独计量，不消耗该预算；模型请求、重试退避与工具执行仍计入其中。单次模型请求超时、传输重试和连续超时熔断始终生效。分析和修改任务都检测重复的既有观察，先反馈可用证据并给模型一次收敛机会；反馈后仍重复相同观察则以 stalled 收尾，保留恢复证据。该检测不授予写入权限，可能改变工作空间的 Shell 调用仍保守使读取缓存失效。

长任务、后台任务与预算应该沿用同一状态模型。未来的调度器不能通过创建新 Run 重置任务约束或绕过用户取消。

## 子代理与上下文

子代理是 Runtime 管理的独立会话，不是工具内部的隐藏 Agent。委派合同限制资源、副作用和交付要求；成员协调与证据交付见[子代理](subagents.md)。

上下文是权威状态的有界投影，不是状态数据库。完整材料与恢复所需事实分开保存，模型只能消费当前有效的工作集。见[上下文生命周期](context.md)。

## 目标与差距

目标是统一任务提交、运行控制、状态查询、干预与恢复，供不同界面使用。已提取无终端 Runtime 装配与会话协调，并实现同实例单执行槽、提交去重及任务级执行上下文。常驻 worker、可恢复异步审批、复杂任务图和跨进程自动续跑尚未实现。

验收重点是终态一致、预算不可绕过、取消不被迟到结果覆盖、恢复保留约束；不能用可演示的聊天过程替代故障验收。

总体事实见[实现对照](../implementation.md)。

实现依据：[engine.py](../../src/saiworks/orchestration/engine.py)、[model_turn.py](../../src/saiworks/orchestration/model_turn.py)。

回归入口：[test_execution_engine_sessions_and_cli_runtime.py](../../tests/test_execution_engine_sessions_and_cli_runtime.py)。
