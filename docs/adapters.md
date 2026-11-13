# 更换 Agent 或平台

本仓库的 **部署 Skill 不绑定对话产品**；执行器和平台有实际代码契约。任意具备文件与命令工具的 Agent 可以部署参考栈。要让另一个 Agent 作为后台业务执行器，需要实现这些接口并通过同一验收矩阵，不能只改模型名或把安全约束写进提示词。

| 接口 | 输入 | 必须返回/保证 | 当前实现 |
| --- | --- | --- | --- |
| MessageSource | 平台事件、群ID、messageId | 规范消息、真实身份/mention、在群结果、稳定游标 | Hi WS + CLI |
| SessionRunner | cwd、account、model、effort、permissions、prompt | threadId、完成状态、最终正文、权限回读 | Codex App Server |
| IndependentSession | 唯一任务、space、argv、cwd | terminal/UUID、可见性、ACK、唤醒、准确归档/删除 | Herdr + Codex |
| FileDelivery | 本人文件、固定远端目录、操作码 | 文件ID、大小/名称/状态回读、可用链接 | Hi Pan broker |
| DocumentPublisher | 已验证摘要、目标父节点、原操作码/hash | 目标ID、正文/父节点一致、人工改动冲突 | REDoc CLI |
| Scheduler | task、时区、依赖、水位 | 单实例、门槛、有限重试、独立清理 | launchd + Python |
| IncidentStore | 脱敏报告、稳定键、scope | candidate、去重、审计、冷归档 | SQLite + Markdown |

实现准则：未知身份/权限/外部结果时停止对应步骤；不把 timeout 当未写入。重启不盲重放中断业务。会话标识、资源归属与操作码由可信层生成/校验。独立治理身份必须可从平台真实元数据验证。

目前没有声称已实现 Slack/飞书/钉钉、Linux systemd、Windows Task Scheduler、Claude/Cursor 后台执行适配器。适配者可复用通用账本与策略，新增平台代码放独立模块并提供反向拒绝测试与真实验收证据。
