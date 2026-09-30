# Skill 文件与发现参考

状态：当前安装位置、文件格式和发现行为，核对于 2026-09-30。能力激活与权限边界见[Skill 接入](../integrations/skills.md)。

## 安装位置与覆盖

每个 Skill 使用一个独立子目录，入口为 SKILL.md。当前只扫描以下目录的直接子目录，不递归发现任意深度的 Skill。

| 扫描顺序 | 位置 | 来源 |
| --- | --- | --- |
| 1 | src/saiworks/skills/builtins/，相对于安装包定位 | 内置 |
| 2 | ~/.saiworks/skills/ | 用户全局 |
| 3 | <workspace>/.saiworks/skills/ | 当前工作空间 |

以 metadata 中的 name 标识同名内容；未给出有效名称时使用子目录名。后扫描者覆盖前者，所以项目覆盖全局和内置，全局覆盖内置。当前没有专用的同名覆盖诊断。

例如项目 Skill 可放在 .saiworks/skills/project-check/SKILL.md。

## 最小推荐格式

```markdown
---
name: project-check
description: 检查当前项目并给出有证据的结论。
version: 1.0.0
---

先检查相关文件与现有验证结果，再给出结论。
需要执行动作时，使用运行时提供的受控能力。
```

frontmatter 位于文件开头，由两行 --- 包围；正文是工作指令。当前解析器支持简单键值和列表，不是完整 YAML 解析器，避免嵌套对象或多行 YAML 值。metadata 扫描读取 frontmatter，选中内容后才加载正文。

| 字段 | 当前处理 |
| --- | --- |
| name | 名称；缺失时使用目录名 |
| description | 目录用途说明；缺失时为空 |
| version | 版本描述；缺失时为 0.1.0 |
| triggers | 兼容读取字符串或列表，不作为自动注入入口 |

metadata 被发现不代表正文已激活，更不代表获得执行许可。references、assets、scripts 的目录存在不表示已经支持完整资源包激活或自动脚本执行。

依据：src/saiworks/app.py、skills/registry.py、skills/parser.py、capabilities/local_source.py。
