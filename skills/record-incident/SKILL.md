---
name: record-incident
description: 记录当前工作区共享指令、SOP、Skill、桥接、自动化或治理的已核验失败，按权限直接提交或交可信桥接代收；不维护治理状态。
---

先读运行时可读的 `SOP/错题记录与治理SOP.md`。只记录自己能核验的实际失败、静默跳过、错误回执、人工补救；根因可待定位。纯业务/纯外部问题且共享处理正确则留业务目录。

- 隔离成员：写本人 `WORK_ROOT/.notebook-outbox/<稳定事件名>.json`，身份和事件键由可信桥接绑定；检查同名 `.receipt`，accepted=true 才称收录。无权读取共享账本或其他成员记录。
- 共享维护者：用 `.codex/gravekeeper/notebook.py submit` 从 stdin 提交完整 JSON，返回 ID 后保留；同事件补证用 amend，不新增重试次数。

JSON 字段和范围以 SOP/运行时说明为准。证据脱敏，时间未知写 unknown 与 time_note，未确认根因明确标推断。记录不授权治理、启动 Agent、读取凭据或修改调度。
