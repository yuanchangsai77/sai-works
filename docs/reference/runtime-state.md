# 当前运行状态参考

状态：源码类型与现有行为参考，核对于 2026-09-30。这里记录已存在的内部字段，不定义新的对外 API 或持久化版本。

## 请求、结果与终态

| 类型 | 现有内容 | 用途 |
| --- | --- | --- |
| UserRequest | prompt、cwd、metadata | 当前运行输入，不是完整 Workspace 或 Principal |
| ExecutionSummary | final_message、tool_results、active_instructions、active_capability_ids、outcome、blockers、checkpoint、workspace_state | 统一收尾事实，供日志、会话和交互消费 |
| RuntimeBlocker | error_code、summary、source、tool、retryability、required_action | 保留为何不能继续或完成及恢复要求 |
| WorkspaceSessionState | origin_root、active_root、approved_roots | 会话路径授权与活动环境投影 |

TaskStatus 与 Run outcome 是不同层级。任务状态为 pending、in_progress、blocked、verified、done；引擎运行终态包括 completed、blocked、stalled、runtime_error、interrupted、exhausted。不能把所有非异常返回解释为完成。

## 当前任务状态迁移

| 当前阶段 | 允许迁移 |
| --- | --- |
| pending | in_progress、blocked |
| in_progress | blocked、verified |
| blocked | in_progress |
| verified | done、in_progress、blocked |
| done | in_progress |

相同阶段可保持不变。TaskCheckpoint 初始化兼容旧阶段别名，未知初始状态按现有实现归入 pending；显式迁移校验拒绝非法阶段和跃迁。目标设计不能直接改变这项兼容行为。

## 检查点与证据

TaskCheckpoint 当前包含 objective、schema_version、task_id、workspace_root、workspace_revision、phase、completed_actions、artifacts、evidence、required_evidence、unmet_deliverables、blockers、runtime_state。它是有界恢复投影，不是通用 Task Graph。

EvidenceRecord 保存 kind、producer、task_id、workspace_revision、artifact_refs、source_task_ids。当前有效性筛选要求 task_id 一致；workspace_change 与 artifact 是累积证据，其他种类需要与当前工作区 revision 一致。该筛选不等于制品内容完整性、来源或访问权限已全部验证。

SessionResumeState 关联最近运行、提示、结果、未解决问题、blocker 与 checkpoint。StoredSession 保存消息、运行标识、激活能力、轨迹、恢复与工作空间状态，以及父子会话关系。会话持久化行为不能从类型默认值单独推断，应同时检查 SessionStore。

## 压缩相关会话字段

| 现有字段 | 当前用途 |
| --- | --- |
| compaction_archive_ids | 记录压缩前原消息归档引用 |
| context_generation | 成功压缩后递增的上下文代次 |
| context_trace_after_run_id | 压缩后历史轨迹的上下文边界，设置为当时最近运行标识 |
| resume_state | 压缩成功后替换为新的空 SessionResumeState |

这些是现有内部字段，不是新增网络契约。压缩仍保留会话身份、工作空间授权和能力状态；详细影响与目标差距见[上下文生命周期](../runtime/context.md)。

## 维护与来源

源码依据：src/saiworks/types.py、orchestration/control.py、sessions/store.py、interaction/commands/session_cmds.py。领域语义见[Agent Runtime](../runtime/agent-runtime.md)、[Evidence & Recovery](../runtime/evidence-recovery.md)、[Workspace](../runtime/workspace.md)。

新增对外字段、错误码或语义另行进行契约协商；此页不把内部类型直接发布为网络 schema。
