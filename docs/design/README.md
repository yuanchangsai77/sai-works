# 目标机制与探索

系统定位和领域分工以[架构基线](../architecture.md)为准。本目录深入尚未闭合的机制，不维护第二套总体架构，不宣称目标字段与能力已经实现。

| 文档 | 目标责任 | 当前关联 |
| --- | --- | --- |
| [共享信任域](trust-domain.md) | 身份、设备、凭据、票据、访问和连接 | 本地策略只是实现起点 |
| [授权交互](approval.md) | 多壳、后台与跨会话审批关联及有效性 | 同步确认回调、后台阻塞 |
| [执行目标](execution-targets.md) | 目标约束、远程节点、容量、租约与故障协调 | 本地 Executor 与 MCP 适配 |
| [执行账本](execution-ledger.md) | 授权、执行、证据和检查点的可查询关系 | 日志、trace 与检查点 |
| [壳与内核边界设计](shell-core-boundary.md) | 权威状态、生命周期、上下文绑定与多壳同步 | 首版行为决策已确认，B/C 运行职责已提取 |
| [壳与内核拆分实施计划](shell-core-migration.md) | 能力映射、职责迁移、阶段验收和兼容策略 | A/B/C 完成，D1 观察已实施；D2/D3/E/F 待实施 |
| [Desktop V0.1](desktop-v0.md) | 同进程薄壳、D/E 解锁条件与纵向切片 | V0.1a 开始实施；GUI 技术选型暂缓 |
| [Desktop 产品与 UI/UX](desktop-product-ux.md) | 工作台功能、信息架构、页面、交互、视觉和分阶段验收 | 目标设计；指导后续生产 Desktop 实施 |
| [持久化接受契约](../reference/runtime-acceptance.md) | 内部提交身份、接受事务与独立任务记录 | 已确认并实施，旧会话格式不变 |
| [同步与审批契约提案](runtime-sync-approval-contract.md) | revision、审批有效性、continuation 与控制资格 | D1 观察已实施；D2 审批及取消待确认 |
| [协议边界](protocol-boundaries.md) | 哪些边界需要协议及其兼容要求 | 已有模型和工具契约 |
| [探索范围](exploration.md) | 经验偏好、非 Coding 场景与连续认知的研究位置 | 不作为当前实现或契约 |

旧 Three Plane / Seven Layer、重复专题与施工记录已删除；精选故障与研究见[历史材料](../archive/README.md)。目标实现不得绕过当前契约；跨项目字段、错误码或语义变化需单独确认。
