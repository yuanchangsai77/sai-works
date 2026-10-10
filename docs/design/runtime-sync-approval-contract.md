# 阶段 D：快照与可恢复审批契约提案

状态：D1 同进程快照与观察已实施，现行入口及数据库升级见[任务观察参考](../reference/runtime-observation.md)；D2 审批、continuation 和按 Task 取消仍待确认与实施。首版仅同进程多客户端，不发布 HTTP 或 WebSocket API。行为依据见[边界设计](shell-core-boundary.md)。

## 兼容性影响

新增 Runtime 查询、订阅、审批和任务取消入口，以及任务库的 revision、审批和 continuation 记录。旧 Session JSON、TaskCheckpoint.phase、工具/模型契约及 ExecutionSummary.outcome 不改名或扩展枚举。旧 CLI/TUI 同步审批继续使用兼容适配；迁移后所有具体批准由 Runtime 绑定 Task + Action 并复核。新的运行管理状态与现有 checkpoint 阶段、Run outcome 分开。

数据库已在 D1 从接受记录版本 1 事务升级到版本 2；新程序保持旧数据可读，但旧程序不能读取升级后的版本 2 记录。升级前停止旧写入进程，禁止旧新程序同时写库，不承诺旧程序只读回退。D2 的审批及 continuation 升级另行确认，具体兼容限制归现行参考。

## 查询与同步

D1 已实现按 Task 聚合提交、事务修订和独立观察游标；调用使用本地观察资格句柄，现行签名及字段只维护在[任务观察参考](../reference/runtime-observation.md)。观察采用非阻塞轮询最新快照，只保留修订游标，不积累通知队列；缺口显式要求重新同步。结果与错误仍归对应提交，审批和 artifact 不新增独立快照字段。

D2 新增审批、artifact 或结果关联的具体快照扩展须先确认；相关权威状态变化与 revision 必须在同一事务提交。文本预览独立于状态修订，不作为已采纳结果。

## 审批记录与决定

- 审批记录：`approval_id`、`task_id`、`action_name`、`arguments`、`action_fingerprint`、`workspace_root`、`required_scope`、`created_at`、`expires_at`、`state`、`decision_actor`、`decided_at`。
- Approval state：`pending`、`approved`、`denied`、`expired`、`cancelled`、`consumed`。Task 保持 `waiting_approval` 时可以包含 expired Approval；重新申请产生新 approval identity。
- `decide(approval_id, decision, expected_action_fingerprint)`：decision 仅为 approve/deny。相同决定重复提交返回原裁决；不同决定冲突返回当前裁决，不改写已提交决定。参数、工作区或范围改变使原决定失效。
- TTL 使用配置 `approval_ttl_seconds`，必须为正数。配置默认值属于实现配置参考，不作为架构承诺。计时使用持久化 UTC 有效期，执行前再次核对时钟与实时权限。
- `cancel(task_id)`：在权威事务中记录取消接受点。此点之后任何批准不得开始新动作。已经开始的动作进入中断和结果核对，取消接受不表示已停止。

## continuation 与预算

等待前保存：原任务及工作区绑定、当前 checkpoint、已完成动作与证据、待决定动作及尚未执行的后续动作、必要模型/工具观察、执行预算消耗、模型尝试消耗和取消资格。

进入等待后关闭普通工具进程和模型连接，释放执行槽；同 Session 后续任务继续排队，其他 Session 可运行。续跑重新获取执行槽，恢复有界上下文并复核动作、观察版本和批准有效性。已完成动作不得因续跑而重放；已消耗预算不重置。不能证明外部结果的记录保留未知事实，要求核对后再恢复。

## 观察与控制资格

观察、取消和审批分别校验资格。D1 由可信 Runtime 装配方授予仅观察的不透明本地句柄，支持撤销；D2 再确认取消和审批资格入口，不能默认扩展已有观察句柄的权限。调用者不能仅通过传入角色字符串获得资格。单用户本地服务不等于所有连接都能控制任务。跨进程句柄传递与 Gateway 身份协议在阶段 E 单独确认。

