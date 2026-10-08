# 运行日志与会话追踪

- 状态：当前实现参考
- 适用：CLI、TUI、Desktop 共用 Runtime
- 数据根目录：当前启动目录下的 `.saiworks/`

本页说明现有文件与查看方法，不把日志描述成完整审计账本。日志由 Runtime 生成，不归属于某个交互壳。

## 1. 文件位置

| 内容 | 位置 | 用途 |
| --- | --- | --- |
| 人类可读运行摘要 | `.saiworks/runs/<run_id>/details.log` | 快速查看输入、模型后端、各轮动作、结果与终态 |
| 结构化事件 | `.saiworks/runs/<run_id>/events.jsonl` | 精确核对模型请求、工具、审批、错误与完成事件 |
| 大型材料 | `.saiworks/runs/<run_id>/artifacts/` | 首次完整模型请求、工具参数/结果、终态 checkpoint |
| 会话状态 | `.saiworks/sessions/<session_id>.json` | 消息、run_ids、trace、恢复状态与当前工作区 |
| 会话简要轨迹 | `.saiworks/sessions/<session_id>.trace.log` | 按会话查看运行摘要 |
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

## 4. 当前最新真实 Desktop 记录

截至 2026-10-02，本次核对的 Desktop session 为 `20261001163052318835-460bda3d`，最近 run 为 `2026-10-01T17-02-42.471782+00-00`。请求“继续”已取得真实 `model.response`，但响应未满足 native tools 严格 JSON 字段约束，最终为 `runtime_error`；这不是 Desktop 丢失回复，也不是连接失败。

## 5. 安全与限制

- 日志经过脱敏与大字段外置，但仍可能包含工作区路径、命令摘要和用户输入；不要直接公开整个 `.saiworks` 目录。
- `events.jsonl` 是诊断索引，部分完整内容只保存在 artifact；并非所有 provider 原始响应都会持久化全文。
- 日志不是不可篡改审计记录，也不等价于完整 Execution Ledger。
- Desktop 目标态应提供内置诊断视图和“打开日志目录”，普通用户不应依赖终端命令定位故障。
