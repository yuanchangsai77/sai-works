# MCP 当前适配参考

状态：按当前源码核对的协议、名称、风险和大小边界；不新增字段或改变现有行为。总体接入责任见[MCP](../integrations/mcp.md)。

## 协议与身份映射

客户端显式支持的协议版本当前为 2025-03-26。初始化缺失版本或返回不支持版本时，关闭 transport 并返回 mcp_protocol_error，不进入 initialized 状态。列表分页受次数限制，JSON-RPC 结构与返回对象需要校验。

工具名由 server stable prefix 与原始 tool name 形成映射；模型侧非法字符被规范化，超长或需要规范化的名称使用确定性摘要后缀。模型名称最长 64 字符；远端调用保留原始名称。冲突不能静默覆盖。

资源 ID 为 mcp-resource://<encoded-server>/<encoded-resource-id>，分别编码服务器与资源标识，防止不同服务器的同名资源混淆。读取由适配层定位原服务器，原 URI 仍属于来源信息，不能直接当作权限。

## 当前风险折叠

| 条件 | 现行结果 |
| --- | --- |
| 显式 risk_overrides 命中 | 使用配置风险值 |
| destructive trait | destructive |
| remote_write trait | write |
| execute trait | execute |
| network trait | network |
| 没有上述事实 | confirm |

traits 根据名称、描述及 annotations 推断。远端 readOnlyHint 不能把未知能力降成免审批 read；显式配置覆盖是单独的信任入口。最终动作仍经过运行时门禁，不由 schema 或远端注释授予权限。

## 有界发现与结果

| 位置 | 当前防御边界 |
| --- | --- |
| 单条传输消息 | 10 MiB，解析前限制 |
| 工具发现 | 默认每 server 256，配置与硬上限见配置参考 |
| 资源发现 | 每 server 1,000 |
| 列表分页 | 最多 1,024 页 |
| 单个 descriptor | 100,000 字符 |
| discovery cache 读取 | 128 MiB |
| MCP 结果各部分 | output、structured content、remote metadata 分别受 100,000 字符限制 |

MCP 输出限制之后还接受工具统一结果预算和模型上下文预算。超限结果有截断诊断；可用 logger 时按当前实现归档较大材料。不能据此认为任意 artifact 的访问与完整性保护已全部闭合。

## 传输与恢复约束

旧 SSE endpoint 协商限制同源，不能向跨源地址发送请求并转发 headers。意外 EOF 需要唤醒等待请求并形成 mcp_transport_closed；主动关闭不应被记录为异常。client 生命周期与重连由 manager 管理，provider 不另建连接治理。

恢复连接只恢复访问能力，不证明失败调用没有生效。副作用重试遵循[执行语义](../runtime/execution-runtime.md)，未知结果先核对。

依据：src/saiworks/mcp/client.py、types.py、adapter.py、transport.py、discovery.py、manager.py。配置见[配置参考](configuration.md)，结果字段见[工具契约](tool-contract.md)。
