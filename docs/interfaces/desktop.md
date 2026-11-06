# 桌面应用

状态：桌面参考应用的同进程控制层与 Tk 参考窗口已有实现，真实 GUI 及生产分发尚未验收。产品范围和分阶段解锁条件见 [桌面应用设计](../design/desktop-scope.md)，完整页面与体验目标见 [桌面应用产品与 UI/UX](../design/desktop-product-ux.md)。

## 当前入口

`interaction.desktop.DesktopController` 是不依赖 GUI 框架的应用控制层。它提供工作区/会话打开、任务提交、当前运行取消、关闭和不可变会话投影。它只组织界面本地的选择状态，所有执行与持久化均委托 Runtime。

`DesktopSessionView` 是面向渲染的不可变投影，当前包含会话身份、工作区、会话状态、消息和上下文代次。它不暴露 Engine、ToolRegistry、Policy 或可变 `StoredSession`。

`interaction.desktop_app.DesktopApp` 是 Tk 8.6 参考窗口，通过 `sai-works-desktop` 启动。模型和工具执行位于后台线程；进度、结果和审批经由 UI 事件队列回到窗口线程。审批对话只转交决定，实际权限校验仍在 Runtime/Engine。

`interaction.desktop_presenter` 是无 GUI 的展示适配层，将 Runtime 进度转换为语义化动作名称、紧凑进度卡和 Execution Rail 所需状态。它不能决定任务是否完成、审批是否有效或操作是否成功；这些事实仍以 Runtime 返回值和 trace 为准。

`interaction.desktop_windows` 收纳 Quick Actions、Diagnostics 和打开日志目录等辅助窗口能力。它只消费控制器给出的只读投影和界面层回调，不直接访问 Runtime、Engine 或会话存储。

当前参考窗口已按产品设计具备基础工作台的主要界面能力：两栏主界面、基于现有 session 的任务导航、紧凑运行卡、按需展开的 Activity/Execution Rail、结果与错误卡、`⌘K` Quick Actions、当前任务诊断和日志目录入口。任务导航和诊断由 `DesktopController` 聚合 Runtime 的 session/trace 投影；它不等同于统一任务观察接口的独立 `TaskSnapshot` 和修订订阅或完整 Artifact 查询。

窗口关闭、SIGTERM 与 SIGINT 共用桌面关闭协调：先取消活动运行，收尾后关闭会话和 Runtime。超过有界等待时必须再次由用户选择继续等待或强制退出；强制退出表示仍需核对外部执行事实。

桌面应用创建 Runtime 时要求已配置真实模型端点，不接受静默 Stub 降级。缺失 `SAIWORKS_MODEL_BASE_URL` 属于启动配置错误；模型端点已配置但不可达则作为运行错误展示，并由内核日志保留调用证据。

参考窗口采用“导航侧栏 + 对话主区 + 底部输入区”的布局。审批窗口必须明确执行主体为 SaiWorks Runtime，展示动作、原因与参数，并提醒用户 macOS 可能把 Automation 权限归到启动 SaiWorks 的宿主应用；这项说明只解释系统归属，不替代逐动作审批。

Conversation、Result、Artifacts 视图已投影当前和历史结果；产物页展示已有引用与来源，不提供完整内容或已保存 diff。流式 message 是可清除的临时预览，最终结果仍以 Runtime 事实替换。

## 当前限制

- controller 与 Runtime 同进程，关闭进程不保证任务续跑。
- 窗口取消仍调用当前运行入口；内核已有按 Task 控制资格和取消桥接，窗口尚未迁移到该稳定控制接口。
- 审批仍沿用同步兼容适配；内核已有审批记录与 TTL，但窗口尚未接入其等待生命周期，可恢复挂起与 continuation 未实施。
- Tk 参考窗口尚未完成生产打包、签名、自动更新与完整无障碍验收，不构成长期 GUI 技术承诺。

实现依据：[desktop.py](../../src/saiworks/interaction/desktop.py)、[desktop_app.py](../../src/saiworks/interaction/desktop_app.py)。

回归入口：[test_desktop_controller.py](../../tests/test_desktop_controller.py)。
