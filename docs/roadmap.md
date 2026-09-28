# SaiWorks 路线图

## 文档职责

本文档只维护：

- 当前可用基线
- 已完成、进行中和未完成条目的实施状态
- 尚未完成的能力
- 推荐优先级
- 下一阶段验收标准

已经完成的功能只在进度表中保留状态和验收证据；原路线目标和验收条目继续保留以便追踪，但不
展开施工步骤或实现细节。当前行为进入对应功能文档；历史方案、阶段提交和实施过程由 Git 记录。
专项设计也不在本文重复定义：

- 总体分层见[总体架构](architecture.md)
- 模型—工具循环见[Agent 执行循环](core/agent-loop.md)
- 授权和内容检查见[执行安全](core/execution-safety.md)
- 项目规则、探测和测试解析见[项目感知](core/project-awareness.md)
- Skill、MCP、能力仓库和 TUI 分别见对应专题文档

版本快照位于 `docs/versions/`，只记录发布时点的不可变能力边界。新的快照只在项目实际
准备发布新版本时创建，不以文档整理本身作为版本升级依据。

## 当前基线

本文维护路线各阶段的逐项实施状态、验收条件及推荐顺序。历史已完成、当前进行中
和未开始的条目都保留在下方；状态以对应实现、测试和专项文档证据为准。

状态核对日期：2026-09-28。

当前 runtime 已具备：

- 单轮与多轮 CLI、会话保存和恢复。
- OpenAI-compatible 与 stub 模型客户端、原生 tool calls 和 JSON fallback。
- 文件、搜索、Shell、测试、patch 和只读 Git 工具。
- `readonly`、`confirm`、`auto` 模式，审批、危险命令识别和凭据写入阻断。
- 项目规则、相关 workspace 摘要、显式 context、项目探测和默认测试命令解析。
- Skill metadata 扫描、工具箱展示、instructions 显式激活和跨轮保留；不再维护独立的
  trigger 自动注入路径。
- MCP 三种 transport、懒 discovery、缓存、重连、tool/resource 协议入口和按需激活。
- 能力仓库目录、manifest、激活/释放、scope、预算和冲突预检，以及按需激活的本地
  Subagent 工具箱和用户 `/capabilities`、`/skill` 入口。
- 原生 inline TUI、会话内编辑历史、审批、中断和运行时输入。
- run 事件与详情日志。

当前测试收集为 560 个用例，覆盖 engine、model、policy、tools、context、Skill、MCP、
能力仓库、CLI 和 TUI 的关键路径。最近一次完整执行结果为 `560 passed in 2.75s`，环境为
Python 3.14.4，基准 revision `263f004` 加当前工作树修改（当前工作树仍有未提交修改）。

### Current Capabilities

当前已可使用的能力包括：

- 在本地项目中执行多轮编码、检查和文件修改任务。
- 对文件、Shell、测试和外部能力执行权限控制与审批。
- 中断任务并通过 checkpoint 恢复，保留关键证据和失败状态。
- 按需激活 Skill、MCP 工具箱和本地 Subagent。
- 通过 CLI/TUI 查看工具执行、审批、重试和任务结果。

以上描述只代表已有实现和测试覆盖，不代表所有 Provider、终端环境或远程服务均已完成兼容性验收。

## 路线实施进度

以下比例是按路线图条目和验收闭环估算，不是代码行数比例。`[x]` 表示已完成，`[~]` 表示
已有可用实现但仍缺少完整验收或后续边界，`[ ]` 表示尚未实现。

### Definition of Done

只有同时满足以下条件，条目才能标记为 `[x]`：

- 已实现，并符合对应专项文档的契约。
- 正常路径、错误路径和关键边界有回归测试。
- 测试已经实际执行并通过，不能只依赖 collected 数量。
- 有可追踪证据：测试名称、CI 结果或对应 revision。
- 与现有核心功能保持兼容；若有破坏性变化，已记录影响和迁移方式。

