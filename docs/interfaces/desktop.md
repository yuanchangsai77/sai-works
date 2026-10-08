# Desktop 交互壳

状态：V0.1a 第一纵向切片实施中。产品范围和分阶段解锁条件见 [Desktop V0.1 设计](../design/desktop-v0.md)，完整页面与体验目标见 [Desktop 产品与 UI/UX](../design/desktop-product-ux.md)。

## 当前入口

`interaction.desktop.DesktopController` 是不依赖 GUI 框架的应用控制层。它提供工作区/会话打开、任务提交、当前运行取消、关闭和不可变会话投影。它只组织壳本地的选择状态，所有执行与持久化均委托 Runtime。

`DesktopSessionView` 是面向渲染的不可变投影，当前包含会话身份、工作区、会话状态、消息和上下文代次。它不暴露 Engine、ToolRegistry、Policy 或可变 `StoredSession`。

`interaction.desktop_app.DesktopApp` 是 Tk 8.6 参考窗口，通过 `sai-works-desktop` 启动。模型和工具执行位于后台线程；进度、结果和审批经由 UI 事件队列回到窗口线程。审批对话只转交决定，实际权限校验仍在 Runtime/Engine。

`interaction.desktop_presenter` 是无 GUI 的展示适配层，将 Runtime 进度转换为语义化动作名称、紧凑进度卡和 Execution Rail 所需状态。它不能决定任务是否完成、审批是否有效或操作是否成功；这些事实仍以 Runtime 返回值和 trace 为准。

`interaction.desktop_windows` 收纳 Quick Actions、Diagnostics 和打开日志目录等辅助窗口能力。它只消费控制器给出的只读投影和壳层回调，不直接访问 Runtime、Engine 或会话存储。

当前参考窗口已按产品设计实现 Phase 1 的壳层纵向切片：两栏主界面、基于现有 session 的任务导航、紧凑运行卡、按需展开的 Activity/Execution Rail、结果与错误卡、`⌘K` Quick Actions、当前任务诊断和日志目录入口。任务导航和诊断由 `DesktopController` 聚合 Runtime 的 session/trace 投影；它不等同于阶段 D 的独立 `TaskSnapshot`、revision 订阅或完整 Artifact 查询。

窗口关闭、SIGTERM 与 SIGINT 共用桌面关闭协调：先取消活动运行，收尾后关闭会话和 Runtime。超过有界等待时必须再次由用户选择继续等待或强制退出；强制退出表示仍需核对外部执行事实。

Desktop 创建 Runtime 时要求已配置真实模型端点，不接受静默 Stub 降级。缺失 `SAIWORKS_MODEL_BASE_URL` 属于启动配置错误；模型端点已配置但不可达则作为运行错误展示，并由内核日志保留调用证据。

参考窗口采用“导航侧栏 + 对话主区 + 底部输入区”的布局。审批窗口必须明确执行主体为 SaiWorks Runtime，展示动作、原因与参数，并提醒用户 macOS 可能把 Automation 权限归到启动 SaiWorks 的宿主应用；这项说明只解释系统归属，不替代逐动作审批。

## 当前限制

- controller 与 Runtime 同进程，关闭进程不保证任务续跑。
- 当前取消仍是 Runtime 当前执行入口，按稳定 Task 控制属于阶段 D。
- 审批仍沿用同步兼容适配；可恢复审批、TTL 和 continuation 尚未实施。
- Tk 参考窗口尚未完成生产打包、签名、自动更新与完整无障碍验收，不构成长期 GUI 技术承诺。

代码依据：`src/saiworks/interaction/desktop.py`、`src/saiworks/interaction/desktop_app.py`。
