# SaiWorks 当前实现与目标差距

核对于 2026-11-18：对照 Runtime/TaskStore、桌面应用投影、能力仓库、上下文装配和工具批次实现。本文只汇总当前事实与限制，工作状态只维护在[路线图](roadmap.md)。历次测试与环境见[证据索引](archive/README.md)，不将历史结果当作本次验收。

## 1. 当前阶段

当前系统是以本地软件项目为主要场景的智能体执行框架（Agent Harness）：有模型—工具循环、会话持久化、能力按需激活、执行审批、证据与恢复机制。它已有执行内核基础，但尚不是通用的远程 Execution Platform 或 Distributed Agent Runtime。引擎内部已将模型轮次、动作处理、结果投影、轮次裁决和收尾分为独立职责；单次 Run 的计数与缓存隔离于按 Task 共享的执行上下文。按 Task 持久化取消已桥接所属活动 Run，并与新动作启动按事务排序；已经启动的工具仍登记结果后以 `interrupted` 收尾，不保证立即终止外部副作用。未确认副作用保留为恢复上下文，由模型决定核查与后续动作，核对边界见[任务控制与审批](reference/runtime-control.md)。

CLI 与 TUI 共用执行引擎，TUI 通过展示适配接入。现有应用入口保留 CLI 兼容适配，另有无终端 Runtime 装配。运行、会话、压缩、命令激活和收尾已迁入 Runtime；独立任务库提供事务接受与提交去重。Runtime 实例按任务单执行槽调度，Task 的取消上下文和工具执行实例隔离；这不妨碍同一轮内受控本地读取最多四项并发，二者是不同层级。同进程任务快照与观察机制已有持久化修订、多观察者和可撤销观察资格，并区分执行结果与会话交付状态；现行接口见[任务观察](reference/runtime-observation.md)。审批记录、有效期、决定竞争与控制资格已有实现；审批与用户问题已接入持久化等待、批准消费、执行槽释放和原轮次续跑，同步入口仅在槽外等待最终结果；已保存等待支持新进程显式恢复。各界面已接入同步兼容回调，完整快照订阅仍未统一，见[等待机制](runtime/agent-runtime.md#等待与续跑)。跨进程同步与常驻服务仍未实现。确认本机旧所有者退出后已可回收遗留执行权，未知动作事实保留给模型，不自动重放；这不等于后台自动续跑。显式推进仍核验委派范围，准备失败保留等待与既有决定；Shell 目录按工作区重新校验，环境丢失后由模型调整未派发步骤。恢复诊断逐轮更新，普通业务失败和重复探索不提前终止推理，明确完成证据与预算仍核验，见[恢复机制](runtime/evidence-recovery.md)。

TUI 输入缓冲区独立于执行状态，粘贴只编辑、明确提交后清空，未完成输入在任务收尾后继续交接，输入控制字符仅在显示层转义，选择组件共用输入缓冲且终端不提前处理粘贴控制字节；审批续跑按任务与提交标识保留原轮展示和未提交草稿，并清理上一执行片段遗留的工具展示句柄，用户回复通过单一终端读取线程交给当前挂起请求；按键兼容与取消边界见[终端交互界面](interfaces/tui.md)，真实终端及输入法兼容尚待验收。

桌面参考应用提供 GUI 无关的 `DesktopController`：它仅保持界面本地的当前会话与展示投影，工作区打开、会话恢复、任务提交、取消和关闭均调用 Runtime 门面。它不暴露 Engine、ToolRegistry 或 Policy，不创建第二套会话与任务存储。`desktop_presenter` 只负责语义动作名称、审批文案和 Activity 展示状态，`desktop_windows` 负责 Quick Actions、Diagnostics 和日志目录入口；二者都不持有 Runtime 权威状态。`DesktopApp` 使用内置 Tk 8.6 提供参考窗口，已接通工作区、基于 session 的任务导航、后台执行、紧凑进度卡、按需 Activity、审批、取消、结果卡、Quick Actions 与当前 run 诊断；窗口已有 Conversation/Result/Artifacts 投影、历史结果和临时流式预览，Artifacts 当前是已有引用与证据来源；尚未接入任务快照与修订订阅或完整文件/diff 查看，也未经生产打包、签名、更新与完整视觉/无障碍验收。

## 2. 目标领域与代码事实

| 目标领域 | 当前机制 | 未闭合边界 | 主要依据 |
| --- | --- | --- | --- |
| Agent Runtime | 模型—工具循环、运行预算、完成门禁、本地子代理和取消入口 | 统一任务管理门面、常驻 worker、完整跨进程故障续跑与长期调度 | orchestration/engine.py、control.py、subagent_runner.py |
| Execution Runtime | 工具注册、参数与安全预检、隔离本地读取批次、有序观察合并、Shell 与 MCP 适配 | 通用 Execution Target、远程租约、统一副作用恢复和资源隔离 | tools/registry.py、schema_validation.py、mcp/adapter.py |
| Capability | 能力仓库、本地有界目录快照、打开、按需激活与释放、scope 和预算 | 完整资源包生命周期、能力与目标绑定及资源上下文整合 | capabilities/warehouse.py、local_source.py、mcp_source.py |
| Workspace | 工作目录、会话初始根与活动根、批准路径、项目上下文与 Shell 状态 | 独立资源环境对象、远程资源与秘密引用生命周期 | types.py、context/workspace.py、tools/builtins/workspace_open.py |
| Policy & Permission | 安全模式、动作风险、内容阻断、路径授权、子代理 effects/resource 约束 | 基于统一主体与目标的策略输入，票据、撤销与跨设备授权 | safety/policy.py、guardrails.py、orchestration/permissions.py |
| Evidence & Recovery | EvidenceRecord、TaskCheckpoint、terminal summary、会话 resume state、文件观察状态和日志 | 完整 Execution Ledger 查询、任意动作重放、分叉和通用回滚 | types.py、sessions/store.py、tools/observation_state.py、observability/logger.py |

上述路径相对于 `src/saiworks/`。具体行为以[运行时机制](runtime/README.md)及已实现契约为准；存在类型或文件不代表目标领域已经完整实现。

## 3. 交互边界现状

| 交互职责 | 现状 | 差距 |
| --- | --- | --- |
| 提交与控制运行 | Runtime 统一执行协调、Task 接受与提交查询，CLI 保留输入 | 跨进程控制，以及各界面对已有同进程 revision 快照的统一消费 |
| 桌面应用控制 | DesktopController 复用 Runtime；Tk 参考窗口已接通主链路 | 生产打包/视觉/无障碍、完整快照订阅、跨界面切换与脱离界面自动续跑 |
| 会话与恢复 | SessionCoordinator 协调存储、恢复和压缩；界面选择会话 | 跨任务 lineage 与独立恢复接口 |
| 事件与观察 | ProgressReporter、日志事件、TUI 事件队列 | 多界面统一订阅、重连与事件位置语义尚未建立 |
| 授权交互 | Runtime 持久化等待、消费批准并续跑；界面兼容回调在槽外收集决定 | 跨界面完整接管、远程控制资格和常驻运行生命周期 |
| 工作空间与能力 | 工作空间状态、能力仓库及命令入口 | 统一面向界面的查询与管理边界 |
| 历史与证据 | 会话 trace、检查点、工具结果和运行日志 | 跨端一致查询、保留策略与访问约束 |

上述行是分析维度，不是现行 API 定义。已有扩展点属于另一条边界，见[运行时扩展点](integrations/extension-points.md)。

当前能力与说明的装载见[能力发现与生命周期](runtime/capability.md)和[上下文](runtime/context.md)；目录不触发远端发现，操作说明按每轮工具与观察选择。隔离读取的准入、取消及观察绑定边界见[工具执行](reference/tool-execution.md)。

## 4. Model Core 与共享信任域

模型客户端、提示构造、动作解析和流式自然语言投影仍在本仓库。它们承担调用与运行协议适配，不应据此继续扩展模型部署、GPU 调度、Provider 平台路由或计量。

当前模型访问以 OpenAI-compatible endpoint 为主要传输形式，另有 stub；这不表示 Model Core 已具备或已接入全部资源能力。调用侧行为见[模型传输参考](reference/model-transport-contract.md)。

当前已有本地授权与委托限制，不能将其视为统一 Principal、Device、Credential、AccessTicket、AccessSession 的实现。共享信任域是目标边界；访问许可与执行许可仍需分开。

## 5. 恢复与证据的实际范围

当前检查点、会话状态和文件观察信息支持继续任务并检查部分前置条件。动作去重用于限定范围内避免重复调用，不是跨节点 exactly-once 保证。日志用于观察与诊断，不是已经完备的执行账本。

恢复不能自动恢复失去的外部进程或证明不确定副作用没有发生。子代理 handoff 需要验证，模型最终文本不能覆盖运行时终态。重放、分叉、回滚与跨设备故障恢复需要专项机制及验收。

## 6. 验证与维护

关键测试位于 `tests/`，包括 engine/session、CLI dispatch、model protocol、policy、MCP、capability、subagent 和 TUI 场景。本页不维护易失效的测试总数或未重新执行的“全部通过”结论。

本地 Coding 任务已有独立的真实模型验收入口，场景与判据见[本地 Coding 任务验收](evaluation.md)。它提供可复盘的任务级结果；尚未积累跨模型、跨故障场景的稳定性证据。

修改实现后更新本页对应差距及专题边界；目标架构只在职责变化时更新。历史测试结果不作为当前自动验收依据；需要复盘的根因见[精选历史](archive/README.md)。

实现依据：[runtime.py](../src/saiworks/runtime.py)、[desktop.py](../src/saiworks/interaction/desktop.py)、[registry.py](../src/saiworks/tools/registry.py)。

回归入口：[test_runtime_approval_control.py](../tests/test_runtime_approval_control.py)、[test_capability_warehouse.py](../tests/test_capability_warehouse.py)。
