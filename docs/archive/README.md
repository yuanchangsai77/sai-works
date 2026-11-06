# 历史证据与研究

只保留不能由当前机制替代的故障根因、可定位的验证记录和明确研究假设；不维护当前待办或优先级。已完成设计的有效约束归入现行机制，不为每次施工新增一份归档。当前事实与工作见[文档入口](../README.md)。

| 材料 | 保留依据 |
| --- | --- |
| [恢复状态冲突](audits/2026-08-12-session-runtime-state-recovery.md) | 实际会话与运行标识；部分成功、隐藏 Shell 状态和终态冲突 |
| [子代理信任与终态分裂](audits/2026-08-12-subagent-context-delegation.md) | 实际安全故障与统一收尾的修复背景 |
| [上下文装配与会话探索](audits/2026-10-15-context-assembly-session-exploration.md) | 当时的观察、上下文和恢复核查；不作为现行队列 |
| [壳核拆分验收](audits/2026-11-04-shell-core-audit.md) | B/C、D1、D2 批次①及取消桥接的分阶段证据，含 R1–R3 根因 |
| [执行可靠性审查](audits/2026-11-03-execution-hardening.md) | 动作阶段、取消归属、本地进程与 MCP 生命周期的独立快照验证 |
| [Harness 优化验收](audits/2026-11-06-harness-optimization.md) | 目录、按需说明、隔离读取及提交前边界回归；限定性能结论 |
| [连续认知研究](research/continuous-cognition.md) | 有独立价值的假设与验证问题，不作为已实现能力 |

历史测试只证明其注明环境和时点的结果，不能代替当前验收。发布能力边界单独固化在 [v0.1](../versions/v0.1.md) 和 [v0.2](../versions/v0.2.md)，不回写历史快照。
