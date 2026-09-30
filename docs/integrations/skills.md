# Skill 能力来源

状态：当前实现与待完善资源包。Skill 提供可激活的工作指令和相关能力，不持有独立 Agent Runtime 或权限。

## 发现与激活

当前扫描内置、全局、项目目录的 SKILL.md metadata，项目来源可以覆盖同名较低优先级来源。metadata 发现不装载全部正文。具体路径、扫描深度、覆盖顺序和最小文件示例见[Skill 文件参考](../reference/skill-format.md)。

Skill 进入 LocalToolboxSource 的目录；正文作为 instructions 叶子显式激活，关联本地工具也按需进入激活集。指令参与共享上下文预算，并由能力生命周期拥有；不再建立 trigger 自动注入旁路。

元数据中的名称、描述与版本用于描述和选择，不是执行许可。外部 Skill 的文字不能扩张会话授权或委派合同。

## 资源包目标

references、assets、scripts 应具有索引、来源、预算与加载生命周期，当前尚未完成统一的逐项资源包机制。不能因目录存在就宣称其中脚本会安全自动执行。

脚本执行应转成普通受控动作，经过同一资源校验、Policy、审批、日志与证据路径。Skill 不自行运行隐藏 Shell，也不以指令激活代替脚本授权。

## 覆盖与恢复

同名覆盖、版本冲突和来源优先级需要用户可检查的诊断。恢复按保存能力标识重建工作集，不把旧正文当成永远有效的运行许可。来源变更应明确观察与核对，具体持久字段不在本页新增。

代码依据：src/saiworks/skills/registry.py、parser.py、model.py、capabilities/local_source.py、warehouse.py。核心激活语义见[Capability](../runtime/capability.md)。
