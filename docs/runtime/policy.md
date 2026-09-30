# Policy & Permission

状态：领域基线与当前实现。执行权限回答某个主体在给定条件下是否可以对某个资源执行某个操作；可访问连接只是输入事实。

## 决策边界

目标策略消费主体、Agent、Task、Capability、Operation、Resource、Target、Risk 与 Context。允许、拒绝、请求人介入、受限执行和沙盒执行是目标决策语义，不是新增的现行返回枚举。

当前 PolicyDecision 仍使用 allowed、requires_confirmation、reason、risk_level、error_code；本轮不变更这些字段。身份、设备、凭据和短期票据的归属见[共享信任设计](../design/trust-domain.md)。

## 当前风险与模式

| 模式 | 直接允许 | 请求确认 | 阻断 |
| --- | --- | --- | --- |
| readonly | read | 无 | 其他风险 |
| confirm | read | write、execute、test、network、destructive、confirm | 未知风险 |
| auto | read、write | execute、test、network、destructive、confirm | 未知风险 |

Shell 的明显破坏性命令会提升风险。当前识别是有限规则，不是任意命令语义分析。非破坏性审批可在同一 Run 内按工具与风险复用；破坏性动作需要单独确认。拒绝与策略阻断保留现有错误语义。

## 多层约束

动作同时受能力可见性、参数预检、路径范围、风险 policy、内容检查和委派合同约束。一个层面的批准不覆盖其他层面的拒绝。激活能力不自动授予权限，用户批准写入也不解除内容安全阻断。

当前工作空间外访问先获得实际路径授权，后续操作仍检查路径与风险。子代理有效权限取用户授权、父会话委派、任务合同及当前策略的交集。任务文本或公共状态不能扩大合同。

## 内容保护与执行限制

当前写入检查覆盖 patch 新增行和 Shell 命令字面文本，识别高置信度凭据。命中返回 blocked_by_security_policy，诊断记录类别与位置，不回显秘密。占位值与环境变量引用按现有规则处理。

字面扫描不是完整防泄漏系统；进程组不是隔离；日志脱敏不是执行前阻断。Sandbox 是目标执行约束，不因本页描述而成为已有能力。

## 人的决定与无感运行

策略允许时自动运行，不应无故增加确认。需要人的决定时，展示具体动作和授权范围；人的决定由 Runtime 校验，不由壳自行解释或扩大。审批传递与代理是交互机制，不能让模型代替人扩大授权。

当前前台通过同步确认回调接入，后台缺少审批通道时阻塞。跨会话、跨设备可恢复审批仍待设计，见[授权交互](../design/approval.md)。

## 目标与差距

下一步明确操作与资源约束、决定有效性、撤销和恢复行为，接入可验证的共享身份事实。来源信任、artifact 访问与注入抵抗需要完整闭环验证，不能把本页原则全部标记为已实现保护。

代码依据：src/saiworks/safety/policy.py、guardrails.py、content/、orchestration/permissions.py、subagent_runner.py。
