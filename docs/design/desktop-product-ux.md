# SaiWorks Desktop 产品功能与 UI/UX 设计

- 日期：2026-10-02
- 状态：目标设计，作为 Desktop 后续实施与验收基线
- 依赖：[壳与内核边界](shell-core-boundary.md)、[Desktop V0.1](desktop-v0.md)、[运行日志与会话追踪](../reference/observability.md)

## 1. 产品定义

SaiWorks Desktop 是本地执行型 Agent 的工作台，不只是聊天窗口。它把“提出目标—观察计划—授予权限—理解执行—核对结果—恢复任务”组织成普通用户可理解的连续体验，同时保持 CLI、TUI 与 Desktop 共用同一 Runtime 权威语义。

Desktop 的价值不是隐藏一切，而是按需呈现复杂度：默认页面简洁；发生工具执行、等待审批、部分失败或恢复时，提供足够的事实和下一步，而不要求用户读取 JSON、记住斜杠命令或打开终端。

### 1.1 目标用户

- 想使用本地 Agent，但不熟悉 CLI/TUI 的普通用户。
- 需要长任务、审批、日志与恢复可见性的专业用户。
- 同时使用 CLI/TUI/Desktop，希望任务语义一致的现有用户。

### 1.2 体验原则

1. **目标优先**：首页首先回答“你想完成什么”，不是展示内部模块。
2. **渐进披露**：日常只显示必要状态，细节通过运行抽屉与诊断页展开。
3. **执行透明**：正在做什么、为什么需要权限、结果是否已核对必须可见。
4. **无感恢复**：可自动恢复的情况自动完成；需要决定时给出明确选择和后果。
5. **身份明确**：产品审批主体始终是 SaiWorks Runtime；系统宿主归属另行解释。
6. **壳不造事实**：任务状态、审批有效性、工具结果和会话持久化来自 Runtime。

## 2. 产品范围

### 2.1 核心功能域

| 功能域 | 用户能力 | Runtime/数据来源 |
| --- | --- | --- |
| 工作区 | 最近项目、选择目录、切换与访问范围提示 | Workspace/session binding |
| 会话 | 新建、恢复、搜索、重命名、归档 | SessionStore 与稳定投影 |
| 任务 | 提交、排队、观察、取消、继续 | Task/Run/Checkpoint |
| 对话 | 文本输入、上下文附件、结果阅读 | Session messages |
| 执行 | 查看当前阶段、工具动作、耗时与结果 | Progress + Run snapshot |
| 审批 | 理解风险、查看参数、批准或拒绝 | Runtime approval contract |
| 结果 | 最终答复、变更摘要、证据和后续动作 | ExecutionSummary/Evidence |
| 诊断 | 会话→任务→run、错误详情、日志目录 | `.saiworks` 日志与 trace |
| 设置 | 模型端点、模型选择、安全模式、外观 | Runtime config / shell preference |

### 2.2 非目标

- Desktop 不创建第二套任务状态机、审批缓存或日志格式。
- 不在 V0.1 承诺窗口关闭后任务继续运行；该能力依赖独立 Runtime。
- 不以“自动执行更多命令”代替好体验；低价值探测和重复审批应由策略与任务设计减少。
- 不在 UI 中直接编辑秘密；凭据由受保护配置或系统凭据能力承载。

## 3. 产品对象模型

Workspace 是资源边界，Session 是连续工作的容器，Task 是用户真正委托的工作对象；Conversation 只是 Task 的一种交互视图，不承担全部执行事实。

```text
Workspace
└── Session
    └── Task
        ├── Conversation
        │   └── User / SaiWorks messages
        ├── Activity
        │   ├── Progress
        │   ├── Actions
        │   ├── Approvals
        │   └── Runs
        ├── Artifacts
        │   ├── Changes
        │   ├── Files
        │   └── Evidence
        └── Result
```

这个层级避免把工具卡、审批、diff、测试结果和诊断全部塞进无限增长的聊天记录。视觉入口可以保持 Conversation-first，但 Task 才是状态、恢复、Activity、Artifact 和 Result 的聚合对象。

## 4. 信息架构

```text
SaiWorks Desktop
├── Home / 工作区入口
├── Workspace
│   ├── Sessions / 会话列表
│   └── Tasks / 任务列表
│       ├── Conversation / 交互视图
│       ├── Activity / 按需展开的执行上下文
│       ├── Artifacts / 变更、结果与证据
│       └── Result / 终态摘要
├── Approvals / 待处理权限
├── Diagnostics / 诊断
│   ├── Session → Task → Run
│   ├── Human-readable details
│   ├── Structured events
│   └── Open log folder / Copy support bundle
└── Settings
    ├── Model
    ├── Safety
    ├── Appearance
    └── Advanced
```

