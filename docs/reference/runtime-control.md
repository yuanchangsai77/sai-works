# 任务控制与审批参考

范围：本地任务控制资格、取消接受点、审批与用户回复，以及持久化等待和显式续跑。已保存等待可由新进程重新装配 Runtime 后推进；控制句柄本身不跨进程，未提供远程协议或常驻 worker。

## 接口与资格

Runtime 的可信装配方调用 `grant_task_control(task_id, permissions, actor="local")` 获取仅含控制资格的不透明本地句柄。permissions 只能是 `cancel`、`approve`、`reply` 的非空子集,与 actor 标签在授予时固定并由 Runtime 持有,句柄本身不携带权限。`revoke_task_control(control)` 撤销句柄;`Runtime.close()` 撤销该实例全部控制句柄,关闭后不再授予新句柄。外部输入不能调用授予入口;句柄不跨 Runtime 实例或进程传递。接口拒绝伪造、已撤销、其他实例及空界面句柄,不通过角色字符串授予资格。观察句柄(TaskAccess)不能用于控制,控制句柄也不能用于观察。

## 审批记录与决定

`Runtime.request_approval(submission_id, action_name, arguments, required_scope=...)` 由可信所有者级调用者发起,从提交记录解析所属 Task 与工作区根,生成动作指纹:Task、submission、动作名、规范化参数(排序键 JSON)、解析后的工作区根与所需 scope 的 sha256。存储层按审批身份去重:以同一 approval_id 重试同指纹请求幂等返回原记录,同身份异指纹报错;Runtime 每次新请求都签发新身份,不为同指纹动作去重。

审批状态:pending、approved、denied、expired、cancelled、consumed。`decide(control, approval_id, decision, expected_action_fingerprint=...)` 需要 `approve` 权限;decision 只能是 approve/deny;期望指纹与记录不符时报错且不写入。首次提交的决定生效,重复或冲突决定返回既有裁决而不改写。记录 TTL 到期后才到达的决定把记录落成 expired;过期判定是惰性的,没有后台扫描。跨 Task 的决定被拒绝。

`cancel(control, now=None)` 需要 `cancel` 权限,在每 Task 取消接受点写入接受时间,幂等返回同一时间。取消接受后:新审批请求报错;待决审批被批量落成 cancelled;迟到的决定得到 cancelled 而非裁决;已提交的裁决不受后续取消影响。decide 与 cancel 竞争在同一写事务内排序,恰好一种顺序成立,调用方能从返回值确认自己落在哪一侧。

已有活动 Run 时，接受取消还会通知绑定的执行上下文。新工具动作的启动门禁与取消接受在 TaskStore 中按写事务排序；取消先提交则不启动新动作，已经通过门禁的工具结果仍记录后再中断。此处不承诺立即终止正在进行的外部副作用。等待任务的取消接受会阻止后续动作；再次推进时以 interrupted 正常收尾并保留此前结果。

