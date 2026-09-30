# 接入扩展点

状态：当前内部扩展边界。这里讨论来源和适配，不是 CLI/TUI/Desktop 控制 Runtime 的接口数量。

| 扩展点 | 当前责任 | 边界 |
| --- | --- | --- |
| ContextLoader | 运行开始收集规则或候选上下文 | 不拥有总预算与最终裁剪 |
| ToolProvider | 提供可注册工具 | 不拥有远端发现、重试、授权或 Agent 生命周期 |
| ResourceProvider | 提供可列举、按需读取的资源 | 不把完整资源自动注入模型或自动授权 |
| CapabilitySource | 提供目录、manifest 与激活材料 | 不取代 Runtime 的策略与执行状态 |

上下文经 ContextPackager 统一处理；工具经 Registry 与执行门禁；大型外部目录经 warehouse 按需激活。兼容性 provider 可以保留，但不应重新引入启动全量发现与注册。

新来源优先适配现有能力模型与结果路径，不建立专属审批、会话或日志旁路。内部扩展点可以直接调用，跨进程来源才需要传输适配。

代码依据：src/saiworks/orchestration/ext.py、tools/base.py、capabilities/source.py、mcp/provider.py。交互控制边界见[壳与内核](../interfaces/runtime-boundary.md)。
