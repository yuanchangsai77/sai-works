# MCP 能力接入

状态：当前接入机制与目标差距。MCP 是外部能力来源，不能拥有独立权限系统或隐藏 Agent 生命周期。

## 责任链

```text
配置 → Manager / Transport → Client / Discovery
     → Capability Source → Warehouse / Activation
     → Tool Adapter → 常规执行门禁 → Result
```

Manager 管理连接与客户端复用；Transport 处理传输；Client 处理协议操作；Discovery 提供有界描述；Capability Source 提供目录与激活材料；Adapter 转为现行工具结果。各层不复制 Runtime 的审批和完成状态。

## 当前实现

支持 stdio、旧 SSE 和 streamable HTTP，提供 initialize、tools list/call 和 resources list/read。协议版本按实现接受范围核对，不根据最新标准推断当前兼容能力。

启动时不全量注册远端工具；打开工具箱时懒发现并缓存 manifest，激活选定叶子后下一轮可见。健康、连接和缓存来源分别展示。配置与环境变量行为见[配置参考](../reference/configuration.md)。

工具描述与 JSON schema 适配到现行 ToolDefinition；实际调用仍接受校验、风险和授权检查。兼容性直接注册 provider 不应成为新的主链路。

## 信任与故障

远端 schema、描述、资源内容及结果是外部输入，不可自行降级风险或授予权限。旧 SSE endpoint 协商接受相对地址或同源绝对地址；跨源目标不能继承凭据并发送请求。

超时、连接失败和协议错误保持现有错误映射。重连可以恢复访问，不保证上次调用没有产生副作用；有副作用的工具失败按 Runtime 的核对与重试约束处理。

## 资源与上下文

资源列举与读取协议入口已经存在，不等于资源已完整进入能力叶子激活、上下文选择、预算和敏感内容过滤。资源应走受控上下文与引用路径，不伪装为自动授权的系统指令。

## 目标与差距

补齐公网兼容性、能力风险描述、资源上下文及来源访问验证。远端 MCP 调用不等于通用 Remote Execution Target；后者还有主体、目标、容量和生命周期问题。

代码依据：src/saiworks/mcp/client.py、transport.py、manager.py、discovery.py、adapter.py、provider.py、capabilities/mcp_source.py。

## 实现细节的权威参考

协议支持集合、稳定名称与资源 ID、风险折叠和大小限制集中在[MCP 当前适配参考](../reference/mcp-adapter.md)。接入文档只维护责任链与差距，不重复所有配置和字段。
