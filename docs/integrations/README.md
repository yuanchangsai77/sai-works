# 外部来源与适配

接入层提供能力、资源或模型响应；Execution Core 统一拥有运行、授权、预算、执行和证据语义。

| 接入 | 文档 | 责任边界 |
| --- | --- | --- |
| MCP | [MCP 接入](mcp.md) | 传输、发现与外部工具适配 |
| Skill | [Skill 来源](skills.md) | 工作指令和受控资源包 |
| Model Core | [模型依赖](model-core.md) | 推理调用与动作响应适配，平台资源管理归独立仓库 |
| 内部来源扩展 | [扩展点](extension-points.md) | 候选上下文、可注册工具、资源与能力来源 |

能力目录、激活和释放由[Capability](../runtime/capability.md)定义。访问身份与连接由[共享信任域](../design/trust-domain.md)定义目标边界。接入不创建第二套 Agent 或审批逻辑。