桌面宽度充足时使用左侧导航；窄窗口收起为顶部工作区菜单。Activity 是 contextual execution panel：空闲与简单问答时不占据固定第三栏，只有任务执行、审批、失败或用户主动查看时才从右侧展开。

## 5. 主窗口布局

```text
┌──────────────────────────────────────────────────────────┐
│ SaiWorks       Project                           ● Ready │
├──────────────┬───────────────────────────────────────────┤
│ New task     │                                           │
│              │              Conversation                 │
│ Today        │                                           │
│ • Task A     │                                           │
│ • Task B     │                                           │
│              │                                           │
│ Earlier      ├───────────────────────────────────────────┤
│ • Task C     │ Attach context…  Ask SaiWorks…      Send │
├──────────────┴───────────────────────────────────────────┤
│ Local · Confirm · deepseek-v4.1-flash                   │
└──────────────────────────────────────────────────────────┘
```

主界面保持安静的两栏结构。执行开始后，Conversation 内首先出现紧凑的任务进度卡：

```text
SaiWorks is working
┌─────────────────────────────────────┐
│ ✓ Understood project                │
│ ✓ Read 3 files                      │
│ ● Running tests                     │
│ ○ Verify changes                    │
└─────────────────────────────────────┘
                              View activity →
```

点击 `View activity`、出现待审批或发生故障时，从右侧展开 Activity panel；关闭后对话宽度恢复，不丢失任务状态。

### 5.1 左侧：导航与任务

- 顶部“新任务”是明确主操作。
- 导航项以用户能识别的 Task 标题为主，按“今天、过去 7 天、更早”分组；Session 作为连续上下文在详情中呈现。
- 当前工作区路径折叠显示，可复制、在 Finder 打开或切换。
- 不显示内部 session ID；ID 放在详情与诊断页。

### 5.2 中部：对话与输入

- 用户、SaiWorks、系统状态使用不同但克制的视觉语义。
- Markdown、代码、diff、表格与路径采用适合内容的渲染器；长工具输出默认折叠。
- 输入框支持多行、拖入文件、显式上下文 chips 和 `⌘Enter` 发送。
- 运行中允许继续输入为“下一条任务”或明确禁用并解释，不能静默丢弃。

### 5.3 Contextual Activity panel

- 默认关闭；运行时以紧凑进度卡提示，只有用户展开、等待审批、阻塞或故障时才获得视觉优先级。
- 展示用户能理解的阶段：理解目标、准备上下文、等待批准、执行、核对、完成。
- 工具按语义显示，如“读取 3 个文件”“准备启动 Music”，不把 `shell_exec` 作为唯一描述。
- 失败项展示影响和恢复动作；技术详情按需展开。
- 用户可从这里取消当前任务，但“已请求取消”和“已安全停止”必须区分。

## 6. 关键页面与交互

### 6.1 首次启动

流程：欢迎 → 选择工作区 → 检测模型 → 测试连接 → 选择默认安全模式 → 进入工作台。

- 自动查询 `/v1/models` 并让用户选择 API slug，展示名不能直接写入模型 ID。
- 连接失败区分：未启动、HTTP 错误、模型不存在、响应协议不兼容。
- 提供“重新检测”和“高级配置”，不要求用户先编辑 `.env`。
- 测试连接结果写入内核诊断，但不创建污染会话的聊天消息。

### 6.2 新任务与普通对话

- 空状态给出三类示例：解释、修改、执行。
- 提交后立即生成任务卡并显示“已接受”；不得用动画暗示已经执行成功。
- 简单问答保持简洁，不自动扫描整个工作区。
- 项目任务在必要时显示使用了哪些上下文和规则。

### 6.3 审批

审批卡必须包含：

- 主体：SaiWorks Runtime。
- 意图：用自然语言说明想完成什么。
- 动作：以“启动应用、修改文件、访问网络、运行命令、删除文件”等用户语义为主。
- 范围：文件、应用、网络目标或命令。
- 风险：读取、写入、执行、外部副作用。
- 选择：拒绝、仅本次允许；长期授权只在契约支持后出现。

macOS Automation 可能显示 Zed/Terminal/Python，是系统对启动宿主的归属；正式 `.app` 打包、Bundle ID 和签名后才能稳定显示 SaiWorks。产品内审批不能因为系统稍后还会弹框而省略。

`shell_exec`、`filesystem.write` 等内部工具名不作为审批标题，只放在默认折叠的“技术详情”中，同时提供 Task ID、Action ID 和完整脱敏参数。

