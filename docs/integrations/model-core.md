# Model Core 依赖边界

状态：目标分工与当前调用适配。Model Core 已在独立仓库演化，本仓库不维护其平台内部设计。

## 分工

SaiWorks 构造任务工作集，表达推理与动作协议要求，消费自然语言和动作提案，负责现实执行与完成裁决。Model Core 管理模型、Provider、Backend、Compute Resource、推理部署、路由、调度、计量和平台故障处理。

同一节点可以同时提供执行和推理资源，但额度、权限、调度及生命周期分别管理。SaiWorks 不根据机器可访问性推断推理许可。

## 当前适配

本仓库保留模型客户端、prompt 构造、解析及流式自然语言投影，当前主要使用 OpenAI-compatible chat endpoint，并有 stub。动作通道由 Runtime 选择 native_tools 或 prompt_json；解析与完成校验仍属 SaiWorks。

请求关联、超时、取消、重试和失败增量处理沿用[模型传输契约](../reference/model-transport-contract.md)。客户端重试决策与访问端点的实现责任分开，不复制平台路由和 GPU 调度。

## 目标与差距

长期可以表达 vision、tool-use 等需求，由 Model Core 选择资源。当前不冻结新的 Intelligence Request schema，也不宣称已有动态能力匹配或所有 Provider 适配。

跨项目契约需要双方权威文档与兼容性核对；本页不新增字段、错误码或能力发现端点。Model Core 的 2026-09-30 文档是目标架构基线，不可作为所有能力已完成的证据。

代码依据：src/saiworks/model/client.py、protocol.py、prompt.py、parser.py、streaming.py、app.py。