`[~]` 表示存在可用实现但尚未满足全部验收要求；`[ ]` 表示尚无可验收的实现。

| 阶段 | Feature coverage | Acceptance | 主要未闭环 |
| --- | ---: | --- | --- |
| P0 长任务连续性与上下文预算 | 约 85% | 部分验收：主链路通过，核心协议仍未闭环 | token 预算、完整语义摘要、统一 artifact 回查、显式复杂任务状态 |
| P1 Skill 完整能力 | 约 55% | 部分验收：Skill 激活主链路通过 | references/assets/scripts 资源包、脚本统一执行、覆盖诊断 |
| P2 MCP 兼容性与资源上下文 | 约 65% | 部分验收：transport/discovery/activation 通过 | 公网兼容性、traits 完善、resource context packaging |
| P3 交互体验、分发与质量门禁 | 约 30% | 部分验收：inline TUI 通过，工程门禁不足 | TUI streaming、overlay、跨进程历史、配置命令、CI/lint/兼容性 |
| P4 Subagent、Team 与 A2A | 约 60%（本地阶段） | 部分验收：本地 session cluster 通过 | 主任务依赖图、常驻 worker、崩溃续跑、审批代理、远程 A2A |

### Tracking IDs

| ID | 条目 | 证据入口 |
| --- | --- | --- |
| P0-01 | ContextPackager 与上下文预算 | `src/saiworks/context/packager.py`、`tests/test_model_client_prompt_and_parser_protocol.py` |
| P0-02 | Checkpoint、evidence 和恢复 | `src/saiworks/orchestration/engine.py`、`tests/test_execution_engine_sessions_and_cli_runtime.py` |
| P0-03 | Task State 与完成门禁 | `src/saiworks/orchestration/control.py`、`tests/test_agent_validation_safety_workflow.py` |
| P0-04 | 动作协议与响应不变量 | `src/saiworks/model/parser.py`、`tests/test_model_client_prompt_and_parser_protocol.py` |
| P0-05 | 信任边界与副作用合同 | `src/saiworks/safety/`、`tests/test_content_safety_interceptor.py`、`tests/test_tool_risk_policy.py`、`tests/test_mcp_runtime_hardening.py` |
| P1-01 | Skill 资源包与激活 | `src/saiworks/skills/`、`tests/test_skill_system.py` |
| P2-01 | MCP transport、traits 与 resources | `src/saiworks/mcp/`、`tests/test_mcp_*.py` |
| P3-01 | TUI、CI 与质量门禁 | `src/saiworks/interaction/`、`tests/test_tui_runtime.py` |
| P4-01 | 本地 Subagent/Team 与集成验收 | `src/saiworks/orchestration/subagent_runner.py`、`tests/test_subagent_session_clusters.py` |
| P4-02 | 跨进程 worker 与远程 A2A | 尚未实现 |

### P0：长任务连续性与上下文预算

- `[x]` **P0-01** `ContextPackager` 已建立，具备分组、来源、字符预算、裁剪和省略记录。
- `[x]` **P0-02** checkpoint 已保存任务事实、read state、恢复信息、active capabilities 和大值引用。
- `[x]` **P0-03** 中断、模型/工具错误、审批拒绝和完成门禁已有结构化恢复路径。
- `[~]` **P0-03** checkpoint 现以单一任务状态管理 `pending/in_progress/blocked/verified/done`，支持旧状态迁移、阻塞恢复及证据失效后撤销完成状态；验收通过 `required_evidence` 与完成门禁检查。状态迁移回归用例已纳入全量测试并通过；任务依赖图由 P4 Subagent/Team 负责。
- `[~]` 已有字符级上下文预算和 artifact envelope；后续采用模型感知 token 预算、未知模型保守估算、误差余量和 usage 回填，并补齐语义摘要及通用 artifact 回查。
- `[~]` **P0-04** `action_protocol` 已进入模型能力 profile、Prompt 和传输解析；parser/runtime 按 profile 拒绝动作通道混用，严格校验 JSON 回复字段及类型，并拒绝 `done=true` 且 actions 非空的响应。对应回归用例已补齐；正式 JSON Schema 与完整验收仍待完成。
- `[ ]` 使用正式 JSON Schema 校验 `message`、`done`、`actions` 及工具参数，并固定 `done/actions` 状态不变量。
- `[~]` **P0-05** 已有凭据扫描、内容拦截、MCP 不可信 metadata 风险提升和 workspace 路径检查；统一不可信内容标记及 prompt injection 端到端测试仍待完成。
- `[~]` **P0-05** 已有 `progress_required`、错误码、retryability 和模型重试；运行时预算、错误分类、退避、幂等和副作用重试合同仍待统一。

