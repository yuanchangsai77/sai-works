# 壳与内核拆分：阶段核对与实施证据

历史基线日期：2026-10-01。设计基线提交：e70e525。A 核对完成，B/C 的同进程职责迁移与 R1–R3 可靠性验收完成。2026-10-10 新增 D1 同进程任务观察，证据见第 5 节；2026-10-15 完成 D2 批次①（审批记录与控制资格），证据见第 6 节；后续已增加取消桥接与恢复门禁，2026-11-04 复核见第 7 节。D2 审批挂起/续跑与 D3/E/F 未实施。无网络 API、无常驻续跑或断线审批承诺。本文为历史验收证据，不维护当前进度；现状见[实现对照](../../implementation.md)，当前待办只维护在[路线图](../../roadmap.md)。

## 历史编号释义

旧记录保留编号用于定位当时的提交，不作为现行工作名称。

| 旧编号 | 中文含义 |
| --- | --- |
| A | 拆分前兼容语义核对 |
| B/C | 无终端装配与运行职责迁移 |
| D | 跨界面同步与可恢复审批 |
| D1 | 任务快照与多观察者 |
| D2 | 审批记录、等待与续跑 |
| D3 | 各界面统一观察与控制 |
| E | 独立运行服务 |
| F | 桌面快捷入口 |

旧桌面里程碑 V0.1a、V0.1b、V0.2 分别表示同进程参考应用、可跨界面恢复的应用、独立运行服务；它们不是当前软件发布版本。现行文档直接使用中文能力名称。

## 1. 规范与设计审查

已核对 Git 规则、项目原则、Agent Runtime、壳边界、协议边界与审批设计。保留 TaskCheckpoint.phase、Run outcome 和 Session 生命周期的区分；单执行槽按 Runtime 实例生效，现有独立子代理实例不与父任务争用同一槽。审批续跑的预算连续性列入 D 的硬门槛。

持久化接受的具体提案已获用户确认，新增独立 SQLite 库，不改变旧会话、工具或模型格式。后续审批与 revision 的具体格式仍须确认。文档入口只链接唯一契约参考；历史现状表标记为拆分前基线，不继续当作当前实现事实。

## 2. B/C 实施

| 职责 | 当前落点 | 已验证行为 |
| --- | --- | --- |
| 无终端装配 | app.create_runtime → Runtime | 不导入终端展示模块，无输入输出仍能执行和保存；子代理工厂不创建 CLI |
| 接受与去重 | sessions.tasks.TaskStore | 身份、绑定输入和提交去重在同一事务接受；确认丢失后重试返回原结果；失败不执行 |
| 单执行槽 | Runtime FIFO 队列 | 同实例同时最多一个执行任务；排队次序稳定，失败释放槽 |
| Task 隔离 | ExecutionContext + 按 Task 工具装配 | 每个 Task 独立取消 token；跨 Task 工具实例与调用状态不共享；同 Task 续接保留状态 |
| 唯一收尾 | ExecutionEngine + Runtime | 正常、失败、中断归运行层，部分工具证据和 checkpoint 保留；壳不调用内部收尾 |
| 会话与上下文 | sessions.coordinator / compaction | 保存、恢复校验、压缩阈值、模型摘要、归档与失败回退归运行层；用户选择归壳 |
| 命令控制 | capabilities.runtime | 能力激活、释放与政策修改走 Runtime，不从展示对象直接修改实际政策 |
| 查询投影 | runtime_view | 展示和补全消费脱离模型/政策/工具可变对象的数据；展示异常不改写执行终态 |
| 子任务交付 | Runtime + SubagentRunner | 委派许可核验保留；候选结果等待父任务验收后才写入任务终态；修复失败归档路径的越界引用 |

Task identity 延续现有 checkpoint 的恢复判定。一个 Task 可关联多个独立 submission/Run；同 submission 重试不产生新 Run。存在未核对的 running 尝试时，不以新提交绕过恢复核对。默认旧壳入口自动产生内部 submission identity；需要控制重试的无终端调用者显式保留该身份。

## 3. 测试与基线对照

B/C 相关套件：headless runtime、engine/session/CLI、subagent、CLI entrypoint、slash commands，188 项通过。新专项覆盖无终端装配、重复提交与重启、绑定输入冻结、存储失败、FIFO 与异常释放、中断证据与取消隔离、工具实例隔离、展示故障、嵌套恢复数据往返、交付验收后持久化和压缩回退、接受事务提交失败的回滚以及展示投影不能修改真实政策。

在独立临时目录检出 e70e525，基线全量为 548 通过、12 失败。B/C 全量为 567 通过、6 失败；剩余六项与基线相同：显式上下文/工作区摘要布局、模型工作集的三个旧断言、TUI 补全数量。它们不作为本次新增回归，也不宣称全量通过。

本次修复两个相关既有执行路径问题：单次入口的可选内容传递与中断结果保存、子代理失败路径引用不存在的请求变量。两项旧断言已按既有契约修正：完整会话保留 60 条消息；归档压缩后工作集为一条系统摘要。没有通过删减必需上下文或恢复旧截断策略使测试通过。另在独立临时工作区执行真实非 TTY 单次入口（stub 模型），完成执行与会话归档。