## 验收门槛

一致快照与更新衔接、重复/缺口重新同步、慢消费者不阻塞执行、两个观察者终态一致；无审批资格不得决定；等待释放执行槽；审批过期保留可恢复任务；不同决定冲突不重复执行；取消接受后迟到批准无效；续跑保留预算与已完成副作用；数据库升级保留旧接受记录。

## D2 实施前的具体协商项

本节为下一轮可实施方案，尚未实施，不把 D1 的确认视为 D2 字段和行为变化的确认。

### 批准范围与控制资格

建议每次人为批准仅覆盖一个确定动作，指纹绑定 Task、submission、动作名称、规范化参数、执行工作区及政策要求的范围。工具或参数、工作区、范围变化均重新申请，不将批准复用到同名工具的其他参数。现有非 destructive 动作按工具名与风险组复用批准将收敛为具体动作批准，因此 CLI/TUI/Desktop 可能增加审批次数，这是需要确认的用户可见变化。Policy 直接允许的动作仍不弹窗。

保留 D1 的仅观察句柄及入口。由可信装配方单独授予可撤销的 Task 控制句柄，分别指定 cancel、approve 资格；建议入口为 `grant_task_control(task_id, permissions)`、`revoke_task_control(control)`、`cancel(control)`、`decide(control, approval_id, decision, expected_action_fingerprint)`。决定前复核句柄、审批所属任务及指纹；观察句柄不得控制。新的控制句柄只供同进程使用，不发布网络身份协议。

### 有效期与决定

建议新增正整数配置 approval_ttl_seconds，默认 300 秒，持久化 UTC 有效期；自动过期不批准也不默认终止 Task。首次已提交决定不可覆盖；重复相同决定返回现有裁决，冲突返回已有裁决。过期后仅允许通过 Runtime 重新申请新审批身份，旧身份不可重新激活。拒绝沿用现有拒绝动作的执行政策；恢复拒绝后的执行也需重新排队，不由 UI 决定任务终态。

### 挂起、调度与续跑

执行循环在待审批动作执行前交回控制，返回内部挂起对象而非对外 ExecutionSummary。Runtime 事务保存 pending Approval、continuation、waiting_approval 状态及 revision，提交成功后才释放槽并通知观察者或调用旧审批适配。旧同步执行入口仍可等待最终结果，但等待不占执行槽，也不在等待时持有模型连接或普通工具进程。

continuation 保存同一 Task/submission/Run 的会话上下文、待执行动作及剩余动作、已完成动作和证据、工作区及权限观察、当前轮次、去重状态、进度恢复状态，以及已消耗执行时间、模型尝试数和连续超时次数。只持久化有版本的结构化数据，不序列化线程、窗口、连接或可执行对象。无法安全恢复的工具状态保留核对要求，不默认为可重建。续跑不创建新接受记录来重置预算，不重新调用模型获取已经确定的待执行动作。

批准后重新排队取得执行槽，恢复并复核预算、实时政策、工作区及动作指纹。同 Session 后续提交不能越过等待中的前序提交，其他 Session 可以使用空闲槽；同一任务不产生两个活跃续跑者。取消接受、审批消费及动作取得启动资格必须通过同一权威事务顺序裁决，不能只设置内存 Event；启动资格之前接受取消则动作不启动，已启动的动作进入现有中断及结果核对流程。

数据库在 D1 版本 2 基础上新增审批、continuation 和取消接受记录，事务升级及未知版本写入门禁；新增等待状态不修改 TaskCheckpoint.phase 或 ExecutionSummary.outcome。升级前停止旧写入程序，不承诺旧程序读写回退。

### 分批交付

先实现审批记录、决定有效性及事务竞态，再改造执行循环的挂起和预算续接，最后接入旧同步审批适配；每批只有行为测试通过才标记完成。必须验收存储失败时不执行、等待释放槽、预算连续、审批过期与重新申请、相同/冲突决定、取消竞态、同 Session 不越序，以及动作不重放。D3 再将各壳的审批 UI 改为上述控制接口，E 单独解决进程退出后的服务生命周期。
