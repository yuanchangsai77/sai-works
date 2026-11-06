# 执行可靠性审查与验收记录

日期：2026-11-03。范围：当前分支自 `6d9c6ea` 后的任务恢复、工具执行阶段、本地资源与 MCP 生命周期改动。本记录核对这些改动的约束，不宣称所有工具、平台和故障组合已经得到形式化证明。

## 审查结论

本轮已修复并回归验证的主要缺陷：

1. 同 Task 不同提交可分别领取执行权；副作用失败依据错误码名单误判为确定。
2. 文件当前内容被误写成历史执行结论；核对缺少主体和独立证据，观察反复覆盖。
3. 取消检查与运行切换之间存在窗口，旧上下文可误清理新资源；上下文绑定阶段中断可遗留执行锁。
4. 首次 Runtime 关闭失败后无法重试，强制关闭收尾异常覆盖已确定的中断结果。
5. 授权拒绝合成结果丢失原调用未启动证明；执行阶段共享结果槽混淆调用归属。
6. 已执行命令报告工作目录越界后被授权重跑；Shell 关闭后在异常返回路径自行重启。
7. 清理期间旧上下文补建资源，回调替换的新实例被误删；组长先退出时忽略 SIGTERM 的子进程仍运行。
8. MCP 关闭失败吞掉异常并丢弃客户端，旧初始化和旧请求可能发布或重建资源；旧失败可误关替代连接。
9. MCP 请求、通知和初始化并发争用连接；排队操作越过关闭点；网络部分建立失败缺少清理归属。

当前审查范围内没有留下已确认但未修复的阻断缺陷。执行仍遵守下述保守边界，不能把绿色测试等同于任意副作用已停止。

## 约束与证据

| 约束 | 代码位置 | 验证证据 |
| --- | --- | --- |
| 同一 Task 一次只领取一个提交，领取与状态变更原子完成 | `sessions/tasks.py` | `test_file_recovery.py` 的双连接竞争；旧提交结束后竞争者才能领取 |
| 取消与动作启动按事务顺序裁决，取消不清理其他上下文 | `runtime.py`、`orchestration/engine.py`、`sessions/tasks.py` | `test_action_journal.py` 的取消与启动/返回竞争；`test_runtime_lifecycle.py` 的旧上下文及装配窗口 |
| 上下文绑定中断释放执行权，不误取消上一个上下文 | `orchestration/engine.py` | `test_runtime_lifecycle.py` 的绑定锁故障注入，先复现锁泄漏后验证释放 |
| 副作用失败默认未知，只有可信内部未启动证明才能安全结束 | `tools/base.py`、`tools/registry.py`、`sessions/tasks.py` | `test_task_action_store.py` 的陌生错误码及严格布尔校验；`test_execution_recovery.py` 的未发现测试命令、已写入后失败与伪造 metadata |
| 执行阶段跟随单次调用，合成拒绝不丢失证明，实际重试不沿用旧证明 | `orchestration/actions.py`、`tools/registry.py` | `test_tool_execution_receipts.py`；`test_execution_recovery.py` 的拒绝工作区授权后继续合法写入 |
| 已产生效果的命令不得因后续路径错误被自动重放 | `orchestration/actions.py` | `test_execution_engine_sessions_and_cli_runtime.py` 的写入后动态 cwd 越界，文件只写入一次 |
| 文件观察与执行历史分离，不自动解除门禁 | `sessions/file_recovery.py`、`sessions/tasks.py` | `test_file_recovery.py` 的匹配、缺失、冲突、链接、运行中提交与 8 条批次上限；`test_execution_recovery.py` 的部分效果后只读可继续、写入仍阻断 |
| 观察及可信裁决追加留存，状态与证据同事务提交 | `sessions/tasks.py` | `test_task_action_store.py` 的裁决主体、脱敏及证据写入拒绝回滚；`test_file_recovery.py` 的连续观察历史 |
| 崩溃回收验证本机进程身份，已有副作用记录绝不自动解锁 | `sessions/process_owner.py`、`sessions/tasks.py` | `test_process_recovery.py` 的真实子进程退出、活进程、PID 复用、用户与命名空间差异 |
| 旧任务库事务升级，失败不留下半升级状态 | `sessions/tasks.py` | `test_file_recovery.py` 的真实 v5 结构迁移及末尾建表失败回滚；`test_runtime_observation.py` 的提交失败回滚；`test_runtime_approval_control.py` 的旧版本兼容与未知版本拒绝 |
| 关闭失败可重试，终态不被收尾清理错误覆盖 | `runtime.py`、`sessions/coordinator.py` | `test_runtime_shutdown.py` 的首次失败后再次关闭、强制关闭保留 KeyboardInterrupt 和 interrupted 摘要 |
| 资源代际防止旧上下文补建；清理不误删替代资源；关闭实例不复活 | `tools/registry.py`、`tools/builtins/shell_exec.py` | `test_shell_lifecycle.py` 的并发关闭、旧上下文、懒加载、失败重试与回调替换 |
| 进程组停止证明保留组长归属，覆盖忽略终止信号的同组子进程 | `tools/process_lifecycle.py` | `test_shell_lifecycle.py` 的真实后台子进程；`test_process_lifecycle.py` 的超时重试、提前回收拒绝与最终回收竞争；`test_mcp_lifecycle.py` 的真实 Stdio 服务器及子进程 |
| MCP 生命周期按代际与具体客户端归属裁决，失败资源保留 | `mcp/manager.py`、`mcp/client.py`、`mcp/adapter.py` | `test_mcp_lifecycle.py` 的慢初始化、旧代际请求、替代客户端、失败清理与旧工具请求 |
| MCP 协议交换串行、排队操作重新检查关闭，关闭可唤醒等待 | `mcp/client.py`、`mcp/transport.py` | `test_mcp_lifecycle.py` 的请求/通知互斥、单次握手、关闭后的排队通知、Stdio/SSE 等待唤醒、部分连接失败清理 |
| 对外数据格式保持兼容，内部事实不由远端输出决定 | `tools/base.py`、`mcp/adapter.py`、`orchestration/action_journal.py` | 未修改 `ToolAction`、`ToolDefinition`、`ToolResult` 及跨端观察/控制对象字段；既有 MCP 兼容测试与远端 metadata 污染测试通过 |

