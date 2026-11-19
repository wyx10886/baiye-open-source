# Changelog · 版本记录

The public Skill and its runtime baseline have separate version lines. / 公开技能与运行源码基线使用独立版本线。

## 1.1.0 · 2026-11-19

Runtime baseline / 运行源码基线：**1.7.2**.

- **中文**：同步习惯候选学习、主动准备门禁、主会话登记保护和最小空号回收。增加中英双语介绍及本更新记录。公开版将习惯采集与复用、主动准备、标题同步和编号回收设为新安装默认关闭，不附带原使用者的日程、时区、入口或同意记录。
- **English**: Synchronize habit candidates, proactive preparation gates, main-session registration safeguards and smallest-vacant-code recycling. Add bilingual introductions and release history. Fresh public installations disable collection/reuse, proactive preparation, title sync and code recycling, without importing anyone's schedule, timezone, pointer or consent records.
- **中文**：保留严格的 UUID 与 rollout 文件读取范围；停用主动功能时不新增用户活动记录。主动策略改为可显式配置的时区偏移和窗口，默认 UTC；不创建调度器，也不发送消息。
- **English**: Preserve strict UUID and rollout-file read boundaries and avoid new user-activity records while proactive preparation is disabled. Generalize timezone offset and scheduling windows with UTC defaults. The runtime neither installs a scheduler nor sends messages.
- **中文**：旧世代、旧身份、跨代修订和数据库写门禁随新版保留；完整扫描失败时不释放编号。习惯推断仍是候选；停止采集、停止复用、遗忘记录分别管理。
- **English**: Retain generations, identity history, cross-generation revisions and database write gates. Incomplete scans do not release codes. Learned inferences remain candidates; collection, reuse and forgetting have separate controls.
- **升级 / Upgrade**：停止旧写入进程并离线备份完整私有 home，先在副本中验证。新增持久台账护栏可能拒绝旧客户端写入，应重载客户端；发生跨代回收后不得直接用旧台账覆盖新库。/ Stop writers, back up the entire private home and validate a copy first. Persistent registry guards may reject old writers; reload them. Do not overwrite a registry containing recycled generations with an older snapshot.

## 1.0.1 · 2026-11-16

- **中文**：首个完整隐私审核后的公开版本；包含有来源记忆、项目接续、会话编号、日常入口、清理、CLI/MCP和管理网页。追加主会话身份门禁和受限 rollout 读取，标题同步默认关闭。
- **English**: First fully reviewed public distribution with evidence-backed memory, project handoffs, session codes, daily entry, retention, CLI/MCP and web management. Add main-session identity gates and constrained rollout reads; title sync defaults off.

## 1.0.0 · 2026-11-16

- **中文**：初始公开快照，随后由 1.0.1 的身份识别修复替代。
- **English**: Initial public snapshot, superseded by the identity-gate fix in 1.0.1.
