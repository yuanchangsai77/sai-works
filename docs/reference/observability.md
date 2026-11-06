# 运行日志与会话追踪

- 状态：当前实现参考
- 适用：CLI、TUI、桌面应用共用 Runtime
- 数据根目录：当前启动目录下的 `.saiworks/`

本页说明现有文件与查看方法，不把日志描述成完整审计账本。日志由 Runtime 生成，不归属于某个交互界面。

## 1. 文件位置

| 内容 | 位置 | 用途 |
| --- | --- | --- |
| 人类可读运行摘要 | `.saiworks/runs/<run_id>/details.log` | 快速查看输入、模型后端、各轮动作、结果与终态 |
| 结构化事件 | `.saiworks/runs/<run_id>/events.jsonl` | 精确核对模型请求、工具、审批、错误与完成事件 |
| 结构化观察 | `.saiworks/runs/<run_id>/observations/` | 单项模型裁剪前的脱敏材料、不可变来源记录及恢复索引 |
| 大型材料 | `.saiworks/runs/<run_id>/artifacts/` | 首次完整模型请求、工具参数/结果、终态 checkpoint |
| 会话状态 | `.saiworks/sessions/<session_id>.json` | 消息、run_ids、trace、恢复状态与当前工作区 |
| 会话简要轨迹 | `.saiworks/sessions/<session_id>.trace.log` | 按会话查看运行摘要 |

引擎在 Run 收尾时额外记录内部 `run.metrics` 事件：轮次数、模型尝试数、真实工具动作数、重复跳过数、无效完成次数，以及模型轮次处理（含重试等待）、工具处理和同步审批等待耗时。耗时按进程内单调时钟累计，仅用于诊断；它不是计费数据，也不进入跨端响应契约。
| 任务接受记录 | `.saiworks/tasks.sqlite3` | submission、task、接受与最终提交状态 |

`run_id` 当前是 UTC 时间戳，例如本地时间 2026-10-02 01:02 对应目录 `2026-10-01T17-02-42...+00-00`。不要仅凭文件名判断 session 新旧：测试也会创建 session，可靠关联应读取 run 的 `run.start.payload.metadata.session_id`，或读取 session 的 `run_ids`。

## 2. 最快查看方式

查看最近一次运行摘要：

```bash
latest_run=$(find .saiworks/runs -mindepth 1 -maxdepth 1 -type d | sort | tail -1)
less "$latest_run/details.log"
```

查看关键结构化事件：

```bash
rg 'runtime.model_backend|model.request|model.response|model.parsed_reply|safety.approval|tool.execute|tool.result|run.error|run.finish' \
  "$latest_run/events.jsonl"
```

从 run 找 session：

```bash
rg '"name": "run.start"' "$latest_run/events.jsonl"
```

输出中的 `payload.metadata.session_id` 是权威关联。再打开：

```bash
less ".saiworks/sessions/<session_id>.json"
```

## 3. 事件阅读顺序

一次正常真实模型调用通常依次包含：

1. `run.start`：prompt、cwd、session_id、task_id。
2. `runtime.model_backend`：真实模型客户端或 Stub。
3. `context.*`：实际装入或跳过的上下文。
4. `model.request` / `model.response` / `model.parsed_reply`。
5. 可选的 `safety.check` / `safety.approval`。
6. 可选的 `tool.execute` / `tool.result`。
7. `run.finish`：唯一终态摘要、outcome、blockers 与 checkpoint。

若存在 `model.response` 但随后是 `run.error`，说明传输成功而响应协议或运行语义失败；若没有 `model.response`，优先检查连接、超时和 HTTP 错误。

`model.request.context_package` 分别记录系统和恢复信息裁剪、观察单元省略/裁剪数及工具 schema 预留字符数。
`omitted_messages` 只统计历史对话，不能代表工具观察是否完整。观察省略时模型输入也保留明确提示；单项正文裁剪保留来源头和截断标记。
上下文摘要的每次模型请求记录独立的开始和终态事件；成功完成的整段摘要另记 `context.compressed`。这些请求计入运行检查点中的模型尝试次数；取消会在下一次分段请求前生效。
逐轮轨迹的结果摘要带有有界调用参数及已有结果归档引用，恢复时按部分证据投影，不当作完整文件内容。
`safety.approval.wait_seconds` 记录同步审批等待时长；显式启用总运行时间上限时，该等待不消耗执行时间预算，请求超时、HTTP 失败和退避仍计入其中。这些传输尝试应与有效回复轮次分开统计。

## 4. 桌面应用诊断投影

参考窗口已有当前运行诊断和打开日志目录入口；它消费 Runtime 事实，不重新计算执行终态。界面范围见[桌面应用](../interfaces/desktop.md)，旧协议故障只作为[历史诊断示例](../archive/audits/2026-11-04-shell-core-audit.md#历史-desktop-协议诊断示例2026-10-02)保留，不代表当前最新运行或现行协议限制。

## 5. 安全与限制

- 日志经过脱敏与大字段外置，但仍可能包含工作区路径、命令摘要和用户输入；不要直接公开整个 `.saiworks` 目录。
- `events.jsonl` 是诊断索引，部分完整内容只保存在 artifact；并非所有 provider 原始响应都会持久化全文。
- 日志不是不可篡改审计记录，也不等价于完整 Execution Ledger。
- 桌面应用已有诊断与日志入口，但不等于完整账本、跨端查询或所有 GUI 故障已经验收。

观察恢复读取独立索引和对应材料，校验会话、授权范围、内容哈希及引用路径。损坏或越界记录跳过；归档写入失败不生成可用引用。模型请求的 `context_package.observation_views` 记录实际入选的观察、正文完整性、可见范围和引用可见性，不能用归档存在代替模型可见。内部观察载荷不重复写入普通工具事件的大型材料。

实现依据：[logger.py](../../src/saiworks/observability/logger.py)、[observations.py](../../src/saiworks/observations.py)。

回归入口：[test_run_logging_and_redaction.py](../../tests/test_run_logging_and_redaction.py)。
