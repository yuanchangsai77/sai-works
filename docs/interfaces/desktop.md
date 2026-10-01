# Desktop 交互壳

状态：V0.1a 第一纵向切片实施中。产品范围和分阶段解锁条件见 [Desktop V0.1 设计](../design/desktop-v0.md)。

## 当前入口

`interaction.desktop.DesktopController` 是不依赖 GUI 框架的应用控制层。它提供工作区/会话打开、任务提交、当前运行取消、关闭和不可变会话投影。它只组织壳本地的选择状态，所有执行与持久化均委托 Runtime。

`DesktopSessionView` 是面向渲染的不可变投影，当前包含会话身份、工作区、会话状态、消息和上下文代次。它不暴露 Engine、ToolRegistry、Policy 或可变 `StoredSession`。

`interaction.desktop_app.DesktopApp` 是 Tk 8.6 参考窗口，通过 `sai-works-desktop` 启动。模型和工具执行位于后台线程；进度、结果和审批经由 UI 事件队列回到窗口线程。审批对话只转交决定，实际权限校验仍在 Runtime/Engine。

窗口关闭、SIGTERM 与 SIGINT 共用桌面关闭协调：先取消活动运行，收尾后关闭会话和 Runtime。超过有界等待时必须再次由用户选择继续等待或强制退出；强制退出表示仍需核对外部执行事实。

## 当前限制

- controller 与 Runtime 同进程，关闭进程不保证任务续跑。
- 当前取消仍是 Runtime 当前执行入口，按稳定 Task 控制属于阶段 D。
- 审批仍沿用同步兼容适配；可恢复审批、TTL 和 continuation 尚未实施。
- Tk 参考窗口尚未完成生产打包、签名、自动更新与完整无障碍验收，不构成长期 GUI 技术承诺。

代码依据：`src/saiworks/interaction/desktop.py`、`src/saiworks/interaction/desktop_app.py`。
