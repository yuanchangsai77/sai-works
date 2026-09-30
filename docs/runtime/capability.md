# Capability

状态：领域基线与当前实现。Capability 是 Execution Core 的核心领域；MCP、Skill 和本地工具是能力来源与适配形式。

## 核心区别

能力存在、能力可见、能力激活、动作获准、目标可用、动作执行分别代表不同事实。打开目录或激活 schema 不产生执行授权。

目标关系为 Capability → Operation → Permission Requirement → Execution Target → Executor。当前代码已实现能力目录与激活，操作、目标和权限要求仍部分体现在工具定义及 policy 中，尚未形成完整独立模型。

## 当前仓库机制

| 阶段 | 当前行为 | 不代表 |
| --- | --- | --- |
| Catalog | 展示有界外层能力与工具箱用途 | 全量远端 discovery 或所有 schema 常驻 |
| Open | 按需获取 manifest，MCP 可触发懒发现 | 自动激活或授权 |
| Activate | 注册选定工具，装载选定指令 | 可以绕过策略执行 |
| Use | 经过常规执行路径，记录使用结果 | 来源天然可信 |
| Release | 按能力或范围释放激活内容 | 撤销已经发生的现实副作用 |

核心能力保持可用，外部目录按需展开。Skill 正文通过 instructions 叶子激活；本地子代理生命周期能力也属于按需工具箱。用户命令可以选择一组 manifest 叶子；模型侧仍先打开再选择激活。

## 生命周期与预算

当前支持 turn/run/session 范围、激活数量和内容预算、名称冲突预检、显式释放及会话隔离。只恢复持久化能力标识，不将整个远端工具目录复制进会话。目录来源、健康度与连接状态是不同观察维度。

当前恢复逐项打开并激活持久化能力标识；遇到 KeyError 或 ValueError 时跳过该项并继续，不保证向调用方返回明确 blocker。因此保存的激活列表不保证全部恢复成功；其他异常并不由这项跳过规则处理。恢复激活后，实际操作仍经过资源与授权检查。

目标是让恢复失败可观察，并说明缺失能力对任务的影响；不能为了恢复而替换成权限更大的实现。这是待完善行为，不能视为当前已有保证。

## 来源与 Agent 边界

MCP 提供协议描述与调用；Skill 提供工作指令及关联能力；本地工具提供确定执行适配。来源内容不能自行成为授权或系统策略。

例如搜索、抓取和浏览器是可组合的不同操作；这里的名称是领域示例，不是宣布已有 web API。需要 Agent 的复杂过程应由 Runtime 管理，不能藏入工具而丢失检查点、预算和审计。

## 目标与差距

下一步完善资源包与操作生命周期、能力和目标的绑定、资源上下文预算及来源校验。无需重建第二个 Registry，优先复用现有 warehouse、source 和工具执行路径。

代码依据：src/saiworks/capabilities/model.py、warehouse.py、source.py、local_source.py、mcp_source.py。接入细节见[MCP](../integrations/mcp.md)、[Skill](../integrations/skills.md)。
