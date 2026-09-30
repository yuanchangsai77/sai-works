# CLI 交互壳

状态：当前实现及责任提取目标。使用方式与参数见[项目 README](../../README.md)和[配置参考](../reference/configuration.md)。

## 当前角色

CLI 接收单次请求或聊天输入，提供会话选择、恢复、能力命令和用户确认，展示结果。现有应用装配返回 CLI 兼容适配，无终端调用使用独立 Runtime 装配。运行收尾、会话持久化、恢复协调、压缩政策和后台子任务归 Runtime。

CLI 保留输入解析、交互选择、展示适配和旧入口转发；运行控制与保存由 Runtime 决定。展示与补全使用查询投影，不能修改真实工具或政策对象。

## 交互原则

用户指令通过同一运行路径提交；斜杠命令只是操作入口，不能直接绕过能力或授权边界。取消传递给 Runtime，再由壳呈现确认后的状态；不能只停止终端动画却让操作继续。

会话列表与选择属于交互，会话内容和恢复有效性属于 Runtime。审批界面转交人的决定，不创建独立批准缓存和权限规则。

## 与其他壳关系

TUI 目前通过 presenter 适配共享 CLI 流程；Desktop、IDE、API 是目标。后台子代理也复用运行机制，不应因无终端而放宽执行约束。

统一职责见[壳与内核边界](runtime-boundary.md)，当前领域语义见[Agent Runtime](../runtime/agent-runtime.md)。

代码依据：src/saiworks/app.py、interaction/cli.py、commands/、presenter.py。