有副作用动作若因进程中断停在 `started`，或失败后没有可信内部的未启动证明而进入 `uncertain`，记录保留用于恢复诊断，不阻塞模型选择的新动作或完成。续跑上下文区分未记录完成回执、错误回执及开始后经过时长，模型据此决定后续方案；详见[执行状态与恢复上下文](../runtime/evidence-recovery.md#执行状态与恢复上下文)。可信内部核对只能在该 Task 没有运行中提交时登记“已发生”或“未发生”，且须提供主体和非空证据，裁决与证据在同一事务追加保存。历史已发生记录也不禁止模型明确选择再次执行；运行时不会自动重放。启动记录保存指纹与脱敏参数摘要，不保存原始参数。取消后的清理失败不改变已接受结果，后续可重试清理。此调整不新增跨端接口字段或错误码。

`pending_approvals(control, now=None)` 需要 `approve` 权限,返回该 Task 当前待决审批,返回前对到期记录执行惰性过期转移。批准在动作启动事务中校验并消费，校验包含任务、提交、参数、当前工作区、风险范围和有效期，与取消接受共用事务排序。只有可信执行阶段证明动作未启动时，才能在记录该证明的事务内退回未使用的批准；已派发或未知结果不能退回批准。工作区访问批准在授予路径范围前单独校验与消费。

请求、决定、取消接受与惰性过期都是权威变化,各自在写事务中递增所属 Task 的 revision,观察者经[任务观察](runtime-observation.md)的 watch 游标可见;幂等重复与无变化操作不递增。等待、回复和批准消费同样递增 revision；等待投影见[任务快照](runtime-observation.md)。

文件恢复投影每次运行至多携带 8 条文件现状观察，路径显示限制 240 字符，并明确观察可能失效；这些有界材料不替代执行前校验。

确认原本机运行所有者已退出后，可回收旧提交执行权并接受同任务继续；已登记副作用动作不阻止执行权回收，其未确认状态和证据保持不变。存活或无法核实的所有者仍不回收，详见[崩溃恢复](../runtime/evidence-recovery.md#崩溃后的执行权与动作结果)。此调整复用现有存储，不增加数据库版本、字段或错误码。

## 等待与用户回复

`Runtime.advance(submission_id)` 推进已有提交，返回下一等待点的 TaskRecord 或最终 ExecutionSummary。等待时提交 state 为 waiting_approval 或 waiting_user，result 仍为空，不发布完成通知。它不替代 accept：调用方须先持久化接受提交。相同提交重复推进不会重放已完成动作，运行中或结果未提交的遗留尝试仍拒绝自动重放。

`Runtime.reply(control, interaction_id, answer)` 需要 reply 资格，只接受本任务用户决策的非空字符串答案。相同答案重复提交幂等，不同答案冲突报错；取消后的答案、其他任务、审批等待及观察资格均不能作为有效回复。返回该交互的只读投影；有效回复及审批决定使对应等待提交重新排队，执行仍需取得槽。选择项是建议，允许自由文本。回复是模型原工具调用的结果，不伪造新用户消息。

内置 request_user_input 工具接受必填 question 字符串和可选 options 字符串数组。无持久化交互通道时返回 interaction_unavailable；空白问题返回已有 invalid_argument_value。委派子代理不直接暴露该工具，沿既有父代理交接路径请求人介入；不使父调用隐藏地等待不可见的人类问题。

兼容的 execute 仍等待最终结果，等待期间让出 Runtime 执行槽。configure_interaction 新增 user_input_callback(question, options)，返回答案或 None；None 表示取消该任务。审批回调和用户提问回调均在等待记录提交、工具清理及执行槽释放之后调用。没有回调时可通过观察、控制和 advance 推进，不要求终端存在。cancel_submission 是可信本地装配方的按提交取消适配，实际仍使用任务控制资格；它不是对外匿名取消接口。

等待与续跑的机制、预算及工具状态边界只维护在[智能体运行时](../runtime/agent-runtime.md#等待与续跑)。

advance 在领取执行权前核验已接受请求与当前子代理委派资格，包含已有结果的提交，不能借此读取或执行父任务。已保存等待在恢复准备阶段发生普通异常时保留续跑材料及既有决定，提交回到可继续的非终态并记录异常类型。若用户中断或预算耗尽，则从原检查点保留已完成结果并按相应终态收尾；不使用准备阶段的临时空会话覆盖已有结果。路径授权恢复边界见[等待与续跑](../runtime/agent-runtime.md#等待与续跑)。

## 数据兼容

任务库增量升级到 user_version=9。v3 审批与取消表、v4 动作尝试、v5 参数摘要、v6 文件恢复描述、v7 证据及 v8 所有者与回收记录均保留；v9 新增内部 task_interactions 和 run_continuations 表，保存有版本的交互及续跑材料。旧 v1 至 v8 库在同一事务中迁移，失败保持原库可用；新程序拒绝未知版本。任务记录仍为 schema_version=2，但 state 扩展为两个等待值；TaskSnapshot 增加 pending_interactions。旧写入程序不能操作升级后的任务库，升级前必须停止旧写入进程，不承诺旧程序只读回退。

Session JSON、TaskCheckpoint 阶段及 ExecutionSummary 终态枚举保持不变。工具目录新增 request_user_input，回复控制增加 reply 资格；不发布新的网络 API。

实现依据：[runtime.py](../../src/saiworks/runtime.py)、[runtime_interactions.py](../../src/saiworks/runtime_interactions.py)、[tasks.py](../../src/saiworks/sessions/tasks.py)、[interactions.py](../../src/saiworks/sessions/interactions.py)、[提问工具](../../src/saiworks/tools/builtins/request_user_input.py)。

回归入口：[等待与续跑](../../tests/test_runtime_waiting.py)、[控制资格](../../tests/test_runtime_approval_control.py)、[动作账本](../../tests/test_action_journal.py)。链接不代表真实模型或 GUI 已验收。
