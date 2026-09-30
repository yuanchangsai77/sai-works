# Shell 执行适配

状态：当前实现。Shell 是 Execution Runtime 的本地 Executor，不是通用 Sandbox 或独立 Agent。

## 生命周期

当前一个工具注册表维护一个串行 Bash 会话，用于同一交互会话内保留 cwd 和环境。不同注册表可以有独立 Bash，不意味着已有全局资源调度。

POSIX 上 Bash 使用独立会话与进程组，关闭时先终止后强制清理；主动脱离进程组的子进程不在该保证内。Windows 不具备同等后台子进程清理保证。

用户中断、退出、命令超时、会话切换或不再保留工具状态时关闭 Shell。超时或协议失败后重建干净进程；不能继续假设旧环境仍存活。

## 工作空间与恢复

Shell 内部 cd 是执行环境状态，不改变 Workspace 的结构化文件工具基准。运行时投影真实 Shell cwd 到检查点；环境变量值不复制到检查点。Shell 已关闭时按下一次实际创建环境重置 cwd 投影。

切换活动工作空间后重建 Shell 默认环境，不能复用旧目录状态。恢复记录不能复活外部进程，也不能保证命令启动的服务已被撤销。

## 授权与隔离

Shell 命令仍受执行风险、资源约束、审批及内容扫描影响。命令字面扫描不能解释任意编码或运行时生成行为。进程组清理不能阻止宿主机文件、网络或系统服务访问。

需要资源隔离时应由执行目标提供实际容器或系统限制；当前机制不宣称已有统一沙盒。目标语义见[执行目标](../design/execution-targets.md)。

代码依据：src/saiworks/tools/builtins/shell_exec.py、tools/shared.py、orchestration/engine.py。
