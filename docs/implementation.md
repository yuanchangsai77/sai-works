# SaiWorks 当前实现与目标差距

状态：当前实现梳理，核对日期 2026-09-30。依据当前源码结构和关键装配、运行、会话及交互入口核对；本轮未运行测试，不构成全量兼容性或故障验收。专题中的历史测试结果需保留原日期，不转写为本轮验收结果。

## 1. 当前阶段

当前系统是以本地软件项目为主要场景的 Agent Harness：有模型—工具循环、会话持久化、能力按需激活、执行审批、证据与恢复机制。它已有执行内核基础，但尚不是通用的远程 Execution Platform 或 Distributed Agent Runtime。

CLI 与 TUI 共用执行引擎，TUI 通过展示适配接入。应用装配仍返回 CLI 对象；会话协调、持久化入口和部分后台运行职责仍留在 CLI 中。因此共用执行机制不等于已有独立、完整的壳接口。

## 2. 目标领域与代码事实

| 目标领域 | 当前机制 | 未闭合边界 | 主要依据 |
| --- | --- | --- | --- |
| Agent Runtime | 模型—工具循环、运行预算、完成门禁、本地子代理和取消入口 | 统一任务管理门面、常驻 worker、跨进程故障续跑与长期调度 | orchestration/engine.py、control.py、subagent_runner.py |
| Execution Runtime | 工具注册与调用、参数校验、安全预检、文件和 Shell 等本地执行、MCP 适配 | 通用 Execution Target、远程租约、统一副作用恢复和资源隔离 | tools/registry.py、schema_validation.py、mcp/adapter.py |
| Capability | 能力仓库、来源、目录、打开、按需激活与释放、scope 和预算 | 完整资源包生命周期、能力与目标绑定及资源上下文整合 | capabilities/warehouse.py、local_source.py、mcp_source.py |
| Workspace | 工作目录、会话初始根与活动根、批准路径、项目上下文与 Shell 状态 | 独立资源环境对象、远程资源与秘密引用生命周期 | types.py、context/workspace.py、tools/builtins/workspace_open.py |
| Policy & Permission | 安全模式、动作风险、内容阻断、路径授权、子代理 effects/resource 约束 | 基于统一主体与目标的策略输入，票据、撤销与跨设备授权 | safety/policy.py、guardrails.py、orchestration/permissions.py |
| Evidence & Recovery | EvidenceRecord、TaskCheckpoint、terminal summary、会话 resume state、文件观察状态和日志 | 完整 Execution Ledger 查询、任意动作重放、分叉和通用回滚 | types.py、sessions/store.py、tools/observation_state.py、observability/logger.py |

上述路径相对于 `src/saiworks/`。具体行为以[运行时机制](runtime/README.md)及已实现契约为准；存在类型或文件不代表目标领域已经完整实现。

## 3. 交互边界现状

| 交互职责 | 现状 | 差距 |
| --- | --- | --- |
| 提交与控制运行 | 引擎执行与取消；CLI 协调输入和运行 | 不依赖终端的统一管理入口和运行状态查询 |
| 会话与恢复 | SessionStore 保存和读取；CLI 协调恢复与持久化 | 会话服务与壳输入、选择、展示职责分离 |
| 事件与观察 | ProgressReporter、日志事件、TUI 事件队列 | 多壳统一订阅、重连与事件位置语义尚未建立 |
| 授权交互 | 装配层传入展示层确认回调 | 可等待、可关联、可恢复的审批交互仍需设计 |
| 工作空间与能力 | 工作空间状态、能力仓库及命令入口 | 统一面向壳的查询与管理边界 |
| 历史与证据 | 会话 trace、检查点、工具结果和运行日志 | 一致的查询边界、保留策略与访问约束 |

这六行是分析维度，不是现行 API 定义。已有扩展点属于另一条边界，见[运行时扩展点](integrations/extension-points.md)。

## 4. Model Core 与共享信任域

模型客户端、提示构造、动作解析和流式自然语言投影仍在本仓库。它们承担调用与运行协议适配，不应据此继续扩展模型部署、GPU 调度、Provider 平台路由或计量。

当前模型访问以 OpenAI-compatible endpoint 为主要传输形式，另有 stub；这不表示 Model Core 已具备或已接入全部资源能力。调用侧行为见[模型传输参考](reference/model-transport-contract.md)。

当前已有本地授权与委托限制，不能将其视为统一 Principal、Device、Credential、AccessTicket、AccessSession 的实现。共享信任域是目标边界；访问许可与执行许可仍需分开。

## 5. 恢复与证据的实际范围

当前检查点、会话状态和文件观察信息支持继续任务并检查部分前置条件。动作去重用于限定范围内避免重复调用，不是跨节点 exactly-once 保证。日志用于观察与诊断，不是已经完备的执行账本。

恢复不能自动恢复失去的外部进程或证明不确定副作用没有发生。子代理 handoff 需要验证，模型最终文本不能覆盖运行时终态。重放、分叉、回滚与跨设备故障恢复需要专项机制及验收。

## 6. 验证与维护

关键测试位于 `tests/`，包括 engine/session、CLI dispatch、model protocol、policy、MCP、capability、subagent 和 TUI 场景。本页不维护易失效的测试总数或未重新执行的“全部通过”结论。

修改实现后更新本页对应差距及专题边界；目标架构只在职责变化时更新。历史测试结果不作为当前自动验收依据；需要复盘的根因见[精选历史](archive/README.md)。