## 分步提交与独立快照验证

验证从暂存区导出完整目录，显式设置源码路径并断言导入的是该目录中的 SaiWorks，随后运行 `python -m pytest -q --tb=short`。因此各组测试不依赖工作区尚未提交的改动。并发测试使用事件交接，进程停止测试使用实际本地进程，已复现缺陷保留修复前失败、修复后通过的回归证据，其余故障注入覆盖保守失败路径。

| 顺序 | 功能边界 | 独立快照结果 |
| --- | --- | --- |
| 1 | 任务执行权、恢复观察、身份核验与裁决证据（`6fae506`） | 811 passed |
| 2 | 工具阶段凭据、资源代际与共用本地进程清理（`2b1c9c3`） | 822 passed |
| 3 | 运行取消归属、关闭重试和动作流程消费凭据（`e492948`） | 834 passed |
| 4 | MCP 客户端、连接和协议交换生命周期，以及本审查记录 | 最终快照 853 passed |

各组提交前检查暂存差异，最终检查完整工作区与提交差异。验证环境为当前本机 Linux、项目虚拟环境；未据此声称其他操作系统或其他 Python 版本完成相同动态验收。

## 明确保留的边界

- 文件匹配、文件缺失、进程退出和连接关闭都不是远端副作用停止或历史执行归属的证明。
- 已启动且失败的测试、Shell 或远端调用可能已有部分效果，继续等待可信核对；不会为了自动续跑而改成盲目重试。
- 崩溃回收只自动结束身份可验证且从未登记副作用动作的提交。旧记录、外机、不同用户、无法识别的进程及已有动作记录保持阻断。
- 本地进程组不能停止主动脱离会话的子进程；非本机 Linux 的能力仅限直接子进程。归属无法核验时不对可能复用的编号发信号。
- MCP 关闭独立于协议交换锁，但网络建立或请求派发已进入底层 I/O 时仍受配置超时约束；它不提供远端执行隔离或强制停止证明。
- 核对主体和证据来自可信本地所有者。存储层保证记录及事务一致性，不伪造对任意外部系统的机器核验或防篡改证明。
- 数据库升级到版本 8，升级时必须停止旧写入进程。内部凭据和证据表不新增 App、Cloud、Agent 的 wire 字段或错误码。
