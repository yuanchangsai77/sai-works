# Runtime 同进程任务控制与审批(D2 批次①)

范围:同进程任务控制资格、按 Task 取消接受点和可核对审批记录。引擎挂起/续跑、预算连续性、审批消费与旧同步审批适配属于 D2 后续批次;跨进程接入与常驻服务属于 E,不在本接口中承诺。

## 接口与资格

Runtime 的可信装配方调用 `grant_task_control(task_id, permissions, actor="local")` 获取仅含控制资格的不透明本地句柄。permissions 只能是 `cancel`、`approve` 的非空子集,与 actor 标签在授予时固定并由 Runtime 持有,句柄本身不携带权限。`revoke_task_control(control)` 撤销句柄;`Runtime.close()` 撤销该实例全部控制句柄,关闭后不再授予新句柄。外部输入不能调用授予入口;句柄不跨 Runtime 实例或进程传递。接口拒绝伪造、已撤销、其他实例及空壳句柄,不通过角色字符串授予资格。观察句柄(TaskAccess)不能用于控制,控制句柄也不能用于观察。

## 审批记录与决定

`Runtime.request_approval(submission_id, action_name, arguments, required_scope=...)` 由可信所有者级调用者发起,从提交记录解析所属 Task 与工作区根,生成动作指纹:Task、submission、动作名、规范化参数(排序键 JSON)、解析后的工作区根与所需 scope 的 sha256。存储层按审批身份去重:以同一 approval_id 重试同指纹请求幂等返回原记录,同身份异指纹报错;Runtime 每次新请求都签发新身份,不为同指纹动作去重。

审批状态:pending、approved、denied、expired、cancelled、consumed。`decide(control, approval_id, decision, expected_action_fingerprint=...)` 需要 `approve` 权限;decision 只能是 approve/deny;期望指纹与记录不符时报错且不写入。首次提交的决定生效,重复或冲突决定返回既有裁决而不改写。记录 TTL 到期后才到达的决定把记录落成 expired;过期判定是惰性的,没有后台扫描。跨 Task 的决定被拒绝。

`cancel(control, now=None)` 需要 `cancel` 权限,在每 Task 取消接受点写入接受时间,幂等返回同一时间。取消接受后:新审批请求报错;待决审批被批量落成 cancelled;迟到的决定得到 cancelled 而非裁决;已提交的裁决不受后续取消影响。decide 与 cancel 竞争在同一写事务内排序,恰好一种顺序成立,调用方能从返回值确认自己落在哪一侧。

`pending_approvals(control, now=None)` 需要 `approve` 权限,返回该 Task 当前待决审批,返回前对到期记录执行惰性过期转移。`consumed` 目前仅是保留枚举值,批准记录的消费语义在 D2 批次②实施。

请求、决定、取消接受与惰性过期都是权威变化,各自在写事务中递增所属 Task 的 revision,观察者经[任务观察](runtime-observation.md)的 watch 游标可见;幂等重复与无变化操作不递增。审批快照字段是否进入 TaskSnapshot 尚未确认,当前快照结构不变。

## 数据兼容

任务库增量升级到 user_version=3,新增 approvals 表与 task_cancellations 表;旧 v1/v2 库在同一升级事务内迁移,升级事务失败保持原库可用,新程序拒绝未知数据库版本。升级前必须停止旧写入进程,不能并行使用旧程序写任务库;不承诺旧程序只读回退。Session JSON、ExecutionSummary、工具及模型格式不变;引擎同步审批路径在本批次保持不变。
