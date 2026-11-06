# 专项设计与探索

本目录只维护未闭合设计、目标约束和必要决策，不维护工作队列或实施日记。当前事实见[实现对照](../implementation.md)，全部工作状态与优先级见[路线图](../roadmap.md)，已完成审查证据见[历史索引](../archive/README.md)。

| 文档 | 唯一职责 |
| --- | --- |
| [交互界面与运行时边界](interface-runtime-boundary.md) | 状态权威、界面职责、跨界面及生命周期目标；不把远期目标发布为现行 API |
| [审批等待与续跑](approval-resume.md) | 已确认的挂起、续跑、预算及审批消费设计；已实施部分只引用现行参考 |
| [桌面应用范围设计](desktop-scope.md) | 同进程参考界面边界、关闭语义及跨界面恢复、独立运行的解锁条件 |
| [桌面应用产品与 UI/UX](desktop-product-ux.md) | 页面、交互、视觉、键盘及产品验收目标；不是实现清单 |
| [授权交互](approval.md) | 人的决定如何传递与核验，不重复审批接口字段 |
| [执行账本](execution-ledger.md) | 完整事实链、查询、恢复及补偿目标，区分当前最小动作记录 |
| [执行目标](execution-targets.md) | 本地到远程的目标、容量、租约及失败语义 |
| [共享信任域](trust-domain.md) | 身份、访问、凭据、票据、撤销及跨项目责任 |
| [协议边界](protocol-boundaries.md) | 何时需要跨进程协议及兼容性协商 |
| [探索范围](exploration.md) | 经验、非 Coding 和连续认知假设，不能提升为近期承诺 |

持久化接受、引擎职责和观察工作集已有实现，其现行定义分别归[接受参考](../reference/runtime-acceptance.md)、[智能体运行时](../runtime/agent-runtime.md)和[上下文机制](../runtime/context.md)。不再保留同内容的提案占位页、实施计划或临时跟踪文件。跨项目字段、错误码和语义变化仍需先说明影响并确认。
