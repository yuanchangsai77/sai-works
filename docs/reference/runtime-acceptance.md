# Runtime 持久化接受记录

状态：用户已确认，B/C 已实施并完成相关验收；证据见[阶段核对](../design/shell-core-audit.md)。本参考记录阶段 B/C 的持久化接受，不发布网络 API。

## 兼容性影响

现有 UserRequest、ExecutionSummary、TaskCheckpoint、Session JSON、模型与工具契约保持原格式。新增独立任务记录，不将 Accepted / Running 等运行管理状态塞入现有 TaskStatus。旧会话仍由现有 store 读取。旧入口自动生成内部 submission identity；重试必须复用原 identity，不从输入文本推断重复提交。

## 首版记录

每条任务记录使用 schema_version=1，包含 task_id、submission_id、session_id（可空）、request_fingerprint、bound_context、state、run_ids、result 和 error。result 保存完整终态汇总及其现有 artifact 引用；error 保存异常类型，诊断细节仍归运行日志。bound_context 保存规范化工作区身份、完整输入和已绑定上下文引用；文件路径继续表示执行时读取，提交时内容保证必须使用快照或版本引用。

使用独立 SQLite 文件保存任务记录，以事务和 submission_id 唯一约束完成身份、绑定上下文与去重记录的原子接受。submission_id 在单用户 Runtime 存储范围内唯一；记录首版不自动清理。相同 id、相同指纹返回原任务；相同 id、不同指纹以内部 ValueError 拒绝，不新增跨方错误码。Accepted 后才能排队执行。

当前使用 queued、running、cancelled、finished；waiting_approval 在阶段 D 实施。它们为运行管理层内部状态，不替代 TaskCheckpoint.phase 或 Run outcome。具体审批状态在阶段 D 契约中评审。当前实现仅提供接受、排队、运行与结果关联记录，不授权自动恢复、外部动作重放或网络协议。

## 故障与回退

事务未提交视为未接受；事务提交后确认丢失仍返回原任务。进程重启保留任务身份，对 running 记录先核对结果，不自动重放。存储失败禁止执行；回退入口不能重新执行已有记录。删除独立任务库会丢失提交去重能力，不能视为普通缓存清理。

## 验收

并发重复提交只产生一条任务；提交内容冲突被拒绝；持久化失败无执行副作用；确认丢失和重启仍能查询原任务；旧会话可读取；重复提交不重置预算或重新执行已有终态任务。