### 6.4 运行结果

结果页面按顺序显示：结论 → 实际改变 → 验证 → 未完成/风险 → 可选下一步。

- 文件变更提供 diff 与 Finder/编辑器打开入口。
- 外部动作提供已执行事实和可核对证据。
- `runtime_error` 不伪装成普通助手回答，使用错误卡并提供“重试、查看诊断、复制详情”。
- 模型响应协议错误应提示“模型已响应，但格式不兼容”，区别于“API 不可用”。

### 6.5 会话恢复

- 会话列表显示最后有效结果和未解决阻塞，不把错误文本当正常对话标题。
- “继续”前先展示恢复点、可能重复的副作用和建议动作。
- 测试会话、临时工作区与用户会话在查询层区分；不能以文件名排序替代最新用户会话。

### 6.6 Diagnostics / 日志

普通用户入口叫“诊断”，而不是“events.jsonl”。默认显示：

- 当前 session、task、run ID 与本地时间。
- 模型后端、模型 ID、请求是否到达、响应是否通过解析。
- 审批与工具时间线。
- 最终 outcome、blocker 和恢复建议。

默认视图不是日志阅读器，而是一份任务体检报告：

```text
Task diagnostics

Model        DeepSeek V4.1 Flash
Response     842 ms
Protocol     ✓ Valid

Execution   6 actions · 5 succeeded · 1 blocked
Permissions 2 requested · 2 approved
Verification 48 tests passed
Duration    1m 42s

Technical details ›   Open logs ›   Copy support bundle ›
```

高级区提供：查看 `details.log`、筛选事件、打开日志目录、复制脱敏支持包。原始 JSON 和 artifact 必须显式展开，默认不展示可能敏感的完整参数。

### 6.7 Command Palette / Quick Actions

`⌘K` 打开统一命令面板，作为 CLI/TUI 到 Desktop 的渐进式专业入口：

- 切换工作区、新建任务、恢复最近任务。
- 查看当前 Activity、取消当前任务、打开诊断。
- 切换模型与安全模式。
- 打开最近文件和当前变更。

命令面板消费与按钮相同的 controller/Runtime 能力，不建立快捷键旁路；高风险动作仍进入同一审批链路。

## 7. 状态模型与视觉语义

| 状态 | 用户文案 | 颜色/图形 | 可用动作 |
| --- | --- | --- | --- |
| Ready | 可以开始 | 中性/绿色小点 | 发送 |
| Accepted | 已接收任务 | 蓝色 | 查看任务 |
| Running | 正在执行… | 蓝色进度 | 取消、查看 Activity |
| Awaiting approval | 等待你的批准 | 琥珀色 | 查看、允许、拒绝 |
| Cancelling | 正在停止并核对 | 琥珀色 | 等待、查看详情 |
| Completed | 已完成 | 绿色 | 查看结果、继续 |
| Blocked | 需要你的选择 | 琥珀色 | 处理建议 |
| Runtime error | 运行失败 | 红色 | 重试、诊断 |
| Disconnected | Runtime 已断开 | 灰色 | 重连（阶段 D/E） |

颜色不能是唯一信号；所有状态同时使用图标、文字和可访问名称。

Activity 的终态同样按 Runtime 事实区分：`completed` 显示完成，`interrupted` 显示已取消，`blocked` 显示受阻，`exhausted/stalled` 显示需要关注，只有实际运行错误显示失败；不能把所有非完成状态统一渲染成失败。

## 8. SaiWorks 视觉语言

SaiWorks 的设计身份是 **Calm Computational Tool**，关键词为 Quiet、Precise、Spatial、Mechanical、Trustworthy。它不使用“AI 紫色渐变、发光和星星”证明智能，而通过空间层级、克制留白、准确状态和执行轨迹建立信任。

底层视觉哲学是：

```text
Intent  →  Execution  →  Evidence
```

Intent 在对话中保持清晰；Execution 通过可展开 Activity 呈现；Evidence 在结果与 Artifact 中收束。这三层不能混成一串聊天气泡。

### 8.1 Execution Rail

Activity 使用纵向 execution rail 作为 SaiWorks 的标志性组件：

```text
    │
    ●  Understanding
    │
    ●  Read project
    │
    ◉  Editing
    │
    ○  Verify
    │
    ○  Complete
```

实心完成、强调环表示当前、空心表示尚未开始；失败和审批在轨道节点上附着，不另造一套时间线。轨道表达真实阶段，不用模型臆测的计划伪装 Runtime 状态。

## 9. 视觉设计系统

### 9.1 基础风格