### P1：Skill 完整能力

- `[x]` **P1-01** metadata 扫描、内置/全局/项目覆盖、工具箱展示、显式激活和 session 保留已完成。
- `[x]` `/skill` 及能力仓库入口可用，Skill instructions 和关联本地工具走统一激活路径。
- `[~]` Skill 正文已进入共享 ContextPackager；独立资源预算、来源摘要和按需引用仍不完整。
- `[ ]` `references/`、`assets/`、`scripts/` 的独立 manifest、激活和生命周期。
- `[ ]` Skill script 转换为普通工具动作并经过统一 policy、审批和日志路径。
- `[ ]` 同名覆盖、版本冲突和来源优先级的专用用户诊断。
- `[~]` Capability 已区分注册、激活、scope 和部分 provider 状态；完整生命周期状态迁移及在线探测仍待补齐。

### P2：MCP 兼容性与资源上下文

- `[x]` **P2-01** stdio、SSE、streamable HTTP、initialize、tools/resources、缓存、重连和按需 discovery 已完成。
- `[x]` MCP 工具通过 capability warehouse 激活，不再默认全量注册。
- `[~]` capability traits 和风险映射已有基础实现，公网 server 兼容性矩阵仍不足。
- `[~]` resource list/read 已有协议入口，但尚未完整接入候选上下文选择、预算和敏感信息过滤。
- `[ ]` MCP resources/prompts 与工具箱一致的叶子激活语义。
- `[ ]` 对 MCP resource、外部文档和 artifact 引用补齐统一的来源信任、授权、完整性和回查校验。

### P3：交互体验、分发与质量门禁

- `[x]` **P3-01** 原生 inline TUI、事件队列、审批、中断、运行中输入和会话内历史已完成。
- `[~]` 模型客户端具备 streaming 接收和自然语言投影；TUI 完整增量展示和稳定输出闭环仍待补齐。
- `[ ]` 统一 overlay 栈和可恢复的帮助/session/Skill/MCP/审批界面。
- `[ ]` 跨进程输入历史、长任务 transcript 索引和日志查询命令。
- `[ ]` `sai-works config get/set/list/path` 完整命令面。
- `[ ]` zsh/fish completion、release checklist、CI、ruff、类型检查和终端兼容性矩阵。
- `[ ]` 为协议混用、prompt injection、重试幂等、artifact 越权和 progress budget 增加端到端质量门禁。

### P4：Subagent、Team 与 A2A

- `[x]` **P4-01** 独立 child session、tool history、run id、权限、context budget、session mirror 和 cluster 状态已完成。
- `[x]` 公共状态、文件锁/原子替换、并发 ready runner 和结构化 handoff evidence 已完成。
- `[~]` 主 Agent 已能批量委派和收集结果；P4 只负责依赖调度、分发和聚合，必须复用 P0-03 的共享 Task State，不得建立第二套完成状态。
- `[ ]` 跨进程常驻 worker、进程崩溃自动续跑和审批代理。
- `[ ]` 远程 A2A transport；远程修改回到本地 patch/artifact 审批流的完整实现。
- `[~]` checkpoint 已有 objective、phase、required evidence、blocker 和 artifacts；复杂任务的目标、验收条件、子任务依赖和状态迁移仍未独立建模。
- `[ ]` 主 Agent 对多子任务结果执行集成验证，并在 TUI/CLI 展示 phase、partial artifacts、真实 Shell cwd 和 blocker。

