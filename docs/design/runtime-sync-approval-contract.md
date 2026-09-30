# 阶段 D：快照与可恢复审批契约提案

状态：待确认，尚未实施。首版仅同进程多客户端，不发布 HTTP 或 WebSocket API。行为依据见[边界设计](shell-core-boundary.md)。

## 兼容性影响

新增 Runtime 查询、订阅、审批和任务取消入口，以及任务库的 revision、审批和 continuation 记录。旧 Session JSON、TaskCheckpoint.phase、工具/模型契约及 ExecutionSummary.outcome 不改名或扩展枚举。旧 CLI/TUI 同步审批继续使用兼容适配；迁移后所有具体批准由 Runtime 绑定 Task + Action 并复核。新的运行管理状态与现有 checkpoint 阶段、Run outcome 分开。

数据库需要从接受记录版本 1 增量迁移；先以事务完成新增表/列，旧记录保持可读。运行中的旧程序不能与新程序同时写同一任务库；启动时核对可写版本。回退到旧程序只允许读取原格式记录，不自动执行新状态任务。

## 查询与同步

- `snapshot(task_id)`：返回 `task_id`、`revision`、`state`、`submission_ids`、`run_ids`、`approvals`、`artifacts`、`result_ref`、`error`。
- revision 按 Task 单调增加，任务状态、审批、artifact 与结果关联变化在同一事务中增加 revision。事务提交后的快照是唯一权威视图。
- `watch(task_id, after_revision)`：返回最新快照或变化提示。提示包含 `task_id`、`from_revision`、`to_revision`、`resync_required`。不保证每个中间 revision 都交付，不保存完整通知日志；缺口、重复、乱序与慢客户端都可通过完整快照恢复。
- 每个观察者使用有界缓冲；生产者不等待消费者。合并通知后必须显式要求重新同步。文本预览独立于状态修订，不作为已采纳结果。

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

观察、取消和审批分别校验资格。首版同进程客户端由 Runtime 授予不透明的本地访问句柄，句柄绑定具体 Task 和权限集合；调用者不能仅通过传入角色字符串获得资格。单用户本地服务不等于所有连接都能控制任务。跨进程句柄传递与 Gateway 身份协议在阶段 E 单独确认。

## 验收门槛

一致快照与更新衔接、重复/缺口重新同步、慢消费者不阻塞执行、两个观察者终态一致；无审批资格不得决定；等待释放执行槽；审批过期保留可恢复任务；不同决定冲突不重复执行；取消接受后迟到批准无效；续跑保留预算与已完成副作用；数据库升级保留旧接受记录。