- 气质：安静、可靠、工具感，避免过度科幻和高密度终端风格。
- 内容最大宽度 760–860 px，长文本保证阅读行长。
- 8 px 间距体系；主要区域 20–24 px 内边距。
- 圆角 8–12 px；阴影只用于浮层和待处理审批。

### 9.2 颜色角色

- `surface/base`：窗口背景和内容表面。
- `text/primary`、`text/secondary`：正文与辅助信息。
- `accent`：发送、选中状态与链接。
- `success`、`warning`、`danger`：只表达真实运行语义。
- 同时提供浅色、深色和跟随系统模式；对比度至少满足 WCAG AA。

### 9.3 字体与内容

- UI 使用系统字体；命令、路径、ID 使用等宽字体。
- 正文基准 14–16 px；辅助信息不低于 12 px。
- 技术错误先给用户语言，再显示可复制原文。

## 10. 键盘、无障碍与国际化

- `⌘N` 新任务、`⌘K` 快速切换、`⌘Enter` 发送、`Esc` 关闭浮层；取消运行不能只依赖 Esc。
- 完整 Tab 顺序、可见焦点、VoiceOver 标签和状态变化播报。
- 动画遵循“减少动态效果”；运行状态不使用无限高频动画。
- 文案进入资源层，初期至少保证中文和英文布局不截断。
- 时间默认显示本地时区，详情中保留 UTC run ID。

## 11. Desktop 与 Runtime 的接口要求

目标 UI 需要稳定的只读投影，而不是读取 Engine：

- `WorkspaceSummary`：身份、路径、访问范围。
- `SessionSummary` / `SessionDetail`：标题、状态、消息、未解决项。
- `TaskSnapshot`：revision、阶段、进度、审批、终态。
- `TaskDetail`：Conversation、Activity、Artifacts 与 Result 的一致聚合投影。
- `RunDiagnostic`：run 关联、事件摘要、blocker、日志引用。
- `ApprovalView`：意图、动作、风险、参数摘要、有效期。

V0.1a 可在同进程 controller 上实现查询适配；运行中换壳、订阅和断线恢复必须等待阶段 D 的 snapshot/revision 契约。UI 不直接扫描 `.saiworks` 来构造权威状态。

## 12. 技术形态决策

Tk 版本继续作为行为原型和低成本回归壳，不作为最终视觉技术承诺。进入生产 Desktop 前对以下方案做一次有界 spike：

- 原生 SwiftUI：macOS 身份、TCC、菜单、无障碍和打包最好；跨平台成本高。
- Tauri：体积与 Web UI 表达平衡；需要本地 Runtime IPC 边界。
- Electron：生态成熟但资源开销较高。
- Qt/PySide：Python 集成直接，分发与原生细节需验证。

选型验收不看静态截图，至少验证会话列表、长对话、实时 Activity、审批、diff、诊断、键盘/VoiceOver、打包签名和自动更新。

## 13. 分阶段实施

### Phase 1：可用工作台

- 两栏主框架、Task 导航、多行输入、消息渲染、紧凑进度卡与 contextual Activity panel。
- 首次启动模型检测和模型 slug 选择。
- 结果/错误卡、当前 run 的诊断摘要和打开日志目录。
- 审批卡明确主体、范围与风险。
- `⌘K` Command Palette 覆盖核心导航与控制动作。

### Phase 2：任务与恢复

- TaskSnapshot/revision、稳定取消、运行中恢复、待审批中心。
- 会话搜索、重命名、归档；用户会话与测试/临时会话隔离。
- Artifact/diff/证据查看器。

### Phase 3：正式桌面产品

- 独立 Runtime/Gateway、窗口退出后续跑与重连。
- `.app` 打包、Bundle ID、签名、公证、更新和系统权限说明。
- 深浅主题、无障碍、国际化和性能门槛。

## 14. 验收指标

- 首次用户不编辑文件即可选择可用模型并完成第一条真实请求。
- 用户能在 10 秒内判断任务是在运行、等审批、失败还是完成。
- 任一失败可从 UI 进入对应 session/task/run，不靠目录排序猜测。
- 审批前能看懂动作主体、范围和后果；拒绝后不重复执行。
- 关闭、取消、恢复的状态文案与 Runtime 事实一致。
- 200 条消息、长 Markdown、diff 和持续进度下仍保持可操作。
- CLI/TUI/Desktop 对同一任务的终态、审批与会话结果一致。
- 空闲和简单问答时 Activity 不占据永久第三栏；任务执行时用户一步可展开完整 execution rail。
- Conversation 不承载唯一的 Activity、Artifact 或 Result 事实；长任务无需滚动整段聊天才能判断完成情况。