因此，各阶段不是“全部完成”或“全部未开始”：P0 的状态一致性主链路基本完成，P1/P2 的核心
能力已可用但扩展边界未闭合，P3/P4 的长期能力仍处于分阶段实施状态。协议、安全、重试和验证等
横向工作已按职责并入对应的 P0–P4 进度，不再维护平行分类。

## 路线目标与验收要求（状态见上方进度表）

### P0：长任务连续性与上下文预算

这是当前最高优先级。目标是让单 Agent 的长编码任务可中断、可恢复、可验证，同时避免
prompt 随历史无限增长。

### P0.1 Context packaging

- 在 context loaders 和 prompt builder 之间建立独立 `ContextPackager`。
- 输入包括 conversation、tool results、Skill、显式 context、checkpoint 摘要和来源
  引用。
- 第一版只做稳定分组、来源标记、字符统计和总量限制，再逐步加入摘要策略。
- 超预算时显式记录裁剪和省略，不允许静默丢失关键上下文。

### P0.2 Session checkpoint

- 保存任务阶段、关键工具结果摘要、最近失败、最近验证结果和 active capabilities。
- 保存文件 read state：path、mtime、hash；恢复后判断 patch 是否仍安全。
- 中断、模型错误、工具错误和审批拒绝时都生成可恢复状态。
- 完整事件和大输出保留在 cold archive，checkpoint 只保存恢复所需的最小事实。

### P0.3 Task plan 与恢复

- 引入 `pending`、`in_progress`、`blocked`、`verified`、`done` 的轻量状态。
- run summary 展示当前阶段、完成事项、阻塞原因、验证状态和下一步。
- resume prompt 使用 checkpoint 恢复包，不依赖模型猜测完整历史。
- 修改任务结束时明确给出测试通过、测试失败或未运行测试的原因。

### P1：Skill 完整能力

当前 workflow instructions 与本地工具已统一进入 `LocalToolboxSource`；Skill 只负责磁盘
发现、版本和覆盖。剩余工作是把磁盘 Skill 从“指令文件”扩展为受控资源包。

- 为 `references/`、`assets/`、`scripts/` 建立独立索引和按需加载。
- 为 Skill 内容设置独立预算、来源引用、裁剪和摘要。
- Skill script 必须转换为普通工具动作，经过同一 policy、审批和日志路径。
- 增加 `/skill` 或等价交互入口。
- 为同名覆盖、版本冲突和来源优先级提供用户可见诊断。

### P2：MCP 兼容性与资源上下文

MCP 最小主链路已经完成，不再规划第二套 transport 或全量工具注册路径。

- 扩充公网 MCP server 兼容性测试和错误诊断。
- 完善 capability traits 与风险映射。
- 将 resource descriptor 纳入候选上下文选择。
- resource 正文经过长度限制、敏感信息保护和 `ContextPackager` 后才能进入 prompt。
- 为 MCP resources/prompts 建立与工具箱一致的叶子激活语义。

### P3：交互体验、分发与质量门禁

### 交互与配置

- 模型增量 streaming。
- 统一 overlay 栈。
- 持久化跨进程输入历史。
- `sai-works config get/set/list/path`。
- 日志查询命令与更清晰的失败诊断摘要。

### 分发与质量

- zsh/fish completion 和 release checklist。
- CI 执行测试、lint 和必要的类型检查。
- 增加 ruff；是否引入 mypy/pyright 由实际复杂度决定。
- 对 SSH、tmux、Windows Terminal、窄屏和组合字符补充兼容性验证。

### P4：Subagent、Team 与 A2A

本地 subagent 的会话基础设施已提前实施；剩余的主任务图、上下文预算增强和跨进程恢复仍按上方
状态推进。首阶段建立独立子会话、会话镜像、集群关系、公共状态空间和进程内并发 runner，具体契约见
[Subagent 会话集群](core/subagent-session-clusters.md)。跨进程常驻调度和故障续跑将复用现有 P0
checkpoint 与恢复能力，但相关 worker 生命周期和续跑机制仍未实现。