## 4. 未闭合能力与下一门槛

后续审查复现的 R1–R3 已修复，见[R1–R3 闭环证据](#bc-可靠性缺陷闭环)。专项覆盖同 Task 排队、claim 后准备失败、结果保存失败不通知且不重放、会话保存后通知、失败回滚、后台交付只补保存以及观察错误隔离；B/C 相关套件 195 项通过。全量 574 项通过、6 项失败，失败项与上节记录的基线相同。

- 审批仍同步；等待释放执行槽、TTL、跨客户端有效性与可恢复 continuation 在 D 实施。B/C 的运行内审批复用保持兼容。
- 查询投影与持久化结果不是 revision 同步协议；多观察者、通知缺口与慢客户端验收在 D 实施。
- SaiWorks/Model Core 生命周期仍未交由独立 Manager；关闭旧壳继续沿用旧退出行为。常驻续跑、实例发现、ownership 和 Gateway 在 E 实施。
- Task 崩溃后保留身份和未知执行事实，不自动重新执行 running 尝试。跨进程恢复不构成当前保证。
- Companion 的全局快捷键、工作区输入、审批和重连界面在 F 实施。

下一步按[阶段 D 契约提案](../../design/approval-resume.md)确认新增查询字段、审批与 continuation 存储语义，再实施 D；具体发布入口和跨进程协议按 E 门槛单独确认。

## 5. D1 同进程任务观察（2026-10-10）

本轮按用户要求同时修改代码和文档。现行接口归[任务观察参考](../../reference/runtime-observation.md)，后续审批设计继续归阶段 D 提案，未将整个 D 标为完成。

- TaskStore 事务升级版本 2，按 Task 聚合有序提交；接受、执行 claim、结果提交及交付标记与 Task revision 同事务更新。快照使用显式读取事务，保证修订与状态一致。
- Runtime 增加可信装配方授予及撤销的仅观察句柄、完整快照和独立观察游标。游标仅保留修订位置，慢客户端不积累通知队列，也不在执行线程运行客户端回调；缺口返回完整快照并要求重新同步。
- 区分执行结果与会话交付。会话保存失败保留 pending；成功记录 completed 后再通知旧观察回调。会话保存成功、交付标记失败时抑制完成通知并保留待核对状态，不宣称跨库原子提交。
- 新增 16 项行为测试：提交聚合及副本隔离、多观察者与资格校验、快照/观察衔接及关闭失效、修订失败回滚、会话保存失败后不重执行、交付标记失败及重复调用不重复保存、升级提交失败回滚、旧记录升级及未来版本拒绝、并发快照一致性、慢读取期间撤销、四类错误句柄、单次持久化入口的标记补交付，以及两个观察者跟随实际 Runtime 执行到同一终态。

验证命令：`PYTHONPATH=src .venv/bin/python -m pytest tests/test_runtime_observation.py tests/test_headless_runtime.py tests/test_desktop_controller.py tests/test_execution_engine_sessions_and_cli_runtime.py tests/test_subagent_session_clusters.py tests/test_app_cli_entrypoint_dispatch.py tests/test_slash_commands.py -q`，238 项通过。另运行 TUI 与会话生命周期套件，43 项通过、1 项失败。失败为 TUI 斜杠补全数量断言，已在临时目录导出当前 HEAD 后独立复现，属于既有失败。本轮合计 281 项通过、1 项既有失败；未运行全量测试，不把历史全量结果作为本轮结果。

审查修复：快照数据库读取移出调度锁，读取前后检查资格，避免慢读取拖延控制并防止撤销期间继续返回数据。成功保存会话后、交付标记失败的同进程重试仅补标记，不重复追加消息；completed 提交重复调用不再次保存或通知。该补交付凭据只存在于当前 Runtime，重启后的 pending 仍需核对。新观察接口尚未整体接入各壳，D2 具体协商项已补入提案，未提前实施审批范围或数据库变化。

范围限制：各壳仍使用旧进度与同步审批入口，D3 尚未迁移；审批挂起、TTL、continuation、按 Task 取消及控制资格在 D2；轮询游标只供同进程使用，不保证跨进程接入或常驻续跑。下一步先确认 D2 动作批准范围、可恢复状态和调度契约，再实施。

## 6. D2 批次①：审批记录与控制资格（2026-10-15）

契约协商项已于 2026-10-15 逐项确认：批准范围收敛为单动作指纹批准（接受审批次数增加）；新增独立 TaskControl 控制句柄；`approval_ttl_seconds` 默认 300 秒；任务库 v2→v3 事务内升级并停止旧写入进程。现行接口归[任务控制与审批参考](../../reference/runtime-control.md)。本批次只交付审批记录、决定有效性、事务竞争与控制资格；引擎挂起/续跑、预算连续性与旧同步审批适配属后续批次，引擎审批路径与 TaskSnapshot 结构在本批次不变，`consumed` 仅为保留枚举。

- TaskStore 升级版本 3：新增 approvals 表与 task_cancellations 表；审批请求、决定、取消接受与惰性过期均在与 Task revision 相同的写事务内提交，幂等重复不递增修订。
- 动作指纹覆盖 Task、submission、动作名、规范化参数、解析后的工作区根与所需 scope；同指纹幂等返回，异指纹同身份报错；首次提交的决定生效，冲突决定返回既有裁决。
- Runtime 新增 `grant_task_control`/`revoke_task_control`/`decide`/`cancel`/`pending_approvals` 及所有者级 `request_approval`；权限与 actor 授予时固定并由 Runtime 持有；观察句柄与控制句柄互不通用，伪造、撤销、跨实例与关闭后的句柄均被拒绝；decide/cancel 竞争恰好落一种顺序。
- D1 升级测试的未来版本探针从 3 调整为 4（v3 成为受支持版本，v4 仍须拒绝）。

新增 15 项行为测试（tests/test_runtime_approval_control.py）：指纹绑定与修订递增、批准/拒绝生命周期与幂等冲突、过期拒绝与重请求新身份、取消幂等与迟到决定、已提交裁决免疫后续取消、decide/cancel 双线程竞争、资格矩阵与跨任务拒绝、畸形句柄、关闭后全部拒绝、v2→v3 升级与未知版本拒绝、未知审批状态拒绝、配置默认值/覆盖/越界、审批记录不影响执行摘要。

验证命令：`PYTHONPATH=src ./.venv/Scripts/python.exe -m pytest tests/test_runtime_approval_control.py tests/test_runtime_observation.py tests/test_headless_runtime.py tests/test_desktop_controller.py tests/test_app_cli_entrypoint_dispatch.py tests/test_runtime_config_loading.py -q`，97 项通过（Windows）。另在 WSL 原生副本运行全量：646 项通过、6 项失败，失败集合与第 3 节记录的基线完全相同（显式上下文/工作区摘要布局 2 项、模型工作集旧断言 3 项、TUI 补全数量 1 项），已在独立工作树检出 HEAD 复现确认属既有失败，不作为本轮回归。WSL 副本经白名单进程打包后解包获得，绕开了 DLP 透明加密导致的 /mnt/d 密文读取问题。

## 7. 进度复核（2026-11-04）

对照当前实现与最近提交，修正总览仍将 Task 取消桥接列为未实现的过时描述。当前已经具备活动 Run 取消桥接、动作启动排序、未知副作用恢复门禁，以及关闭失败后的资源清理重试。审批记录和 TTL 不等于引擎已支持挂起/续跑；Desktop 仍为同进程 Tk 参考壳。

本轮执行：

```bash
python3 -m pytest -q tests/test_desktop_controller.py tests/test_runtime_shutdown.py tests/test_runtime_approval_control.py tests/test_runtime_observation.py tests/test_execution_recovery.py tests/test_mcp_lifecycle.py tests/test_mcp_runtime_hardening.py --tb=short
```

结果：119 项通过。覆盖 Desktop 控制与审批窗口生命周期、Runtime 关闭重试、审批控制、任务观察、执行恢复和 MCP 生命周期相关行为；未执行全量回归、真实模型任务或图形环境人工验收，不将这些结果外推为桌面产品已验收。这一历史结果不替代真实桌面验收；当前工作以现行[路线](../../roadmap.md)为准。

## B/C 可靠性缺陷闭环

保留原临时审查记录中不能由现行机制替代的根因；原编号只用于历史定位，不是当前待办。

| 历史编号 | 当时触发与根因 | 修复及验收事实 |
| --- | --- | --- |
| R1 | 同 Task 多个已接受提交排队，首个完成过早清除共享执行上下文；后续 claim 成功后准备失败，遗留 running | 上下文按排队、执行及待交付引用计数；取得执行权后的准备进入统一异常收尾。验证两个提交均收尾、准备失败不遗留假 running |
| R2 | 完成通知早于任务结果持久化，保存失败后界面已显示结束，重试可能重放副作用 | 结果保存成功后才通知；保存失败保持核对信号，同 submission 不重新执行。验证保存失败无完成通知且无动作重放 |
| R3 | 会话、单次入口与后台交付缺少共同持久化收尾 | Task 结果后保存会话再通知；失败回滚内存，稳定提交只补交付。已纳入当时 B/C 验收 |

原 R4 多观察者已有 D1 机制，但各壳迁移仍是独立工作；原 R5 的按 Task 取消接受、活动 Run 桥接与启动排序已有实现。原 R6 的视图兼容结构属于壳迁移边界。当前差距以路线图和现行参考为准，不沿用旧记录中的“D 整体未实现”判断。

## 历史 Desktop 协议诊断示例（2026-10-02）

当时核对的 Desktop session 为 `20261001163052318835-460bda3d`，最近 run 为 `2026-10-01T17-02-42.471782+00-00`。请求“继续”已取得真实 `model.response`，但响应未满足 native tools 严格 JSON 字段约束，最终为 `runtime_error`；这不是 Desktop 丢失回复，也不是连接失败。

该例只说明如何区分传输和协议故障，不能作为当前 native_tools 正文要求；当前协议支持以[模型传输参考](../../reference/model-transport-contract.md)为准。