- 本地 subagent 使用独立 session、tool history、run id、权限和 context budget。
- 子会话可继承主会话、使用新配置或从会话镜像仓库启动。
- parent 与子会话不直连，只通过有来源的结构化公共状态交换有界结果。
- 主 Agent 可批量创建子会话，并通过独立 runtime 并发执行所有 ready 成员。
- 并发公共状态写入使用文件锁和原子替换；workspace 修改仍需 patch 前 hash 或文件锁。
- Team 在本地状态结构稳定后再扩展到远程 A2A。
- 远程修改必须以 patch/artifact 回到本地审批流。

## Next Milestone：M1 Runtime Reliability

目标：先让单 Agent 的动作执行、安全边界和任务恢复形成可验证闭环，为后续 Skill、MCP resource
和 Subagent 扩展提供稳定契约。M1 从 P0–P4 现有条目中选取近期交付项，不构成新的长期路线体系。

进度：`0/5` 完成。

- `[ ]` **M1.1 执行协议闭环**（P0-04）：拒绝 native tool calls 与 JSON fallback 混用，加入 Schema 校验和 `done/actions` 不变量。验收：非法响应不得产生工具副作用，合法响应保持兼容。
- `[ ]` **M1.2 副作用与恢复合同**（P0-05）：定义副作用等级、重试条件、幂等键和审批生命周期。验收：超时、中断和审批拒绝不会盲目重复执行不可幂等动作。
- `[ ]` **M1.3 统一信任边界**（P0-05/P1-01/P2-01）：统一 Skill、MCP、文档、Subagent 和 artifact 的来源及授权检查。验收：注入指令、路径越权和跨会话 artifact 读取测试通过。
- `[ ]` **M1.4 质量门禁**（P3-01）：引入 CI、pytest、ruff 和关键场景回归。验收：记录 revision、测试结果和失败场景证据。
- `[ ]` **M1.5 Task State 与上下文增强**（P0-03/P4-01）：定义单一 Task State Model，完善模型感知 token 预算、摘要和 checkpoint 契约。验收：中断恢复保留关键约束，失效 read state 必须重新验证。

M1 完成后，再集中处理 Skill 资源包、MCP resource packaging、TUI streaming 和更完整的 Subagent 调度。

### M1 依赖顺序

近期执行按以下依赖推进，P0–P4 的长期归属保持不变：

1. 执行协议与 `done/actions` 状态不变量。
2. 安全边界、副作用等级、审批生命周期、重试和幂等合同。
3. pytest、ruff、CI、失败注入和关键场景回归门禁。
4. Task State、模型感知上下文预算、摘要和 checkpoint 增强。
5. Skill、MCP、Subagent 资源扩展及远程 A2A。

TUI streaming、终端兼容性和公网 MCP 兼容性可以与上述步骤并行，但不能降低前三项的验收门槛。

## 暂不优先

- 自动创建或修改虚拟环境。
- 完整 review 产品模式。
- session rename/tag/fork。
- 大规模测试目录重构。
- 与现有 `run_tests` 重叠的诊断工具。
- 让模型永久安装新能力。

## 下一阶段验收标准（P0 剩余增强）

- `[x]` 长任务可以中断、保存和恢复，并清楚说明恢复点。
- `[x]` resume 后有最小恢复包、任务状态、关键结果摘要和可回查来源。
- `[x]` 恢复后的文件修改能验证 read state，失效时明确要求重新读取。
- `[~]` prompt 已有预算内的 hot context 和 warm summary；模型感知 token 预算、误差余量、usage 回填及完整语义摘要仍待补齐。
- `[x]` 裁剪和省略可观察；语义摘要的统一策略仍待补齐。
- `[x]` 每次修改任务都给出明确验证结论。
- `[x]` 现有短任务、Skill、MCP、审批和 TUI 行为保持兼容。
